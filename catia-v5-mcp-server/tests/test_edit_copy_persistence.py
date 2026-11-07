"""Persistence must be read from disk, and only an owned copy may be saved."""
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest
import win32com.client

VERIFY = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/independent_verify_part.py'))


@pytest.fixture
def scene(tmp_path, monkeypatch):
    baseline = tmp_path / 'part.CATPart'
    baseline.write_text(json.dumps({'R': 1.0}))
    copy = tmp_path / 'part.edit-evidence' / '000-R-test' / baseline.name
    copy.parent.mkdir(parents=True)
    copy.write_bytes(baseline.read_bytes())
    calls = []
    faults = {}

    open_documents = []

    class Document:
        Name = baseline.name
        def __init__(self, path):
            self.FullName = ('part.CATPart' if faults.get('relative_document') else
                             str(tmp_path / 'user.CATPart') if faults.get('wrong_document') else path)
            self.values = json.loads(Path(path).read_text())
            parameter = SimpleNamespace()
            parameter.ValuateFromString = lambda value: self.values.update(R=float(value[:-2]))
            class Params:
                def Item(inner, name):
                    parameter.Value = self.values[name]
                    return parameter
            self.Part = SimpleNamespace(Parameters=Params(), Update=lambda: calls.append('update'),
                MainBody=SimpleNamespace(Shapes=SimpleNamespace(Count=1)),
                CreateReferenceFromObject=lambda body: body,
                Relations=SimpleNamespace(Count=1, Item=lambda _: SimpleNamespace(Value='R')))
        def Activate(self): calls.append('activate')
        def Close(self):
            calls.append('close')
            close_number = calls.count('close')
            if faults.get('close_raises_at') == close_number:
                raise RuntimeError('simulated close failure')
            if faults.get('close_noop_at') != close_number:
                open_documents.remove(self)
        def Save(self):
            calls.append('save')
            Path(self.FullName).write_text(json.dumps({'R': 9.0} if faults.get('lost_edit') else self.values))
            if faults.get('changed_baseline'):
                baseline.write_text('changed externally')
        def GetWorkbench(self, _):
            if faults.get('change_path_before_save') and not calls.count('save'):
                self.FullName = str(tmp_path / 'user.CATPart')
            return SimpleNamespace(GetMeasurable=lambda _: SimpleNamespace(Volume=(100 + self.values['R']) * 1e-9))
        def ExportData(self, path, kind):
            calls.append(kind)
            Path(path).write_bytes(b'export')
            if faults.get('change_path_before_final_close'):
                self.FullName = str(tmp_path / 'user.CATPart')

    def open_doc(path):
        calls.append('open')
        document = Document(path)
        open_documents.append(document)
        return document
    class Documents:
        Open = staticmethod(open_doc)
        @property
        def Count(self):
            if faults.get('preopen_collection_unreadable') or (faults.get('collection_unreadable') and 'close' in calls):
                raise RuntimeError('cannot read document collection')
            return len(open_documents) + bool(faults.get('other_document_open') or faults.get('preopened_target')
                                             or faults.get('other_unbound_document')
                                             or faults.get('other_unbound_same_name')
                                             or faults.get('preopen_document_path_unreadable')
                                             or (faults.get('document_path_unreadable') and 'close' in calls))
        def Item(self, index):
            if faults.get('preopen_document_path_unreadable') or (faults.get('document_path_unreadable') and 'close' in calls):
                raise RuntimeError('cannot read document identity')
            if index > len(open_documents):
                if faults.get('preopened_target'):
                    return SimpleNamespace(FullName=str(copy))
                if faults.get('other_unbound_document'):
                    return SimpleNamespace(Name='Product1.CATProduct', FullName='Product1.CATProduct')
                if faults.get('other_unbound_same_name'):
                    return SimpleNamespace(Name=baseline.name, FullName=baseline.name)
                return SimpleNamespace(FullName=str(tmp_path / 'elsewhere' / baseline.name))
            return open_documents[index - 1]
    app = SimpleNamespace(Documents=Documents())
    monkeypatch.setattr(win32com.client, 'GetActiveObject', lambda _: app)
    monkeypatch.setitem(VERIFY['verify'].__globals__, '_read_stl_points', lambda _: [(0, 0, 0), (1, 1, 1)])
    spec = {'partPath': str(copy), 'editCopyOf': str(baseline),
        'parameterChanges': {'R': {'type': 'length', 'value': 1.1}},
        'collectCylindricalFaces': False, 'expected': {'featureCount': 1, 'parameters': {'R': 1.1}},
        'exportStep': str(copy.parent / 'edited.step')}
    return baseline, copy, calls, faults, spec


def test_edit_is_saved_closed_and_read_back_from_disk(scene):
    baseline, copy, calls, faults, spec = scene
    original = baseline.read_bytes()
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'PASS'
    assert calls.count('open') == calls.count('close') == 2
    assert calls.index('save') < calls.index('close') < calls.index('open', 1)
    assert json.loads(copy.read_text()) == {'R': 1.1}
    assert baseline.read_bytes() == original
    assert result['editPersistence']['savedAndReopened'] is True
    assert result['volumeExpectationBasis']['method'] == 'native_edited_copy_before_save'
    assert result['measurements']['parameters']['R'] == 1.1
    assert result['fileSha256'] == VERIFY['_sha256'](str(copy))


def test_verifier_does_not_open_a_document_when_required_instance_is_unavailable(scene, monkeypatch):
    import catia_mcp.catia_instance

    _, _, calls, _, spec = scene
    monkeypatch.setenv('CATIA_REQUIRED_CATTEMP', 'configured-by-host')
    monkeypatch.setattr(catia_mcp.catia_instance, 'get_active_catia',
                        lambda: (_ for _ in ()).throw(RuntimeError('instance unavailable')))
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert any(c['name'] == 'catia_available' and c['passed'] is False for c in result['checks'])
    assert calls == []


