"""T09 shadow dispatch: structure checks never change the real decision."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from catia_mcp.contracts import PydanticValidationError
from jsonschema import ValidationError as JsonSchemaValidationError

from catia_mcp.contracts.dispatch import (
    ApplyEditPlanInputWire,
    CompileIntentInputWire,
    shadow_check,
)
from catia_mcp.server import CATIAMCPServer

REPO_ROOT = Path(__file__).resolve().parents[2]
PLANS = REPO_ROOT / "contracts" / "catia" / "v1" / "fixtures" / "plans"


def valid_intent_input() -> dict:
    return {
        "editSessionId": "sess0000abcd",
        "intent": {
            "schema": "cad-intent-draft/1",
            "text": "length set 200 mm",
            "target": {"parameter": "Part1\\L", "attribute": "length"},
            "operation": {"type": "set", "amount": 200, "unit": "mm", "scope": "overall"},
            "reference": {"basis": "current"},
        },
    }


def fake_result(name: str = "catia_compile_intent") -> dict:
    return {
        "ok": True, "code": "OK", "operation_id": "op-shadow",
        "tool": name, "message": "", "data": {"decision": "planned"},
        "effects": {"modified": False}, "diagnostics": [], "warnings": [],
        "recovery": {"rollback_supported": False, "automatic_retry_allowed": False,
                     "next_actions": []},
    }


class FakeModule:
    def __init__(self):
        self.calls = []

    def execute(self, name, arguments):
        self.calls.append((name, arguments))
        return fake_result(name)


@pytest.fixture
def server(monkeypatch):
    instance = CATIAMCPServer()
    monkeypatch.setattr(instance, "_record_shadow_check", instance._record_shadow_check)
    yield instance
    instance.connection.disconnect = lambda: None


def install_fake(server, monkeypatch, name="catia_compile_intent"):
    module = FakeModule()
    server._tool_router[name] = module
    guard_calls = []
    monkeypatch.setattr(server.editing_tools, "guard", lambda tool: guard_calls.append(tool) or None)
    connections = []
    monkeypatch.setattr(server.connection, "ensure_connected", lambda: connections.append(True))
    return module, connections, guard_calls


# -- registry and payloads ---------------------------------------------------


def test_only_edit_contracts_are_registered():
    assert shadow_check("catia_export", {})["status"] == "not_registered"
    assert shadow_check("catia_compile_intent", valid_intent_input())["registered"] is True
    assert shadow_check("catia_apply_edit_plan", {
        "editSessionId": "sess0000abcd",
        "plan": json.loads((PLANS / "edit-plan-v1.json").read_text(encoding="utf-8")),
    })["status"] == "ok"


def test_shadow_reports_differences_with_paths():
    payload = valid_intent_input()
    payload["intent"]["operation"]["amount"] = "200"
    outcome = shadow_check("catia_compile_intent", payload)
    assert outcome["status"] == "difference"
    assert any("operation.amount" in problem["path"] for problem in outcome["problems"])
    assert all(problem["code"] == "INVALID_ARGUMENT" for problem in outcome["problems"])


def test_pydantic_error_alias_does_not_replace_the_jsonschema_error():
    assert PydanticValidationError is not JsonSchemaValidationError


# -- shadow is transparent ---------------------------------------------------


def test_shadow_agrees_with_the_promoted_schema_and_executes_valid_input(server, monkeypatch):
    module, connections, guards = install_fake(server, monkeypatch)
    result = server._execute_tool("catia_compile_intent", valid_intent_input())
    assert result.isError is False
    assert json.loads(result.content[0].text) == fake_result()
    assert len(module.calls) == 1
    assert connections == [True]
    assert guards == ["catia_compile_intent"]
    assert server._shadow_differences == []


def test_promoted_active_schema_rejects_string_amount_before_execution(server, monkeypatch):
    module, connections, guards = install_fake(server, monkeypatch)
    payload = valid_intent_input()
    payload["intent"]["operation"]["amount"] = "200"
    result = server._execute_tool("catia_compile_intent", payload)
    body = json.loads(result.content[0].text)
    assert body["code"] == "INVALID_ARGUMENT"
    assert result.isError is True
    assert module.calls == [] and connections == [] and guards == []


def test_invalid_json_schema_terminates_before_connect_and_execute(server, monkeypatch):
    module, connections, guards = install_fake(server, monkeypatch)
    result = server._execute_tool("catia_compile_intent", {"editSessionId": "s"})
    body = json.loads(result.content[0].text)
    assert body["code"] == "INVALID_ARGUMENT"
    assert connections == [] and module.calls == [] and guards == []
    assert server._shadow_differences == []


def test_shadow_still_runs_after_generation_prepare(server, monkeypatch):
    module, connections, guards = install_fake(server, monkeypatch)
    order = []

    class Scope:
        def prepare(self, name, arguments):
            order.append("prepare")
            return {**arguments, "prepared": True}

    monkeypatch.setattr(server, "generation_scope", Scope())
    monkeypatch.setattr(server.connection, "ensure_connected", lambda: order.append("connect"))
    monkeypatch.setattr(module, "execute", lambda name, arguments: order.append("execute") or fake_result(name))
    server._execute_tool("catia_compile_intent", valid_intent_input())
    assert order == ["prepare", "connect", "execute"]


def test_generation_schema_restrictions_are_still_enforced(tmp_path, monkeypatch):
    root = tmp_path / "generation"
    root.mkdir()
    (root / "input.pdf").write_bytes(b"%PDF-1.4\n")
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))

    async def run():
        instance = CATIAMCPServer()
        monkeypatch.setattr(instance.connection, "ensure_connected", lambda: pytest.fail("COM accessed"))
        try:
            schema = instance._schemas["catia_build_part"]
            assert "evidencePath" in schema["required"]
            assert schema["properties"]["editCheck"]["const"] is True
            assert schema["properties"]["expected"]["not"] == {}
            result = await instance.call_tool("catia_build_part", {"requirements": {}})
            assert result.isError and result.structuredContent["code"] == "INVALID_ARGUMENT"
        finally:
            await instance.close()

    asyncio.run(run())


def test_clarification_draft_reaches_the_original_judgement(server, monkeypatch):
    module, connections, guards = install_fake(server, monkeypatch)
    payload = {"editSessionId": "sess0000abcd",
               "intent": {"schema": "cad-intent-draft/1", "decision": "needs_clarification",
                          "target": {}, "operation": {}}}
    result = server._execute_tool("catia_compile_intent", payload)
    assert result.isError is False
    assert module.calls and module.calls[0][1]["intent"]["decision"] == "needs_clarification"
    assert server._shadow_differences == []
