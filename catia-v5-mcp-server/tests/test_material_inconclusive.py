import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from catia_mcp import cad_support, feature_geometry
from catia_mcp.drawing import feature_workflow as workflow


def test_unreliable_booleans_preserve_typed_budget_and_never_pass(monkeypatch):
    shape = SimpleNamespace(volume=500000.0)
    shape.wrapped = shape
    monkeypatch.setattr(cad_support, "_volume", lambda value: value.volume)
    monkeypatch.setattr(cad_support, "_difference_witnesses", lambda *args: [None, None])
    monkeypatch.setattr(cad_support, "_boolean_volume", lambda operation, *args: 500000.5 if operation == "common" else 0.0)
    with pytest.raises(cad_support.MaterialComparisonInconclusive) as caught:
        cad_support._overlap_volume(shape, shape, .01)
    assert caught.value.summary == {"reason": "INSUFFICIENT_CONSISTENT_BOOLEANS",
        "consistencyBudgetMm3": .01, "attemptCount": 10, "candidateCount": 0,
        "volumeDiagnostics": {"actualMm3": 500000., "referenceMm3": 500000.,
                              "netDifferenceMm3": 0., "maxIntersectionExcessMm3": .5}}


def test_helper_emits_bounded_inconclusive_protocol(tmp_path, monkeypatch, capsys):
    spec = tmp_path / "spec.json"
    spec.write_text(json.dumps({"requirements": {}}), encoding="utf8")
    def fail(_spec):
        raise cad_support.MaterialComparisonInconclusive("x" * 20000, "FUZZY_LEVELS_DISAGREE", .01, [{}, {}], [{}])
    monkeypatch.setattr(feature_geometry, "evaluate_features", fail)
    assert cad_support.main(["evaluate", str(spec)]) == 1
    text = capsys.readouterr().out
    response = json.loads(text)
    assert len(text) < 1000
    assert response["ok"] is False
    assert response["code"] == "MATERIAL_COMPARISON_INCONCLUSIVE"
    assert response["error"]["summary"] == {"reason": "FUZZY_LEVELS_DISAGREE", "consistencyBudgetMm3": .01,
                                             "attemptCount": 2, "candidateCount": 1}


def test_module_entrypoint_preserves_inconclusive_error_identity(tmp_path):
    """The helper runs as __main__ while feature_geometry imports it by name."""
    spec = tmp_path / "spec.json"
    spec.write_text('{}', encoding="utf-8")
    script = '''
import runpy, sys
from catia_mcp import feature_geometry
from catia_mcp.cad_support import MaterialComparisonInconclusive
def fail(_):
    raise MaterialComparisonInconclusive("private diagnostic", "FUZZY_LEVELS_DISAGREE", .01, [{}, {}], [{}])
feature_geometry.evaluate_features = fail
sys.argv = ["catia_mcp.cad_support", "evaluate", sys.argv[1]]
runpy.run_module("catia_mcp.cad_support", run_name="__main__")
'''
    result = subprocess.run([sys.executable, "-B", "-c", script, str(spec)],
                            cwd=Path(__file__).resolve().parents[1], capture_output=True,
                            text=True, encoding="utf-8", timeout=15, check=False)
    assert result.returncode == 1
    response = json.loads(result.stdout)
    assert response["code"] == "MATERIAL_COMPARISON_INCONCLUSIVE"
    assert response["error"] == {"type": "MaterialComparisonInconclusive",
        "message": "Material comparison could not establish a reliable bound.",
        "summary": {"reason": "FUZZY_LEVELS_DISAGREE", "consistencyBudgetMm3": .01,
                    "attemptCount": 2, "candidateCount": 1}}


@pytest.mark.parametrize("diagnostic", [None, {},
    {"actualMm3": 100., "referenceMm3": 99.9, "netDifferenceMm3": 0., "maxIntersectionExcessMm3": .1},
    {"actualMm3": 100., "referenceMm3": 99.9, "netDifferenceMm3": .1, "maxIntersectionExcessMm3": float("nan")}])
def test_malformed_volume_diagnostics_are_not_trusted(diagnostic):
    assert not workflow._valid_volume_diagnostics(diagnostic)


def test_build_reports_unverified_comparison_without_retry(tmp_path, monkeypatch):
    req = {"unit": "mm", "parameters": []}
    plan = {"planId": "test", "features": [], "parameters": []}
    class Executor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3": 100.}}
        def save(self, path): return {"path": path}
    calls = []
    def support(operation, spec):
        calls.append(spec)
        if "actual" not in spec:
            return {"ok": True, "data": {"verdict": "PASS", "expected": {"volumeMm3": 100}}}
        return {"ok": False, "operation": "evaluate", "exitCode": 1, "code": "MATERIAL_COMPARISON_INCONCLUSIVE",
                "error": {"type": "MaterialComparisonInconclusive", "summary": {
                    "reason": "INSUFFICIENT_CONSISTENT_BOOLEANS", "consistencyBudgetMm3": .01,
                    "attemptCount": 10, "candidateCount": 0,
                    "volumeDiagnostics": {"actualMm3": 100., "referenceMm3": 99.9,
                                          "netDifferenceMm3": .1, "maxIntersectionExcessMm3": .1}}}}
    monkeypatch.setattr(workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(workflow, "run_support", support)
    monkeypatch.setattr(workflow, "run_independent_verifier", lambda *a, **k: {"verdict": "PASS", "checks": []})
    result = workflow.build_features(None, req, plan, tmp_path / "part.CATPart", [], {})
    assert len(calls) == 2
    assert result["ok"] is False and result["code"] == "MATERIAL_COMPARISON_INCONCLUSIVE"
    assert result["data"]["failure"]["stage"] == "verification"
    assert result["data"]["failure"]["summary"]["consistencyBudgetMm3"] == .01
    assert result["data"]["failure"]["summary"]["volumeDiagnostics"]["maxIntersectionExcessMm3"] == .1
    assert result["effects"]["modified"] is True
    assert result["recovery"]["automatic_retry_allowed"] is False
    assert len(json.dumps(result)) < 5000
