"""Cross-language structural parity and separate domain/serialization checks.

For every migrated family the same payload is evaluated by:

* the packaged Python wire model (Pydantic),
* the exported JSON Schema through jsonschema Draft 2020-12.

Ajv is exercised by the Node parity spec against the same declared matrix.
Domain decisions, error codes and serialization digests are reported in
separate sections so a domain rejection is never counted as a schema defect.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path


def _load_fixture(repo: Path, name: str) -> dict:
    return json.loads((repo / "contracts/catia/v1/fixtures/plans" / name).read_text(encoding="utf-8"))


def _feature_request() -> dict:
    return {
        "schema": "catia-feature-requirements/1", "unit": "mm", "unitEvidence": [],
        "parameters": [{"name": "T", "value": 8, "evidence": []}],
        "features": [{"id": "base", "operation": "pad", "length": "T",
                      "sketch": {"plane": "xy", "profiles": [["circle", 0, 0, "T"]]}}],
    }


def _success_receipt() -> dict:
    hex64 = "a" * 64
    return {
        "ok": True, "code": "BUILD_COMPLETED", "operation_id": "op-1", "tool": "catia_build_part",
        "message": "built", "effects": {"modified": True},
        "data": {
            "businessState": "completed", "planId": "sha256:" + "b" * 64,
            "stages": [{"stage": "requirements", "status": "passed"}],
            "verification": {"verdict": "PASS", "stepExport": {"path": "C:\\out\\p.step", "sha256": hex64}},
            "editability": {"passed": True, "declaredParameterCount": 2,
                            "verifiedParameterCount": 2, "sampled": False},
            "delivery": {"path": "C:\\out\\p.CATPart", "sha256": hex64,
                         "reportJson": "C:\\out\\r.json", "reportJsonSha256": hex64,
                         "stepPath": "C:\\out\\p.step"},
        },
        "diagnostics": [], "warnings": [],
        "recovery": {"rollback_supported": False, "automatic_retry_allowed": False, "next_actions": []},
    }


def _intent_snapshot() -> dict:
    impact = {"handle": "unused", "original": 0.0, "probeValue": 1.0, "probeDelta": 1.0,
              "writeStuck": True, "bboxDeltaX": 1.0, "bboxDeltaY": 0.0, "bboxDeltaZ": 0.0,
              "bboxEdgeDeltas": {"x": {"min": -0.5, "max": 0.5},
                                  "y": {"min": 0.0, "max": 0.0},
                                  "z": {"min": 0.0, "max": 0.0}},
              "holeCentersMoved": True, "holeRadiiChanged": False, "holeRadiusDelta": 0.0,
              "geometryChanged": True, "edgeDistancesPreserved": True, "restored": True}
    return {"schema": "catia-model-snapshot/1", "editSessionId": "sess123", "modelRevision": 0,
            "parameters": [{"handle": "Part1\\L", "name": "Part1\\L", "value": 160.0,
                            "unit": "mm", "kind": "driver", "writable": True}],
            "holes": [], "geometry": {"volumeMm3": 1.0,
                                       "bbox": {"min": {"x": 0, "y": 0, "z": 0},
                                                "max": {"x": 160, "y": 1, "z": 1},
                                                "dimensions": {"x": 160.0, "y": 1.0, "z": 1.0}}},
            "impacts": {"Part1\\L": impact}, "capabilities": {"invariants": {}}, "limitations": []}


def _summary() -> dict:
    return {"reason": "FUZZY_LEVELS_DISAGREE", "consistencyBudgetMm3": 0.01,
            "attemptCount": 2, "candidateCount": 1}


def build_matrix(repo: Path) -> list[dict]:
    plan = _load_fixture(repo, "edit-plan-v1.json")
    intent = {"schema": "cad-intent-draft/1",
              "target": {"parameter": "Part1\\L", "attribute": "length"},
              "operation": {"type": "set", "amount": 200, "unit": "mm", "scope": "overall"},
              "reference": {"basis": "current"}}
    feature = _feature_request()
    receipt = _success_receipt()
    cases = [
        # contract, case, payload, expected validity, known-difference reason
        ("intent-draft", "minimal", {"schema": "cad-intent-draft/1"}, True, None),
        ("intent-draft", "decimal_amount", {**intent, "operation": {**intent["operation"], "amount": 200.0}}, True, None),
        ("intent-draft", "string_amount", {**intent, "operation": {**intent["operation"], "amount": "200"}}, False, None),
        ("intent-draft", "boolean_amount", {**intent, "operation": {**intent["operation"], "amount": True}}, False, None),
        ("intent-draft", "unknown_field", {**intent, "userText": "host"}, False, None),
        ("intent-draft", "integer_valued_hole_count", {"schema": "cad-intent-draft/1",
                                                       "target": {"holeCount": 1.0}}, True, None),
        ("compile-intent-input", "valid", {"editSessionId": "s", "intent": intent}, True, None),
        ("compile-intent-input", "missing_intent", {"editSessionId": "s"}, False, None),
        ("apply-edit-plan-input", "frozen_plan", {"editSessionId": "sess0000abcd", "plan": plan}, True, None),
        ("apply-edit-plan-input", "boolean_value", {"editSessionId": "sess0000abcd",
                                                    "plan": {**plan, "changes": [{**plan["changes"][0], "newValue": True}]}},
         False, None),
        ("apply-edit-plan-input", "integer_valued_revision",
         {"editSessionId": "sess0000abcd", "plan": {**plan, "expectedRevision": 3.0}}, True, None),
        ("feature-requirements", "compact_ok", feature, True, None),
        ("feature-requirements", "short_tuple", {**feature, "features": [{**feature["features"][0],
                                                                           "sketch": {"plane": "xy", "profiles": [["circle", 0, 0]]}}]},
         False, None),
        ("feature-requirements", "chamfer_angle",
         {**feature, "features": [feature["features"][0],
                                   {"id": "cut", "operation": "chamfer", "dependsOn": ["base"],
                                    "length": "T", "angle": 30,
                                    "edges": {"type": "linear", "direction": [1, 0, 0], "through": [0, 0, 0]}}]},
         False, None),
        ("feature-requirements", "coord_out_of_range",
         {**feature, "features": [{**feature["features"][0],
                                    "sketch": {"plane": "xy", "profiles": [["polygon", [[1_000_001, 0], [0, 0], [0, 1]]]]}}]},
         False, None),
        ("feature-requirements", "extra_feature_key",
         {**feature, "features": [{**feature["features"][0], "legacy": True}]}, False, None),
        ("build-result", "success", receipt, True, None),
        ("build-result", "missing_modified",
         {**receipt, "effects": {}}, False, None),
        ("build-result", "string_modified",
         {**receipt, "effects": {"modified": "true"}}, False, None),
        ("cad-support-inconclusive-summary", "valid", _summary(), True, None),
        ("cad-support-inconclusive-summary", "float_attempt",
         {**_summary(), "attemptCount": 1.0}, False,
         "JSON Schema integer accepts 1.0; the existing helper protocol requires type int. Python wins; the schema mismatch is recorded."),
        ("cad-support-inconclusive-summary", "extra_key",
         {**_summary(), "extra": 1}, False, None),
        ("cad-support-inconclusive-summary", "null_diagnostics",
         {**_summary(), "volumeDiagnostics": None}, False,
         "Optional fields are nullable in JSON Schema; the Python model rejects an explicit null diagnostic to avoid hiding a missing measurement."),
    ]
    return [{"contract_id": c, "case": case, "payload": payload,
             "expected_valid": expected, "known_difference": known}
            for c, case, payload, expected, known in cases]


def build_report(repo: Path) -> dict:
    server = repo / "catia-v5-mcp-server"
    sys.path.insert(0, str(server))
    from jsonschema import Draft202012Validator
    from catia_mcp.contracts import export
    from catia_mcp.contracts.dispatch import CompileIntentInputWire
    from catia_mcp.contracts.edit_plan import EditPlanWire
    from catia_mcp.contracts.features import FeatureRequirementsWire
    from catia_mcp.contracts.intent import IntentDraftWire
    from catia_mcp.contracts.results import OperationResultWire
    from catia_mcp.contracts.cad_support import InconclusiveSummary

    models = {
        "intent-draft": IntentDraftWire,
        "compile-intent-input": CompileIntentInputWire,
        "apply-edit-plan-input": __import__("catia_mcp.contracts.dispatch", fromlist=["ApplyEditPlanInputWire"]).ApplyEditPlanInputWire,
        "edit-plan": EditPlanWire,
        "feature-requirements": FeatureRequirementsWire,
        "build-result": OperationResultWire,
        "cad-support-inconclusive-summary": InconclusiveSummary,
    }
    schemas = export.build_schemas()
    structural = []
    divergences = []
    known = []
    for case in build_matrix(repo):
        contract = case["contract_id"]
        model = models[contract]
        try:
            model.model_validate(case["payload"])
            python_ok = True
        except Exception:
            python_ok = False
        schema_ok = Draft202012Validator(schemas[contract]).is_valid(case["payload"])
        agree = python_ok == schema_ok == case["expected_valid"]
        record = {"contract_id": contract, "case": case["case"],
                  "python": python_ok, "jsonschema": schema_ok,
                  "expected": case["expected_valid"], "agree": agree}
        structural.append(record)
        if not agree:
            (known if case["known_difference"] else divergences).append(
                {**record, "reason": case["known_difference"]})

    # Domain decisions are reported separately.
    from catia_mcp.drawing.feature_model import compile_features
    from catia_mcp.editing.intent import compile_intent
    from catia_mcp.editing.validation import plan_hash, validate_plan
    from catia_mcp.contracts.results import classify_build_result
    from catia_mcp.contracts.cad_support import classify_cad_support_response

    snapshot = _load_fixture(repo, "edit-snapshot-v1.json")
    plan = _load_fixture(repo, "edit-plan-v1.json")
    domain = {
        "edit_plan_valid_codes": [p["code"] for p in validate_plan(plan, snapshot, session_id="sess0000abcd")],
        "edit_plan_bad_codes": [p["code"] for p in validate_plan(
            {**plan, "changes": [{**plan["changes"][0], "oldValue": 150.0}]},
            snapshot, session_id="sess0000abcd")],
        "intent_planned": compile_intent(
            {"schema": "cad-intent-draft/1", "text": "length set 200 mm",
             "target": {"parameter": "Part1\\L", "attribute": "length"},
             "operation": {"type": "set", "amount": 200, "unit": "mm", "scope": "overall"},
             "reference": {"basis": "current"}}, _intent_snapshot(), session_id="sess123")["decision"],
        "feature_duplicate_code": None,
        "result_states": {
            "success": classify_build_result(_success_receipt())["state"],
        },
        "support_states": {
            "inconclusive": classify_cad_support_response(
                {"ok": False, "operation": "evaluate", "exitCode": 1,
                 "code": "MATERIAL_COMPARISON_INCONCLUSIVE",
                 "error": {"type": "MaterialComparisonInconclusive", "message": "x",
                           "summary": _summary()}})["state"],
        },
    }
    try:
        compile_features({"schema": "catia-feature-requirements/1", "unit": "mm", "unitEvidence": [],
                          "parameters": [{"name": "T", "value": 8, "evidence": []}],
                          "features": [{"id": "a", "operation": "pad", "length": "T",
                                        "sketch": {"plane": "xy", "profiles": [["circle", 0, 0, "T"]]}},
                                       {"id": "a", "operation": "pad", "length": "T",
                                        "sketch": {"plane": "xy", "profiles": [["circle", 0, 0, "T"]]}}]},
                         None, require_evidence=False)
    except Exception as error:
        domain["feature_duplicate_code"] = getattr(error, "code", None)

    oracles = _load_fixture(repo, "hash-oracles.json")
    build = _load_fixture(repo, "build-plan-v1.json")
    feature_plan = _load_fixture(repo, "feature-plan-v1.json")
    from catia_mcp.drawing.plan import _plan_id
    feature_payload = {k: v for k, v in feature_plan.items() if k != "planId"}
    digests = {
        "edit_plan": plan_hash(plan),
        "build_plan": _plan_id(build),
        "feature_plan": "sha256:" + hashlib.sha256(
            json.dumps(feature_payload, sort_keys=True, allow_nan=False).encode()).hexdigest(),
    }
    hashes_ok = all(digests[key] == oracles[{"edit_plan": "edit_plan", "build_plan": "build_plan",
                                             "feature_plan": "feature_plan"}[key]]["expected_digest"]
                    for key in digests)
    registry = json.loads((repo / "contracts/catia/v1/registry.json").read_text(encoding="utf-8"))
    return {
        "schema_version": "catiaagent-contract-parity/1",
        "structural": structural,
        "divergences": divergences,
        "known_differences": known,
        "domain": domain,
        "hashes": {"digests": digests, "match_frozen": hashes_ok},
        "registry_modes": {item["contract_id"]: item["mode"] for item in registry["contracts"]},
        "ajv_evidence": "PilotDeck/tests/tool/protocol/catia-contract-parity.spec.ts",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    report = build_report(Path(args.repo_root).resolve())
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ok = (not report["divergences"] and report["hashes"]["match_frozen"]
          and not any(item["expected"] != item["python"] for item in report["structural"]))
    print(f"cases={len(report['structural'])} divergences={len(report['divergences'])} "
          f"known={len(report['known_differences'])} hashes_ok={report['hashes']['match_frozen']}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
