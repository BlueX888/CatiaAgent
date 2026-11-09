"""Freeze an offline checkpoint and re-run every registered G1/G2 test.

Order matters: the source fingerprint is captured first, then all reports are
written under the release directory.  A fingerprint change aborts the checkpoint
because a moved source tree can no longer be certified by these reports.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
import gate  # noqa: E402
from task_state import source_fingerprint  # noqa: E402

TASKS = [f"T{index:02d}" for index in range(2, 25)]
PROFILES = ["C03", "C05", "C06"]
PYTHON_FREEZE = "constraints/python-offline-freeze.txt"


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=True).stdout


def snapshot_files(repo: Path) -> list[dict]:
    files = []
    for line in _git(repo, "status", "--porcelain=v1", "-uall").splitlines():
        if not line.strip():
            continue
        status, relative = line[:2], line[3:]
        path = repo / relative
        files.append({
            "status": status.strip(),
            "path": relative,
            "sha256": _sha(path) if path.is_file() else None,
        })
    return sorted(files, key=lambda item: item["path"])


def planned_test_runs(tasks_json: Path) -> list[dict]:
    plan = json.loads(tasks_json.read_text(encoding="utf-8"))
    selected = []
    for task in plan["tasks"]:
        if task["id"] not in TASKS:
            continue
        for spec in task.get("test_runs", []):
            selected.append({"task_id": task["id"], "test_run_id": spec["id"],
                             "source_path": spec.get("source_path"),
                             "requires_build": spec.get("requires_build", False)})
    return selected


def _prepend_node(variables: dict[str, str]) -> None:
    node = variables.get("NODE")
    if node:
        folder = os.path.dirname(node)
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")


def _run_profile(profile_id: str, variables: dict[str, str], *, run_root: str,
                 out_dir: str, fingerprint: str) -> dict:
    profiles = json.loads((SCRIPT_DIR / "command_profiles.json").read_text(encoding="utf-8"))["profiles"]
    profile = profiles[profile_id]
    os.makedirs(out_dir, exist_ok=True)
    run_vars = dict(variables)
    report_path = None
    if profile.get("report_path"):
        report_path = gate.expand(profile["report_path"], {**run_vars, "OUT_DIR": os.path.abspath(out_dir)})
        run_vars["REPORT_PATH"] = report_path
        os.makedirs(os.path.dirname(report_path), exist_ok=True)
    records = gate.run_profile(profile, run_vars, out_dir=out_dir)
    record = records[-1]
    if profile.get("report_from_stdout") and report_path:
        Path(report_path).write_text(Path(record["stdout_path"]).read_text(encoding="utf-8"),
                                     encoding="utf-8")
    verdict = gate.evaluate(record, report_format=profile.get("report_format"),
                            report_path=report_path, recorded_fingerprint=fingerprint,
                            current_fingerprint=fingerprint,
                            mandatory_test_ids=profile.get("mandatory_test_ids"),
                            allowed_skips=profile.get("allowed_skips"))
    result = {"profile": profile_id, "status": verdict["status"], "reasons": verdict["reasons"],
              "exit_code": record["exit_code"], "report_path": report_path}
    (Path(out_dir) / "gate-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def build_checkpoint(repo_root: Path, run_root: Path, tasks_json: Path,
                     out_dir: Path) -> dict:
    variables = gate.load_variables(str(run_root / "repository-map.json"))
    variables["REPO_ROOT"] = str(repo_root)
    variables["RUN_ROOT"] = str(run_root)
    _prepend_node(variables)
    fingerprint = source_fingerprint(str(repo_root))
    out_dir.mkdir(parents=True, exist_ok=True)
    freeze = {
        "schema_version": "catiaagent-offline-freeze/1",
        "source_fingerprint": fingerprint,
        "baseline_commit": _git(repo_root, "rev-parse", "HEAD").strip(),
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "changed_files": snapshot_files(repo_root),
    }
    freeze_path = out_dir / "freeze.json"
    freeze_path.write_text(json.dumps(freeze, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    run_vars = dict(variables)
    run_vars["RUN_ROOT"] = str(out_dir)
    profile_results = {}
    for profile_id in PROFILES:
        profile_results[profile_id] = _run_profile(
            profile_id, variables, run_root=str(run_root),
            out_dir=str(out_dir / "commands" / profile_id), fingerprint=fingerprint)

    test_results = []
    for task_id in TASKS:
        plan = json.loads(tasks_json.read_text(encoding="utf-8"))
        task = next(item for item in plan["tasks"] if item["id"] == task_id)
        for index, spec in enumerate(task.get("test_runs", []), start=1):
            target = out_dir / "tasks" / task_id / f"{index:02d}"
            try:
                result = gate.run_test_run(spec, run_vars, run_root=str(out_dir),
                                           out_dir=str(target), current_fingerprint=fingerprint)
            except gate.GateError as error:
                result = {"test_run_id": spec.get("id"), "status": "FAIL",
                          "reasons": [str(error)]}
            test_results.append(result)

    final_fingerprint = source_fingerprint(str(repo_root))
    registry = repo_root / "contracts/catia/v1/registry.json"
    corpus = repo_root / "contracts/catia/v1/corpus-index.json"
    schemas = sorted((repo_root / "contracts/catia/v1/schema").glob("*.json"))
    manifest = {
        "schema_version": "catiaagent-offline-release/1",
        "plan_version": "1.0",
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source_fingerprint": fingerprint,
        "final_fingerprint": final_fingerprint,
        "fingerprint_stable": fingerprint == final_fingerprint,
        "baseline_commit": freeze["baseline_commit"],
        "gates": profile_results,
        "test_runs": test_results,
        "artifacts": {
            "registry_sha256": _sha(registry),
            "corpus_sha256": _sha(corpus),
            "schemas": {path.stem: _sha(path) for path in schemas},
            "python_constraints": _sha(run_root / PYTHON_FREEZE)
            if (run_root / PYTHON_FREEZE).is_file() else None,
        },
        "scope": {
            "implemented": ["T00", "T01"] + TASKS,
            "offline_gates": ["G1", "G2"],
            "not_verified": {
                "G3": "Windows product platform matrix (Python 3.10 + 3.12 packages)",
                "G4": "CAD support geometry environment",
                "G5": "real CATIA execution and acceptance",
                "G6": "frozen release and product certification",
                "remote_ci": "root CI wiring and a real workflow run",
                "model_providers": "real provider requests (no product credentials)",
            },
        },
    }
    all_profiles = all(item["status"] == "PASS" for item in profile_results.values())
    all_tests = bool(test_results) and all(item["status"] == "PASS" for item in test_results)
    manifest["status"] = ("OFFLINE_READY"
                          if all_profiles and all_tests and manifest["fingerprint_stable"]
                          else "NOT_VERIFIED")
    manifest["status_reasons"] = [
        *(f"{name}: {item['reasons']}" for name, item in profile_results.items()
          if item["status"] != "PASS"),
        *(f"{item.get('test_run_id')}: {item.get('reasons')}" for item in test_results
          if item["status"] != "PASS"),
        *( [] if manifest["fingerprint_stable"] else ["source fingerprint changed during the run"]),
    ]
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[2]))
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--tasks-json", required=True)
    parser.add_argument("--out-dir", required=True)
    args = parser.parse_args(argv)
    manifest = build_checkpoint(Path(args.repo_root).resolve(), Path(args.run_root).resolve(),
                                Path(args.tasks_json).resolve(), Path(args.out_dir).resolve())
    print(f"status={manifest['status']} gates={ {k: v['status'] for k, v in manifest['gates'].items()} } "
          f"test_runs={len(manifest['test_runs'])}")
    return 0 if manifest["status"] == "OFFLINE_READY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
