"""Candidate protocol tests use the isolated geometry helper, never CATIA."""
import json
import sys

import pytest

from catia_mcp.cad_support_client import run_support
from catia_mcp.drawing import feature_workflow
from test_feature_model import design


def test_compile_rejection_crosses_real_helper_protocol(design):
    pytest.importorskip("build123d")
    req, _ = design
    next(p for p in req["parameters"] if p["name"] == "R")["value"] = -1
    result = run_support("evaluate", {"requirements": req}, python=sys.executable)
    assert result["ok"] is False
    assert result["exitCode"] == 1
    assert result["code"] == "CANDIDATE_REJECTED"
    assert result["error"]["type"] == "CandidateRejected"


def test_known_feature_rejection_crosses_real_helper_protocol(design):
    pytest.importorskip("build123d")
    req, _ = design
    req["features"][1]["sketch"]["offset"] = "T * 2"
    result = run_support("evaluate", {"requirements": req}, python=sys.executable)
    assert result["ok"] is False
    assert result["code"] == "CANDIDATE_REJECTED"
    assert "boss" in result["error"]["message"]
    assert "SINGLE_SOLID_REQUIRED" in result["error"]["message"]


def test_unknown_feature_exception_is_not_a_candidate_rejection(design, tmp_path, monkeypatch, capsys):
    pytest.importorskip("build123d")
    from catia_mcp import cad_support

    req, _ = design
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"requirements": req}), encoding="utf-8")
    def broken_inspect(_shape):
        raise RuntimeError("simulated kernel failure")
    monkeypatch.setattr(cad_support, "inspect_shape", broken_inspect)
    assert cad_support.main(["evaluate", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert result.get("code") != "CANDIDATE_REJECTED"
    assert "simulated kernel failure" in result["error"]["message"]


def test_nonfinite_measurement_is_not_a_candidate_rejection(design, tmp_path, monkeypatch, capsys):
    pytest.importorskip("build123d")
    from catia_mcp import cad_support

    req, _ = design
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"requirements": req}), encoding="utf-8")
    monkeypatch.setattr(cad_support, "inspect_shape", lambda _shape: {
        "valid": False, "solidCount": 1, "volumeMm3": float("nan"),
        "solidVolumesMm3": [float("nan")],
    })
    assert cad_support.main(["evaluate", str(path)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result.get("code") != "CANDIDATE_REJECTED"
    assert "Nonfinite geometry volume" in result["error"]["message"]


@pytest.mark.parametrize("reply", [
    {"ok": False, "error": {"type": "ValueError", "message": "old helper"}},
    {"ok": False, "operation": "evaluate", "exitCode": 1, "code": "CANDIDATE_REJECTED"},
    {"ok": False, "operation": "inspect", "exitCode": 1, "code": "CANDIDATE_REJECTED",
     "error": {"type": "CandidateRejected", "message": "bad"}},
    {"ok": False, "operation": "evaluate", "exitCode": 2, "code": "CANDIDATE_REJECTED",
     "error": {"type": "CandidateRejected", "message": "bad"}},
    {"ok": False, "operation": "evaluate", "exitCode": 1, "code": "CANDIDATE_REJECTED",
     "error": {"type": "ValueError", "message": "bad"}},
])
def test_missing_or_inconsistent_rejection_is_not_retried(monkeypatch, reply):
    monkeypatch.setattr(feature_workflow, "run_support", lambda *a, **k: reply)
    with pytest.raises(feature_workflow.CadSupportError, match="CAD_SUPPORT_EVALUATION_FAILED"):
        feature_workflow._evaluate({})


def test_rejection_during_actual_comparison_is_not_a_candidate(monkeypatch):
    reply = {"ok": False, "operation": "evaluate", "exitCode": 1, "code": "CANDIDATE_REJECTED",
             "error": {"type": "CandidateRejected", "message": "invalid"}}
    monkeypatch.setattr(feature_workflow, "run_support", lambda *a, **k: reply)
    with pytest.raises(feature_workflow.CadSupportError, match="CAD_SUPPORT_EVALUATION_FAILED"):
        feature_workflow._evaluate({}, "existing.step")
