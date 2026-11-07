"""Feature-build orchestration, called under the existing STA and machine lock."""
import copy
from contextlib import nullcontext
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import stat
import tempfile
import time

from catia_mcp.cad_support_client import run_support
from catia_mcp.drawing.feature_executor import FeatureExecutor
from catia_mcp.drawing.verify import run_independent_verifier
from catia_mcp.results import operation_result
from catia_mcp.drawing.preview import create_part_preview


def _file_sha256(path):
    result = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def _write_receipt(path, value):
    """Create a durable, immutable-named receipt before advancing the journal."""
    with Path(path).open("x", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.flush()
        os.fsync(output.fileno())
    return _file_sha256(path)


class CadSupportError(RuntimeError):
    def __init__(self, code, detail, *, summary=None):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.summary = summary


class VerifierSessionUnknown(RuntimeError):
    """A verifier could not establish the CATIA session's state."""


def _valid_volume_diagnostics(value):
    return (isinstance(value, dict)
            and set(value) == {"actualMm3", "referenceMm3", "netDifferenceMm3", "maxIntersectionExcessMm3"}
            and all(type(item) in (int, float) and math.isfinite(item) and item >= 0 for item in value.values())
            and value["actualMm3"] > 0 and value["referenceMm3"] > 0
            and math.isclose(value["netDifferenceMm3"], abs(value["actualMm3"] - value["referenceMm3"]),
                             rel_tol=1e-9, abs_tol=1e-9))


def _evaluate(requirements, actual=None, *, volume_tolerance_mm3=None, timeout=None):
    spec = {"requirements": requirements}
    if actual:
        # Kernel-noise floor: a fixed 0.01 mm3 absolute check fails large parts
        # whose CATIA and OCCT booleans differ by a few mm3; the evaluator
        # scales the comparison to 0.01% of its own expected volume.
        spec["actual"] = str(actual)
        spec["volumeToleranceMm3"] = "auto" if volume_tolerance_mm3 is None else volume_tolerance_mm3
    try:
        result = run_support("evaluate", spec, **({"timeout": timeout} if timeout is not None else {}))
    except ValueError as exc:
        # Client-side setup or serialization failed, not a rejected variant.
        raise CadSupportError("CAD_SUPPORT_REQUEST_FAILED", str(exc)) from exc
    if not result["ok"] and result.get("code") in {
        "CAD_SUPPORT_TIMEOUT", "CAD_SUPPORT_START_FAILED", "CAD_SUPPORT_PROTOCOL_ERROR"
    }:
        if result["code"] == "CAD_SUPPORT_TIMEOUT":
            raise CadSupportError(result["code"],
                "Helper descendants may still be running; confirm and clean up before resuming. " + str(result))
        raise CadSupportError(result["code"], result)
    if not result["ok"]:
        error = result.get("error")
        if (actual is not None and result.get("operation") == "evaluate"
                and result.get("exitCode") == 1 and result.get("code") == "MATERIAL_COMPARISON_INCONCLUSIVE"
                and isinstance(error, dict) and error.get("type") == "MaterialComparisonInconclusive"):
            summary = error.get("summary")
            if (isinstance(summary, dict)
                    and set(summary) in ({"reason", "consistencyBudgetMm3", "attemptCount", "candidateCount"},
                                         {"reason", "consistencyBudgetMm3", "attemptCount", "candidateCount", "volumeDiagnostics"})
                    and ("volumeDiagnostics" not in summary
                         or _valid_volume_diagnostics(summary["volumeDiagnostics"]))
                    and isinstance(summary["reason"], str)
                    and summary["reason"] in {"COMMON_UNION_DISAGREES", "INSUFFICIENT_CONSISTENT_BOOLEANS", "FUZZY_LEVELS_DISAGREE"}
                    and type(summary["consistencyBudgetMm3"]) in (int, float)
                    and math.isfinite(summary["consistencyBudgetMm3"]) and summary["consistencyBudgetMm3"] > 0
                    and type(summary["attemptCount"]) is int and 1 <= summary["attemptCount"] <= 10
                    and type(summary["candidateCount"]) is int and 0 <= summary["candidateCount"] <= summary["attemptCount"]):
                raise CadSupportError("MATERIAL_COMPARISON_INCONCLUSIVE",
                    "Material comparison could not establish a reliable bound; the saved model remains unverified.",
                    summary=summary)
        if (actual is None and result.get("operation") == "evaluate"
                and result.get("exitCode") == 1 and result.get("code") == "CANDIDATE_REJECTED"
                and isinstance(error, dict) and error.get("type") == "CandidateRejected"
                and isinstance(error.get("message"), str)):
            raise ValueError(f"Geometry candidate rejected: {result}")
        raise CadSupportError("CAD_SUPPORT_EVALUATION_FAILED", result)
    if result["data"].get("verdict") != "PASS":
        raise CadSupportError("CAD_SUPPORT_EVALUATION_FAILED", result)
    return result["data"]


def _expectation(plan, geometry, *, native_volume=None):
    expected = geometry["expected"]
    volume = expected["volumeMm3"]
    if type(volume) not in (int,float) or not math.isfinite(volume) or volume<=0:
        raise ValueError("Invalid independent nominal volume")
    if native_volume is not None:
        if type(native_volume) not in (int,float) or not math.isfinite(native_volume) or native_volume<=0:
            raise ValueError("Invalid native baseline volume")
        # Reopen checks persistence in the same kernel. Independent STEP
        # material comparison below still checks the full nominal geometry.
        volume = native_volume
    if type(volume) not in (int,float) or not math.isfinite(volume) or volume<=0:
        raise ValueError("Invalid expected volume")
    # Curved extrema are not exact in CATIA's STL tessellation. The mandatory
    # independent STEP comparison below checks bbox at the original tolerance.
    return {"volumeMm3": volume,
            "featureCount": len(plan["features"]),
            "parameters": {p["name"]: p["value"] for p in plan["parameters"]}}


def _measurable_difference(checks):
    """A conservative comparison FAIL alone does not prove an actual edit."""
    measurable = False
    for check in checks:
        name = check.get("name")
        if name in {"bbox", "surface_distance"}:
            value, tolerance = check.get("actualMm"), check.get("toleranceMm")
        elif name == "volume_measurement":
            value, tolerance = check.get("actualMm3"), check.get("toleranceMm3")
        else:
            value, tolerance = check.get("minimumMm3"), check.get("toleranceMm3")
        if name == "surface_distance" and (value is None or tolerance is None):
            # The surface check may be unavailable for an edited copy; it then
            # proves nothing, but it must not crash the build.
            continue
        if (any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in (value, tolerance))):
            raise ValueError("Geometry-change comparison lacks finite lower-bound measurements")
        # A different failed check may be inconclusive.  The check providing
        # the lower-bound evidence must itself report a failure.
        measurable |= check.get("passed") is False and value > tolerance
    return measurable


