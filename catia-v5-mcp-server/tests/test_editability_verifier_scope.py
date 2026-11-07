"""The fast path omits optional observations, never requested checks."""
import json
import runpy
from pathlib import Path
from types import SimpleNamespace

import pytest
import win32com.client

from catia_mcp.drawing.verify import run_independent_verifier


VERIFIER = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts' / 'independent_verify_part.py'))


@pytest.mark.parametrize('key', ['holes', 'holeCentres', 'cylinders', 'fullCylinders'])
def test_skip_never_suppresses_requested_cylinder_checks(key):
    result = VERIFIER['verify']({'parameterChanges': {'R': {'type': 'length', 'value': 1}},
                                 'expected': {key: []}, 'collectCylindricalFaces': False})
    assert result['verdict'] == 'FAIL'
    assert result['checks'][0]['name'] == 'cylindrical_faces_collection'


@pytest.mark.parametrize('value, changes', [(False, {}), ('false', {'R': {}}), (0, {'R': {}})])
def test_skip_requires_explicit_boolean_and_parameter_copy(value, changes):
    result = VERIFIER['verify']({'parameterChanges': changes, 'collectCylindricalFaces': value})
    assert result['verdict'] == 'FAIL'
    assert result['checks'][0]['name'] == 'cylindrical_faces_collection'


def test_parameter_copy_skip_keeps_all_other_checks_and_export(tmp_path, monkeypatch):
    target = tmp_path / 'part.CATPart'
    target.write_bytes(b'saved part')
    calls = []

    class Parameter:
        Value = 1.0
        def ValuateFromString(self, value):
            self.Value = float(value[:-2])

    param = Parameter()
    class Params:
        def Item(self, name):
            assert name == 'R'
            return param

    class Selection:
        Count = 0
        def Clear(self): pass
        def Add(self, body): pass
        def Search(self, term): calls.append(term)

    part = SimpleNamespace(Parameters=Params(), MainBody=SimpleNamespace(Shapes=SimpleNamespace(Count=1)),
                           Relations=SimpleNamespace(Count=1, Item=lambda _: SimpleNamespace(Value='R')),
                           Update=lambda: None, CreateReferenceFromObject=lambda body: body)
    opened = []
    doc = SimpleNamespace(Name='part.CATPart', FullName=str(target), Part=part, Selection=Selection(), Activate=lambda: None,
                          GetWorkbench=lambda _: SimpleNamespace(GetMeasurable=lambda _: SimpleNamespace(Volume=100e-9)),
                          ExportData=lambda path, _: Path(path).write_bytes(b'export'))
    doc.Close = lambda: opened.remove(doc)
    class Documents:
        @property
        def Count(self): return len(opened)
        def Item(self, index): return opened[index - 1]
        def Open(self, _):
            opened.append(doc)
            return doc
    app = SimpleNamespace(Documents=Documents())
    monkeypatch.setattr(win32com.client, 'GetActiveObject', lambda _: app)
    monkeypatch.setitem(VERIFIER['verify'].__globals__, '_read_stl_points', lambda _: [(0, 0, 0), (1, 1, 1)])
    base = {'partPath': str(target), 'expected': {'volumeMm3': 100, 'featureCount': 1, 'parameters': {'R': 1.1}},
            'parameterChanges': {'R': {'type': 'length', 'value': 1.1}}}
    full = VERIFIER['verify']({**base, 'exportStep': str(tmp_path / 'full.step')})
    assert full['verdict'] == 'PASS'
    assert calls and full['measurements']['cylindricalFaces'] == []
    calls.clear()
    limited = VERIFIER['verify']({**base, 'exportStep': str(tmp_path / 'copy.step'), 'collectCylindricalFaces': False})
    assert limited['verdict'] == 'PASS'
    assert not calls
    assert 'cylindricalFaces' not in limited['measurements']
    assert limited['measurements']['cylindricalFacesStatus'] == 'skipped_for_parameter_copy'
    assert limited['stepExport']['sha256'] == full['stepExport']['sha256']
    assert limited['checks'] == full['checks']


def test_wrapper_sends_skip_only_for_parameter_copy(tmp_path, monkeypatch):
    part = tmp_path / 'part.CATPart'
    part.write_bytes(b'part')
    captured = []
    class Child:
        returncode = 0
        def communicate(self, **kwargs):
            return json.dumps({'verdict': 'PASS', 'checks': [{'passed': True}]}), ''
    def popen(command, **kwargs):
        captured.append(json.loads(Path(command[-1]).read_text(encoding='utf-8')))
        return Child()
    monkeypatch.setattr('subprocess.Popen', popen)
    monkeypatch.setenv('CATIA_MCP_WORK_DIR', str(tmp_path))
    assert run_independent_verifier(str(part), {})['verdict'] == 'PASS'
    assert 'collectCylindricalFaces' not in captured[-1]
    assert run_independent_verifier(str(part), {}, parameter_changes={'R': {'type': 'length', 'value': 1.1}},
                                    collect_cylindrical_faces=False)['verdict'] == 'PASS'
    assert captured[-1]['collectCylindricalFaces'] is False
    count = len(captured)
    assert run_independent_verifier(str(part), {'holes': []}, parameter_changes={'R': {}},
                                    collect_cylindrical_faces=False)['verdict'] == 'FAIL'
    assert len(captured) == count
