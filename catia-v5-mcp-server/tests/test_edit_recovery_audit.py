import json

import pytest

from catia_mcp.drawing import feature_workflow as workflow


def journal(tmp_path):
    part = tmp_path / "part.CATPart"
    step = tmp_path / "part.step"
    part.write_bytes(b"part")
    step.write_bytes(b"step")
    root = tmp_path / "part.edit-evidence"
    root.mkdir()
    manifest = tmp_path / "part.edit-progress.json"
    manifest_hash = workflow._write_receipt(manifest, {
        "schema": "catia-edit-progress/1", "plan": {"parameters": [{"name": "L", "value": 10.0}]},
        "partPath": str(part), "partSha256": workflow._file_sha256(part),
        "stepPath": str(step), "stepSha256": workflow._file_sha256(step), "maxAttempts": 256,
    })
    return root, manifest, manifest_hash


def rejected(root, manifest_hash, sequence, previous, *, name="L", after=10.2):
    intent = root / f"attempt-{sequence:06d}.intent.json"
    outcome = root / f"attempt-{sequence:06d}.outcome.json"
    digest = workflow._write_receipt(intent, {
        "schema": "catia-edit-attempt/1", "manifestSha256": manifest_hash,
        "previousOutcomeSha256": previous, "sequence": sequence,
        "parameter": name, "before": 10.0, "after": after,
    })
    return workflow._write_receipt(outcome, {
        "schema": "catia-edit-outcome/1", "intentSha256": digest, "status": "candidate_rejected",
    })


def test_partial_journal_reports_only_the_anchored_boundary(tmp_path):
    root, manifest, manifest_hash = journal(tmp_path)
    first = rejected(root, manifest_hash, 1, None)
    head = rejected(root, manifest_hash, 2, first, after=9.8)
    result = workflow._audit_edit_receipts(manifest, manifest_hash, head, 2, require_complete=False)
    assert result == {"attempts": 2, "verifiedParameterCount": 0, "complete": False,
                      "nextParameter": "L", "nextCandidateIndex": 2}
    with pytest.raises(ValueError, match="coverage is incomplete"):
        workflow._audit_edit_receipts(manifest, manifest_hash, head, 2)


@pytest.mark.parametrize("damage", ["unpaired", "rollback", "head", "candidate", "parameter", "baseline"])
def test_partial_journal_rejects_unanchored_or_unexpected_work(tmp_path, damage):
    root, manifest, manifest_hash = journal(tmp_path)
    head = rejected(root, manifest_hash, 1, None, name="X" if damage == "parameter" else "L",
                    after=10.4 if damage == "candidate" else 10.2)
    attempts = 1
    if damage == "unpaired":
        (root / "attempt-000002.intent.json").write_text("{}", encoding="utf-8")
    elif damage == "rollback":
        head, attempts = None, 0
    elif damage == "head":
        head = "0" * 64
    elif damage == "baseline":
        (tmp_path / "part.CATPart").write_bytes(b"changed")
    with pytest.raises(ValueError):
        workflow._audit_edit_receipts(manifest, manifest_hash, head, attempts, require_complete=False)


def test_unpaired_intent_cannot_be_hidden_as_a_recorded_attempt(tmp_path):
    root, manifest, manifest_hash = journal(tmp_path)
    (root / "attempt-000001.intent.json").write_text(json.dumps({}), encoding="utf-8")
    with pytest.raises(ValueError, match="incomplete attempt"):
        workflow._audit_edit_receipts(manifest, manifest_hash, None, 1, require_complete=False)
