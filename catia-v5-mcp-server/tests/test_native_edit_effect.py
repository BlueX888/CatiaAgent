"""Saved independent native measurements can prove an actual material edit."""
import json
import pytest
from catia_mcp.drawing import feature_workflow as workflow


def test_native_change_deducts_both_measurement_allowances():
    check=workflow._native_volume_change(331938.336123,331779.623367)
    assert check['name']=='missing_material' and check['passed'] is False
    assert check['minimumMm3']==pytest.approx(158.712756-(331938.336123+331779.623367)*2e-6)
    assert workflow._measurable_difference([check])


@pytest.mark.parametrize('a,b',[(100.,100.),(1e6,1e6+.1),(100.,100.01)])
def test_equal_or_uncertain_native_changes_need_the_existing_step_proof(a,b):
    assert workflow._native_volume_change(a,b) is None


@pytest.mark.parametrize('bad',[True,None,0,-1,float('nan'),float('inf')])
def test_invalid_native_measurements_cannot_prove_an_edit(bad):
    with pytest.raises(ValueError,match='finite positive'):
        workflow._native_volume_change(100.,bad)


@pytest.mark.parametrize('bad_variant',[False,True])
def test_native_effect_requires_saved_reopen_and_full_variant_geometry(tmp_path,monkeypatch,bad_variant):
    req={'unit':'mm','parameters':[{'name':'R','value':2.}]}
    plan={'planId':'test','features':[],'parameters':[{'name':'R','value':2.,'type':'length'}]}
    class Executor:
        def __init__(self,connection):pass
        def close(self):pass
        def execute(self,plan):return {'steps':[],'final':{'volumeMm3':100.}}
        def save(self,path):
            from pathlib import Path
            Path(path).write_text('nominal')
            return {'path':path}
    def evaluate(requirements,actual=None,**kwargs):
        if actual is not None and '.edit-evidence' in str(actual):
            assert kwargs['volume_tolerance_mm3']==.01
            if bad_variant:raise ValueError('Expected variant geometry mismatch')
        return {'expected':{'volumeMm3':100.},'comparison':{'checks':[]}}
    def verify(path,expectation,export_step,parameter_changes=None,**kwargs):
        from pathlib import Path
        Path(export_step).write_text('step')
        return {'verdict':'PASS','checks':[],'measurements':{'volumeMm3':110. if parameter_changes else 100.},
            'editPersistence':{'savedAndReopened':True}}
    monkeypatch.setattr(workflow,'FeatureExecutor',Executor)
    monkeypatch.setattr(workflow,'_evaluate',evaluate)
    monkeypatch.setattr(workflow,'run_independent_verifier',verify)
    monkeypatch.setattr(workflow,'create_part_preview',lambda *args,**kwargs:{})
    def unnecessary_boolean(*args):raise AssertionError('A second boolean is unnecessary once the net native change is conclusive')
    monkeypatch.setattr(workflow,'run_support',unnecessary_boolean)
    result=workflow.build_features(None,req,plan,tmp_path/'part.CATPart',[],{})
    assert result['ok'] is not bad_variant
    evidence=list((tmp_path/'part.edit-evidence').glob('*/effect-comparison.json'))
    if bad_variant:
        assert not evidence and 'Expected variant geometry mismatch' in result['message']
    else:
        assert len(evidence)==1
        proof=json.loads(evidence[0].read_text())
        assert proof['operation']=='compare_native_saved_volumes'
        assert proof['data']['checks'][0]['minimumMm3']>9.99
        report = json.loads((tmp_path / 'part.build.json').read_text(encoding='utf-8'))
        assert report['editability']['parameters'][0]['passed']
