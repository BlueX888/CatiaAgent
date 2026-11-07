import asyncio
from jsonschema import Draft202012Validator, ValidationError,validate
from catia_mcp.drawing.feature_schema import FEATURE_REQUIREMENTS_JSON
from catia_mcp.server import CATIAMCPServer,argument_error

def invalid_requirement():
    return {'schema':'catia-feature-requirements/1','unit':'mm','unitEvidence':[],
            'parameters':[{'name':'D','value':5,'evidence':[]}],
            'features':[{'id':'marks','operation':'pocket','length':'D','sketch':{
                'plane':'xy','profiles':[{'type':'wire','start':{'x':0,'y':0},'segments':[
                    {'type':'arc','end':{'x':1,'y':1},'radius':2},
                    *[{'type':'line','end':{'x':i,'y':i}} for i in range(150)]]}]}}]}

def test_union_error_identifies_literal_radius_without_dumping_wire():
    try:validate(invalid_requirement(),FEATURE_REQUIREMENTS_JSON)
    except ValidationError as error:message=argument_error(error)
    else:raise AssertionError('Invalid literal radius was accepted')
    assert '$.features[0].sketch.profiles[0].segments[0].radius' in message
    assert 'string' in message and len(message)<1000
    assert 'marks' not in message and "'start'" not in message
    assert "'line' was expected" not in message

def test_large_rejected_container_is_bounded():
    try:validate(['privatevalue']*10000,{'type':'string'})
    except ValidationError as error:message=argument_error(error)
    assert len(message)<500 and 'privatevalue' not in message

def test_required_field_retains_its_name():
    try:validate({}, {'type':'object','required':['path']})
    except ValidationError as error:message=argument_error(error)
    assert 'path' in message and '$' in message


def test_shaft_error_uses_shaft_branch_not_unrelated_chamfer_angle():
    req = {'schema':'catia-feature-requirements/1','unit':'mm','unitEvidence':[],
           'parameters':[{'name':'D','value':5,'evidence':[]}],
           'features':[{'id':'outer','operation':'shaft','angle':360,'sketch':{
               'plane':'yz','axisX':0,'profiles':[{'type':'circle','radius':'D'}]}}]}
    error = next(Draft202012Validator(FEATURE_REQUIREMENTS_JSON).iter_errors(req), None)
    assert error is not None
    message = argument_error(error)
    assert '$.features[0].sketch' in message and 'axisX' in message
    assert '45' not in message


def test_shaft_branch_error_survives_server_validation(monkeypatch):
    req = {'schema':'catia-feature-requirements/1','unit':'mm','unitEvidence':[],
           'parameters':[{'name':'D','value':5,'evidence':[]}],
           'features':[{'id':'outer','operation':'shaft','angle':360,'sketch':{
               'plane':'yz','axisX':0,'profiles':[{'type':'circle','radius':'D'}]}}]}
    async def run():
        server=CATIAMCPServer()
        monkeypatch.setattr(server.connection,'ensure_connected',lambda:pytest.fail('COM accessed'))
        try:
            result=await server.call_tool('catia_validate_requirements',{'requirements':req})
            assert result.isError and result.structuredContent['code']=='INVALID_ARGUMENT'
            assert 'axisX' in result.structuredContent['message']
            assert '45' not in result.structuredContent['message']
        finally:await server.close()
    import pytest
    asyncio.run(run())

def test_invalid_feature_call_still_stops_before_com(monkeypatch):
    async def run():
        server=CATIAMCPServer()
        monkeypatch.setattr(server.connection,'ensure_connected',lambda:(_ for _ in ()).throw(AssertionError('COM accessed')))
        try:
            result=await server.call_tool('catia_validate_requirements',{'requirements':invalid_requirement()})
            assert result.isError and result.structuredContent['code']=='INVALID_ARGUMENT'
            assert result.structuredContent['effects']['modified'] is False
            assert '.radius' in result.structuredContent['message']
        finally:await server.close()
    asyncio.run(run())
