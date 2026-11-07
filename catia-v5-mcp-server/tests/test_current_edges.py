"""CATIA history cells must not be used as current finishing targets."""
import copy
import math
from types import SimpleNamespace
import pytest
from catia_mcp.drawing.builder import BuildError
from catia_mcp.drawing.feature_executor import FeatureExecutor
from catia_mcp.drawing import feature_executor as native

def line(length,midpoint,direction=(0,1,0)):
    return {'kind':'linear','length':length,'midpoint':list(midpoint),'direction':list(direction)}

def test_retired_full_edge_is_rejected_but_current_shortened_edge_is_kept():
    current=line(62.865,(-12.7,66.3575,-31.75))
    retired=line(66.04,(-12.7,64.77,-31.75))
    assert not FeatureExecutor._matches_current_edge(retired,current)
    assert FeatureExecutor._matches_current_edge(current,current)

@pytest.mark.parametrize('direction',[(0,0,0),(0,math.nan,0),(0,math.inf,0)])
def test_invalid_direction_cannot_authorize_an_edge(direction):
    current=line(10,(0,5,0))
    assert not FeatureExecutor._matches_current_edge(line(10,(0,5,0),direction),current)

def test_linear_grouping_keeps_crossing_or_different_length_edges_separate():
    a=dict(line(10,(0,0,0),(1,0,0)),index=1)
    b=dict(line(10,(0,0,0),(0,1,0)),index=2)
    c=dict(line(8,(0,0,0),(1,0,0)),index=3)
    reverse=dict(line(10,(0,0,0),(-1,0,0)),index=4)
    assert sorted(map(len,FeatureExecutor._edge_groups([a,b,c,reverse])))==[1,1,2]

def test_opposite_semicircle_is_not_a_current_edge_match():
    a={'kind':'circular','length':math.pi*5,'radius':5,'centre':[0,0,0],'axis':[0,0,1],
       'start':[-5,0,0],'end':[5,0,0],'midpoint':[0,5,0]}
    b=copy.deepcopy(a);b['midpoint']=[0,-5,0]
    assert not FeatureExecutor._matches_current_edge(a,b)
    assert FeatureExecutor._matches_current_edge(a,a)

def test_inventory_failure_retains_evidence_and_refuses_selection(tmp_path,monkeypatch):
    executor=object.__new__(FeatureExecutor);executor.diagnostic_dir=tmp_path
    executor.doc=SimpleNamespace(ExportData=lambda path,kind:__import__('pathlib').Path(path).write_text('STEP'))
    monkeypatch.setattr(native,'run_support',lambda *a:{'ok':False,'code':'CAD_SUPPORT_TIMEOUT'})
    with pytest.raises(BuildError,match='CURRENT_EDGE_INVENTORY_FAILED'):executor._current_edges()
    assert len(list(tmp_path.glob('*.step')))==len(list(tmp_path.glob('*.json')))==1

def test_step_inventory_contains_only_current_solid_edges(tmp_path):
    bd=pytest.importorskip('build123d');pytest.importorskip('cadgen')
    from catia_mcp.cad_support import inspect_edges
    solid=bd.Solid.make_box(10,20,30);path=tmp_path/'box.step';bd.export_step(solid,path)
    result=inspect_edges({'sourceStep':str(path)})
    assert result['valid'] and len(result['edges'])==12
    assert sorted(e['length'] for e in result['edges'])==pytest.approx([10]*4+[20]*4+[30]*4)
    assert all(e['kind']=='linear' for e in result['edges'])

def test_two_solid_inventory_cannot_be_a_finishing_authority(tmp_path):
    bd=pytest.importorskip('build123d');pytest.importorskip('cadgen')
    from catia_mcp.cad_support import inspect_edges
    shape=bd.Compound(children=[bd.Solid.make_box(10,10,10),bd.Solid.make_box(10,10,10,plane=bd.Plane(origin=(30,0,0)))])
    path=tmp_path/'two.step';bd.export_step(shape,path)
    with pytest.raises(ValueError,match='one sound solid'):inspect_edges({'sourceStep':str(path)})

def arc(start,sweep,radius=3):
    point=lambda a:[radius*math.cos(a),radius*math.sin(a),0]
    return {'kind':'circular','length':radius*abs(sweep),'radius':radius,'centre':[0,0,0],
            'axis':[0,0,1],'start':point(start),'midpoint':point(start+sweep/2),'end':point(start+sweep)}

def test_step_seam_halves_certify_a_whole_circle():
    from catia_mcp.cad_support import _complete_circles
    result=_complete_circles([arc(0,math.pi),arc(0,-math.pi)])
    assert len(result)==1 and result[0]['length']==pytest.approx(6*math.pi)
    native=dict(arc(0,2*math.pi))
    assert FeatureExecutor._matches_current_edge(native,result[0])

@pytest.mark.parametrize('arcs',[
    [arc(0,math.pi),arc(0,math.pi)],
    [arc(0,math.pi-.005),arc(math.pi,-math.pi+.005)],
    [arc(0,math.pi-.005),arc(math.pi,math.pi-.005)],
])
def test_duplicate_or_gapped_arcs_cannot_certify_a_whole_circle(arcs):
    from catia_mcp.cad_support import _complete_circles
    assert not _complete_circles(arcs)