def _native_volume_change(before, after):
    """A net volume change lower-bounds one directed material difference."""
    if any(type(v) not in (int,float) or not math.isfinite(v) or v<=0 for v in (before,after)):
        raise ValueError("Edit effect requires finite positive independent native volumes")
    delta=after-before
    # Deduct an allowance for each measurement, using the CAD comparison's
    # relative numerical limit plus the verifier's six-decimal rounding floor.
    uncertainty=sum(max(1e-6,v*2e-6) for v in (before,after))
    lower=max(0.0,abs(delta)-uncertainty)
    if lower <= .01:
        return None
    return {"name":"excess_material" if delta>0 else "missing_material", "passed":False,
        "minimumMm3":lower,"toleranceMm3":.01,"nativeNetChangeMm3":delta,
        "nativeBeforeMm3":before,"nativeAfterMm3":after,"measurementAllowanceMm3":uncertainty,
        "method":"independent_saved_reopen_native_net_volume_lower_bound"}


def _audit_edit_receipts(manifest_path, manifest_hash, head_hash, attempts, *, require_complete=True):
    """Check an anchored journal boundary; this does not authorize a resume."""
    manifest_path = Path(manifest_path)
    if _file_sha256(manifest_path) != manifest_hash:
        raise ValueError("Edit manifest digest changed.")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    part = Path(manifest["partPath"])
    step = Path(manifest["stepPath"])
    if _file_sha256(part) != manifest["partSha256"] or _file_sha256(step) != manifest["stepSha256"]:
        raise ValueError("Delivered baseline changed during edit checks.")
    parameters = manifest["plan"]["parameters"]
    required = {item["name"]: item["value"] for item in parameters}
    if (len(required) != len(parameters) or type(attempts) is not int
            or not 0 <= attempts <= manifest["maxAttempts"]):
        raise ValueError("Invalid edit parameter set or attempt count.")
    root = part.parent / f"{part.stem}.edit-evidence"
    # Any unrecorded attempt can represent a still-running verifier. Do not
    # mistake the previously committed head for a clean recovery boundary.
    expected_files = {f"attempt-{sequence:06d}.{kind}.json"
                      for sequence in range(1, attempts + 1) for kind in ("intent", "outcome")}
    actual_files = {item.name for item in root.iterdir() if item.name.startswith("attempt-")}
    if actual_files != expected_files:
        raise ValueError("Edit journal has an unrecorded or incomplete attempt.")
    previous = None
    passed = set()
    parameter_index = 0
    candidate_index = 0
    for sequence in range(1, attempts + 1):
        intent_path = root / f"attempt-{sequence:06d}.intent.json"
        outcome_path = root / f"attempt-{sequence:06d}.outcome.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        name = intent["parameter"]
        if parameter_index >= len(parameters) or candidate_index >= 6:
            raise ValueError("Edit journal exceeds the parameter candidate plan.")
        parameter = parameters[parameter_index]
        original = parameter["value"]
        if type(original) not in (int, float) or not math.isfinite(original):
            raise ValueError("Invalid edit parameter value.")
        step = max(abs(original) * .02, .05)
        candidate = original + (step, -step, 2 * step, -2 * step, 4 * step, -4 * step)[candidate_index]
        if (intent.get("schema") != "catia-edit-attempt/1" or intent.get("sequence") != sequence
                or intent.get("manifestSha256") != manifest_hash or intent.get("previousOutcomeSha256") != previous
                or name != parameter["name"] or intent.get("before") != original or name in passed
                or type(intent.get("after")) not in (int, float) or not math.isfinite(intent["after"])
                or intent["after"] != candidate
                or outcome.get("schema") != "catia-edit-outcome/1"
                or outcome.get("intentSha256") != _file_sha256(intent_path)):
            raise ValueError("Edit journal chain or parameter identity is invalid.")
        status = outcome.get("status")
        if status not in {"candidate_rejected", "no_effect", "passed"}:
            raise ValueError("Edit attempt has an invalid status.")
        if status != "candidate_rejected":
            directory = outcome.get("directory")
            if not isinstance(directory, str) or Path(directory).name != directory or directory in ("", ".", ".."):
                raise ValueError("Invalid edit evidence directory.")
            evidence_dir = root / directory
            if status == "passed":
                edit_path = evidence_dir / "edit-result.json"
                if _file_sha256(edit_path) != outcome.get("editResultSha256"):
                    raise ValueError("Edit result digest changed.")
                edit = json.loads(edit_path.read_text(encoding="utf-8"))
                if (edit.get("parameter") != name or edit.get("before") != intent["before"]
                        or edit.get("after") != intent["after"] or edit.get("passed") is not True):
                    raise ValueError("Edit result does not match its intent.")
                evidence = edit["evidence"]
                if evidence.get("directory") != directory:
                    raise ValueError("Edit evidence directory does not match its result.")
            else:
                evidence = outcome
            for filename, field in ((part.name, "copySha256"), ("edited.step", "stepSha256"),
                                    ("reopen-check.json", "reopenCheckSha256"),
                                    ("variant-comparison.json", "variantComparisonSha256"),
                                    ("effect-comparison.json", "effectComparisonSha256")):
                if _file_sha256(evidence_dir / filename) != evidence.get(field):
                    raise ValueError("Edit evidence digest changed.")
            if status == "passed":
                reopened = json.loads((evidence_dir / "reopen-check.json").read_text(encoding="utf-8"))
                effect = json.loads((evidence_dir / "effect-comparison.json").read_text(encoding="utf-8"))
                checks = effect.get("data", {}).get("checks")
                if (reopened.get("verdict") != "PASS" or reopened.get("sessionHealth") == "unknown"
                        or reopened.get("editPersistence", {}).get("savedAndReopened") is not True
                        or effect.get("ok") is not False or effect.get("exitCode") != 1
                        or effect.get("data", {}).get("verdict") != "FAIL" or checks != edit.get("geometryChangeChecks")
                        or not _measurable_difference(checks)):
                    raise ValueError("Edit receipts do not prove a saved, measurable change.")
                passed.add(name)
        if status == "passed":
            parameter_index += 1
            candidate_index = 0
        else:
            candidate_index += 1
        previous = _file_sha256(outcome_path)
    if previous != head_hash:
        raise ValueError("Edit journal head does not match its anchor.")
    complete = passed == set(required)
    if require_complete and not complete:
        raise ValueError("Edit parameter coverage is incomplete.")
    return {"attempts": attempts, "verifiedParameterCount": len(passed), "complete": complete,
            "nextParameter": parameters[parameter_index]["name"] if parameter_index < len(parameters) else None,
            "nextCandidateIndex": candidate_index if parameter_index < len(parameters) else None}