def test_an_in_memory_success_cannot_mask_a_lost_saved_edit(scene):
    _, _, calls, faults, spec = scene
    faults['lost_edit'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    failed = {c['name'] for c in result['checks'] if not c['passed']}
    assert {'parameters', 'volume'} <= failed
    assert calls.count('open') == 2


def test_a_different_returned_document_is_not_mutated_saved_or_closed(scene):
    _, _, calls, faults, spec = scene
    faults['wrong_document'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert calls == ['open']


@pytest.mark.parametrize('fault', ['close_raises_at', 'close_noop_at'])
@pytest.mark.parametrize('close_number', [1, 2])
def test_close_failure_or_document_remaining_open_cannot_pass(scene, fault, close_number):
    _, _, calls, faults, spec = scene
    faults[fault] = close_number
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert any('close' in c['name'] and not c['passed'] for c in result['checks'])
    assert result['sessionHealth'] == 'unknown'
    if close_number == 1:
        assert calls.count('open') == 1
        assert calls.count('close') == 1


@pytest.mark.parametrize('fault', ['collection_unreadable', 'document_path_unreadable'])
def test_unreadable_documents_collection_cannot_prove_close(scene, fault):
    _, _, calls, faults, spec = scene
    faults[fault] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert any('close' in c['name'] and not c['passed'] for c in result['checks'])
    assert result['sessionHealth'] == 'unknown'
    assert calls.count('open') == 1


def test_another_document_with_the_same_name_is_not_closed_or_misidentified(scene):
    _, _, calls, faults, spec = scene
    faults['other_document_open'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'PASS'
    assert calls.count('close') == 2


def test_unbound_differently_named_document_remains_open(scene):
    _, _, calls, faults, spec = scene
    faults['other_unbound_document'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'PASS'
    assert calls.count('open') == calls.count('close') == 2


def test_unbound_document_with_same_name_remains_ambiguous(scene):
    _, _, calls, faults, spec = scene
    faults['other_unbound_same_name'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert result['sessionHealth'] == 'unknown'
    assert not calls


def test_a_document_retargeted_before_final_close_is_not_closed(scene):
    _, _, calls, faults, spec = scene
    faults['change_path_before_final_close'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert calls.count('close') == 1


def test_wrong_document_on_read_only_verify_is_never_closed(scene):
    _, _, calls, faults, spec = scene
    faults['wrong_document'] = True
    spec.pop('editCopyOf')
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert calls == ['open']


def test_relative_document_identity_is_not_trusted_or_closed(scene):
    _, _, calls, faults, spec = scene
    faults['relative_document'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert result['sessionHealth'] == 'unknown'
    assert calls == ['open']


@pytest.mark.parametrize('fault', ['preopened_target', 'preopen_collection_unreadable',
                                   'preopen_document_path_unreadable'])
def test_existing_or_unreadable_document_collection_is_not_touched(scene, fault):
    _, _, calls, faults, spec = scene
    faults[fault] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert result['sessionHealth'] == 'unknown'
    assert not calls


def test_changed_path_before_save_is_not_saved_or_closed(scene):
    _, _, calls, faults, spec = scene
    faults['change_path_before_save'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert result['sessionHealth'] == 'unknown'
    assert 'save' not in calls and 'close' not in calls


@pytest.mark.parametrize('which', ['baseline', 'copy'])
@pytest.mark.parametrize('close_fault', [False, True])
def test_final_hash_error_preserves_close_report(scene, monkeypatch, which, close_fault):
    baseline, copy, calls, faults, spec = scene
    if close_fault:
        faults['close_noop_at'] = 2
    sha256 = VERIFY['_sha256']
    def failing_sha256(path):
        if calls.count('close') == 2 and Path(path) == (baseline if which == 'baseline' else copy):
            raise OSError('simulated hash read failure')
        return sha256(path)
    monkeypatch.setitem(VERIFY['verify'].__globals__, '_sha256', failing_sha256)
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert result['sessionHealth'] == 'unknown'
    assert any(c['name'] == 'document_closed' and c['passed'] is not close_fault for c in result['checks'])
    assert any(c['name'] == ('edit_baseline_unchanged' if which == 'baseline' else 'edit_copy_hash')
               and not c['passed'] for c in result['checks'])


def test_changed_original_cannot_pass_copy_verification(scene):
    _, _, _, faults, spec = scene
    faults['changed_baseline'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert any(c['name'] == 'edit_baseline_unchanged' and not c['passed'] for c in result['checks'])


@pytest.mark.parametrize('fault', ['original', 'unowned', 'different_bytes', 'volume', 'no_changes'])
def test_unsafe_save_scope_is_rejected_before_open(scene, fault):
    baseline, copy, calls, _, spec = scene
    if fault == 'original': spec['partPath'] = str(baseline)
    if fault == 'unowned':
        unowned = baseline.parent / 'elsewhere' / copy.name
        unowned.parent.mkdir()
        unowned.write_bytes(copy.read_bytes())
        spec['partPath'] = str(unowned)
    if fault == 'different_bytes': copy.write_bytes(b'not a fresh copy')
    if fault == 'volume': spec['expected']['volumeMm3'] = 101.159
    if fault == 'no_changes':
        spec['parameterChanges'] = {}
        spec['collectCylindricalFaces'] = True
    result = VERIFY['verify'](spec)
    assert result['verdict'] == 'FAIL'
    assert not calls
    assert result['checks'][-1]['name'] == 'edit_copy_scope'
