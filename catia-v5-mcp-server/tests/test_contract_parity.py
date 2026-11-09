"""T16 parity: structural agreement, separate domain evidence, active promotion."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from mcp.types import ListToolsRequest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts" / "architecture"))

import check_contract_parity  # noqa: E402
from catia_mcp.contracts.export import build_schemas  # noqa: E402
from catia_mcp.drawing.feature_schema import FEATURE_REQUIREMENTS_JSON  # noqa: E402
from catia_mcp.server import CATIAMCPServer  # noqa: E402


def report() -> dict:
    return check_contract_parity.build_report(REPO_ROOT)


def test_no_unexplained_structural_divergences():
    data = report()
    assert data["divergences"] == []
    assert data["hashes"]["match_frozen"] is True
    assert len(data["structural"]) >= 20
    known = {(item["contract_id"], item["case"]) for item in data["known_differences"]}
    for case in data["structural"]:
        assert case["python"] == case["expected"], case
        if (case["contract_id"], case["case"]) not in known:
            assert case["jsonschema"] == case["expected"], case


def test_known_cross_language_differences_are_explicit():
    data = report()
    known = {(item["contract_id"], item["case"]) for item in data["known_differences"]}
    assert known == {
        ("cad-support-inconclusive-summary", "float_attempt"),
        ("cad-support-inconclusive-summary", "null_diagnostics"),
    }
    assert all(item["reason"] for item in data["known_differences"])


def test_domain_decisions_are_reported_separately():
    domain = report()["domain"]
    assert domain["edit_plan_valid_codes"] == []
    assert domain["edit_plan_bad_codes"] == ["STALE_PLAN"]
    assert domain["intent_planned"] == "planned"
    assert domain["feature_duplicate_code"] == "FEATURE_ID_INVALID"
    assert domain["result_states"]["success"] == "success"
    assert domain["support_states"]["inconclusive"] == "inconclusive"


def test_registry_declares_one_authority_per_family():
    data = report()
    modes = data["registry_modes"]
    assert modes["compile-intent-input"] == "active"
    assert modes["apply-edit-plan-input"] == "active"
    assert modes["edit-plan"] == "shadow"
    assert modes["feature-requirements"] == "shadow"
    assert set(modes.values()) <= {"active", "shadow", "legacy"}


def test_active_tools_serve_the_generated_schema():
    schemas = build_schemas()

    async def run():
        server = CATIAMCPServer()
        try:
            listed = await server.server.request_handlers[ListToolsRequest](
                ListToolsRequest(method="tools/list"))
            tools = {tool.name: tool for tool in listed.root.tools}
            for name, contract_id in (("catia_compile_intent", "compile-intent-input"),
                                      ("catia_apply_edit_plan", "apply-edit-plan-input")):
                served = tools[name].inputSchema
                generated = schemas[contract_id]
                assert "$id" not in served
                assert set(served["properties"]) == set(generated["properties"])
                assert served["required"] == generated["required"]
                assert set(served.get("$defs", {})) == set(generated.get("$defs", {}))
            # Descriptions are presentation and survive on the active schemas.
            assert "editSessionId" in tools["catia_apply_edit_plan"].inputSchema["properties"]
            assert tools["catia_compile_intent"].inputSchema["properties"]["intent"].get("description")
            # A legacy tool keeps its hand-written schema (no $defs).
            assert "$defs" not in tools["catia_export"].inputSchema
        finally:
            await server.close()

    asyncio.run(run())


def test_feature_schema_compatibility_entry_is_preserved():
    assert FEATURE_REQUIREMENTS_JSON["properties"]["schema"]["const"] == "catia-feature-requirements/1"
    assert FEATURE_REQUIREMENTS_JSON["required"] == ["schema", "unit", "unitEvidence", "parameters", "features"]


def test_registry_digest_still_matches_generated_schema():
    registry = json.loads((REPO_ROOT / "contracts/catia/v1/registry.json").read_text(encoding="utf-8"))
    from catia_mcp.contracts.export import schema_digest
    schemas = build_schemas()
    for item in registry["contracts"]:
        assert item["schema_sha256"] == schema_digest(schemas[item["contract_id"]])
