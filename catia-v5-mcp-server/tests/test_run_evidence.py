"""T23 run evidence: atomic sidecar, same-run binding, explicit status."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from catia_mcp.run_evidence import (
    EvidenceError,
    binds_same_run,
    build_manifest,
    classify_status,
    load_sidecar,
    write_build_evidence,
    write_sidecar,
)

HEX = "a" * 64


def manifest(**overrides) -> dict:
    values = dict(
        run_id="run-1", attempt_id="attempt-1", plan_id="sha256:" + "b" * 64,
        source_fingerprint="c" * 64, code_version="b58e40b",
        contract_version="catia-feature-plan/1",
        inputs=[{"name": "plan", "sha256": HEX}],
        artifacts={"report": {"path": "report.json", "sha256": HEX},
                   "delivery": {"path": "part.CATPart", "sha256": HEX}},
        effects={"modified": True}, verification={"verdict": "PASS"}, status="PASS",
        created_at="2026-11-09T07:00:00Z",
    )
    values.update(overrides)
    return build_manifest(**values)


def test_status_classification_is_explicit():
    assert classify_status(verification_verdict="PASS", delivery_sha256=HEX, effects_modified=True) == "PASS"
    assert classify_status(verification_verdict="FAIL", delivery_sha256=HEX, effects_modified=False) == "FAIL"
    assert classify_status(verification_verdict="INCONCLUSIVE", delivery_sha256=HEX, effects_modified=True) == "INCONCLUSIVE"
    assert classify_status(verification_verdict="PASS", delivery_sha256=None, effects_modified=True) == "NOT_VERIFIED"
    assert classify_status(verification_verdict=None, delivery_sha256=HEX, effects_modified=None) == "NOT_VERIFIED"


def test_sidecar_round_trips_and_rejects_tampering(tmp_path):
    path = tmp_path / "evidence.json"
    payload = manifest()
    assert write_sidecar(path, payload) == str(path.resolve())
    assert load_sidecar(path)["manifestSha256"] == payload["manifestSha256"]

    data = json.loads(path.read_text(encoding="utf-8"))
    data["effects"]["modified"] = False
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(EvidenceError, match="digest mismatch"):
        load_sidecar(path)


def test_half_written_sidecar_cannot_project_completed(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text('{"schema": "catia-run-evidence/1", "status": "PA', encoding="utf-8")
    with pytest.raises(EvidenceError):
        load_sidecar(path)


def test_binding_requires_same_run_and_source(tmp_path):
    payload = manifest()
    assert binds_same_run(payload, run_id="run-1", source_fingerprint="c" * 64) is True
    assert binds_same_run(payload, run_id="run-2", source_fingerprint="c" * 64) is False
    assert binds_same_run(payload, run_id="run-1", source_fingerprint="d" * 64) is False
    assert binds_same_run(payload, run_id="run-1", source_fingerprint="c" * 64,
                          report_sha256="d" * 64) is False


def test_digest_is_content_identity_not_a_signature():
    first = manifest()
    second = manifest(effects={"modified": False}, status="FAIL")
    assert first["manifestSha256"] != second["manifestSha256"]
    assert len(first["manifestSha256"]) == 64


def test_build_sidecar_is_written_beside_the_report_without_changing_it(tmp_path, monkeypatch):
    monkeypatch.setenv("PILOTDECK_RUN_ID", "run-42")
    monkeypatch.setenv("PILOTDECK_ATTEMPT_ID", "attempt-9")
    monkeypatch.setenv("PILOTDECK_SOURCE_FINGERPRINT", "e" * 64)
    report = tmp_path / "part-report.json"
    report.write_text('{"completed": true}', encoding="utf-8")
    delivery_file = tmp_path / "part.CATPart"
    delivery_file.write_bytes(b"CATPart")
    from catia_mcp.run_evidence import file_sha256
    before = report.read_text(encoding="utf-8")
    plan = {"planId": "sha256:" + "f" * 64}
    evidence_path = write_build_evidence(
        report, plan=plan,
        delivery={"path": str(delivery_file), "sha256": file_sha256(delivery_file),
                  "stepPath": str(tmp_path / "part.step"), "stepSha256": HEX},
        verification={"verdict": "PASS"},
    )
    assert report.read_text(encoding="utf-8") == before
    sidecar = load_sidecar(evidence_path)
    assert sidecar["status"] == "PASS"
    assert sidecar["runId"] == "run-42"
    assert sidecar["sourceFingerprint"] == "e" * 64
    assert sidecar["planId"] == plan["planId"]
    assert binds_same_run(sidecar, run_id="run-42", source_fingerprint="e" * 64) is True


def test_inconclusive_verification_is_never_promoted(tmp_path, monkeypatch):
    monkeypatch.delenv("PILOTDECK_RUN_ID", raising=False)
    report = tmp_path / "report.json"
    report.write_text("{}", encoding="utf-8")
    delivery_file = tmp_path / "part.CATPart"
    delivery_file.write_bytes(b"x")
    evidence = write_build_evidence(
        report, plan={"planId": "sha256:" + "0" * 64},
        delivery={"path": str(delivery_file), "sha256": "a" * 64},
        verification={"verdict": "INCONCLUSIVE"},
    )
    assert load_sidecar(evidence)["status"] == "INCONCLUSIVE"
