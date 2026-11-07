import hashlib
import importlib.util
import json
from pathlib import Path
import pytest

spec=importlib.util.spec_from_file_location('material_chain',Path(__file__).parents[1]/'tools/compose_material_evidence.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)


@pytest.fixture
def edges(tmp_path):
    sources=[]
    for n in ('A','B','C'):
        p=tmp_path/f'{n}.step';p.write_text(n)
        sources.append({'path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    result=[]
    for i in range(2):
        data={'verdict':'PASS','sources':{'actual':sources[i],'reference':sources[i+1]},'checks':[
            {'name':'bbox','passed':True,'actualMm':.0001,'toleranceMm':.005},
            {'name':'missing_material','passed':True,'actualMm3':.2,'toleranceMm3':1.},
            {'name':'excess_material','passed':True,'actualMm3':.3,'toleranceMm3':1.}]}
        p=tmp_path/f'edge{i}.json';p.write_text(json.dumps(data));result.append(p)
    return result


def test_directional_upper_bounds_and_source_binding(edges):
    r=module.compose_edges(edges,.005,1.)
    assert r['ok'] and r['data']['checks'][1]['actualMm3']==pytest.approx(.4)
    assert r['data']['checks'][2]['actualMm3']==pytest.approx(.6)
    assert len(r['data']['proofEdges'])==2
    assert r['data']['sources']['actual']['sha256']!=r['data']['sources']['reference']['sha256']


def test_source_edit_rejected(edges):
    data=json.loads(edges[0].read_text());Path(data['sources']['actual']['path']).write_text('changed')
    with pytest.raises(ValueError,match='hash mismatch'):module.compose_edges(edges,.005,1.)


def test_reordered_or_disconnected_edges_rejected(edges):
    with pytest.raises(ValueError,match='directed chain'):module.compose_edges(edges[::-1],.005,1.)


@pytest.mark.parametrize('value',[False,float('nan'),1.1,-.1])
def test_invalid_bound_rejected(edges,value):
    data=json.loads(edges[0].read_text());data['checks'][1]['actualMm3']=value;edges[0].write_text(json.dumps(data))
    with pytest.raises(ValueError,match='bound'):module.compose_edges(edges,.005,1.)


def test_summed_bound_cannot_spend_tolerance_twice(edges):
    for p in edges:
        data=json.loads(p.read_text());data['checks'][1]['actualMm3']=.6;p.write_text(json.dumps(data))
    r=module.compose_edges(edges,.005,1.)
    assert not r['ok'] and r['data']['verdict']=='FAIL'
