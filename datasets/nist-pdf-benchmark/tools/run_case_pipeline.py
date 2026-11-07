"""Run one NIST case through prepare -> isolated generation -> acceptance.

The generation side sees only the PDF copied by prepare-generation-run.mjs.
Acceptance runs the independent reopen verifier and the OCCT material
comparison against the frozen reference STEP. A re-export requires independent
material-equivalence evidence supplied with --reference-equivalence; otherwise
its comparison remains diagnostic and the acceptance cannot pass.

Usage:
    python run_case_pipeline.py --case FTC-09 --pdf <derived.pdf> \
        --reference <reference.step> --run-label v4
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
AGENT = ROOT / "AgentCPM"
REPO = ROOT / "catia-v5-mcp-server"
BENCH = ROOT / "datasets" / "nist-pdf-benchmark"
NODE = Path(r"F:\CodingAgent\claude-code\work\CatiaAgent\20261021-node22\node\node-v22.23.3-win-x64\node.exe")
PILOT_HOME = Path(r"F:\CatiaAgent\work\ui-home-20261024")
RUNS = ROOT / "work" / "isolated-generation"
RESULTS = ROOT / "runs" / "nist-pdf-benchmark" / "20261030"


def node(args, timeout=None):
    env = dict(os.environ, PILOT_HOME=str(PILOT_HOME))
    completed = subprocess.run([str(NODE), *args], cwd=str(AGENT), env=env,
                               capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=timeout)
    return completed.returncode, completed.stdout, completed.stderr


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def edited_span_axes(parameters, minimum_change_mm=0.02):
    """Read actual span changes, so translating a part cannot fake resizing."""
    axes = set()
    for edit in parameters:
        if edit.get("passed") is not True:
            continue
        before, after = edit.get("bboxBeforeMm"), edit.get("bboxAfterMm")
        if not isinstance(before, dict) or not isinstance(after, dict):
            continue
        for axis in "xyz":
            try:
                values = [box[side][axis] for box in (before, after) for side in ("min", "max")]
                if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                    continue
                a, b, c, d = values
                if a <= b and c <= d and abs((d-c)-(b-a)) > minimum_change_mm:
                    axes.add(axis)
            except (KeyError, TypeError):
                continue
    return sorted(axes)


def generation_evidence(host, pdf, run_code, expected_input_sha256=None,
                        required_span_axes=None, minimum_span_change_mm=0.02):
    """Bind the successful terminal, build report and actual delivery together."""
    host = Path(host).resolve()
    fallback = host.parent / "generation" / "deliveries" / "part.CATPart"
    delivery = fallback if fallback.is_file() else None
    errors = []
    terminal_completed = False
    terminal = None
    manifest_status = None
    span_axes = []
    try:
        manifest = json.loads((host / "manifest.json").read_text(encoding="utf-8"))
        manifest_status = manifest.get("status")
        workspace = Path(manifest["workspace"]).resolve()
        if workspace != (host.parent / "generation").resolve():
            raise ValueError("manifest workspace does not match host")
        input_hash = sha256(pdf)
        if input_hash != manifest.get("inputSha256"):
            errors.append("input PDF does not match manifest")
        if expected_input_sha256 and input_hash != expected_input_sha256.lower():
            errors.append("input PDF does not match frozen expectation")
        if run_code != 0 or manifest.get("status") != "generated_verified" or manifest.get("result", {}).get("passed") is not True:
            errors.append("generation did not finish successfully")
        for line in (host / "events.jsonl").read_text(encoding="utf-8").splitlines():
            event = json.loads(line)
            if event.get("type") in {"error", "turn_completed"}:
                terminal = event
        terminal_completed = bool(terminal and terminal.get("type") == "turn_completed"
                                  and terminal.get("finishReason") == "completed")
        if not terminal_completed:
            errors.append("generation terminal is not completed")
        report_path = Path(manifest["result"]["reportPath"]).resolve()
        if not report_path.is_relative_to(workspace / "deliveries"):
            raise ValueError("build report is outside the delivery directory")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        delivery = Path(report["delivery"]["path"]).resolve()
        if not delivery.is_relative_to(workspace / "deliveries") or delivery.suffix.lower() != ".catpart":
            raise ValueError("reported CATPart is outside the delivery directory")
        if not delivery.is_file() or sha256(delivery) != report["delivery"].get("sha256"):
            errors.append("reported CATPart is missing or does not match build hash")
        verification = report.get("verification", {})
        edits = report.get("editability", {})
        checks, parameters = verification.get("checks"), edits.get("parameters")
        if (report.get("completed") is not True or verification.get("verdict") != "PASS"
                or not isinstance(checks, list) or not checks or any(c.get("passed") is not True for c in checks)
                or edits.get("passed") is not True or not isinstance(parameters, list) or not parameters
                or any(p.get("passed") is not True for p in parameters)):
            errors.append("build report lacks successful verification and parameter edits")
        if isinstance(parameters, list):
            span_axes = edited_span_axes(parameters, minimum_span_change_mm)
        if required_span_axes is not None:
            if (not isinstance(required_span_axes, list) or any(a not in {"x", "y", "z"} for a in required_span_axes)
                    or type(minimum_span_change_mm) not in (float, int) or not math.isfinite(minimum_span_change_mm)
                    or minimum_span_change_mm <= 0):
                raise ValueError("invalid frozen primary span acceptance")
            missing = sorted(set(required_span_axes)-set(span_axes))
            if missing:
                errors.append(f"Independent parameter edits do not demonstrate overall span changes: {missing}")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(f"generation evidence is incomplete: {exc}")
    return {"passed": not errors, "errors": errors, "terminalCompleted": terminal_completed,
            "terminal": terminal, "manifestStatus": manifest_status,
            "editedBBoxSpanAxes": span_axes,
            "delivery": str(delivery) if delivery else None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--reference-equivalence", default=None)
    parser.add_argument("--run-label", required=True)
    parser.add_argument("--expectation", type=Path, default=None, help="Versioned, frozen case expectation")
    parser.add_argument("--results-dir", type=Path, default=None, help="New batch directory; existing runs are never overwritten")
    parser.add_argument("--skip-run", action="store_true", help="recheck an existing run identified by --case and --run-label")
    args = parser.parse_args(argv)
    pdf = Path(args.pdf).resolve()
    reference = Path(args.reference).resolve()
    label = args.run_label
    out_dir = (args.results_dir or RESULTS) / args.case / label
    if args.skip_run and not (out_dir / "run.json").is_file():
        raise SystemExit("--skip-run requires an existing run.json")
    expectation_path = (args.expectation or (BENCH / "verification" / f"{args.case}.json")).resolve()
    expectation = json.loads(expectation_path.read_text(encoding="utf-8"))
    if expectation.get("caseId") != args.case:
        raise SystemExit("Expectation caseId does not match --case")
    frozen_input = expectation.get("input", {}).get("sha256")
    if frozen_input and sha256(pdf) != frozen_input.lower():
        raise SystemExit("Input PDF does not match frozen expectation")
    if args.skip_run:
        run_path = out_dir / "run.json"
        saved = json.loads(run_path.read_text(encoding="utf-8"))
        if saved.get("case") != args.case or saved.get("label") != label:
            raise SystemExit("Existing run does not match case and label")
        host = Path(saved["host"])
        run_code = saved.get("runCode")
    else:
        out_dir.mkdir(parents=True, exist_ok=False)
        code, stdout, stderr = node([str(AGENT / "products" / "catia-agent" / "acceptance"
                                     / "prepare-generation-run.mjs"),
                                 "--pdf", str(pdf), "--runs-dir", str(RUNS)], timeout=180)
        if code:
            print(stdout[-2000:], stderr[-2000:])
            return 1
        prepared = json.loads(stdout.strip().splitlines()[-1])
        host = Path(prepared["directory"]) / "host"
        print("prepared", prepared["directory"], flush=True)
        run_code, stdout, stderr = node(["--import", "tsx",
                                 str(AGENT / "products" / "catia-agent" / "acceptance"
                                     / "run-prepared-generation.mjs"), str(host)], timeout=3 * 3600)
        (out_dir / "runner.log").write_text(stdout + "\n" + stderr, encoding="utf-8")
    parameter_acceptance = expectation.get("parameterAcceptance", {})
    evidence = generation_evidence(host, pdf, run_code, frozen_input,
        parameter_acceptance.get("requiredBBoxSpanEditAxes"),
        parameter_acceptance.get("minimumBBoxSpanChangeMm", 0.02))
    if not args.skip_run:
        terminal = evidence["terminal"] or {}
        summary = {"case": args.case, "label": label, "host": str(host), "runCode": run_code,
                   "expectationPath": str(expectation_path), "expectationSha256": sha256(expectation_path),
                   "manifestStatus": evidence["manifestStatus"], "terminal": terminal.get("type"),
                   "finishReason": terminal.get("finishReason"),
                   "events": json.dumps(terminal, ensure_ascii=False) if terminal else None,
                   "generationEvidence": evidence}
        (out_dir / "run.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"generationEvidence": evidence}, ensure_ascii=False), flush=True)
    if not evidence["terminalCompleted"]:
        print("generation has no completed terminal; CATIA diagnostic is not safe to start", flush=True)
        return 1
    if not evidence["delivery"]:
        print("no delivery", flush=True)
        return 2
    # An incomplete generation can still be inspected, but a reference match
    # for that file is a diagnostic, not a successful generation or acceptance.
    stem = "acceptance" if evidence["passed"] else "diagnostic-acceptance"
    if args.skip_run:
        stem += f"-recheck-{uuid4().hex}"
    acceptance = out_dir / f"{stem}.json"
    verify_command = [str(REPO / ".venv" / "Scripts" / "python.exe"),
         str(BENCH / "tools" / "verify_case.py"), "--case", args.case,
         "--part", evidence["delivery"], "--reference", str(reference),
         "--out", str(acceptance), "--expectation", str(expectation_path)]
    if args.reference_equivalence:
        verify_command.extend(["--reference-equivalence", args.reference_equivalence])
    completed = subprocess.run(
        verify_command,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    acceptance.with_suffix(".stdout").write_text(stdout + "\n" + stderr, encoding="utf-8")
    print(stdout[-2000:], flush=True)
    return code if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
