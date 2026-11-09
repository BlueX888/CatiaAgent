"""T17 offline checkpoint: frozen identity, complete reruns, explicit scope."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import offline_evidence  # noqa: E402


def run_root(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    (root / "constraints").mkdir(parents=True)
    (root / "constraints" / "python-offline-freeze.txt").write_text("pydantic==2.14.1\n", encoding="utf-8")
    (root / "repository-map.json").write_text(json.dumps({
        "paths": {"REPO_ROOT": str(REPO_ROOT), "RUN_ROOT": str(root)},
        "executables": {"NODE": str(REPO_ROOT / "node.exe"),
                        "PNPM": str(REPO_ROOT / "pnpm.cjs")},
    }), encoding="utf-8")
    return root


def tasks_json() -> Path:
    return REPO_ROOT / "docs/CatiaAgent_Unattended_Development_Plan/catiaagent_unattended_plan/tasks.json"


def test_registered_reruns_cover_every_core_contract_task():
    planned = offline_evidence.planned_test_runs(tasks_json())
    tasks = {item["task_id"] for item in planned}
    assert {f"T{index:02d}" for index in range(2, 17)} <= tasks
    assert any(item["test_run_id"] == "T13-wheel-check" for item in planned)


def test_manifest_is_offline_ready_only_when_all_evidence_passes(tmp_path, monkeypatch):
    root = run_root(tmp_path)
    monkeypatch.setattr(offline_evidence, "_run_profile",
                        lambda *args, **kwargs: {"status": "PASS", "reasons": []})
    monkeypatch.setattr(offline_evidence.gate, "run_test_run",
                        lambda spec, *args, **kwargs: {"test_run_id": spec["id"],
                                                       "status": "PASS", "reasons": []})
    manifest = offline_evidence.build_checkpoint(REPO_ROOT, root, tasks_json(), tmp_path / "release")
    assert manifest["status"] == "OFFLINE_READY"
    assert manifest["fingerprint_stable"] is True
    assert manifest["artifacts"]["registry_sha256"]
    assert manifest["artifacts"]["corpus_sha256"]
    unverified = manifest["scope"]["not_verified"]
    assert {"G3", "G4", "G5", "G6", "remote_ci", "model_providers"} <= set(unverified)
    assert "T16" in manifest["scope"]["implemented"]


def test_a_failure_blocks_offline_ready(tmp_path, monkeypatch):
    root = run_root(tmp_path)
    monkeypatch.setattr(offline_evidence, "_run_profile",
                        lambda *args, **kwargs: {"status": "FAIL", "reasons": ["x"]})
    monkeypatch.setattr(offline_evidence.gate, "run_test_run",
                        lambda spec, *args, **kwargs: {"test_run_id": spec["id"],
                                                       "status": "PASS", "reasons": []})
    manifest = offline_evidence.build_checkpoint(REPO_ROOT, root, tasks_json(), tmp_path / "release")
    assert manifest["status"] == "NOT_VERIFIED"
    assert manifest["status_reasons"]


def test_changed_fingerprint_invalidates_the_checkpoint(tmp_path, monkeypatch):
    root = run_root(tmp_path)
    calls = iter(["a" * 64, "b" * 64])
    monkeypatch.setattr(offline_evidence, "source_fingerprint", lambda _: next(calls))
    monkeypatch.setattr(offline_evidence, "snapshot_files", lambda _: [])
    monkeypatch.setattr(offline_evidence, "_run_profile",
                        lambda *args, **kwargs: {"status": "PASS", "reasons": []})
    monkeypatch.setattr(offline_evidence.gate, "run_test_run",
                        lambda spec, *args, **kwargs: {"test_run_id": spec["id"],
                                                       "status": "PASS", "reasons": []})
    manifest = offline_evidence.build_checkpoint(REPO_ROOT, root, tasks_json(), tmp_path / "release")
    assert manifest["fingerprint_stable"] is False
    assert manifest["status"] == "NOT_VERIFIED"


def test_freeze_lists_changed_files_with_content_hashes(tmp_path, monkeypatch):
    root = run_root(tmp_path)
    monkeypatch.setattr(offline_evidence, "_run_profile",
                        lambda *args, **kwargs: {"status": "PASS", "reasons": []})
    monkeypatch.setattr(offline_evidence.gate, "run_test_run",
                        lambda spec, *args, **kwargs: {"status": "PASS", "test_run_id": spec["id"],
                                                       "reasons": []})
    offline_evidence.build_checkpoint(REPO_ROOT, root, tasks_json(), tmp_path / "release")
    freeze = json.loads((tmp_path / "release/freeze.json").read_text(encoding="utf-8"))
    assert freeze["schema_version"] == "catiaagent-offline-freeze/1"
    assert freeze["source_fingerprint"] and freeze["baseline_commit"]
    for item in freeze["changed_files"]:
        assert "path" in item and "status" in item
