"""Live acceptance for the PDF drawing -> parametric part workflow.

Not collected by pytest: it needs CATIA V5 and the fixture set. Run:

    .venv\\Scripts\\python.exe tests\\acceptance\\run_pdf_modeling_acceptance.py
    .venv\\Scripts\\python.exe tests\\acceptance\\run_pdf_modeling_acceptance.py DP-01 --repeat 2

Environment:
    CATIA_PDF_MODELING_ROOT   fixture root (default F:\\CatiaAgent\\datasets\\pdf-modeling)
    CATIA_PDF_MODELING_RUNS   output root   (default F:\\CatiaAgent\\runs\\pdf-modeling\\<date>)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

FIXTURE_ROOT = Path(os.environ.get("CATIA_PDF_MODELING_ROOT", r"F:\CatiaAgent\datasets\pdf-modeling"))
RUN_ROOT = Path(
    os.environ.get(
        "CATIA_PDF_MODELING_RUNS",
        rf"F:\CatiaAgent\runs\pdf-modeling\{datetime.now():%Y%m%d}",
    )
)
WORK_ROOT = Path(os.environ.get("CATIA_MCP_WORK_DIR", r"F:\CatiaAgent\work\pdf-modeling"))
os.environ["CATIA_MCP_WORK_DIR"] = str(WORK_ROOT)

from catia_mcp.connection import CATIAConnection  # noqa: E402
from catia_mcp.drawing.pdf_evidence import inspect_pdf  # noqa: E402
from catia_mcp.drawing.plan import PlanError, compile_build_plan  # noqa: E402
from catia_mcp.drawing.requirements import validate_requirements  # noqa: E402
from catia_mcp.drawing.tools import DrawingTools  # noqa: E402


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_case(case_id: str) -> tuple[dict, dict]:
    requirements = json.loads((FIXTURE_ROOT / "requirements" / f"{case_id}.json").read_text(encoding="utf-8"))
    expectation = json.loads((FIXTURE_ROOT / "verification" / f"{case_id}.json").read_text(encoding="utf-8"))
    return requirements, expectation


def resolve_drawing(requirements: dict) -> Path:
    raw = Path(requirements["drawing"]["path"])
    return raw if raw.is_absolute() else (FIXTURE_ROOT / raw)


def decide(case_id: str) -> dict:
    """Read + validate + compile without touching the model."""
    requirements, expectation = load_case(case_id)
    drawing = resolve_drawing(requirements)
    started = time.time()
    result: dict = {
        "caseId": case_id,
        "drawing": str(drawing),
        "drawingSha256": sha256(drawing) if drawing.is_file() else None,
        "expectedOutcome": expectation.get("expectedOutcome"),
        "stages": {},
    }
    try:
        manifest = inspect_pdf(str(drawing))
        result["stages"]["inspect"] = {"status": "passed", "evidencePath": manifest["evidencePath"],
                                       "spans": sum(len(p["spans"]) for p in manifest["pages"])}
    except Exception as exc:
        result["stages"]["inspect"] = {"status": "failed", "detail": str(exc)}
        result["decision"] = "invalid"
        result["elapsedSeconds"] = round(time.time() - started, 2)
        return result

    validated = validate_requirements(requirements, manifest)
    result["decision"] = validated["decision"]
    result["questions"] = validated["questions"]
    result["problems"] = validated["problems"]
    result["evidenceChecks"] = validated["evidenceChecks"]
    result["stages"]["validate"] = {"status": "passed", "decision": validated["decision"]}
    if validated["decision"] == "ready":
        try:
            plan = compile_build_plan(validated["normalized"])
            result["planId"] = plan["planId"]
            result["steps"] = len(plan["steps"])
            result["stages"]["plan"] = {"status": "passed", "planId": plan["planId"]}
        except PlanError as exc:
            result["stages"]["plan"] = {"status": "failed", "detail": str(exc)}
            result["decision"] = "invalid"
    result["elapsedSeconds"] = round(time.time() - started, 2)
    return result


def build(case_id: str, run_index: int) -> dict:
    requirements, expectation = load_case(case_id)
    delivery = RUN_ROOT / f"{case_id}-run{run_index}.CATPart"
    if delivery.exists():
        delivery.unlink()
    tools = DrawingTools(CATIAConnection())
    started = time.time()
    result = tools.execute(
        "catia_build_part",
        {
            "requirements": requirements,
            "deliveryPath": str(delivery),
            "editCheck": True,
            "expected": expectation["expected"],
        },
    )
    elapsed = round(time.time() - started, 2)
    blob = json.dumps(result, ensure_ascii=False)
    return {
        "resultBytes": len(blob),
        "caseId": case_id,
        "run": run_index,
        "deliveryPath": str(delivery),
        "ok": result.get("ok"),
        "code": result.get("code"),
        "message": result.get("message"),
        "elapsedSeconds": elapsed,
        "stages": result.get("data", {}).get("stages"),
        "verification": result.get("data", {}).get("verification"),
        "editability": result.get("data", {}).get("editability"),
        "delivery": result.get("data", {}).get("delivery"),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*", help="case ids; default all")
    parser.add_argument("--repeat", type=int, default=1, help="independent builds per supported case")
    parser.add_argument("--decide-only", action="store_true", help="do not build")
    args = parser.parse_args()

    cases = args.cases or sorted(p.stem for p in (FIXTURE_ROOT / "requirements").glob("*.json"))
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
    WORK_ROOT.mkdir(parents=True, exist_ok=True)

    report = {
        "schema": "catia-pdf-modeling-acceptance/1",
        "startedAt": datetime.now().isoformat(timespec="seconds"),
        "fixtureRoot": str(FIXTURE_ROOT),
        "runRoot": str(RUN_ROOT),
        "cases": [],
    }
    failures = 0
    for case_id in cases:
        print(f"== {case_id}")
        decision = decide(case_id)
        print(f"   decision {decision['decision']} (expected {decision['expectedOutcome']})")
        expected = decision["expectedOutcome"]
        actual = "supported" if decision["decision"] == "ready" else decision["decision"]
        matches = actual == expected
        entry = {"decision": decision, "decisionMatches": matches, "builds": []}
        if not matches:
            failures += 1
            print("   MISMATCH")
        if (
            not args.decide_only
            and decision["decision"] == "ready"
            and decision["expectedOutcome"] == "supported"
        ):
            for run_index in range(1, args.repeat + 1):
                build_result = build(case_id, run_index)
                entry["builds"].append(build_result)
                print(f"   build run{run_index}: ok={build_result['ok']} "
                      f"code={build_result['code']} {build_result['elapsedSeconds']}s")
                if build_result["verification"]:
                    verdict = build_result["verification"].get("verdict")
                    print(f"      verification {verdict}")
                if not build_result["ok"]:
                    failures += 1
                if build_result.get("resultBytes", 0) > 19000:
                    failures += 1
                    print(f"   RESULT TOO LARGE for the UI preview cap: {build_result['resultBytes']} bytes")
        report["cases"].append(entry)

    report["finishedAt"] = datetime.now().isoformat(timespec="seconds")
    report["failures"] = failures
    report_path = RUN_ROOT / "acceptance-report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"report: {report_path}")
    print(f"failures: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
