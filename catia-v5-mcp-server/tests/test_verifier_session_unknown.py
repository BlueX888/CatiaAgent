"""Unknown verifier completion must stop subsequent COM work."""
import json
import subprocess

import pytest

from catia_mcp.drawing import feature_workflow, tools, verify


@pytest.mark.parametrize("kind", ["timeout", "invalid", "exit_mismatch", "reported_unknown", "reported_blocked", "contradictory_fail", "start", "communication"])
def test_verifier_marks_ambiguous_completion_unknown(tmp_path, monkeypatch, kind):
    part = tmp_path / "part.CATPart"
    part.write_bytes(b"part")
    monkeypatch.setenv("CATIA_MCP_WORK_DIR", str(tmp_path))

    class Child:
        pid = 123456
        returncode = 1 if kind in {"exit_mismatch", "contradictory_fail"} else 0
        def communicate(self, **kwargs):
            if kind == "timeout":
                raise subprocess.TimeoutExpired("synthetic", 1)
            if kind == "communication":
                raise OSError("synthetic pipe error")
            if kind == "invalid":
                return "not json", ""
            report = {"verdict": "PASS", "checks": [{"passed": True}]}
            if kind == "reported_unknown":
                report["sessionHealth"] = "unknown"
            elif kind == "reported_blocked":
                report["sessionHealth"] = "blocked"
            elif kind == "contradictory_fail":
                report["verdict"] = "FAIL"
            return json.dumps(report), ""
        def kill(self):
            raise OSError("synthetic termination failure")

    def popen(*args, **kwargs):
        if kind == "start":
            raise OSError("synthetic spawn error")
        return Child()
    monkeypatch.setattr(subprocess, "Popen", popen)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("synthetic")))
    report = verify.run_independent_verifier(str(part), {}, timeout=1)
    assert report["verdict"] == "FAIL"
    assert report["sessionHealth"] == "unknown"


@pytest.mark.parametrize("report,expect_edit", [
    ({"verdict": "FAIL", "sessionHealth": "unknown", "error": "verifier_timeout", "checks": []}, False),
    ({"verdict": "PASS", "sessionHealth": "unknown", "checks": []}, False),
    ({"verdict": "FAIL", "checks": [{"passed": False}]}, False),
    ({"verdict": "PASS", "checks": [{"passed": True}]}, True),
])
def test_legacy_builder_stops_after_failed_or_unknown_verification(tmp_path, monkeypatch, report, expect_edit):
    edits = []
    previews = []
    class Executor:
        def __init__(self, connection): pass
        def execute(self, plan): return {"steps": [], "bindings": {}, "final": {}}
        def save(self, path): return {"path": path, "sha256": "x", "sizeBytes": 1}
        def editability_check(self, path):
            edits.append(path)
            return {"passed": True}
    monkeypatch.setattr(tools, "BuildExecutor", Executor)
    monkeypatch.setattr(tools, "run_independent_verifier", lambda *a, **k: report)
    monkeypatch.setattr(tools, "_write_reports", lambda **k: {})
    monkeypatch.setattr(tools, "plan_summary", lambda plan: {})
    monkeypatch.setattr("catia_mcp.drawing.preview.create_part_preview", lambda *a, **k: previews.append(k) or {})
    plan = {"planId": "test", "expected": {"kind": "test", "volumeMm3": 1,
            "bbox": {}, "holes": [], "featureCount": 1, "parameters": {}}}
    result = tools.DrawingTools(None)._execute_build(plan, tmp_path / "p.CATPart", {}, [], "catia_build_part")
    assert bool(edits) is expect_edit
    assert bool(previews) is expect_edit
    if expect_edit:
        assert previews[0]["step_path"].suffix == ".step"
    assert result["ok"] is expect_edit
    if report.get("sessionHealth") == "unknown":
        assert result["code"] == "VERIFIER_SESSION_UNKNOWN"
        assert result["data"]["sessionHealth"] == "unknown"
        assert not result["recovery"]["automatic_retry_allowed"]


def test_feature_builder_does_not_close_after_verifier_unknown(tmp_path, monkeypatch):
    closed = []
    class Executor:
        def __init__(self, connection): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3": 1}}
        def save(self, path): return {"path": path}
        def close(self): closed.append(True)
    monkeypatch.setattr(feature_workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(feature_workflow, "_evaluate", lambda *a, **k: {"expected": {"volumeMm3": 1}})
    monkeypatch.setattr(feature_workflow, "run_independent_verifier", lambda *a, **k: {
        "verdict": "FAIL", "sessionHealth": "unknown", "error": "verifier_timeout"})
    plan = {"planId": "test", "features": [], "parameters": []}
    result = feature_workflow.build_features(None, {}, plan, tmp_path / "p.CATPart", [], {})
    assert result["code"] == "VERIFIER_SESSION_UNKNOWN"
    assert result["data"]["sessionHealth"] == "unknown"
    assert not result["recovery"]["automatic_retry_allowed"]
    assert not closed


def test_standalone_verifier_exposes_unknown_session(monkeypatch):
    monkeypatch.setattr(tools, "run_independent_verifier", lambda *a, **k: {
        "verdict": "FAIL", "sessionHealth": "unknown", "error": "verifier_timeout"})
    result = tools.DrawingTools(None)._verify({"partPath": "irrelevant", "expected": {}})
    assert result["code"] == "VERIFIER_SESSION_UNKNOWN"
    assert result["data"]["sessionHealth"] == "unknown"
    assert result["effects"]["modified"] is None
    assert not result["recovery"]["automatic_retry_allowed"]


def test_feature_edit_verifier_unknown_stops_remaining_work(tmp_path, monkeypatch):
    closed = []
    verifies = []
    class Executor:
        def __init__(self, connection): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3": 1}}
        def save(self, path):
            from pathlib import Path
            Path(path).write_bytes(b"part")
            return {"path": path}
        def close(self): closed.append(True)
    def evaluator(requirements, actual=None, **kwargs):
        return {"expected": {"volumeMm3": 1}, "comparison": {"checks": []}}
    def verifier(*args, **kwargs):
        verifies.append(kwargs)
        if len(verifies) == 1:
            from pathlib import Path
            Path(kwargs["export_step"]).write_bytes(b"step")
            return {"verdict": "PASS", "checks": [], "measurements": {"volumeMm3": 1}}
        return {"verdict": "FAIL", "sessionHealth": "unknown", "error": "verifier_timeout"}
    monkeypatch.setattr(feature_workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(feature_workflow, "_evaluate", evaluator)
    monkeypatch.setattr(feature_workflow, "run_independent_verifier", verifier)
    monkeypatch.setattr(feature_workflow, "create_part_preview", lambda *a, **k: pytest.fail("preview ran"))
    monkeypatch.setattr(feature_workflow, "run_support", lambda *a, **k: pytest.fail("comparison ran"))
    req = {"unit": "mm", "parameters": [{"name": "R", "value": 2}]}
    plan = {"planId": "test", "features": [], "parameters": [{"name": "R", "value": 2, "type": "length"}]}
    result = feature_workflow.build_features(None, req, plan, tmp_path / "p.CATPart", [], {})
    assert len(verifies) == 2
    assert result["code"] == "VERIFIER_SESSION_UNKNOWN"
    assert result["data"]["sessionHealth"] == "unknown"
    root = tmp_path / "p.edit-evidence"
    assert (root / "attempt-000001.intent.json").is_file()
    assert not (root / "attempt-000001.outcome.json").exists()
    assert result["data"]["editProgress"]["journalHeadSha256"] is None
    assert not closed
