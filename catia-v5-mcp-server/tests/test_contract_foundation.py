"""T04 contract foundation: strict wire primitives and error mapping."""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from typing import Optional

import pytest
from pydantic import Field

from catia_mcp.contracts import (
    ContractError,
    ContractModel,
    PydanticValidationError,
    WireFloat,
    WireInt,
    WireModel,
    is_explicit_null,
    present_fields,
    problems_from_pydantic,
    validate_contract,
)

PACKAGE = Path(__file__).resolve().parents[1] / "catia_mcp" / "contracts"


class Numbers(WireModel):
    count: WireInt
    length: WireFloat


class OptionalNumbers(WireModel):
    count: Optional[WireInt] = None


class Named(ContractModel):
    name: str = Field(alias="Name")


def test_integer_valued_float_is_accepted_as_integer():
    model = Numbers.model_validate({"count": 4.0, "length": 10})
    assert model.count == 4 and isinstance(model.count, int)
    assert model.length == 10.0


@pytest.mark.parametrize("value", [True, False, "4", 4.5, float("nan"), float("inf")])
def test_boolean_string_fraction_and_non_finite_are_not_integers(value):
    with pytest.raises(PydanticValidationError):
        Numbers.model_validate({"count": value, "length": 1})


@pytest.mark.parametrize("value", [True, "10", float("nan"), float("inf"), float("-inf")])
def test_float_rejects_boolean_string_and_non_finite(value):
    with pytest.raises(PydanticValidationError):
        Numbers.model_validate({"count": 1, "length": value})


def test_unknown_fields_are_rejected():
    with pytest.raises(PydanticValidationError):
        Numbers.model_validate({"count": 1, "length": 1, "extra": True})


def test_missing_required_field_is_an_error_not_a_default():
    with pytest.raises(PydanticValidationError):
        Numbers.model_validate({"length": 1})


def test_explicit_null_is_visible_and_not_confused_with_missing():
    missing = OptionalNumbers.model_validate({})
    explicit = OptionalNumbers.model_validate({"count": None})
    assert missing.count is None and explicit.count is None
    assert not is_explicit_null(missing, "count")
    assert is_explicit_null(explicit, "count")
    assert present_fields(missing) == set()
    assert present_fields(explicit) == {"count"}


def test_aliases_are_accepted_and_canonical_dump_uses_field_names():
    model = Named.model_validate({"Name": "plate"})
    assert model.name == "plate"
    assert model.model_dump() == {"name": "plate"}


def test_error_mapping_preserves_code_message_and_path():
    with pytest.raises(PydanticValidationError) as caught:
        Numbers.model_validate({"count": True, "length": "10"})
    problems = problems_from_pydantic(caught.value)
    assert {problem["code"] for problem in problems} == {"INVALID_ARGUMENT"}
    assert {problem["path"] for problem in problems} == {"count", "length"}
    assert all(problem["message"] for problem in problems)


def test_validate_contract_raises_contract_error_with_path():
    with pytest.raises(ContractError) as caught:
        validate_contract(Numbers, {"count": 1, "length": 1, "extra": 2})
    assert caught.value.code == "INVALID_ARGUMENT"
    assert caught.value.path == "extra"
    assert caught.value.to_problem()["code"] == "INVALID_ARGUMENT"


def test_contracts_import_without_pywin32():
    script = (
        "import sys; sys.modules['win32com'] = None; sys.modules['pythoncom'] = None; "
        "sys.modules['win32com.client'] = None; import catia_mcp.contracts as c; "
        "print(c.WireInt is not None)"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_contracts_source_stays_python_310_compatible():
    for path in PACKAGE.glob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 10))


def test_contract_package_has_no_execution_or_io_imports():
    forbidden = {"win32com", "pythoncom", "catia_mcp.coordinator", "catia_mcp.server",
                 "catia_mcp.connection", "subprocess", "socket", "pathlib", "os"}
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        assert not (imported & forbidden), (path.name, imported & forbidden)