class EditBudgetExpired(RuntimeError):
    """The batch stopped at a boundary without claiming complete coverage."""


class EditBudget:
    # The cleanup reserve is not a guarantee that an in-process COM call can
    # be interrupted. The verifier is isolated, and unknown cleanup blocks it.
    seconds = 1200
    max_attempts = 256
    cleanup_seconds = 10

    def __init__(self):
        self.deadline = time.monotonic() + self.seconds
        self.attempts = 0

    def timeout(self):
        remaining = self.deadline - time.monotonic() - self.cleanup_seconds
        if remaining <= 0:
            raise EditBudgetExpired("Edit-check time budget exhausted; coverage is incomplete.")
        return min(120, remaining)

    def begin_attempt(self):
        self.timeout()
        if self.attempts >= self.max_attempts:
            raise EditBudgetExpired("Edit-check attempt budget exhausted; coverage is incomplete.")
        self.attempts += 1


def build_features(connection, requirements, plan, target, stages, arguments):
    executor = FeatureExecutor(connection)
    stage = "geometry_preflight"
    delivery = None
    modified = False
    session_unknown = False
    edits = []
    declared = []
    budget = None
    progress_path = None
    manifest_hash = None
    journal_head = None
    try:
        if arguments.get("expected") is not None or arguments.get("editCheck", True) is not True:
            raise ValueError("Feature builds require automatic expectations and editCheck=true.")
        target = Path(target).resolve()
        if target.suffix.lower() != ".catpart" or target.exists():
            raise ValueError("Choose a new .CATPart path.")
        # The adjacent STEP/report are owned by this build, never overwritten.
        step_path = target.with_suffix(".step")
        report_path = target.with_suffix(".build.json")
        progress_path = target.with_suffix(".edit-progress.json")
        if step_path.exists() or report_path.exists() or progress_path.exists():
            raise ValueError("STEP, report or edit progress destination already exists.")
        geometry = _evaluate(requirements)
        stages.append({"stage": stage, "status": "passed"})
        stage = "modeling"
        modified = True
        executor.diagnostic_dir = target.parent / f"{target.stem}.edge-evidence"
        built = executor.execute(plan)
        stages.append({"stage": stage, "status": "passed", "stepCount": len(built["steps"]), "final": built["final"]})
        stage = "delivery"
        target.parent.mkdir(parents=True, exist_ok=True)
        delivery = executor.save(str(target))
        stage = "verification"
        native_volume = built["final"]["volumeMm3"]
        verification = run_independent_verifier(str(target), _expectation(plan, geometry, native_volume=native_volume), export_step=str(step_path))
        if verification.get("sessionHealth") == "unknown":
            session_unknown = True
            raise VerifierSessionUnknown(f"Independent verifier left CATIA session unknown: {verification}")
        if verification.get("verdict") != "PASS":
            raise ValueError(f"Independent reopen failed: {verification}")
        comparison = _evaluate(requirements, step_path)
        verification["volumeExpectationBasis"] = {"method":"native_pre_save_measurement",
            "nativeBeforeSaveMm3":native_volume,"independentNominalOCCTMm3":geometry["expected"]["volumeMm3"]}
        verification["checks"].append({"name": "independent_material_comparison", "passed": True,
                                        "detail": comparison["comparison"]["checks"]})
        stages.append({"stage": stage, "status": "passed", "checks": verification["checks"]})
        stage = "editability"
        edit_root = target.parent / f"{target.stem}.edit-evidence"
        edit_root.mkdir(exist_ok=False)
        # Every declared parameter must be verified. A bounded batch can stop
        # incomplete; sampling is never reported as successful coverage.
        declared = list(plan["parameters"])
        checked_parameters = declared
        budget = EditBudget()
        manifest_hash = _write_receipt(progress_path,
                      {"schema": "catia-edit-progress/1", "plan": plan,
                       "requirements": requirements, "verification": verification,
                       "baselineComparison": comparison,
                       "partPath": str(target), "partSha256": _file_sha256(target),
                       "stepPath": str(step_path), "stepSha256": _file_sha256(step_path),
                       "maxAttempts": budget.max_attempts, "timeBudgetSeconds": budget.seconds})
        for index, parameter in enumerate(checked_parameters):
            name, original = parameter["name"], parameter["value"]
            # Check the smallest useful change first. If it is below the geometry
            # measurement threshold, try larger bounded changes on fresh copies.
            step = max(abs(original) * .02, .05)
            valid_variant = False
            for change in (step, -step, 2 * step, -2 * step, 4 * step, -4 * step):
                budget.begin_attempt()
                sequence = budget.attempts
                intent_path = edit_root / f"attempt-{sequence:06d}.intent.json"
                outcome_path = edit_root / f"attempt-{sequence:06d}.outcome.json"
                intent_hash = _write_receipt(intent_path, {
                    "schema": "catia-edit-attempt/1", "manifestSha256": manifest_hash,
                    "previousOutcomeSha256": journal_head, "sequence": sequence,
                    "parameter": name, "before": original, "after": original + change,
                })
                variant = copy.deepcopy(requirements)
                source_param = next(p for p in variant["parameters"] if p["name"] == name)
                # Requirements retain their source units; normalized values are mm.
                scale = original / source_param["value"] if source_param["value"] else (25.4 if variant["unit"] in {"inch", "in", "inches"} and parameter["type"] == "length" else 1)
                source_param["value"] += change / scale
                try:
                    variant_geometry = _evaluate(variant, timeout=budget.timeout())
                except ValueError:
                    journal_head = _write_receipt(outcome_path, {
                        "schema": "catia-edit-outcome/1", "intentSha256": intent_hash,
                        "status": "candidate_rejected"})
                    continue
                valid_variant = True
                value = original + change
                # Retain failed edits as well as successes for unattended audits.
                with nullcontext(tempfile.mkdtemp(prefix=f"{index:03d}-{name}-",dir=edit_root)) as temporary:
                    copy_path = Path(temporary) / target.name
                    shutil.copy2(target, copy_path)
                    edited_step = Path(temporary) / "edited.step"
                    expectation = _expectation(plan, variant_geometry)
                    # The independent process saves/reopens only this owned copy
                    # and compares native measurements across that persistence.
                    expectation.pop("volumeMm3")
                    expectation["parameters"][name] = value
                    check = run_independent_verifier(str(copy_path), expectation,
                        export_step=str(edited_step), parameter_changes={name: {"type": parameter["type"], "value": value}},
                        collect_cylindrical_faces=False, edit_copy_of=str(target), timeout=budget.timeout())
                    if check.get("sessionHealth") == "unknown":
                        session_unknown = True
                        raise VerifierSessionUnknown(f"Independent verifier left CATIA session unknown: {check}")
                    _write_receipt(Path(temporary) / "reopen-check.json", check)
                    if check.get("verdict") != "PASS":
                        raise ValueError(f"Parameter {name} did not propagate: {check}")
                    if check.get("editPersistence", {}).get("savedAndReopened") is not True:
                        raise ValueError(f"Parameter {name} lacks saved-copy reopen proof")
                    # CATIA and OCCT kernels differ by more than 0.01 mm3 on large
                    # parts (0.17 mm3 at 6.7e11 mm3). Keep a rel-scaled floor: 1e-7
                    # of the variant volume is ~200k times that noise and still
                    # 1000x below the smallest meaningful parameter edit.
                    edit_volume_tolerance = max(0.01, variant_geometry["expected"]["volumeMm3"] * 1e-7)
                    variant_comparison = _evaluate(variant, edited_step, volume_tolerance_mm3=edit_volume_tolerance, timeout=budget.timeout())
                    comparison_path = Path(temporary) / "variant-comparison.json"
                    comparison_hash = _write_receipt(comparison_path, variant_comparison)
                    # Compare two CATIA exports, not CATIA against a separately
                    # reconstructed OCCT body, so kernel error cannot mimic an edit.
                    native_change = _native_volume_change(verification["measurements"]["volumeMm3"],
                        check["measurements"]["volumeMm3"])
                    # Full expected-variant STEP material checks already passed.
                    # A conclusive independent native net-volume change proves
                    # an edit without relying on fragile STEP-vs-STEP cut calls.
                    changed = ({"ok":False,"exitCode":1,"operation":"compare_native_saved_volumes",
                        "data":{"verdict":"FAIL","checks":[native_change]}} if native_change else
                        run_support("compare", {"actual": str(edited_step), "reference": str(step_path)},
                                    timeout=budget.timeout()))
                    if changed.get("code") == "CAD_SUPPORT_TIMEOUT":
                        raise CadSupportError("CAD_SUPPORT_TIMEOUT", "Comparison helper timed out; its descendants may still be running.")
                    _write_receipt(Path(temporary) / "effect-comparison.json", changed)
                    data = changed.get("data", {})
                    checks = data.get("checks")
                    if data.get("method") == "measurement_fallback":
                        expected_checks = {"volume_measurement", "bbox_measurement", "surface_distance"}
                    else:
                        expected_checks = {native_change["name"]} if native_change else {"bbox", "missing_material", "excess_material"}
                    if not isinstance(checks, list) or {c.get("name") for c in checks} != expected_checks:
                        raise ValueError(f"Parameter {name} geometry-change comparison failed: {changed}")
                    if changed.get("ok") is True and data.get("verdict") == "PASS" and changed.get("exitCode") == 0:
                        journal_head = _write_receipt(outcome_path, {
                            "schema": "catia-edit-outcome/1", "intentSha256": intent_hash,
                            "status": "no_effect", "directory": Path(temporary).name,
                            "copySha256": _file_sha256(copy_path), "stepSha256": _file_sha256(edited_step),
                            "reopenCheckSha256": _file_sha256(Path(temporary) / "reopen-check.json"),
                            "variantComparisonSha256": comparison_hash,
                            "effectComparisonSha256": _file_sha256(Path(temporary) / "effect-comparison.json")})
                        continue
                    if (changed.get("ok") is not False or changed.get("exitCode") != 1
                            or data.get("verdict") != "FAIL" or not any(c.get("passed") is False for c in checks)):
                        raise ValueError(f"Parameter {name} geometry-change comparison failed: {changed}")
                    if not _measurable_difference(checks):
                        journal_head = _write_receipt(outcome_path, {
                            "schema": "catia-edit-outcome/1", "intentSha256": intent_hash,
                            "status": "no_effect", "directory": Path(temporary).name,
                            "copySha256": _file_sha256(copy_path), "stepSha256": _file_sha256(edited_step),
                            "reopenCheckSha256": _file_sha256(Path(temporary) / "reopen-check.json"),
                            "variantComparisonSha256": comparison_hash,
                            "effectComparisonSha256": _file_sha256(Path(temporary) / "effect-comparison.json")})
                        continue
                    edit = {"parameter": name, "before": original, "after": value,
                                  "passed": True, "geometryChangeChecks": checks,
                                  "evidence": {"directory": Path(temporary).name,
                                      "copySha256": _file_sha256(copy_path),
                                      "stepSha256": _file_sha256(edited_step),
                                      "reopenCheckSha256": _file_sha256(Path(temporary) / "reopen-check.json"),
                                      "variantComparisonSha256": comparison_hash,
                                      "effectComparisonSha256": _file_sha256(Path(temporary) / "effect-comparison.json")},
                                  "bboxBeforeMm": verification.get("measurements", {}).get("bbox"),
                                  "bboxAfterMm": check.get("measurements", {}).get("bbox")}
                    edit_result_hash = _write_receipt(Path(temporary) / "edit-result.json", edit)
                    journal_head = _write_receipt(outcome_path, {
                        "schema": "catia-edit-outcome/1", "intentSha256": intent_hash,
                        "status": "passed", "directory": Path(temporary).name,
                        "editResultSha256": edit_result_hash})
                    edits.append(edit)
                    budget.timeout()
                    break
            else:
                if not valid_variant:
                    raise ValueError(f"No valid edit-check variant for parameter {name}.")
                raise ValueError(f"Parameter {name} did not produce a measurable geometry change.")
        budget.timeout()
        _audit_edit_receipts(progress_path, manifest_hash, journal_head, budget.attempts)
        editability = {"passed": True, "parameters": edits,
                       "declaredParameterCount": len(declared),
                       "sampled": len(checked_parameters) < len(declared)}
        stages.append({"stage": stage, "status": "passed", "detail": editability})
        delivery["preview"] = create_part_preview(str(target), {}, step_path=step_path)
        stage = "report"
        with report_path.open("x", encoding="utf-8") as output:
            json.dump({"schema": "catia-feature-build-report/1", "completed": True,
                       "plan": plan, "trace": built, "verification": verification,
                       "geometry": comparison, "editability": editability, "delivery": delivery},
                      output, ensure_ascii=False, indent=2, allow_nan=False)
        delivery.update({"reportJson": str(report_path), "reportJsonSha256": _file_sha256(report_path),
                         "stepPath": str(step_path)})
        stages.append({"stage": "delivery", "status": "completed", **delivery})
        # Freeze the delivery: the receipt hash must keep describing the file.
        try:
            for frozen in (target, step_path):
                os.chmod(frozen, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            delivery["readOnly"] = True
        except OSError as exc:
            delivery["readOnly"] = f"failed: {exc}"
        return operation_result(tool="catia_build_part", ok=True, code="BUILD_COMPLETED",
            message="Built, independently verified and parameter-edit tested.", modified=True,
            data={"businessState": "completed", "planId": plan["planId"],
                  "stages": [{"stage": item["stage"], "status": item["status"]} for item in stages],
                  "verification": {"verdict": verification["verdict"], "stepExport": verification.get("stepExport")},
                  "editability": {"passed": True, "declaredParameterCount": len(declared),
                                  "verifiedParameterCount": len(edits), "sampled": editability["sampled"],
                                  "manifestSha256": manifest_hash, "journalHeadSha256": journal_head,
                                  "attempts": budget.attempts},
                  "delivery": delivery})
    except Exception as exc:
        if modified and isinstance(exc, CadSupportError) and exc.code == "CAD_SUPPORT_TIMEOUT":
            session_unknown = True
        journal_inspection = None
        if isinstance(exc, EditBudgetExpired) and manifest_hash is not None:
            try:
                journal_inspection = {"status": "verified_recorded_boundary", **_audit_edit_receipts(
                    progress_path, manifest_hash, journal_head, budget.attempts, require_complete=False)}
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                journal_inspection = {"status": "unverified"}
        scene = getattr(exc, "scene", {}) or {}
        reason = str(exc)
        stages.append({"stage": stage, "status": "failed", "detail": reason[:2000],
                       "failedStep": getattr(exc, "stage", None),
                       "sceneSummary": {
                           "stepCount": len(scene.get("steps") or []),
                           "failureKeys": sorted((scene.get("failure") or {}).keys()),
                       }})
        return operation_result(tool="catia_build_part", ok=False,
            code=("VERIFIER_SESSION_UNKNOWN" if session_unknown and not isinstance(exc, CadSupportError) else
                  "EDIT_COVERAGE_INCOMPLETE" if isinstance(exc, EditBudgetExpired) else
                  exc.code if isinstance(exc, CadSupportError) else "FEATURE_BUILD_FAILED"),
            message=reason[:2000], modified=modified,
            data={"businessState": "session_unknown" if session_unknown else ("verification_incomplete" if isinstance(exc, EditBudgetExpired) else ("verification_failed" if delivery else "build_failed")),
                  **({"sessionHealth": "unknown"} if session_unknown else {}),
                  **({"editProgress": {"declaredParameterCount": len(declared), "verifiedParameterCount": len(edits),
                                       "attempts": budget.attempts, "manifestPath": str(progress_path),
                                       "manifestSha256": manifest_hash, "journalHeadSha256": journal_head,
                                       **({"journalInspection": journal_inspection} if journal_inspection else {})}} if budget else {}),
                  "failure": {"stage": stage, "reason": reason[:2000],
                              "failedStep": getattr(exc, "stage", None),
                              **({"classification": "material_comparison_inconclusive", "summary": exc.summary}
                                 if isinstance(exc, CadSupportError) and exc.code == "MATERIAL_COMPARISON_INCONCLUSIVE" else {})},
                  "stages": stages, "delivery": delivery, "planId": plan["planId"]})
    finally:
        if not session_unknown:
            executor.close()
