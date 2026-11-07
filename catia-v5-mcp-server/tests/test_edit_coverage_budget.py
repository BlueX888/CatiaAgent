import json
from pathlib import Path

import pytest

from catia_mcp.drawing import feature_workflow as workflow


@pytest.mark.parametrize("attempt_limit, expected_count, completed", [(20, 13, True), (3, 3, False)])
def test_large_plan_requires_every_edit_or_reports_incomplete(tmp_path, monkeypatch, attempt_limit, expected_count, completed):
    parameters = [{"name": f"P{i}", "value": 10.0, "type": "length"} for i in range(13)]
    requirements = {"unit": "mm", "parameters": [dict(p) for p in parameters]}
    plan = {"planId": "large", "features": [], "parameters": parameters}
    seen = []

    class Executor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3": 100.0}}
        def save(self, path):
            Path(path).write_text("part")
            return {"path": path}

    def verify(path, expectation, export_step, parameter_changes=None, **kwargs):
        Path(export_step).write_text("step")
        if parameter_changes:
            seen.extend(parameter_changes)
            assert 0 < kwargs["timeout"] <= 120
        return {"verdict": "PASS", "checks": [],
                "measurements": {"volumeMm3": 101.0 if parameter_changes else 100.0},
                "editPersistence": {"savedAndReopened": True}}

    monkeypatch.setattr(workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(workflow.EditBudget, "max_attempts", attempt_limit)
    monkeypatch.setattr(workflow, "_evaluate", lambda *a, **kw: {
        "expected": {"volumeMm3": 100}, "comparison": {"checks": []}})
    monkeypatch.setattr(workflow, "run_independent_verifier", verify)
    monkeypatch.setattr(workflow, "create_part_preview", lambda *a, **kw: {})
    result = workflow.build_features(None, requirements, plan, tmp_path / "part.CATPart", [], {})

    assert result["ok"] is completed
    assert seen == [f"P{i}" for i in range(expected_count)]
    assert len(list((tmp_path / "part.edit-evidence").glob("*/edit-result.json"))) == expected_count
    manifest = json.loads((tmp_path / "part.edit-progress.json").read_text(encoding="utf-8"))
    assert len(manifest["plan"]["parameters"]) == 13
    manifest_hash = workflow._file_sha256(tmp_path / "part.edit-progress.json")
    progress = result["data"]["editability"] if completed else result["data"]["editProgress"]
    assert progress["manifestSha256"] == manifest_hash
    previous = None
    for sequence in range(1, expected_count + 1):
        root = tmp_path / "part.edit-evidence"
        intent_path = root / f"attempt-{sequence:06d}.intent.json"
        outcome_path = root / f"attempt-{sequence:06d}.outcome.json"
        intent = json.loads(intent_path.read_text(encoding="utf-8"))
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        assert intent["manifestSha256"] == manifest_hash
        assert intent["previousOutcomeSha256"] == previous
        assert intent["sequence"] == sequence
        assert outcome["intentSha256"] == workflow._file_sha256(intent_path)
        receipt = root / outcome["directory"] / "edit-result.json"
        assert outcome["editResultSha256"] == workflow._file_sha256(receipt)
        details = json.loads(receipt.read_text(encoding="utf-8"))["evidence"]
        assert details["variantComparisonSha256"] == workflow._file_sha256(receipt.parent / "variant-comparison.json")
        previous = workflow._file_sha256(outcome_path)
    assert progress["journalHeadSha256"] == previous
    if completed:
        assert result["data"]["editability"]["verifiedParameterCount"] == 13
        assert result["data"]["editability"]["sampled"] is False
        workflow._audit_edit_receipts(tmp_path / "part.edit-progress.json", manifest_hash, previous, expected_count)
        receipt.write_text("tampered", encoding="utf-8")
        with pytest.raises(ValueError, match="digest changed"):
            workflow._audit_edit_receipts(tmp_path / "part.edit-progress.json", manifest_hash, previous, expected_count)
    else:
        assert result["code"] == "EDIT_COVERAGE_INCOMPLETE"
        assert result["data"]["businessState"] == "verification_incomplete"
        assert result["data"]["editProgress"]["verifiedParameterCount"] == 3
        assert result["data"]["editProgress"]["journalInspection"] == {
            "status": "verified_recorded_boundary", "attempts": 3, "verifiedParameterCount": 3,
            "complete": False, "nextParameter": "P3", "nextCandidateIndex": 0,
        }
        assert not (tmp_path / "part.build.json").exists()


def test_deadline_reserves_cleanup_and_never_launches_another_attempt(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(workflow.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(workflow.EditBudget, "seconds", 50)
    budget = workflow.EditBudget()
    assert budget.timeout() == 40
    budget.begin_attempt()
    now[0] = 40
    with pytest.raises(workflow.EditBudgetExpired):
        budget.begin_attempt()
    assert budget.attempts == 1
