"""T13 export: complete schemas, deterministic registry and TS copy."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from catia_mcp.contracts.export import (
    CONTRACT_MODELS,
    build_registry,
    build_schemas,
    render_files,
    render_ts,
    schema_digest,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts" / "catia" / "v1"
TS_PATH = REPO_ROOT / "PilotDeck" / "src" / "tool" / "protocol" / "generated" / "catiaContracts.ts"


def refs(node) -> list[str]:
    found = []
    if isinstance(node, dict):
        if isinstance(node.get("$ref"), str):
            found.append(node["$ref"])
        for value in node.values():
            found.extend(refs(value))
    elif isinstance(node, list):
        for value in node:
            found.extend(refs(value))
    return found


def test_every_schema_is_draft_2020_12_and_self_contained():
    schemas = build_schemas()
    assert set(schemas) == set(CONTRACT_MODELS)
    for contract_id, schema in schemas.items():
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith(f"/{contract_id}.json")
        defs = schema.get("$defs", {})
        for reference in refs(schema):
            assert reference.startswith("#/$defs/"), (contract_id, reference)
            assert reference.split("/")[-1] in defs, (contract_id, reference)


def test_generation_is_deterministic():
    assert json.dumps(build_schemas(), sort_keys=True) == json.dumps(build_schemas(), sort_keys=True)
    assert build_registry() == build_registry()


def test_registry_digests_match_schema_bytes():
    schemas = build_schemas()
    registry = build_registry()
    assert registry["schema_version"] == "catiaagent-contract-registry/1"
    by_id = {item["contract_id"]: item for item in registry["contracts"]}
    assert set(by_id) == set(schemas)
    active = {cid for cid, item in by_id.items() if item["mode"] == "active"}
    assert active == {"compile-intent-input", "apply-edit-plan-input"}
    for contract_id, item in by_id.items():
        assert item["mode"] in {"shadow", "active"}
        assert item["schema_sha256"] == schema_digest(schemas[contract_id])
        path = REPO_ROOT / item["schema_path"]
        assert path.is_file()
        assert json.loads(path.read_text(encoding="utf-8")) == schemas[contract_id]


def test_legacy_families_and_boundary_notes_are_registered():
    registry = build_registry()
    ids = {item["contract_id"] for item in registry["legacy"]}
    assert {"editing-protocol-v1", "feature-schema-v1", "drawing-evidence-v1",
            "server-dynamic-policy", "host-run-context-v1"} <= ids
    assert registry["structural_vs_domain"]["rule"]
    assert registry["structural_vs_domain"]["domain_only"]


def test_wire_aliases_survive_export():
    schemas = build_schemas()
    intent = schemas["intent-draft"]
    assert "schema" in intent["properties"]
    assert "schema" in intent["required"]
    edit = schemas["edit-plan"]
    assert "schema" in edit["properties"]
    assert "editSessionId" in edit["properties"]


def test_generated_files_match_and_ts_is_valid_text():
    files = render_files()
    for relative, data in files.items():
        path = REPO_ROOT / relative
        assert path.is_file(), relative
        assert path.read_bytes() == data, relative
    text = TS_PATH.read_text(encoding="utf-8")
    assert "export const CATIA_CONTRACT_SCHEMAS" in text
    assert "as const;" in text
    assert "Do not edit by hand" in text
    for contract_id in CONTRACT_MODELS:
        assert f'"{contract_id}"' in text
    assert text == render_ts(build_schemas())


def test_export_check_mode_is_clean():
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "architecture" / "export_contracts.py"),
         "--repo-root", str(REPO_ROOT), "--check"],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_no_openapi_range_shorthand_leaks_into_published_schemas():
    text = "".join((CONTRACTS / "schema" / name).read_text(encoding="utf-8")
                   for name in sorted(p.name for p in (CONTRACTS / "schema").glob("*.json")))
    for shorthand in ('"gt":', '"ge":', '"lt":', '"le":'):
        assert shorthand not in text
    assert '"exclusiveMinimum":' in text or '"minimum":' in text


def test_ts_literals_do_not_contain_js_unsafe_tokens():
    text = TS_PATH.read_text(encoding="utf-8")
    # JSON with quoted keys is valid inside a TS object literal; reject only
    # accidental JSONP/executable prefixes.
    assert not re.search(r"^(\)|;|import\s)", text)
