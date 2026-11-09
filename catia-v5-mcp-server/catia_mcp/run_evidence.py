"""Versioned run-evidence sidecar.

The sidecar is written next to an existing report and never replaces it or its
plan id.  It binds one run/attempt to the source fingerprint, input digests,
delivery/report digests, effects and verification status.  The digest is a
content identifier, not an authenticity signature or a permission token.

A half-written file or a digest mismatch cannot project ``completed``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EVIDENCE_SCHEMA = "catia-run-evidence/1"
STATUSES = {"PASS", "FAIL", "INCONCLUSIVE", "NOT_VERIFIED"}


class EvidenceError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def classify_status(*, verification_verdict: str | None, delivery_sha256: str | None,
                    effects_modified: bool | None) -> str:
    verdict = str(verification_verdict or "").upper()
    if verdict == "INCONCLUSIVE":
        return "INCONCLUSIVE"
    if verdict == "PASS" and effects_modified is True and isinstance(delivery_sha256, str) and len(delivery_sha256) == 64:
        return "PASS"
    if verdict == "FAIL" and effects_modified is False:
        return "FAIL"
    return "NOT_VERIFIED"


def build_manifest(
    *,
    run_id: str,
    attempt_id: str,
    plan_id: str | None,
    source_fingerprint: str | None,
    code_version: str | None,
    contract_version: str | None,
    inputs: list[dict[str, Any]],
    artifacts: dict[str, Any],
    effects: dict[str, Any],
    verification: dict[str, Any],
    status: str,
    created_at: str | None = None,
) -> dict[str, Any]:
    if status not in STATUSES:
        raise EvidenceError(f"unsupported evidence status {status!r}")
    payload = {
        "schema": EVIDENCE_SCHEMA,
        "runId": run_id,
        "attemptId": attempt_id,
        "planId": plan_id,
        "sourceFingerprint": source_fingerprint,
        "codeVersion": code_version,
        "contractVersion": contract_version,
        "inputs": inputs,
        "artifacts": artifacts,
        "effects": effects,
        "verification": verification,
        "status": status,
        "createdAt": created_at or _now(),
    }
    payload["manifestSha256"] = hashlib.sha256(_canonical(payload)).hexdigest()
    return payload


def write_sidecar(path: str | Path, manifest: dict[str, Any]) -> str:
    """Atomically write and read back the sidecar; return its absolute path."""
    target = Path(path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = _canonical(manifest) + b"\n"
    fd, temporary = tempfile.mkstemp(prefix=".evidence-", suffix=".json", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    loaded = load_sidecar(target)
    if loaded["manifestSha256"] != manifest["manifestSha256"]:
        raise EvidenceError("sidecar digest changed during read-back")
    return str(target)


def load_sidecar(path: str | Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EvidenceError(f"sidecar unreadable: {error}") from error
    if not isinstance(payload, dict) or payload.get("schema") != EVIDENCE_SCHEMA:
        raise EvidenceError("sidecar schema mismatch")
    digest = payload.pop("manifestSha256", None)
    if not isinstance(digest, str) or hashlib.sha256(_canonical(payload)).hexdigest() != digest:
        raise EvidenceError("sidecar digest mismatch or half-written file")
    payload["manifestSha256"] = digest
    if payload.get("status") not in STATUSES:
        raise EvidenceError("sidecar status is not declared")
    return payload


def binds_same_run(manifest: dict[str, Any], *, run_id: str, source_fingerprint: str | None,
                   report_sha256: str | None = None) -> bool:
    """Whether the evidence belongs to this run and source snapshot."""
    if manifest.get("runId") != run_id:
        return False
    if source_fingerprint is not None and manifest.get("sourceFingerprint") != source_fingerprint:
        return False
    report = (manifest.get("artifacts") or {}).get("report") or {}
    if report_sha256 is not None and report.get("sha256") != report_sha256:
        return False
    return True


def write_build_evidence(report_path: str | Path, *, plan: dict[str, Any], delivery: dict[str, Any],
                         verification: dict[str, Any], status: str | None = None) -> str:
    """Write the sidecar for a feature build next to its JSON report."""
    report = Path(report_path)
    effects_modified = True if delivery.get("path") else None
    evidence_status = status or classify_status(
        verification_verdict=verification.get("verdict"),
        delivery_sha256=delivery.get("sha256"),
        effects_modified=effects_modified,
    )
    manifest = build_manifest(
        run_id=os.environ.get("PILOTDECK_RUN_ID") or str(plan.get("planId") or "unbound"),
        attempt_id=os.environ.get("PILOTDECK_ATTEMPT_ID") or str(plan.get("planId") or "unbound"),
        plan_id=plan.get("planId"),
        source_fingerprint=os.environ.get("PILOTDECK_SOURCE_FINGERPRINT"),
        code_version=os.environ.get("PILOTDECK_VERSION"),
        contract_version="catia-feature-plan/1",
        inputs=[{"name": "plan", "sha256": hashlib.sha256(_canonical(plan)).hexdigest()}],
        artifacts={
            "report": {"path": str(report), "sha256": file_sha256(report) if report.is_file() else None},
            "delivery": {"path": delivery.get("path"), "sha256": delivery.get("sha256")},
            "step": {"path": delivery.get("stepPath"), "sha256": delivery.get("stepSha256")},
        },
        effects={"modified": effects_modified},
        verification={"verdict": verification.get("verdict"), "sessionHealth": verification.get("sessionHealth")},
        status=evidence_status,
    )
    return write_sidecar(report.with_name(report.stem + ".evidence.json"), manifest)


def write_edit_evidence(report_path: str | Path, *, session: dict[str, Any], delivery: dict[str, Any],
                        verification: dict[str, Any], operation_id: str | None) -> str:
    report = Path(report_path)
    status = classify_status(
        verification_verdict=verification.get("verdict"),
        delivery_sha256=delivery.get("sha256"),
        effects_modified=True if delivery.get("path") else None,
    )
    session_id = str(session.get("editSessionId") or "unbound")
    manifest = build_manifest(
        run_id=os.environ.get("PILOTDECK_RUN_ID") or session_id,
        attempt_id=os.environ.get("PILOTDECK_ATTEMPT_ID") or session_id,
        plan_id=operation_id,
        source_fingerprint=os.environ.get("PILOTDECK_SOURCE_FINGERPRINT"),
        code_version=os.environ.get("PILOTDECK_VERSION"),
        contract_version="catia-edit-plan/1",
        inputs=[{"name": "delivery", "sha256": delivery.get("sha256")}],
        artifacts={
            "report": {"path": str(report), "sha256": file_sha256(report) if report.is_file() else None},
            "delivery": {"path": delivery.get("path"), "sha256": delivery.get("sha256")},
        },
        effects={"modified": True if delivery.get("path") else None},
        verification={"verdict": verification.get("verdict")},
        status=status,
    )
    return write_sidecar(report.with_name(report.stem + ".evidence.json"), manifest)
