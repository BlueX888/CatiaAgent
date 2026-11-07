import copy
import math
import pytest

from catia_mcp.drawing.feature_model import FeatureError, compile_features
from catia_mcp.drawing.feature_executor import validate_compiled_plan
from catia_mcp.drawing.feature_schema import FEATURE_REQUIREMENTS_JSON


def oblique_design():
    q=math.sqrt(.5)
    return {'schema':'catia-feature-requirements/1','unit':'mm','unitEvidence':[],
        'parameters':[{'name':n,'value':v,'evidence':[]} for n,v in [('L',10),('W',6),('T',4),('D',3)]],
        'features':[{'id':'base','operation':'pad','length':'T','sketch':{
            'plane':{'normal':[q,0,-q],'xDirection':[0,1,0]},'offset':'D',
            'profiles':[['rectangle',0,0,'L','W']]}}]}


def test_custom_frame_roundtrip_and_schema():
    req=oblique_design();plan=compile_features(req,None,require_evidence=False)
    assert validate_compiled_plan(plan)['planId']==plan['planId']
    jsonschema=pytest.importorskip('jsonschema')
    jsonschema.validate(req,FEATURE_REQUIREMENTS_JSON)


@pytest.mark.parametrize('frame',[
    {'normal':[0,0,0],'xDirection':[1,0,0]},
    {'normal':[0,0,2],'xDirection':[1,0,0]},
    {'normal':[0,0,1],'xDirection':[0,0,1]},
    {'normal':[0,0,1],'xDirection':[True,0,0]},
    {'normal':[0,0,float('nan')],'xDirection':[1,0,0]},
    {'normal':[0,0,10**500],'xDirection':[1,0,0]},
    {'normal':[0,0,1],'xDirection':[1,0]},
    {'normal':[0,0,1],'xDirection':[1,0,0],'origin':[0,0,0]},
])
def test_bad_frames_rejected(frame):
    req=oblique_design();req['features'][0]['sketch']['plane']=frame
    with pytest.raises(FeatureError):compile_features(req,None,require_evidence=False)


def test_oblique_geometry_and_signed_offset():
    bd=pytest.importorskip('build123d')
    from catia_mcp.feature_geometry import evaluate_features
    req=oblique_design();q=math.sqrt(.5)
    for d in (3.,-3.):
        req['parameters'][-1]['value']=d
        result=evaluate_features({'requirements':req})
        assert result['verdict']=='PASS'
        actual=result['expected']
        assert actual['volumeMm3']==pytest.approx(240.)
        assert actual['bbox']['min']==pytest.approx([q*d,0,-q*(d+4)])
        assert actual['bbox']['max']==pytest.approx([q*(d+10),10,q*(6-d)])
