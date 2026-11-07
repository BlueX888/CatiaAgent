"""A false zero cut needs an independently consistent, conservative identity."""
from types import SimpleNamespace
import math
import pytest
from catia_mcp import cad_support as support

@pytest.fixture
def setup(monkeypatch):
    shape=SimpleNamespace(wrapped=1000.)
    monkeypatch.setattr(support,'_volume',lambda shape:shape)
    monkeypatch.setattr(support,'_shared_interior',lambda *a:True)
    monkeypatch.setattr(support,'_shared_ball_volume',lambda *a:4.)
    monkeypatch.setattr(support,'_difference_witnesses',lambda *a:[None,[1,2,3]])
    calls=[]
    def install(common=lambda f:1. if f<1e-5 else 999.,union=lambda f:1001.,cut=None):
        def boolean(operation,left,right,fuzzy):
            calls.append(operation)
            if operation=='common':return common(fuzzy)
            if operation=='union':return union(fuzzy)
            if cut:return cut(fuzzy)
            return 999. if fuzzy<1e-5 else 0.
        monkeypatch.setattr(support,'_boolean_volume',boolean)
        return shape,calls
    return install

def test_false_zero_cuts_and_tiny_common_keep_all_rejected_evidence(setup):
    shape,calls=setup();overlap,_,detail=support._overlap_volume(shape,shape,.01)
    assert overlap==999. and detail['method']=='common_union_identity'
    assert len(detail['cutAttempts'])==10
    assert {r['reason'] for r in detail['cutAttempts']}=={
        'ValueError: intersection contradicts a shared interior ball',
        'ValueError: zero reference difference contradicts a strict material-only witness'}
    assert len([r for r in detail['attempts'] if r['status']=='candidate'])==2

@pytest.mark.parametrize('common,union',[(lambda f:1000.,lambda f:1000.),
    (lambda f:999.,lambda f:1000.),
    (lambda f:999. if f<1e-4 else 998.,lambda f:1001. if f<1e-4 else 1002.)])
def test_witness_identity_residual_and_fuzzy_disagreement_still_reject(setup,common,union):
    shape,_=setup(common,union,cut=lambda f:0.)
    with pytest.raises(ValueError,match='inconclusive'):support._overlap_volume(shape,shape,.01)

def test_one_false_zero_level_does_not_enable_fallback(setup):
    shape,calls=setup(common=lambda f:999. if f==0 else 1.,cut=lambda f:0. if f==0 else 999.)
    with pytest.raises(ValueError,match='fewer than two'):support._overlap_volume(shape,shape,.01)
    assert 'union' not in calls

def test_unrelated_boolean_failure_does_not_enable_fallback(setup,monkeypatch):
    shape,calls=setup();original=support._boolean_volume
    def boolean(operation,left,right,fuzzy):
        if operation=='common' and fuzzy==0:raise ValueError('common boolean returned invalid geometry')
        return original(operation,left,right,fuzzy)
    monkeypatch.setattr(support,'_boolean_volume',boolean)
    with pytest.raises(ValueError,match='fewer than two'):support._overlap_volume(shape,shape,.01)
    assert 'union' not in calls

def test_real_shifted_holes_with_false_zero_cuts_preserve_material_difference(monkeypatch):
    bd=pytest.importorskip('build123d')
    plate=bd.Box(20,20,10);hole=bd.Cylinder(3,12)
    actual,reference=plate-hole,plate-hole.moved(bd.Location((.2,0,0)))
    original=support._boolean_volume
    def false_zero_cut(operation,*args):return 0. if operation=='cut' else original(operation,*args)
    monkeypatch.setattr(support,'_boolean_volume',false_zero_cut)
    overlap,_,detail=support._overlap_volume(actual,reference,.01)
    delta=(math.pi*9-(18*math.acos(.2/6)-.1*math.sqrt(36-.04)))*10
    assert support._volume(actual.wrapped)-overlap==pytest.approx(delta,abs=1e-6)
    assert detail['method']=='common_union_identity'

def test_real_disjoint_solids_with_false_zero_cuts_keep_zero_overlap(monkeypatch):
    bd=pytest.importorskip('build123d')
    actual=bd.Box(2,2,2);reference=actual.moved(bd.Location((5,0,0)))
    original=support._boolean_volume
    def false_zero_cut(operation,*args):return 0. if operation=='cut' else original(operation,*args)
    monkeypatch.setattr(support,'_boolean_volume',false_zero_cut)
    overlap,_,detail=support._overlap_volume(actual,reference,.01)
    assert overlap==0 and detail['method']=='common_union_identity'
