import pytest
from test_feature_model import design
from catia_mcp.drawing.requirements import validate_requirements
from catia_mcp.drawing.plan import compile_build_plan, plan_summary
from catia_mcp.drawing.feature_model import PLAN_SCHEMA
from catia_mcp.drawing.tools import DrawingTools
from catia_mcp.drawing import feature_workflow as workflow


def test_public_routing_preserves_feature_plan(design):
    normalized = validate_requirements(*design)["normalized"]
    plan = compile_build_plan(normalized)
    assert plan["schema"] == PLAN_SCHEMA
    assert "boss (pad)" in plan_summary(plan)
    definitions = DrawingTools(None).get_tool_definitions()
    for tool in definitions:
        if tool["name"] in {"catia_validate_requirements", "catia_compile_build_plan", "catia_build_part"}:
            schemas = tool["inputSchema"]["properties"]["requirements"]["oneOf"]
            assert len(schemas) == 2
            assert schemas[1]["properties"]["schema"]["const"] == design[0]["schema"]


def test_geometry_preflight_failure_never_mutates(design, tmp_path, monkeypatch):
    class Executor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): raise AssertionError("Must not touch CATIA")
    monkeypatch.setattr(workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(workflow, "run_support", lambda *a, **k: {"ok": False, "code": "CAD_SUPPORT_TIMEOUT"})
    plan = compile_build_plan(validate_requirements(*design)["normalized"])
    result = workflow.build_features(None, design[0], plan, tmp_path / "p.CATPart", [], {})
    assert result["ok"] is False
    assert result["data"]["stages"][-1]["stage"] == "geometry_preflight"
    assert not (tmp_path / "p.CATPart").exists()


@pytest.mark.parametrize("failure", [
    "CAD_SUPPORT_TIMEOUT", "CAD_SUPPORT_START_FAILED", "CAD_SUPPORT_PROTOCOL_ERROR",
    "client_value_error",
])
@pytest.mark.parametrize("geometry_rejection_first", [True, False])
def test_edit_variant_retries_geometry_rejection_but_not_support_failure(tmp_path, monkeypatch, failure, geometry_rejection_first):
    req = {"unit": "mm", "parameters": [{"name": "R", "value": 2}]}
    plan = {"planId": "test", "features": [], "parameters": [{"name": "R", "value": 2, "type": "length"}]}
    variants = []
    class Executor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3": 100.}}
        def save(self, path):
            from pathlib import Path
            Path(path).write_text("part")
            return {"path": path}
    def support(operation, spec, **kwargs):
        assert operation == "evaluate"
        value = spec["requirements"]["parameters"][0]["value"]
        if value != 2:
            variants.append(value)
            if geometry_rejection_first and len(variants) == 1:
                return {"ok": False, "operation": "evaluate", "exitCode": 1,
                        "code": "CANDIDATE_REJECTED",
                        "error": {"type": "CandidateRejected", "message": "invalid geometry"}}
            if failure == "client_value_error":
                raise ValueError("interpreter unavailable")
            return {"ok": False, "code": failure}
        return {"ok": True, "data": {"verdict": "PASS", "expected": {"volumeMm3": 100}, "comparison": {"checks": []}}}
    monkeypatch.setattr(workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(workflow, "run_support", support)
    def verifier(*a, **kw):
        from pathlib import Path
        Path(kw["export_step"]).write_text("step")
        return {"verdict": "PASS", "checks": [], "measurements": {"volumeMm3": 100.}}
    monkeypatch.setattr(workflow, "run_independent_verifier", verifier)
    result = workflow.build_features(None, req, plan, tmp_path / "part.CATPart", [], {})
    assert result["ok"] is False
    assert result["code"] == ("CAD_SUPPORT_REQUEST_FAILED" if failure == "client_value_error" else failure)
    assert result["data"]["failure"]["stage"] == "editability"
    if failure == "CAD_SUPPORT_TIMEOUT":
        assert "descendants may still be running" in result["data"]["failure"]["reason"]
        assert "before resuming" in result["data"]["stages"][-1]["detail"]
    assert variants == ([2.05, 1.95] if geometry_rejection_first else [2.05])


def test_failed_independent_reopen_cannot_complete_delivery(design, tmp_path, monkeypatch):
    class Executor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3":1.}}
        def save(self, path): return {"path": path}
    monkeypatch.setattr(workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(workflow, "_evaluate", lambda *a: {"expected": {
        "volumeMm3": 1, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}})
    monkeypatch.setattr(workflow, "run_independent_verifier", lambda *a, **k: {"verdict": "FAIL"})
    plan = compile_build_plan(validate_requirements(*design)["normalized"])
    result = workflow.build_features(None, design[0], plan, tmp_path / "p.CATPart", [], {})
    assert result["ok"] is False
    assert result["data"]["businessState"] == "verification_failed"
    assert result["data"]["stages"][-1]["stage"] == "verification"


def test_step_geometry_mismatch_cannot_pass_com_reopen(design, tmp_path, monkeypatch):
    class Executor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3":1.}}
        def save(self, path): return {"path": path}
    def evaluate(requirements, actual=None, **kwargs):
        if actual is not None:
            raise ValueError("Exact STEP bbox/material mismatch")
        return {"expected": {"volumeMm3": 1, "bbox": {"min": [0, 0, 0], "max": [1, 1, 1]}}}
    monkeypatch.setattr(workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(workflow, "_evaluate", evaluate)
    monkeypatch.setattr(workflow, "run_independent_verifier", lambda *a, **k: {"verdict": "PASS", "checks": []})
    plan = compile_build_plan(validate_requirements(*design)["normalized"])
    result = workflow.build_features(None, design[0], plan, tmp_path / "p.CATPart", [], {})
    assert result["ok"] is False
    assert result["data"]["businessState"] == "verification_failed"
    assert "STEP bbox/material mismatch" in result["message"]


@pytest.mark.parametrize("measurable", [True, False])
def test_editability_retries_unmeasurable_edits_against_delivered_step(tmp_path, monkeypatch, measurable):
    req = {"unit": "mm", "parameters": [{"name": "RP", "value": 1.8622}]}
    plan = {"planId": "test", "features": [], "parameters": [{"name": "RP", "value": 1.8622, "type": "length"}]}
    attempts = []
    class Executor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3":100.}, "trace": "x" * 250_000}
        def save(self, path):
            from pathlib import Path
            Path(path).write_text("part")
            return {"path": path}
    def evaluate(requirements, actual=None, **kwargs):
        if actual is not None and '.edit-evidence' in str(actual):
            assert kwargs['volume_tolerance_mm3'] == .01
        return {"expected": {"volumeMm3": 100}, "comparison": {"checks": []}}
    def verify(path, expectation, export_step, parameter_changes=None, collect_cylindrical_faces=True, edit_copy_of=None, **kwargs):
        from pathlib import Path
        Path(export_step).write_text("step")
        if parameter_changes:
            assert collect_cylindrical_faces is False
            assert edit_copy_of == str(tmp_path / 'part.CATPart')
            assert 'volumeMm3' not in expectation
            attempts.append(parameter_changes["RP"]["value"])
        else:
            assert collect_cylindrical_faces is True
        return {"verdict": "PASS", "checks": [], "measurements":{"volumeMm3":100.},
                "editPersistence": {"savedAndReopened": True}}
    def compare(operation, spec, **kwargs):
        assert operation == "compare"
        assert spec["reference"] == str(tmp_path / "part.step")
        changed = measurable and abs(attempts[-1] - 1.8622) > .075
        checks = [{"name": name, "passed": not (changed and name == "missing_material")}
                  for name in ("bbox", "missing_material", "excess_material")]
        checks[0].update(actualMm=0,toleranceMm=.01)
        for check in checks[1:]:
            check.update(actualMm3=.02 if changed else 0,minimumMm3=.02 if changed else 0,toleranceMm3=.01)
        return {"ok": not changed, "exitCode": int(changed),
                "data": {"verdict": "FAIL" if changed else "PASS", "checks": checks}}
    monkeypatch.setattr(workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(workflow, "_evaluate", evaluate)
    monkeypatch.setattr(workflow, "run_independent_verifier", verify)
    monkeypatch.setattr(workflow, "run_support", compare)
    monkeypatch.setattr(workflow, "create_part_preview", lambda *a, **kw: {})
    result = workflow.build_features(None, req, plan, tmp_path / "part.CATPart", [], {})
    if measurable:
        assert result["ok"] is True
        assert attempts == [1.8622 + .05, 1.8622 - .05, 1.8622 + .1]
        import json
        report_path = tmp_path / "part.build.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report_path.stat().st_size > 200_000
        assert len(json.dumps(result)) < 200_000
        edit = report["editability"]["parameters"][0]
        assert edit["after"] == 1.8622 + .1
        assert any(c["passed"] is False for c in edit["geometryChangeChecks"])
        evidence = edit["evidence"]
        folder = tmp_path / "part.edit-evidence" / evidence["directory"]
        import hashlib
        assert result["data"]["delivery"]["reportJsonSha256"] == hashlib.sha256(report_path.read_bytes()).hexdigest()
        for key, filename in (("copySha256", "part.CATPart"), ("stepSha256", "edited.step"),
                              ("reopenCheckSha256", "reopen-check.json"),
                              ("effectComparisonSha256", "effect-comparison.json")):
            assert evidence[key] == hashlib.sha256((folder / filename).read_bytes()).hexdigest()
    else:
        assert result["ok"] is False
        assert len(attempts) == 6
        assert "did not produce a measurable geometry change" in result["message"]


def test_editability_comparison_error_never_passes(tmp_path, monkeypatch):
    req = {"unit": "mm", "parameters": [{"name": "R", "value": 2}]}
    plan = {"planId": "test", "features": [], "parameters": [{"name": "R", "value": 2, "type": "length"}]}
    class Executor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): return {"steps": [], "final": {"volumeMm3":100.}}
        def save(self, path):
            from pathlib import Path
            Path(path).write_text("part")
            return {"path": path}
    def verify(path, expectation, export_step, **kwargs):
        from pathlib import Path
        Path(export_step).write_text("step")
        return {"verdict": "PASS", "checks": [], "measurements":{"volumeMm3":100.},
                "editPersistence": {"savedAndReopened": True}}
    monkeypatch.setattr(workflow, "FeatureExecutor", Executor)
    monkeypatch.setattr(workflow, "_evaluate", lambda *a, **kw: {"expected": {"volumeMm3": 100}, "comparison": {"checks": []}})
    monkeypatch.setattr(workflow, "run_independent_verifier", verify)
    monkeypatch.setattr(workflow, "run_support", lambda *a, **kw: {"ok": False, "exitCode": 1, "data": {"verdict": "FAIL", "code": "INVALID_GEOMETRY"}})
    result = workflow.build_features(None, req, plan, tmp_path / "part.CATPart", [], {})
    assert result["ok"] is False
    assert "geometry-change comparison failed" in result["message"]
    root = tmp_path / "part.edit-evidence"
    evidence = [path for path in root.iterdir() if path.is_dir()]
    assert len(evidence)==1
    assert (root / "attempt-000001.intent.json").is_file()
    assert not (root / "attempt-000001.outcome.json").exists()
    assert (evidence[0]/"edited.step").read_text()=="step"
    assert (evidence[0]/"reopen-check.json").is_file()
    assert (evidence[0]/"effect-comparison.json").is_file()


def test_comparison_uncertainty_cannot_prove_a_parameter_edit():
    checks=[{'name':'bbox','passed':True,'actualMm':0,'toleranceMm':.01},
        {'name':'missing_material','passed':False,'actualMm3':.0105,'minimumMm3':.009,'toleranceMm3':.01},
        {'name':'excess_material','passed':True,'actualMm3':.0015,'minimumMm3':0,'toleranceMm3':.01}]
    assert workflow._measurable_difference(checks) is False
    checks[1]['minimumMm3']=.011
    assert workflow._measurable_difference(checks) is True


def test_another_passing_check_cannot_supply_the_failed_check_lower_bound():
    checks = [{'name': 'missing_material', 'passed': False, 'minimumMm3': .009, 'toleranceMm3': .01},
              {'name': 'excess_material', 'passed': True, 'minimumMm3': .02, 'toleranceMm3': .01}]
    assert workflow._measurable_difference(checks) is False


def test_an_unreported_material_lower_bound_is_not_edit_evidence():
    with pytest.raises(ValueError,match='lower-bound'):
        workflow._measurable_difference([{'name':'missing_material','actualMm3':5,'toleranceMm3':.01}])


def test_native_nominal_reopen_keeps_native_volume_basis():
    plan={'features':[],'parameters':[]};baseline={'expected':{'volumeMm3':100.}}
    assert workflow._expectation(plan,baseline)['volumeMm3']==100.
    assert workflow._expectation(plan,baseline,native_volume=100.045)['volumeMm3']==100.045


@pytest.mark.parametrize('value',[True,0,-1,float('nan'),float('inf')])
def test_invalid_native_baselines_fail_closed(value):
    with pytest.raises(ValueError,match='native baseline'):
        workflow._expectation({'features':[],'parameters':[]},{'expected':{'volumeMm3':100.}},native_volume=value)


@pytest.mark.parametrize('value',[True,0,-1,float('nan'),float('inf')])
def test_native_measurement_cannot_override_an_invalid_independent_nominal(value):
    with pytest.raises(ValueError,match='independent nominal'):
        workflow._expectation({'features':[],'parameters':[]},{'expected':{'volumeMm3':value}},native_volume=100.)
