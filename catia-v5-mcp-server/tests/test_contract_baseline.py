"""T02 contract baseline: frozen corpus, three plan-hash families, edit domain cases.

The fixtures under ``contracts/catia/v1/fixtures`` were produced once from the
original implementation before any contract model existed.  This module only
asserts the pinned bytes/digests; it never re-records an oracle from a new model.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from catia_mcp.drawing.feature_model import SCHEMA as FEATURE_SCHEMA, FeatureError, compile_features
from catia_mcp.drawing.plan import _plan_id
from catia_mcp.editing.intent import compile_intent
from catia_mcp.editing.validation import plan_hash, validate_plan

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACTS = REPO_ROOT / "contracts" / "catia" / "v1"
FIXTURES = CONTRACTS / "fixtures"
PLANS = FIXTURES / "plans"

# Section 10 of the plan.  Every case below must appear in corpus-index.json.
REQUIRED_GROUPS = {
    "pure_numeric_fields": {
        "normal_int", "integer_valued_float", "boolean_as_number", "numeric_string",
        "nan", "positive_infinity", "negative_infinity", "out_of_range",
    },
    "draft_branches": {
        "planned", "needs_clarification", "unsupported", "missing_binding",
        "missing_unit", "missing_direction",
    },
    "edit_context": {
        "wrong_session", "stale_revision", "stale_old_value", "non_writable_driver",
        "duplicate_change",
    },
    "feature_expressions": {
        "zero_expression_string", "full_revolution", "named_parameters",
        "unreferenced_dimension", "dimension_conflict",
    },
    "feature_dependencies": {
        "duplicate_id", "forward_dependency", "non_first_solid",
        "no_auto_sorting", "missing_previous_solid_state",
    },
    "evidence_binding": {
        "missing_evidence", "forged_reference", "source_hash_changed",
        "reference_drawing_conflict",
    },
    "result_receipts": {
        "full_success", "no_modification_failure", "partial_modification",
        "modification_unknown", "contradictory_fields",
    },
    "dual_channel": {"text_structured_agree", "text_structured_contradict"},
    "serialization": {"default_fields", "unicode", "key_order", "int_vs_float", "null_values"},
    "mode_policy": {"normal_mode", "generation_mode", "bound_file_changed", "deadline_expired"},
    "fault_recovery": {
        "disconnect", "cancel", "reconnect", "duplicate_submit", "post_execution_output_error",
    },
}
REQUIRED_IDS = {f"{group}.{case}" for group, cases in REQUIRED_GROUPS.items() for case in cases}


def load_index() -> dict:
    return json.loads((CONTRACTS / "corpus-index.json").read_text(encoding="utf-8"))


def load_plan(name: str) -> dict:
    return json.loads((PLANS / name).read_text(encoding="utf-8"))


def load_oracles() -> dict:
    return json.loads((PLANS / "hash-oracles.json").read_text(encoding="utf-8"))


def raw_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# corpus completeness
# ---------------------------------------------------------------------------


def test_corpus_index_contains_every_required_case():
    index = load_index()
    seen = [entry["id"] for entry in index["cases"]]
    assert len(seen) == len(set(seen)), "duplicate case ids in corpus-index.json"
    missing = REQUIRED_IDS - set(seen)
    assert not missing, f"missing required cases: {sorted(missing)}"
    extra = set(seen) - REQUIRED_IDS
    assert not extra, f"unexpected cases (update REQUIRED_GROUPS deliberately): {sorted(extra)}"


def test_each_corpus_case_declares_structure_domain_and_side_effect_expectations():
    index = load_index()
    assert index["schema_version"] == "catiaagent-contract-corpus/1"
    for entry in index["cases"]:
        assert entry["oracle_kind"] in {"existing_test", "source_assertion", "fixture"}
        assert entry["oracle_location"].strip()
        for field in ("observable_assertion", "structure_expectation",
                      "domain_expectation", "side_effect_expectation"):
            assert entry[field].strip(), f"{entry['id']} missing {field}"


def test_existing_test_oracles_actually_exist():
    index = load_index()
    checked = 0
    for entry in index["cases"]:
        if entry["oracle_kind"] != "existing_test":
            continue
        location, _, test_name = entry["oracle_location"].partition("::")
        path = REPO_ROOT / location
        assert path.is_file(), f"{entry['id']} oracle file missing: {location}"
        text = path.read_text(encoding="utf-8", errors="replace")
        assert test_name in text, f"{entry['id']} oracle name not found in {location}: {test_name}"
        checked += 1
    assert checked >= 40, f"too few independently located oracles: {checked}"


# ---------------------------------------------------------------------------
# frozen raw bytes and the three distinct hash families
# ---------------------------------------------------------------------------


def test_fixture_raw_bytes_are_pinned():
    oracles = load_oracles()
    for key, meta in oracles.items():
        if key == "edit_snapshot":
            continue
        path = PLANS / meta["path"]
        assert path.stat().st_size == meta["bytes"], key
        assert raw_sha(path) == meta["raw_sha256"], key


def test_edit_plan_hash_is_bare_hex_with_unicode_preserved():
    oracles = load_oracles()
    plan = load_plan("edit-plan-v1.json")
    expected = oracles["edit_plan"]["expected_digest"]
    assert len(expected) == 64 and not expected.startswith("sha256:")
    assert plan_hash(plan) == expected
    # Key insertion order must not matter: sort_keys is part of the rule.
    reordered = dict(reversed(list(plan.items())))
    assert plan_hash(reordered) == expected


def test_edit_plan_hash_keeps_wire_integer_and_float_distinct():
    oracles = load_oracles()
    float_plan = load_plan("edit-plan-v1.json")
    int_plan = load_plan("edit-plan-int-float-unicode-v1.json")
    assert float_plan["changes"][0]["newValue"] == 200.0
    assert int_plan["changes"][0]["newValue"] == 200
    assert plan_hash(float_plan) != plan_hash(int_plan)
    assert plan_hash(float_plan) == oracles["edit_plan"]["expected_digest"]
    assert plan_hash(int_plan) == oracles["edit_plan_int_float_unicode"]["expected_digest"]


def test_build_plan_hash_excludes_plan_id_and_uses_compact_separators():
    oracles = load_oracles()
    plan = load_plan("build-plan-v1.json")
    expected = oracles["build_plan"]["expected_digest"]
    assert expected.startswith("sha256:")
    assert _plan_id(plan) == expected
    assert plan["planId"] == expected
    without_id = {key: value for key, value in plan.items() if key != "planId"}
    different_id = {**without_id, "planId": "sha256:" + "0" * 64}
    assert _plan_id(different_id) == expected


def test_feature_plan_hash_uses_python_default_json_serialization():
    oracles = load_oracles()
    plan = load_plan("feature-plan-v1.json")
    expected = oracles["feature_plan"]["expected_digest"]
    payload = {key: value for key, value in plan.items() if key != "planId"}
    recomputed = "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    assert recomputed == plan["planId"] == expected
    # The feature family escapes non-ASCII by default; the other two do not.
    assert "\\u" in json.dumps({"partName": "板件-Ω"}, sort_keys=True)


def test_hash_families_disagree_on_the_same_serialization_choice():
    oracles = load_oracles()
    build = {key: value for key, value in load_plan("build-plan-v1.json").items() if key != "planId"}
    # Compact separators belong to build; default separators belong to feature.
    compact = "sha256:" + hashlib.sha256(
        json.dumps(build, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    spaced = "sha256:" + hashlib.sha256(
        json.dumps(build, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    assert compact == oracles["build_plan"]["expected_digest"]
    assert spaced != compact


def test_unicode_and_null_variants_are_hash_stable():
    oracles = load_oracles()
    build = load_plan("build-plan-unicode-null-v1.json")
    feature = load_plan("feature-plan-unicode-null-v1.json")
    assert build["partName"] == "板件-Ω" and build["source"] is None
    assert feature["partName"] == "板件-Ω" and feature["source"] is None
    assert _plan_id(build) == oracles["build_plan_unicode_null"]["expected_digest"]
    payload = {key: value for key, value in feature.items() if key != "planId"}
    assert "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    ).hexdigest() == oracles["feature_plan_unicode_null"]["expected_digest"]


# ---------------------------------------------------------------------------
# edit-context domain cases against the frozen snapshot
# ---------------------------------------------------------------------------


def _codes(plan: dict, snapshot: dict, session_id: str = "sess0000abcd") -> list[str]:
    return [problem["code"] for problem in validate_plan(plan, snapshot, session_id=session_id)]


def test_frozen_edit_plan_is_valid_and_zero_write_contract_holds():
    plan = load_plan("edit-plan-v1.json")
    snapshot = load_plan("edit-snapshot-v1.json")
    assert _codes(plan, snapshot) == []


def test_wrong_session_is_rejected():
    plan = load_plan("edit-plan-v1.json")
    snapshot = load_plan("edit-snapshot-v1.json")
    assert _codes(plan, snapshot, session_id="other") == ["INVALID_PLAN"]


def test_stale_revision_is_rejected():
    plan = load_plan("edit-plan-v1.json")
    snapshot = load_plan("edit-snapshot-v1.json")
    plan["expectedRevision"] = 2
    assert _codes(plan, snapshot) == ["STALE_REVISION"]


def test_stale_old_value_is_rejected():
    plan = load_plan("edit-plan-v1.json")
    snapshot = load_plan("edit-snapshot-v1.json")
    plan["changes"][0]["oldValue"] = 150.0
    assert _codes(plan, snapshot) == ["STALE_PLAN"]


def test_duplicate_change_is_rejected():
    plan = load_plan("edit-plan-v1.json")
    snapshot = load_plan("edit-snapshot-v1.json")
    plan["changes"].append(dict(plan["changes"][0]))
    assert _codes(plan, snapshot) == ["INVALID_PLAN"]


def test_boolean_new_value_cannot_impersonate_a_dimension():
    plan = load_plan("edit-plan-v1.json")
    snapshot = load_plan("edit-snapshot-v1.json")
    plan["changes"][0]["newValue"] = True
    assert _codes(plan, snapshot) == ["INVALID_PLAN"]


def test_non_writable_parameter_is_rejected():
    plan = load_plan("edit-plan-v1.json")
    snapshot = load_plan("edit-snapshot-v1.json")
    plan["changes"][0]["parameter"] = "Part2\\PartBody\\Sketch.1\\Length.1"
    plan["changes"][0]["oldValue"] = 160.0
    assert _codes(plan, snapshot) == ["PARAM_NOT_WRITABLE"]


# ---------------------------------------------------------------------------
# draft and feature-dependency gaps registered by the corpus map
# ---------------------------------------------------------------------------


def _binding_snapshot() -> dict:
    return {
        "schema": "catia-model-snapshot/1",
        "editSessionId": "sess123",
        "modelRevision": 0,
        "parameters": [{"handle": "Part1\\L", "name": "Part1\\L", "value": 160.0,
                        "unit": "mm", "kind": "driver", "writable": True}],
        "holes": [],
        "geometry": {"volumeMm3": 1.0, "bbox": {"min": {"x": 0, "y": 0, "z": 0},
                                                "max": {"x": 1, "y": 1, "z": 1},
                                                "dimensions": {"x": 1.0, "y": 1.0, "z": 1.0}}},
        "impacts": {},
        "capabilities": {"invariants": {}},
        "limitations": [],
    }


def test_missing_parameter_binding_stays_a_clarification():
    payload = {"schema": "cad-intent-draft/1", "text": "set length to 20 mm",
               "target": {"attribute": "length"},
               "operation": {"type": "set", "amount": 20, "unit": "mm", "scope": "overall"},
               "reference": {"basis": "current"}, "preserve": []}
    result = compile_intent(payload, _binding_snapshot(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "target.parameter" in result["unresolved"]
    assert [candidate["handle"] for candidate in result["candidates"]] == ["Part1\\L"]
    assert result.get("plan") is None


def _feature_request(features: list[dict]) -> dict:
    return {"schema": FEATURE_SCHEMA, "unit": "mm", "unitEvidence": [],
            "parameters": [{"name": "T", "value": 8, "evidence": []}],
            "features": features}


def _base_feature() -> dict:
    return {"id": "base", "operation": "pad", "length": "T",
            "sketch": {"plane": "xy", "profiles": [{"type": "rectangle", "width": "T", "height": "T"}]}}


def test_duplicate_feature_ids_are_a_domain_rejection():
    with pytest.raises(FeatureError) as caught:
        compile_features(_feature_request([_base_feature(), _base_feature()]), None, require_evidence=False)
    assert caught.value.code == "FEATURE_ID_INVALID"


def test_missing_previous_solid_dependency_is_a_domain_rejection():
    boss = {"id": "boss", "operation": "pad", "length": "T",
            "sketch": {"plane": "xy", "profiles": [{"type": "circle", "x": "0", "y": "0", "radius": "T"}]}}
    with pytest.raises(FeatureError) as caught:
        compile_features(_feature_request([_base_feature(), boss]), None, require_evidence=False)
    assert caught.value.code == "FEATURE_DEPENDENCY_INVALID"


def test_numeric_string_cannot_impersonate_a_dimension():
    plan = load_plan("edit-plan-v1.json")
    snapshot = load_plan("edit-snapshot-v1.json")
    plan["changes"][0]["newValue"] = "200"
    assert _codes(plan, snapshot) == ["INVALID_PLAN"]


def test_non_finite_plan_values_record_the_legacy_boundary():
    # Section 10: NaN/infinity are not legal generic JSON fixtures; construct
    # them locally. These assertions pin the legacy behaviour, not an endorsement.
    snapshot = load_plan("edit-snapshot-v1.json")
    positive = load_plan("edit-plan-v1.json")
    positive["changes"][0]["newValue"] = float("inf")
    assert _codes(positive, snapshot) == ["OUT_OF_RANGE"]
    negative = load_plan("edit-plan-v1.json")
    negative["changes"][0]["newValue"] = float("-inf")
    assert _codes(negative, snapshot) == ["OUT_OF_RANGE"]
    # Known baseline gap: legacy validate_plan does not reject NaN. T06's
    # strict wire model must reject it; this test keeps the gap visible.
    nan_plan = load_plan("edit-plan-v1.json")
    nan_plan["changes"][0]["newValue"] = float("nan")
    assert _codes(nan_plan, snapshot) == []
