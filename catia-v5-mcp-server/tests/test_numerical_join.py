import pytest
from catia_mcp.feature_geometry import evaluate_features


@pytest.fixture(autouse=True)
def cad_kernel():
    pytest.importorskip('build123d')


def joined_pads(gap):
    return {'schema':'catia-feature-requirements/1','unit':'mm','unitEvidence':[],
        'parameters':[{'name':n,'value':v,'evidence':[]} for n,v in [('L',20),('W',10),('T',5),('G',gap)]],
        'features':[
            {'id':'a','operation':'pad','length':'T','sketch':{'plane':'xy','profiles':[['rectangle',0,0,'L','W']]}},
            {'id':'b','operation':'pad','length':'T','dependsOn':['a'],
             'sketch':{'plane':'xy','profiles':[['rectangle','L+G',0,'L','W']]}}]}


def test_micron_join_is_bounded_and_reported():
    r=evaluate_features({'requirements':joined_pads(5e-6)})
    assert r['expected']['solidCount']==1 and r['expected']['valid']
    assert r['expected']['volumeMm3']==pytest.approx(2000.,abs=.001)
    assert 'numericalJoinToleranceMm' not in r['steps'][0]
    assert r['steps'][1]['numericalJoinToleranceMm']==1e-5


def test_larger_clearance_stays_disconnected():
    with pytest.raises(ValueError,match='SINGLE_SOLID_REQUIRED'):
        evaluate_features({'requirements':joined_pads(.0001)})


def test_touching_pads_use_exact_union():
    r=evaluate_features({'requirements':joined_pads(0.)})
    assert r['expected']['volumeMm3']==pytest.approx(2000.)
    assert 'numericalJoinToleranceMm' not in r['steps'][1]
