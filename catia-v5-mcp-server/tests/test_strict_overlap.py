"""An invalid or unstable boolean must not be promoted to a geometry PASS."""
from types import SimpleNamespace

import pytest

from catia_mcp import cad_support


@pytest.fixture(autouse=True)
def measured_fake_volume(monkeypatch):
    monkeypatch.setattr(cad_support, '_volume', lambda wrapped: wrapped.volume)
    monkeypatch.setattr(cad_support, '_difference_witnesses', lambda *a: [None,None])


def shape(volume):
    result = SimpleNamespace(volume=volume)
    result.wrapped = result
    return result


def test_intersection_above_input_volume_cannot_be_clamped_to_a_pass(monkeypatch):
    actual = shape(volume=1000.0)
    monkeypatch.setattr(cad_support, '_boolean_volume',
                        lambda operation, left, right, fuzzy: 1000.5 if operation == 'common' else 0.0)
    with pytest.raises(ValueError, match='inconclusive'):
        cad_support._overlap_volume(actual, actual)


def test_fuzzy_results_must_agree_even_if_each_decomposes(monkeypatch):
    actual = shape(volume=1000.0)
    def unstable(operation, left, right, fuzzy):
        common = 1000.0 if fuzzy < 1e-4 else 999.9
        return common if operation == 'common' else 1000.0 - common
    monkeypatch.setattr(cad_support, '_boolean_volume', unstable)
    with pytest.raises(ValueError, match='fuzzy levels disagree'):
        cad_support._overlap_volume(actual, actual)


def test_each_difference_must_balance_its_own_input(monkeypatch):
    actual, reference = shape(volume=1000.0), shape(volume=999.0)
    def broken(operation, left, right, fuzzy):
        return 998.0 if operation == 'common' else (2.0 if left is actual else 0.0)
    monkeypatch.setattr(cad_support, '_boolean_volume', broken)
    with pytest.raises(ValueError, match='inconclusive'):
        cad_support._overlap_volume(actual, reference)


def test_consistent_candidates_choose_the_smallest_overlap(monkeypatch):
    actual = shape(volume=1000.0)
    def coherent(operation, left, right, fuzzy):
        common = 999.0 - fuzzy * 1e-5
        return common if operation == 'common' else 1000.0 - common
    monkeypatch.setattr(cad_support, '_boolean_volume', coherent)
    overlap, fuzzy, diagnostics = cad_support._overlap_volume(actual, actual)
    assert overlap < 999.0
    assert fuzzy is True
    assert len(diagnostics['attempts']) == 4


def test_consistency_budget_cannot_hide_small_parameter_changes(monkeypatch):
    actual = shape(volume=500000.0)
    def unstable(operation, left, right, fuzzy):
        common = 500000.0 if fuzzy < 1e-4 else 499999.985
        return common if operation == 'common' else 500000.0 - common
    monkeypatch.setattr(cad_support, '_boolean_volume', unstable)
    with pytest.raises(ValueError, match='fuzzy levels disagree'):
        cad_support._overlap_volume(actual, actual, 0.01)


def test_false_zero_intersection_is_rejected(monkeypatch):
    actual = shape(volume=1000.0)
    monkeypatch.setattr(cad_support, '_boolean_volume',
                        lambda operation, *args: 0.0 if operation == 'common' else 1000.0)
    monkeypatch.setattr(cad_support, '_shared_interior', lambda *args: True)
    with pytest.raises(ValueError, match='shared interior'):
        cad_support._overlap_volume(actual, actual)

def test_false_identical_material_is_rejected_by_strict_difference_witnesses(monkeypatch):
    actual=shape(1000.)
    monkeypatch.setattr(cad_support,'_difference_witnesses',lambda *a:[[1,2,3],[4,5,6]])
    monkeypatch.setattr(cad_support,'_boolean_volume',lambda operation,*a:1000. if operation=='common' else 0.)
    with pytest.raises(ValueError,match='material-only witness'):cad_support._overlap_volume(actual,actual)

