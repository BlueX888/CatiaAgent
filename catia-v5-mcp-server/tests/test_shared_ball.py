"""A small positive but impossible common must not become a material verdict."""
from types import SimpleNamespace
import math
import pytest
from catia_mcp import cad_support as support

@pytest.mark.parametrize('fallback',[False,True])
def test_false_positive_common_is_inconclusive(monkeypatch,fallback):
    shape=SimpleNamespace(wrapped=1000.)
    monkeypatch.setattr(support,'_volume',lambda s:s)
    monkeypatch.setattr(support,'_shared_interior',lambda *a:True)
    monkeypatch.setattr(support,'_difference_witnesses',lambda *a:[None,None])
    monkeypatch.setattr(support,'_shared_ball_volume',lambda *a:4.)
    def boolean(operation,*args):
        if operation=='cut':
            if fallback:raise ValueError('cut boolean returned invalid volume')
            return 999.99
        return .01 if operation=='common' else 1999.99
    monkeypatch.setattr(support,'_boolean_volume',boolean)
    with pytest.raises(ValueError,match='shared interior ball'):
        support._overlap_volume(shape,shape,1.)

def test_real_shared_ball_is_bounded_by_both_solids():
    bd=pytest.importorskip('build123d')
    box=bd.Box(10,10,10).solids()[0]
    assert support._shared_ball_volume(box,box)==pytest.approx(4*math.pi*.9999**3/3)
    assert support._shared_ball_volume(box,box.moved(bd.Location((11,0,0))))==0
    thin=bd.Box(.1,10,10).solids()[0]
    bound=support._shared_ball_volume(thin,box)
    assert 0<bound<4*math.pi*.05**3/3
