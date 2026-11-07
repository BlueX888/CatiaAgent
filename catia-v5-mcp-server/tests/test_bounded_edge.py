"""Finite-segment selection must disambiguate collinear edges without guessing."""
import copy
from types import SimpleNamespace
import pytest
from catia_mcp.drawing.builder import BuildError
from catia_mcp.drawing.feature_model import compile_features,FeatureError
from catia_mcp.drawing.feature_executor import FeatureExecutor,validate_compiled_plan


def bounded_notch():
    return {'schema':'catia-feature-requirements/1','partName':'BoundedEdgeProbe','unit':'mm','unitEvidence':[],
        'parameters':[{'name':k,'value':v,'evidence':[]} for k,v in [('L',40),('W',30),('T',8),('N',5),('FR',1)]],
        'features':[
            {'id':'plate','operation':'pad','length':'T','sketch':{'plane':'xy','profiles':[
                {'type':'rectangle','x':'-L/2','y':'-W/2','width':'L','height':'W'}]}},
            {'id':'notch','operation':'pocket','length':'T','direction':'reverse','dependsOn':['plate'],
             'sketch':{'plane':'xy','offset':'T','profiles':[
                 {'type':'rectangle','x':'-N','y':'-W/2','width':'N*2','height':'N'}]}},
            {'id':'round_left','operation':'fillet','radius':'FR','dependsOn':['notch'],
             'edges':{'type':'linear','direction':[1,0,0],'through':['-L*0.375','-W/2','T'],'withinSegment':True}}]}


def test_flag_is_typed_consumed_and_preserved_by_plan_validation():
    req=bounded_notch();plan=compile_features(req,None,require_evidence=False)
    assert validate_compiled_plan(plan)['features'][-1]['edges']['withinSegment'] is True
    req['features'][-1]['edges']['withinSegment']='true'
    with pytest.raises(FeatureError,match='must be boolean'):compile_features(req,None,require_evidence=False)


def _executor():
    executor=object.__new__(FeatureExecutor)
    executor._edge_candidates=lambda:[{'index':i,'kind':'linear','length':10.,'midpoint':[x,0,0],
        'direction':[1,0,0]} for i,x in [(1,0),(2,10)]]
    return executor


def test_native_selector_can_bound_the_point_to_one_segment():
    selector={'type':'linear','direction':[1,0,0],'through':[{'value':v} for v in (0,0,0)]}
    executor=_executor()
    with pytest.raises(BuildError,match='matched 2 edges'):executor._resolve_edge(selector,'round')
    assert executor._resolve_edge({**selector,'withinSegment':True},'round')[0]['index']==1
    selector['through'][0]['value']=5
    with pytest.raises(BuildError,match='matched 2 edges'):
        executor._resolve_edge({**selector,'withinSegment':True},'round')


def test_occt_selector_also_bounds_the_point():
    pytest.importorskip('build123d');pytest.importorskip('cadgen')
    from catia_mcp.feature_geometry import evaluate_features
    req=bounded_notch()
    assert evaluate_features({'requirements':req})['verdict']=='PASS'
    del req['features'][-1]['edges']['withinSegment']
    with pytest.raises(ValueError,match='matched 2 edges'):evaluate_features({'requirements':req})
