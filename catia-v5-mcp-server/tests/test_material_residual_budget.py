"""A measured decomposition residual is charged, never rounded into a pass."""
from types import SimpleNamespace
import math
import pytest
from catia_mcp import cad_support as support


def install(monkeypatch,a,b,common,left,right):
    shapes=SimpleNamespace(wrapped=a),SimpleNamespace(wrapped=b)
    monkeypatch.setattr(support,'_volume',lambda shape:shape)
    monkeypatch.setattr(support,'_shared_interior',lambda *args:True)
    monkeypatch.setattr(support,'_shared_ball_volume',lambda *args:1.)
    monkeypatch.setattr(support,'_difference_witnesses',lambda *args:[None,None])
    def boolean(operation,first,second,fuzzy):
        if operation=='common':return common
        if operation=='cut':return left if first is shapes[0] else right
        raise AssertionError('No fallback is eligible for decomposition errors')
    monkeypatch.setattr(support,'_boolean_volume',boolean)
    return shapes


def test_small_measured_mass_residual_is_fully_charged_to_both_directions(monkeypatch):
    a,b=install(monkeypatch,1e6,1e6+.004,1e6,0.,0.)
    overlap,_,detail=support._overlap_volume(a,b,.01)
    assert a.wrapped-overlap==pytest.approx(.004,abs=1e-8)
    assert b.wrapped-overlap==pytest.approx(.008,abs=1e-8)
    assert detail['consistencyToleranceMm3']==.01
    assert all(r['uncertaintyMm3']==pytest.approx(.004,abs=1e-8) for r in detail['attempts'])


def test_charged_uncertainty_pushes_a_marginal_material_difference_over_its_limit(monkeypatch):
    a,b=install(monkeypatch,1e6,1e6,1e6-.008,.004,.008)
    overlap,_,_=support._overlap_volume(a,b,.01)
    assert a.wrapped-overlap==pytest.approx(.012,abs=1e-8)
    assert a.wrapped-overlap>.01  # The caller must reject this conservative bound.


def test_large_solid_residual_cannot_be_suppressed_as_relative_roundoff(monkeypatch):
    volume = 1e11
    common = volume - .009
    a, b = install(monkeypatch, volume, volume, common, .004, .004)
    overlap, _, detail = support._overlap_volume(a, b, .01)
    residual = abs(math.fsum((common, .004, -volume)))
    assert 0 < residual < .01
    assert all(row['uncertaintyMm3'] == residual for row in detail['attempts'])
    assert a.wrapped - overlap > .01


def test_large_solid_impossible_common_is_not_clamped_into_a_pass(monkeypatch):
    volume = 1e11
    common = volume + .006
    a, b = install(monkeypatch, volume, volume, common, .003, .003)
    overlap, _, detail = support._overlap_volume(a, b, .01)
    assert 0 < common - volume < .01
    assert all(row['uncertaintyMm3'] > common - volume for row in detail['attempts'])
    assert all('roundoffAdjustmentMm3' not in row for row in detail['attempts'])
    assert a.wrapped - overlap > .01


def test_large_real_solid_with_small_removed_material_never_passes():
    bd = pytest.importorskip('build123d')
    actual = bd.Box(10000, 10000, 1000)
    notch = bd.Box(.1, .1, 1.1).moved(bd.Location((0, 0, 499.45)))
    reference = actual - notch
    assert reference.is_valid
    actual_volume = support._volume(actual.wrapped)
    assert actual_volume - support._volume(reference.wrapped) > .01
    try:
        overlap, _, _ = support._overlap_volume(actual, reference, .01)
    except ValueError as exc:
        assert 'inconclusive' in str(exc).lower()
    else:
        assert actual_volume - overlap > .01


def test_residual_over_the_requested_limit_remains_inconclusive(monkeypatch):
    a,b=install(monkeypatch,1e6,1e6+.02,1e6,0.,0.)
    with pytest.raises(ValueError,match='fewer than two'):
        support._overlap_volume(a,b,.01)


def test_small_shapes_keep_the_independent_relative_error_limit(monkeypatch):
    a,b=install(monkeypatch,100.,100.0005,100.,0.,0.)
    with pytest.raises(ValueError,match='fewer than two'):
        support._overlap_volume(a,b,.01)


def test_shared_material_lower_bound_still_rejects_a_tiny_positive_common(monkeypatch):
    a,b=install(monkeypatch,1e6,1e6,.002,1e6-.002,1e6-.002)
    with pytest.raises(ValueError,match='shared interior ball'):
        support._overlap_volume(a,b,.01)


def test_shared_ball_difference_just_over_budget_is_rejected(monkeypatch):
    a,b=install(monkeypatch,10000.,10000.,1.,9999.,9999.)
    monkeypatch.setattr(support, '_shared_ball_volume', lambda *args: 1.01)
    assert 1.01 - 1. > .01
    assert 1. + .01 == 1.01  # Adding the budget hides the excess.
    with pytest.raises(ValueError,match='shared interior ball'):
        support._overlap_volume(a,b,.01)
