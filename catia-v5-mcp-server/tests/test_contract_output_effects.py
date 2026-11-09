"""T10 output shadow: malformed receipts are recorded, never re-executed."""

from __future__ import annotations

import json

import pytest

import catia_mcp.results as results_module
from catia_mcp.contracts.results import OperationResultWire
from catia_mcp.drawing.feature_workflow import _shadow_support_response
from catia_mcp.results import (
    clear_shadow_differences,
    minimal_failure_result,
    operation_result,
    record_result_shadow,
    shadow_differences,
)
from catia_mcp.server import CATIAMCPServer


@pytest.fixture(autouse=True)
def clean_shadow():
    clear_shadow_differences()
    yield
    clear_shadow_differences()


def valid_failure(modified=None) -> dict:
    return operation_result(tool="catia_build_part", ok=False, code="FEATURE_BUILD_FAILED",
                            message="boom", data={"businessState": "build_failed"},
                            modified=modified, operation_id="op-valid")


def test_operation_result_is_structurally_valid_and_keeps_null():
    receipt = valid_failure(modified=None)
    assert OperationResultWire.model_validate(receipt).effects.modified is None
    assert receipt["effects"]["modified"] is None
    assert shadow_differences() == []


def test_malformed_result_is_recorded_without_mutation():
    broken = {"ok": True, "tool": "catia_build_part"}
    record_result_shadow(broken)
    assert shadow_differences() and shadow_differences()[0]["kind"] == "output_structure"
    assert broken == {"ok": True, "tool": "catia_build_part"}


def test_contradictory_build_receipt_is_recorded():
    receipt = valid_failure(modified=True)
    receipt["ok"] = True  # success flag with a failure business state
    record_result_shadow(receipt)
    kinds = [item["kind"] for item in shadow_differences()]
    assert "output_receipt" in kinds


def test_minimal_failure_constructor_is_independent(monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("minimal_failure_result must not call operation_result")

    monkeypatch.setattr(results_module, "operation_result", explode)
    receipt = minimal_failure_result(tool="catia_build_part", code="OUTPUT_INVALID",
                                     message="bad receipt", modified=None)
    assert receipt["effects"]["modified"] is None
    assert receipt["recovery"]["automatic_retry_allowed"] is False
    assert OperationResultWire.model_validate(receipt).code == "OUTPUT_INVALID"


class FakeModule:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def execute(self, name, arguments):
        self.calls += 1
        return self.result


@pytest.fixture
def server(monkeypatch):
    instance = CATIAMCPServer()
    monkeypatch.setattr(instance.editing_tools, "guard", lambda name: None)
    monkeypatch.setattr(instance.connection, "ensure_connected", lambda: None)
    return instance


def test_invalid_output_is_returned_unchanged_and_executed_once(server):
    module = FakeModule({"ok": True})
    server._tool_router["catia_compile_intent"] = module
    result = server._execute_tool("catia_compile_intent", {"editSessionId": "s", "intent": {"schema": "cad-intent-draft/1", "target": {}, "operation": {}}})
    assert module.calls == 1
    assert json.loads(result.content[0].text) == {"ok": True}
    assert result.structuredContent == {"ok": True}
    assert shadow_differences()


def test_modified_none_is_preserved_and_not_retried(server):
    module = FakeModule(valid_failure(modified=None))
    server._tool_router["catia_compile_intent"] = module
    result = server._execute_tool("catia_compile_intent", {"editSessionId": "s", "intent": {"schema": "cad-intent-draft/1", "target": {}, "operation": {}}})
    assert module.calls == 1
    assert result.structuredContent["effects"]["modified"] is None
    assert result.structuredContent["recovery"]["automatic_retry_allowed"] is False
    assert json.loads(result.content[0].text) == result.structuredContent


def test_support_response_shadow_separates_valid_from_malformed():
    valid = {"ok": False, "operation": "evaluate", "exitCode": 1,
             "code": "MATERIAL_COMPARISON_INCONCLUSIVE",
             "error": {"type": "MaterialComparisonInconclusive", "message": "x",
                       "summary": {"reason": "FUZZY_LEVELS_DISAGREE",
                                   "consistencyBudgetMm3": 0.01, "attemptCount": 2,
                                   "candidateCount": 1}}}
    _shadow_support_response(valid)
    assert shadow_differences() == []
    broken = {"ok": False, "operation": "evaluate", "code": "MATERIAL_COMPARISON_INCONCLUSIVE",
              "error": {"type": "MaterialComparisonInconclusive", "message": "x"}}
    _shadow_support_response(broken)
    assert shadow_differences() and shadow_differences()[0]["kind"] == "support_response"


def test_support_response_without_operation_is_ignored():
    _shadow_support_response({"ok": True, "data": {"verdict": "PASS"}})
    assert shadow_differences() == []
