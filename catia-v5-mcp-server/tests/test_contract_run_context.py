"""T19 run identity: host-only structure, persistence record, no second state machine."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from mcp.types import ListToolsRequest
from pydantic import ValidationError

from catia_mcp.contracts.export import build_schemas, schema_digest
from catia_mcp.contracts.run_context import RUN_CONTEXT_SCHEMA, RunContext, RunRecord
from catia_mcp.server import CATIAMCPServer

REPO_ROOT = Path(__file__).resolve().parents[2]
SAMPLE = REPO_ROOT / "contracts/catia/v1/fixtures/run_context/sample.json"
REGISTRY = REPO_ROOT / "contracts/catia/v1/registry.json"
CORPUS = REPO_ROOT / "contracts/catia/v1/corpus-index.json"


def sample() -> dict:
    return json.loads(SAMPLE.read_text(encoding="utf-8"))


def test_run_context_accepts_host_identity():
    context = RunContext.model_validate(sample())
    assert context.draft_schema == RUN_CONTEXT_SCHEMA
    assert context.runId == "run-20261109-01"
    assert context.sourceFingerprint == "a" * 64
    assert context.deadlineMs == 1893456000000


@pytest.mark.parametrize("field", ["runId", "attemptId", "sourceFingerprint", "codeVersion",
                                    "contractVersion", "workingDirectory", "createdAt"])
def test_missing_identity_fields_are_rejected(field):
    value = sample()
    del value[field]
    with pytest.raises(ValidationError):
        RunContext.model_validate(value)


@pytest.mark.parametrize("field,value", [
    ("sourceFingerprint", "not-a-digest"), ("deadlineMs", -1), ("deadlineMs", True),
    ("runId", ""), ("createdAt", ""),
])
def test_invalid_identity_values_are_rejected(field, value):
    payload = sample()
    payload[field] = value
    with pytest.raises(ValidationError):
        RunContext.model_validate(payload)


def test_integer_valued_deadline_is_normalised():
    payload = sample()
    payload["deadlineMs"] = 1893456000000.0
    assert RunContext.model_validate(payload).deadlineMs == 1893456000000


def test_unknown_identity_fields_are_rejected():
    value = sample()
    value["permission"] = "admin"
    with pytest.raises(ValidationError):
        RunContext.model_validate(value)


def test_registry_marks_run_context_host_only():
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    entry = next(item for item in registry["contracts"] if item["contract_id"] == "run-context")
    assert entry["mode"] == "shadow"
    assert entry["model_fillable"] is False
    assert entry["wire_version"] == RUN_CONTEXT_SCHEMA
    schemas = build_schemas()
    assert entry["schema_sha256"] == schema_digest(schemas["run-context"])


def test_run_context_is_not_a_model_tool():
    async def run():
        server = CATIAMCPServer()
        try:
            listed = await server.server.request_handlers[ListToolsRequest](
                ListToolsRequest(method="tools/list"))
            names = {tool.name for tool in listed.root.tools}
            assert "run-context" not in names and "catia_run_context" not in names
            assert all("runId" not in tool.inputSchema.get("properties", {}) for tool in listed.root.tools)
        finally:
            await server.close()

    asyncio.run(run())


def test_corpus_records_version_and_digest_for_run_context():
    corpus = json.loads(CORPUS.read_text(encoding="utf-8"))
    entries = [item for item in corpus["supplementary_cases"] if item["group"] == "run_context"]
    assert {item["id"] for item in entries} >= {
        "run_context.valid", "run_context.missing_run_id",
        "run_context.unknown_field", "run_context.not_a_tool",
    }
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    assert any(item["contract_id"] == "run-context" and item["schema_sha256"] for item in registry["contracts"])


def test_schema_accepts_fixture_and_rejects_unknown_field():
    schema = build_schemas()["run-context"]
    validator = Draft202012Validator(schema)
    assert validator.is_valid(sample())
    broken = sample()
    broken["extra"] = True
    assert not validator.is_valid(broken)


def test_run_record_keeps_source_and_sequence():
    record = RunRecord.model_validate({
        "context": sample(), "stateSource": "journal", "sequence": 4,
        "updatedAt": "2026-11-09T07:05:00Z",
    })
    assert record.stateSource == "journal"
    assert record.sequence == 4
    assert record.context.runId == "run-20261109-01"
