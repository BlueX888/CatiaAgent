"""Offset axes must survive schema, compilation, evaluation and plan checks."""
import copy
import math
import pytest
from catia_mcp.drawing.feature_model import SCHEMA,compile_features,FeatureError
from catia_mcp.drawing.feature_schema import FEATURE_REQUIREMENTS_JSON
from catia_mcp.drawing.feature_executor import validate_compiled_plan
from catia_mcp.drawing.builder import BuildError


def offset_holes():
    req={'schema':SCHEMA,'partName':'OffsetDrillProbe','unit':'mm','unitEvidence':[],
        'parameters':[{'name':k,'value':v,'evidence':[],'type':'angle' if k=='A' else 'length'}
            for k,v in [('L',80),('W',60),('T',30),('R',4),('CX',15),('CZ',10),('DEP',20),('A',360)]],
        'features':[{'id':'plate','operation':'pad','length':'T','sketch':{'plane':'xy','profiles':[
            {'type':'rectangle','x':'-L/2','y':'-W/2','width':'L','height':'W'}]}}]}
    previous='plate'
    for label,axis in [('positive','CX'),('negative','-CX')]:
        pocket='bore_'+label;tip='tip_'+label
        req['features'].append({'id':pocket,'operation':'pocket','dependsOn':[previous],'length':'DEP',
            'sketch':{'plane':'zx','offset':'-W/2','profiles':[{'type':'circle','x':'CZ','y':axis,'radius':'R'}]}})
        req['features'].append({'id':tip,'operation':'groove','dependsOn':[pocket],'angle':'A','axisX':axis,
            'sketch':{'plane':'xy','offset':'CZ','profiles':[{'type':'polygon','points':[
                {'x':axis,'y':'-W/2+DEP'}, {'x':f'({axis})+R','y':'-W/2+DEP'},
                {'x':axis,'y':'-W/2+DEP+R'}]}]}})
        previous=tip
    return req


def offset_shaft(sign=1):
    axis='CX' if sign>0 else '-CX'
    return {'schema':SCHEMA,'partName':'OffsetShaftProbe','unit':'mm','unitEvidence':[],
        'parameters':[{'name':k,'value':v,'evidence':[],'type':'angle' if k=='A' else 'length'}
            for k,v in [('R',4),('CX',15),('CZ',10),('DEP',20),('A',360)]],
        'features':[{'id':'shaft','operation':'shaft','angle':'A','axisX':axis,
            'sketch':{'plane':'xy','offset':'CZ','profiles':[
                {'type':'rectangle','x':axis,'width':'R','height':'DEP'}]}}]}


def test_offset_axes_compile_with_evidence_dependencies_and_round_trip():
    req=offset_holes()
    plan=compile_features(req,None,require_evidence=False)
    assert [f['axisX']['value'] for f in plan['features'] if 'axisX' in f]==[15,-15]
    assert all(f['axisX']['parameters']==['CX'] for f in plan['features'] if 'axisX' in f)
    assert validate_compiled_plan(plan)['planId']==plan['planId']
    plan['features'][2]['axisX']['value']+=1
    with pytest.raises(BuildError,match='do not match'):validate_compiled_plan(plan)


def test_nonrevolution_cannot_silently_ignore_axis():
    req=offset_holes();req['features'][0]['axisX']='CX'
    with pytest.raises(FeatureError,match='does not consume axisX'):compile_features(req,None,require_evidence=False)


def test_public_schema_accepts_offset_revolutions_and_rejects_axis_on_pad():
    schema=pytest.importorskip('jsonschema')
    req=offset_holes();schema.validate(req,FEATURE_REQUIREMENTS_JSON)
    req['features'][0]['axisX']='CX'
    with pytest.raises(schema.ValidationError):schema.validate(req,FEATURE_REQUIREMENTS_JSON)


def test_offset_blind_holes_have_cylindrical_and_conical_material():
    pytest.importorskip('build123d');pytest.importorskip('cadgen')
    from catia_mcp.feature_geometry import evaluate_features
    req=offset_holes();result=evaluate_features({'requirements':req})
    expected=80*60*30-2*(math.pi*4**2*20+math.pi*4**2*4/3)
    assert result['expected']['volumeMm3']==pytest.approx(expected,abs=1e-5)
    assert result['expected']['bbox']['min']==pytest.approx([-40,-30,0])
    assert result['expected']['bbox']['max']==pytest.approx([40,30,30])
    for axis in ('CX','-CX'):
        bad=copy.deepcopy(req)
        bad['features'][2]['axisX']=f'({axis})+L'
        with pytest.raises(ValueError,match='REVOLUTION_AXIS_SIDE'):evaluate_features({'requirements':bad})


@pytest.mark.parametrize('sign',[1,-1])
def test_shaft_axis_can_be_offset_to_either_side(sign):
    pytest.importorskip('build123d');pytest.importorskip('cadgen')
    from catia_mcp.feature_geometry import evaluate_features
    result=evaluate_features({'requirements':offset_shaft(sign)})
    assert result['expected']['volumeMm3']==pytest.approx(math.pi*4**2*20,abs=1e-5)
    assert result['expected']['bbox']['min']==pytest.approx([sign*15-4,0,6])
    assert result['expected']['bbox']['max']==pytest.approx([sign*15+4,20,14])
