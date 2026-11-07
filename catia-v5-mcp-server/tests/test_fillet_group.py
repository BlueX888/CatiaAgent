"""Intersecting edges must share one finishing operation and one radius."""
import copy
import pytest
from catia_mcp.drawing.feature_model import compile_features,FeatureError
from catia_mcp.drawing.feature_executor import validate_compiled_plan
from catia_mcp.drawing.feature_executor import _reference_choices

def grouped_corner():
    return {'schema':'catia-feature-requirements/1','partName':'GroupedCorner','unit':'mm','unitEvidence':[],
        'parameters':[{'name':k,'value':v,'evidence':[]} for k,v in [('L',40),('W',30),('T',20),('R',2.5)]],
        'features':[
            {'id':'block','operation':'pad','length':'T','sketch':{'plane':'xy','profiles':[
                {'type':'rectangle','x':0,'y':0,'width':'L','height':'W'}]}},
            {'id':'corner','operation':'fillet','radius':'R','dependsOn':['block'],'edges':[
                {'type':'linear','direction':[1,0,0],'through':['L/2',0,0],'withinSegment':True},
                {'type':'linear','direction':[0,1,0],'through':[0,'W/2',0],'withinSegment':True},
                {'type':'linear','direction':[0,0,1],'through':[0,0,'T/2'],'withinSegment':True}]}]}

def test_group_survives_compilation_and_untrusted_plan_validation():
    plan=compile_features(grouped_corner(),None,require_evidence=False)
    assert len(validate_compiled_plan(plan)['features'][-1]['edges'])==3
    corrupted=copy.deepcopy(plan);corrupted['features'][-1]['edges'][0]['through'][0]['value']+=1
    with pytest.raises(Exception,match='do not match'):validate_compiled_plan(corrupted)

def test_alias_recovery_is_bounded_and_tries_the_other_topology_cell_early():
    groups=[['retired','current'] for _ in range(8)]
    choices=list(_reference_choices(groups))
    assert choices[:2]==[(0,)*8,(1,)*8]
    assert len(choices)==len(set(choices))==16
    assert all(len(choice)==8 and set(choice)<={0,1} for choice in choices)

@pytest.mark.parametrize('size',[0,1,65])
def test_group_has_bounded_cardinality(size):
    req=grouped_corner();req['features'][-1]['edges']=(req['features'][-1]['edges']*22)[:size]
    with pytest.raises(FeatureError,match='2.*64'):compile_features(req,None,require_evidence=False)

def test_duplicate_selectors_and_chamfer_groups_are_rejected():
    req=grouped_corner();req['features'][-1]['edges'][1]=copy.deepcopy(req['features'][-1]['edges'][0])
    with pytest.raises(FeatureError,match='duplicate'):compile_features(req,None,require_evidence=False)
    req=grouped_corner();finish=req['features'][-1];finish.update(operation='chamfer',length='R');finish.pop('radius')
    with pytest.raises(FeatureError,match='only.*fillet'):compile_features(req,None,require_evidence=False)

def test_public_schema_exposes_only_fillet_groups():
    jsonschema=pytest.importorskip('jsonschema')
    from catia_mcp.drawing.feature_schema import FEATURE_REQUIREMENTS_JSON
    req=grouped_corner();jsonschema.Draft202012Validator(FEATURE_REQUIREMENTS_JSON).validate(req)
    finish=req['features'][-1];finish.update(operation='chamfer',length='R');finish.pop('radius')
    with pytest.raises(jsonschema.ValidationError):jsonschema.Draft202012Validator(FEATURE_REQUIREMENTS_JSON).validate(req)

def test_missing_one_rounded_edge_fails_global_material_check(tmp_path):
    bd=pytest.importorskip('build123d');pytest.importorskip('cadgen')
    from catia_mcp.feature_geometry import evaluate_features
    req=grouped_corner();result=evaluate_features({'requirements':req})
    assert result['verdict']=='PASS' and result['expected']['solidCount']==1
    block=bd.Solid.make_box(40,30,20)
    two_edges=[e for e in block.edges() if e.geom_type==bd.GeomType.LINE and
               ((abs(e.center().Y)<1e-7 and abs(e.center().Z)<1e-7) or
                (abs(e.center().X)<1e-7 and abs(e.center().Z)<1e-7))]
    assert len(two_edges)==2
    incomplete=block.fillet(2.5,two_edges);path=tmp_path/'omitted-third-edge.step';bd.export_step(incomplete,path)
    failed=evaluate_features({'requirements':req,'actual':str(path),'volumeToleranceMm3':.01})
    assert failed['verdict']=='FAIL'

def test_different_points_on_one_edge_do_not_make_a_group():
    pytest.importorskip('build123d');pytest.importorskip('cadgen')
    from catia_mcp.feature_geometry import evaluate_features
    req=grouped_corner();req['features'][-1]['edges'][1]=dict(req['features'][-1]['edges'][0],through=['L/3',0,0])
    with pytest.raises(ValueError,match='same edge'):evaluate_features({'requirements':req})
