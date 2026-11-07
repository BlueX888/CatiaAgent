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
import hashlib
import json
import math
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


def reference_provenance(reference, expectation, equivalence_path=None):
    """Bind the comparison reference to the frozen source or proven equivalent."""
    digest = hashlib.sha256()
    with reference.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    actual = digest.hexdigest()
    frozen = expectation.get("reference", {}).get("sha256")
    provenance = {"sha256": actual, "frozenSha256": frozen, "verified": False}
    if isinstance(frozen, str) and actual == frozen.lower():
        return {**provenance, "verified": True, "method": "frozen_reference"}
    if not equivalence_path or not isinstance(frozen, str):
        return {**provenance, "reason": "Reference does not match the frozen SHA-256."}

    evidence = json.loads(Path(equivalence_path).read_text(encoding="utf-8"))
    data = evidence.get("data") or {}
    sources = data.get("sources") or {}
    hashes = {(sources.get(role) or {}).get("sha256") for role in ("actual", "reference")}
    checks = {item.get("name"): item for item in data.get("checks", [])}
    limits = {"bbox": ("actualMm", "toleranceMm", expectation["tolerances"]["lengthMm"]),
              "missing_material": ("actualMm3", "toleranceMm3", expectation["tolerances"]["volumeMm3"]),
              "excess_material": ("actualMm3", "toleranceMm3", expectation["tolerances"]["volumeMm3"])}
    def valid_check(name, actual_key, tolerance_key, limit):
        check = checks.get(name) or {}
        value, tolerance = check.get(actual_key), check.get(tolerance_key)
        return (check.get("passed") is True and isinstance(value, (float, int))
                and not isinstance(value, bool) and math.isfinite(value) and value >= 0
                and isinstance(tolerance, (float, int)) and not isinstance(tolerance, bool)
                and math.isfinite(tolerance) and 0 <= value <= tolerance <= limit)
    verified = (evidence.get("ok") is True and data.get("verdict") == "PASS"
                and hashes == {actual, frozen.lower()}
                and all(valid_check(name, *keys) for name, keys in limits.items()))
    result = {**provenance, "verified": verified, "method": "material_equivalence",
              "evidence": str(Path(equivalence_path).resolve()),
              "reason": None if verified else "Reference equivalence evidence is incomplete or does not match the frozen reference and tolerances."}
    if verified:
        # Reserve the worst reference mismatch so two comparisons cannot silently
        # spend the frozen tolerance twice (either direction may be larger).
        result["remainingLengthMm"] = limits["bbox"][2] - checks["bbox"]["actualMm"]
        result["remainingVolumeMm3"] = limits["missing_material"][2] - max(
            checks["missing_material"]["actualMm3"], checks["excess_material"]["actualMm3"])
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--part", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--expectation", default=None)
    parser.add_argument("--reference-equivalence", default=None,
                        help="CAD support comparison JSON proving an alternate reference matches the frozen reference")
    args = parser.parse_args(argv)

    expectation_path = Path(args.expectation or (BENCH / "verification" / f"{args.case}.json"))
    expectation = json.loads(expectation_path.read_text(encoding="utf-8"))
    if expectation.get("caseId") != args.case:
        raise SystemExit("Expectation caseId does not match --case")
    part = Path(args.part).resolve()
    reference = Path(args.reference).resolve()
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    provenance = reference_provenance(reference, expectation, args.reference_equivalence)
    step_path = out.with_suffix(".delivered.step")
    spec_path = out.with_suffix(".spec.json")
    compare_path = out.with_suffix(".compare.json")
    for path in (out, step_path, spec_path, compare_path):
        if path.exists():
            raise SystemExit(f"Refusing to overwrite {path}")

    spec = {
        "partPath": str(part),
        "expected": {k: expectation["expected"][k] for k in ("volumeMm3", "bbox")},
        "tolerances": {k: expectation["tolerances"][k]
                       for k in ("lengthMm", "coordinateMm", "volumeMm3")},
        "exportStep": str(step_path),
    }
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    python = str(Path(sys.executable))
    verify_code, stdout, stderr = run([python, str(REPO / "scripts" / "independent_verify_part.py"),
                                       str(spec_path)])
    try:
        verification = json.loads(stdout)
    except json.JSONDecodeError:
        verification = {"verdict": "FAIL", "raw": stdout[-4000:], "stderr": stderr[-2000:]}

    comparison = None
    compare_code = None
    if step_path.exists():
        compare_spec = {
            "actual": str(step_path), "reference": str(reference),
            "lengthToleranceMm": provenance.get("remainingLengthMm", expectation["tolerances"]["lengthMm"]),
            "volumeToleranceMm3": provenance.get("remainingVolumeMm3", expectation["tolerances"]["volumeMm3"]),
        }
        if "probes" in expectation:
            compare_spec["probes"] = expectation["probes"]
        compare_path.write_text(json.dumps(compare_spec, indent=1), encoding="utf-8")
        compare_code, stdout, stderr = run([str(CAD_PYTHON), "-m", "catia_mcp.cad_support",
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
        "referenceProvenance": provenance,
        "expectation": str(expectation_path),
        "verification": verification,
        "comparison": comparison,
        "verdict": "PASS" if (provenance["verified"] and verify_code == 0 and compare_code == 0
                              and verification.get("verdict") == "PASS"
                              and (comparison or {}).get("ok") is True
                              and (comparison or {}).get("data", {}).get("verdict") == "PASS") else "FAIL",
    }
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"case": args.case, "verdict": report["verdict"],
                      "verification": verification.get("verdict"),
                      "comparison": (comparison or {}).get("data", {}).get("verdict"),
                      "out": str(out)}, ensure_ascii=False))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
