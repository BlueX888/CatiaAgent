"""T05 intent draft contract: wire aliases, canonical gaps, compiler parity."""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from catia_mcp.contracts.intent import (
    INTENT_SCHEMA,
    CanonicalIntentDraft,
    IntentDraftWire,
    WireDecision,
    canonicalize_intent,
)
from catia_mcp.editing.intent import compile_intent

HANDLE = "Part1\\L"
SESSION = "sess123"


def _impact() -> dict:
    return {
        "handle": "unused", "original": 0.0, "probeValue": 1.0, "probeDelta": 1.0,
        "writeStuck": True, "bboxDeltaX": 1.0, "bboxDeltaY": 0.0, "bboxDeltaZ": 0.0,
        "bboxEdgeDeltas": {"x": {"min": -0.5, "max": 0.5},
                            "y": {"min": 0.0, "max": 0.0},
                            "z": {"min": 0.0, "max": 0.0}},
        "holeCentersMoved": True, "holeRadiiChanged": False, "holeRadiusDelta": 0.0,
        "geometryChanged": True, "edgeDistancesPreserved": True, "restored": True,
    }


def snapshot() -> dict:
    return {
        "schema": "catia-model-snapshot/1",
        "editSessionId": SESSION,
        "modelRevision": 0,
        "parameters": [{"handle": HANDLE, "name": HANDLE, "value": 160.0,
                        "unit": "mm", "kind": "driver", "writable": True}],
        "holes": [],
        "geometry": {"volumeMm3": 1.0, "bbox": {"min": {"x": 0, "y": 0, "z": 0},
                                                "max": {"x": 160, "y": 1, "z": 1},
                                                "dimensions": {"x": 160.0, "y": 1.0, "z": 1.0}}},
        "impacts": {HANDLE: _impact()},
        "capabilities": {"invariants": {}},
        "limitations": [],
    }


def draft(**overrides) -> dict:
    value = {
        "schema": INTENT_SCHEMA,
        "text": "length set 200 mm",
        "target": {"parameter": HANDLE, "attribute": "length"},
        "operation": {"type": "set", "amount": 200, "unit": "mm", "scope": "overall"},
        "reference": {"basis": "current"},
    }
    value.update(overrides)
    return value


def canonical(value: dict) -> CanonicalIntentDraft:
    return canonicalize_intent(IntentDraftWire.model_validate(value))


def compile_decision(value: dict) -> dict:
    return compile_intent(value, snapshot(), session_id=SESSION)


# -- wire completeness -------------------------------------------------------


def test_decision_aliases_are_explicit_in_the_wire_type():
    assert set(get_args(WireDecision)) == {
        "planned", "plan", "propose",
        "needs_clarification", "clarify", "clarification",
        "unsupported", "reject", "rejected", "refuse",
    }


@pytest.mark.parametrize("declared,expected", [
    ("planned", "planned"), ("plan", "planned"), ("propose", "planned"),
    ("needs_clarification", "needs_clarification"), ("clarify", "needs_clarification"),
    ("clarification", "needs_clarification"),
    ("unsupported", "unsupported"), ("reject", "unsupported"),
    ("rejected", "unsupported"), ("refuse", "unsupported"),
])
def test_declared_decision_aliases_normalise(declared, expected):
    assert canonical(draft(decision=declared)).decision == expected


def test_clarification_and_unsupported_branches_represent_incomplete_drafts():
    clarify = canonical({"schema": INTENT_SCHEMA, "decision": "needs_clarification"})
    assert clarify.decision == "needs_clarification"
    assert clarify.target is None and clarify.operation is None
    assert {"target.parameter", "operation.unit"} <= set(clarify.missing)

    unsupported = canonical({"schema": INTENT_SCHEMA, "decision": "reject"})
    assert unsupported.decision == "unsupported"


def test_host_only_fields_are_not_part_of_the_draft():
    for field in ("userText", "userConfirmed", "confirmationText"):
        with pytest.raises(ValidationError):
            IntentDraftWire.model_validate(draft(**{field: "host value"}))


# -- missing values stay missing ---------------------------------------------


def test_missing_parameter_is_not_selected_from_candidates():
    value = draft()
    value["target"] = {"attribute": "length"}
    result = canonical(value)
    assert result.target.parameter is None
    assert "target.parameter" in result.missing
    assert not hasattr(result, "candidates")


