"""The native binder quotes local dimensions, never constructed full paths."""
from types import SimpleNamespace
import pytest
from catia_mcp.drawing.builder import BuildError, BuildExecutor

class Relations:
    def __init__(self, existing=False, value=None):
        self.body=None;self.value=value;self.existing=existing
    def Item(self, name):
        if not self.existing:raise KeyError(name)
        return self
    def Modify(self, body):self.body=body
    def CreateFormula(self, name, comment, target, body):
        self.body=body;return self
    @property
    def Value(self):return self.body if self.value is None else self.value

@pytest.mark.parametrize('existing',[False,True])
@pytest.mark.parametrize('expression,expected',[
    ('OR', '`OR`'),
    ('AND + OR / 2', '`AND` + `OR` / 2'),
    ('FLOOR - ORIGIN + OR2', '`FLOOR` - `ORIGIN` + `OR2`'),
    ('T + 2mm + 1E-3mm', '`T` + 2mm + 1E-3mm'),
    ('CA * 4 + 0deg', '`CA` * 4 + 0deg'),
    ('0mm', '0mm'),
])
def test_local_identifiers_and_units(expression,expected,existing):
    executor=BuildExecutor();relations=Relations(existing)
    executor.part=SimpleNamespace(Relations=relations)
    assert executor._bind(object(),expression,'test')==expected
    assert relations.body==expected

def test_empty_native_formula_still_stops_build():
    executor=BuildExecutor();executor.part=SimpleNamespace(Relations=Relations(value=''))
    with pytest.raises(BuildError,match='empty body'):
        executor._bind(object(),'OR','test')
