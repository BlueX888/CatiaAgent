from types import SimpleNamespace
from decimal import Decimal
import math
import pytest
from catia_mcp import cad_support as support


@pytest.fixture
def setup(monkeypatch):
    shape=SimpleNamespace(wrapped=1000.)
    monkeypatch.setattr(support,'_volume',lambda shape:shape)
    monkeypatch.setattr(support,'_shared_interior',lambda *a:True)
    monkeypatch.setattr(support,'_difference_witnesses',lambda *a:[None,None])
    def install(common=999.,union=1001.,witnesses=None):
        def boolean(operation,left,right,fuzzy):
            if operation=='cut':raise ValueError('cut boolean returned invalid volume')
            value=common if operation=='common' else union
            return value(fuzzy) if callable(value) else value
        monkeypatch.setattr(support,'_boolean_volume',boolean)
        if witnesses is not None:monkeypatch.setattr(support,'_difference_witnesses',lambda *a:witnesses)
        return shape
    return install


def test_union_fallback_retains_invalid_cut_evidence(setup):
    s=setup();overlap,_,r=support._overlap_volume(s,s)
    assert overlap==999.
    assert r['method']=='common_union_identity' and len(r['cutAttempts'])==10
    assert len(r['attempts'])==4


@pytest.mark.parametrize('common,union',[(999.,1000.),(1000.5,999.5),(0.,2000.),(999.,999.)])
def test_inconsistent_union_or_false_zero_remains_inconclusive(setup,common,union):
    s=setup(common,union)
    with pytest.raises(ValueError,match='inconclusive'):support._overlap_volume(s,s)


def test_full_overlap_cannot_ignore_material_only_points(setup):
    s=setup(1000.,1000.,witnesses=[[1,2,3],None])
    with pytest.raises(ValueError,match='material-only witness'):support._overlap_volume(s,s)


def test_union_levels_must_agree(setup):
    common=lambda f:999. if f<1e-4 else 998.
    s=setup(common,lambda f:2000.-common(f))
    with pytest.raises(ValueError,match='identity disagrees'):support._overlap_volume(s,s)


def test_identity_error_is_charged_to_material_bounds(setup):
    s=setup(1000.0001,1000.0001)
    overlap,_,r=support._overlap_volume(s,s,.01)
    assert overlap==pytest.approx(999.9997,abs=1e-9)
    assert r['attempts'][0]['uncertaintyMm3']==pytest.approx(.0002)


@pytest.mark.parametrize('common_steps,union_steps', [(-331, 6), (-320, 5), (1, 1), (0, 0)])
def test_large_union_fallback_contains_exact_decimal_bound(monkeypatch, common_steps, union_steps):
    volume = 1e11
    unit = math.ulp(volume)
    shape = SimpleNamespace(wrapped=volume)
    common = volume + common_steps * unit
    union = volume + union_steps * unit
    monkeypatch.setattr(support, '_volume', lambda value: value)
    monkeypatch.setattr(support, '_difference_witnesses', lambda *args: [None, None])
    def boolean(operation, *args):
        if operation == 'cut':
            raise ValueError('cut boolean returned invalid volume')
        return common if operation == 'common' else union
    monkeypatch.setattr(support, '_boolean_volume', boolean)
    overlap, _, detail = support._overlap_volume(shape, shape, .01)
    a, c, u = map(Decimal.from_float, (volume, common, union))
    residual = abs(c + u - a - a)
    uncertainty = max(residual, c-a, a-u, Decimal(0))
    exact_lower = min(c, a+a-u, a) - uncertainty
    exact_upper = min(a, max(c, a+a-u) + uncertainty)
    assert Decimal.from_float(overlap) <= exact_lower
    assert Decimal.from_float(detail['overlapIntervalMm3']['max']) >= exact_upper
    assert detail['attempts'][0]['residualMm3'] == float(residual)
    if (common_steps, union_steps) == (-331, 6):
        assert a - exact_lower == Decimal('0.010009765625')
        assert volume - overlap > .01


@pytest.mark.parametrize('common_steps,union_steps', [(328, -197), (197, -328)])
def test_large_union_bounds_reject_excess_just_over_budget(monkeypatch, common_steps, union_steps):
    volume = 2e11
    unit = math.ulp(volume)
    common = volume + common_steps * unit
    union = volume + union_steps * unit
    assert max(common-volume, volume-union) > .01
    monkeypatch.setattr(support, '_difference_witnesses', lambda *args: [None, None])
    monkeypatch.setattr(support, '_boolean_volume',
                        lambda operation, *args: common if operation == 'common' else union)
    with pytest.raises(ValueError, match='do not reconstruct'):
        support._union_overlap_volume(None, None, volume, volume, .01, [])


def test_general_boolean_errors_do_not_enable_union(monkeypatch,setup):
    s=setup()
    def boolean(operation,*args):
        assert operation!='union'
        raise ValueError('common boolean returned invalid geometry')
    monkeypatch.setattr(support,'_boolean_volume',boolean)
    with pytest.raises(ValueError,match='fewer than two consistent'):support._overlap_volume(s,s)


def test_analytic_shifted_hole_with_invalid_cut(monkeypatch):
    bd=pytest.importorskip('build123d')
    import math
    plate=bd.Box(20,20,10);hole=bd.Cylinder(3,12)
    a,b=plate-hole,plate-hole.moved(bd.Location((.2,0,0)))
    boolean=support._boolean_volume
    def invalid_cut(operation,*args):
        if operation=='cut':raise ValueError('cut boolean returned invalid volume')
        return boolean(operation,*args)
    monkeypatch.setattr(support,'_boolean_volume',invalid_cut)
    overlap,_,r=support._overlap_volume(a,b,.01)
    delta=(math.pi*9-(18*math.acos(.2/6)-.1*math.sqrt(36-.04)))*10
    assert support._volume(a.wrapped)-overlap==pytest.approx(delta,abs=1e-6)
    assert r['method']=='common_union_identity'