def test_nonzero_balanced_cuts_can_survive_when_false_identical_levels_are_rejected(monkeypatch):
    actual=shape(1000.)
    monkeypatch.setattr(cad_support,'_difference_witnesses',lambda *a:[[1,2,3],[4,5,6]])
    def boolean(operation,left,right,fuzzy):
        common=1000. if fuzzy<1e-4 else 999.
        return common if operation=='common' else 1000.-common
    monkeypatch.setattr(cad_support,'_boolean_volume',boolean)
    overlap,_,diagnostics=cad_support._overlap_volume(actual,actual)
    assert overlap==999.
    assert sum(a['status']=='candidate' for a in diagnostics['attempts'])==2


def test_even_small_impossible_intersection_is_charged(monkeypatch):
    actual = shape(volume=1000.0)
    monkeypatch.setattr(cad_support, '_boolean_volume',
                        lambda operation, *args: 1000.0 + 1e-10 if operation == 'common' else 0.0)
    overlap, _, diagnostics = cad_support._overlap_volume(actual, actual)
    assert overlap < 1000.0
    assert diagnostics['attempts'][0]['uncertaintyMm3'] > 0
    assert 'roundoffAdjustmentMm3' not in diagnostics['attempts'][0]


def test_small_decomposition_error_is_charged_instead_of_clamped(monkeypatch):
    actual=shape(volume=1000.0)
    monkeypatch.setattr(cad_support,'_boolean_volume',lambda operation,*args:1000.0001 if operation=='common' else 0.0)
    overlap,_,diagnostics=cad_support._overlap_volume(actual,actual,.01)
    assert overlap==pytest.approx(999.9999)
    assert diagnostics['overlapIntervalMm3']['max']==1000
    attempt=diagnostics['attempts'][0]
    assert attempt['commonMm3']>1000
    assert attempt['uncertaintyMm3']==pytest.approx(.0001)
    assert 'roundoffAdjustmentMm3' not in attempt


def test_finer_fallback_still_requires_balanced_consistent_results(monkeypatch):
    actual = shape(volume=1000.0)
    def fine_only(operation, left, right, fuzzy):
        if fuzzy >= 1e-6:
            raise ValueError('coarse boolean returned invalid geometry')
        return 999.0 if operation == 'common' else 1.0
    monkeypatch.setattr(cad_support, '_boolean_volume', fine_only)
    overlap, _, diagnostics = cad_support._overlap_volume(actual, actual)
    assert overlap == 999.0
    assert len(diagnostics['attempts']) == 10
    assert sum(x['status'] == 'candidate' for x in diagnostics['attempts']) == 4


def test_finer_fallback_cannot_rescue_an_unbalanced_boolean(monkeypatch):
    actual = shape(volume=1000.0)
    def unbalanced(operation, left, right, fuzzy):
        if fuzzy == 0:
            return 999.0 if operation == 'common' else 1.0
        return 999.0 if operation == 'common' else 0.0
    monkeypatch.setattr(cad_support, '_boolean_volume', unbalanced)
    with pytest.raises(ValueError, match='fewer than two consistent'):
        cad_support._overlap_volume(actual, actual)


def test_intermediate_fallback_rejects_false_zero_before_using_consistent_results(monkeypatch):
    actual=shape(volume=1000.0)
    def sparse_failure(operation,left,right,fuzzy):
        overlap=1000.0 if fuzzy in (2e-4,5e-4) else 0.0
        return overlap if operation=='common' else 1000.0-overlap
    monkeypatch.setattr(cad_support,'_boolean_volume',sparse_failure)
    monkeypatch.setattr(cad_support,'_shared_interior',lambda *args:True)
    overlap,_,diagnostics=cad_support._overlap_volume(actual,actual)
    assert overlap==1000.0
    assert sum(x['status']=='candidate' for x in diagnostics['attempts'])==2