def test_missing_unit_is_never_defaulted_to_mm():
    value = draft()
    del value["operation"]["unit"]
    result = canonical(value)
    assert result.operation.unit is None
    assert "operation.unit" in result.missing


def test_missing_single_side_direction_stays_visible():
    value = draft()
    value["operation"]["type"] = "increase"
    value["operation"]["scope"] = "single_side"
    result = canonical(value)
    assert result.operation.scope == "single_side"
    assert result.reference.direction is None
    assert "reference.direction" in result.missing


def test_text_is_kept_verbatim():
    value = draft(text="把长度改为 200 mm，不要动孔")
    assert canonical(value).text == "把长度改为 200 mm，不要动孔"


def test_canonicalization_does_not_alias_the_wire_input():
    value = draft(preserve=["center_position"])
    wire = IntentDraftWire.model_validate(value)
    result = canonicalize_intent(wire)
    result.preserve.append("hole_centers")
    assert wire.preserve == ["center_position"]


# -- alias normalization -----------------------------------------------------


def test_operation_scope_unit_direction_and_preserve_aliases():
    value = draft()
    value["operation"] = {"type": "改为", "amount": 200, "unit": "毫米",
                          "scope": "整体"}
    value["reference"] = {"basis": "当前", "direction": "+"}
    value["preserve"] = ["中心", "孔边距"]
    result = canonical(value)
    assert result.operation.type == "set"
    assert result.operation.unit == "mm"
    assert result.operation.scope == "overall"
    assert result.reference.basis == "current"
    assert result.reference.direction == "positive"
    assert result.preserve == ["center_position", "hole_edge_distance"]


def test_attribute_alias_is_normalised_but_unknown_stays_visible():
    value = draft()
    value["target"]["attribute"] = "长度"
    assert canonical(value).target.attribute == "length"
    value["target"]["attribute"] = "mystery"
    assert canonical(value).target.attribute == "mystery"


# -- strict numeric policy ---------------------------------------------------


@pytest.mark.parametrize("amount", [True, False, "200", float("nan"), float("inf")])
def test_amount_rejects_boolean_string_and_non_finite(amount):
    value = draft()
    value["operation"]["amount"] = amount
    with pytest.raises(ValidationError):
        IntentDraftWire.model_validate(value)


def test_revision_accepts_finite_numbers_and_becomes_int():
    value = draft()
    value["reference"]["revision"] = 3.0
    assert canonical(value).reference.revision == 3


# -- compiler parity ---------------------------------------------------------


@pytest.mark.parametrize("case,path", [
    ("missing_parameter", "target.parameter"),
    ("missing_unit", "operation.unit"),
    ("missing_direction", "reference.direction"),
])
def test_structural_gap_matches_the_compilers_clarification_path(case, path):
    value = draft()
    if case == "missing_parameter":
        value["target"] = {"attribute": "length"}
    elif case == "missing_unit":
        del value["operation"]["unit"]
    else:
        value["operation"]["type"] = "increase"
        value["operation"]["scope"] = "single_side"
    result = canonical(value)
    compiled = compile_decision(value)
    assert compiled["decision"] == "needs_clarification"
    assert path in compiled["unresolved"]
    assert path in result.missing


def test_planned_draft_still_compiles():
    value = draft()
    result = canonical(value)
    compiled = compile_decision(value)
    assert result.decision == "planned"
    assert compiled["decision"] == "planned"
    assert compiled["plan"]["changes"][0]["newValue"] == 200.0


def test_domain_rejection_is_not_collapsed_into_structure():
    # A semantic refusal is a compiler decision, not a structural one: the
    # contract must not pretend the draft was malformed.
    value = draft(text="在零件上新增一个孔")
    result = canonical(value)
    compiled = compile_decision(value)
    assert result.decision == "planned"
    assert compiled["decision"] == "unsupported"


def test_declared_clarification_matches_compiler_decision():
    value = {"schema": INTENT_SCHEMA, "decision": "clarify"}
    assert canonical(value).decision == "needs_clarification"
    assert compile_decision(value)["decision"] == "needs_clarification"


def test_declared_unsupported_matches_compiler_decision():
    value = {"schema": INTENT_SCHEMA, "decision": "reject"}
    assert canonical(value).decision == "unsupported"
    assert compile_decision(value)["decision"] == "unsupported"
