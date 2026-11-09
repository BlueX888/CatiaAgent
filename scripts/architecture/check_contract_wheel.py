"""Build, unpack and exercise the packaged contract schemas.

Emits ``contract-wheel-check/1`` JSON with exactly five mandatory checks:
wheel_built, contracts_packaged, dependency_metadata, isolated_import,
schema_compilation.  Any missing or failed check exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REQUIRED_CHECKS = [
    "wheel_built",
    "contracts_packaged",
    "dependency_metadata",
    "isolated_import",
    "schema_compilation",
]
EXPECTED_MODULES = {
    "__init__.py", "base.py", "errors.py", "intent.py", "edit_plan.py",
    "results.py", "cad_support.py", "dispatch.py", "features.py",
    "feature_primitives.py", "export.py",
}


def run(argv: list[str], cwd: Path, evidence: Path) -> tuple[int, str]:
    proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr, encoding="utf-8")
    return proc.returncode, proc.stdout + proc.stderr


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-root", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    server_root = Path(args.server_root).resolve()
    out_path = Path(args.out).resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_root = out_path.parent / "wheel-evidence"
    evidence_root.mkdir(parents=True, exist_ok=True)
    checks: list[dict] = []

    def add(check_id: str, passed: bool, evidence: Path, detail: str = "") -> None:
        checks.append({"id": check_id, "passed": bool(passed),
                       "evidence_path": str(evidence), "detail": detail})

    with tempfile.TemporaryDirectory(prefix="catia-wheel-") as temporary:
        work = Path(temporary)
        dist = work / "dist"
        dist.mkdir()
        evidence = evidence_root / "wheel-build.txt"
        wheel = None
        try:
            exit_code, _ = run([sys.executable, "-m", "pip", "wheel", str(server_root),
                                "--no-deps", "--no-build-isolation", "-w", str(dist)],
                               work, evidence)
            wheels = sorted(dist.glob("*.whl"))
            wheel = wheels[0] if wheels else None
            add("wheel_built", exit_code == 0 and wheel is not None and wheel.stat().st_size > 0,
                evidence, f"exit={exit_code} wheel={wheel.name if wheel else None}")
        except Exception as error:
            evidence.write_text(f"wheel build failed: {error}", encoding="utf-8")
            add("wheel_built", False, evidence, str(error))

        site = work / "site"
        names: list[str] = []
        if wheel is not None:
            try:
                with zipfile.ZipFile(wheel) as archive:
                    names = archive.namelist()
                    archive.extractall(site)
            except Exception as error:
                (evidence_root / "unpack.txt").write_text(str(error), encoding="utf-8")
        packaged = {name.split("catia_mcp/contracts/", 1)[1] for name in names
                    if "catia_mcp/contracts/" in name and name.endswith(".py")}
        manifest = evidence_root / "packaged-contracts.txt"
        manifest.write_text("\n".join(sorted(packaged)), encoding="utf-8")
        add("contracts_packaged", EXPECTED_MODULES.issubset(packaged), manifest,
            f"missing={sorted(EXPECTED_MODULES - packaged)}")

        metadata_path = evidence_root / "METADATA.txt"
        requires = ""
        if names:
            metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
            if metadata_names:
                source = site / metadata_names[0]
                requires = source.read_text(encoding="utf-8", errors="replace")
                metadata_path.write_text(requires, encoding="utf-8")
        dependency_ok = ("Requires-Dist: pydantic" in requires
                         and ">=2.4" in requires and "<3" in requires
                         and "Requires-Dist: mcp" in requires
                         and "Requires-Dist: jsonschema" in requires)
        add("dependency_metadata", dependency_ok, metadata_path,
            "pydantic>=2.4,<3 and runtime dependencies must be declared")

        isolated_script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(site)!r})\n"
            "import catia_mcp.contracts.export as export\n"
            "print(json.dumps({'file': export.__file__, 'schemas': sorted(export.build_schemas())}))\n"
        )
        import_evidence = evidence_root / "isolated-import.txt"
        import_ok = False
        try:
            proc = subprocess.run([sys.executable, "-I", "-c", isolated_script], cwd=site,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=120)
            import_evidence.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr,
                                       encoding="utf-8")
            payload = json.loads(proc.stdout)
            import_ok = (proc.returncode == 0
                         and Path(payload["file"]).resolve().is_relative_to(site.resolve())
                         and "intent-draft" in payload["schemas"])
        except Exception as error:
            import_evidence.write_text(f"isolated import failed: {error}", encoding="utf-8")
        add("isolated_import", import_ok, import_evidence, "import from unpacked wheel, cwd outside repo")

        compile_script = (
            "import json, sys\n"
            f"sys.path.insert(0, {str(site)!r})\n"
            "from catia_mcp.contracts.export import build_schemas\n"
            "from jsonschema import Draft202012Validator\n"
            "result = {}\n"
            "for cid, schema in build_schemas().items():\n"
            "    Draft202012Validator.check_schema(schema)\n"
            "    defs = schema.get('$defs', {})\n"
            "    refs = []\n"
            "    def walk(node):\n"
            "        if isinstance(node, dict):\n"
            "            if isinstance(node.get('$ref'), str) and node['$ref'].startswith('#/$defs/'):\n"
            "                refs.append(node['$ref'].split('/')[-1])\n"
            "            for value in node.values(): walk(value)\n"
            "        elif isinstance(node, list):\n"
            "            for value in node: walk(value)\n"
            "    walk(schema)\n"
            "    missing = [name for name in refs if name not in defs]\n"
            "    result[cid] = {'valid': not missing, 'refs': len(refs), 'missing': missing}\n"
            "print(json.dumps(result))\n"
        )
        compile_evidence = evidence_root / "schema-compilation.txt"
        compile_ok = False
        try:
            proc = subprocess.run([sys.executable, "-I", "-c", compile_script], cwd=site,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=120)
            compile_evidence.write_text(proc.stdout + "\n--- stderr ---\n" + proc.stderr,
                                        encoding="utf-8")
            payload = json.loads(proc.stdout)
            compile_ok = (proc.returncode == 0 and payload
                          and all(item["valid"] for item in payload.values()))
        except Exception as error:
            compile_evidence.write_text(f"schema compilation failed: {error}", encoding="utf-8")
        add("schema_compilation", compile_ok, compile_evidence,
            "Draft 2020-12 compile plus local $ref resolution")

    by_id = {check["id"]: check for check in checks}
    ordered = [by_id.get(check_id, {"id": check_id, "passed": False,
                                    "evidence_path": "", "detail": "check not produced"})
               for check_id in REQUIRED_CHECKS]
    payload = {
        "schema_version": "contract-wheel-check/1",
        "source_fingerprint": os.environ.get("GATE_SOURCE_FINGERPRINT"),
        "checks": ordered,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if all(check["passed"] for check in ordered) else 1


if __name__ == "__main__":
    raise SystemExit(main())
