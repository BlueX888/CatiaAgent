"""Acceptance check for one delivered NIST CATPart.

Runs the independent reopen verifier (CATIA, separate process) and then an OCCT
material comparison of the delivered STEP against the reference STEP. Writes a
single report; never modifies the delivery.

Usage (main MCP interpreter):
    python verify_case.py --case FTC-09 --part work/.../part.CATPart \
        --reference datasets/NIST/.../nist_ftc_09_asme1_rd.stp \
        --out runs/nist-pdf-benchmark/20261029/FTC-09/acceptance.json
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REPO = ROOT / "catia-v5-mcp-server"
BENCH = ROOT / "datasets" / "nist-pdf-benchmark"
CAD_PYTHON = Path(r"F:\CatiaAgent\work\text-to-cad-env\Scripts\python.exe")


def run(command, cwd=None):
    completed = subprocess.run(command, cwd=cwd or REPO, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    return completed.returncode, completed.stdout, completed.stderr


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--part", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--expectation", default=None)
    args = parser.parse_args(argv)

    expectation_path = Path(args.expectation or (BENCH / "verification" / f"{args.case}.json"))
    expectation = json.loads(expectation_path.read_text(encoding="utf-8"))
    part = Path(args.part).resolve()
    reference = Path(args.reference).resolve()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    step_path = out.with_suffix(".delivered.step")
    if step_path.exists():
        raise SystemExit(f"Refusing to overwrite {step_path}")

    spec = {
        "partPath": str(part),
        "expected": {k: expectation["expected"][k] for k in ("volumeMm3", "bbox")},
        "tolerances": {k: expectation["tolerances"][k]
                       for k in ("lengthMm", "coordinateMm", "volumeMm3")},
        "exportStep": str(step_path),
    }
    spec_path = out.with_suffix(".spec.json")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    python = str(Path(sys.executable))
    code, stdout, stderr = run([python, str(REPO / "scripts" / "independent_verify_part.py"),
                                str(spec_path)])
    try:
        verification = json.loads(stdout)
    except json.JSONDecodeError:
        verification = {"verdict": "FAIL", "raw": stdout[-4000:], "stderr": stderr[-2000:]}

    comparison = None
    if step_path.exists():
        compare_spec = {
            "actual": str(step_path), "reference": str(reference),
            "lengthToleranceMm": expectation["tolerances"]["lengthMm"],
            "volumeToleranceMm3": expectation["tolerances"]["volumeMm3"],
        }
        compare_path = out.with_suffix(".compare.json")
        compare_path.write_text(json.dumps(compare_spec, indent=1), encoding="utf-8")
        code, stdout, stderr = run([str(CAD_PYTHON), "-m", "catia_mcp.cad_support",
                                    "compare", str(compare_path)])
        try:
            comparison = json.loads(stdout)
        except json.JSONDecodeError:
            comparison = {"ok": False, "raw": stdout[-4000:], "stderr": stderr[-2000:]}

    report = {
        "schema": "nist-case-acceptance/1",
        "caseId": args.case,
        "part": str(part),
        "reference": str(reference),
        "expectation": str(expectation_path),
        "verification": verification,
        "comparison": comparison,
        "verdict": "PASS" if (verification.get("verdict") == "PASS"
                              and (comparison or {}).get("ok") is True) else "FAIL",
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"case": args.case, "verdict": report["verdict"],
                      "verification": verification.get("verdict"),
                      "comparison": (comparison or {}).get("data", {}).get("verdict"),
                      "out": str(out)}, ensure_ascii=False))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
