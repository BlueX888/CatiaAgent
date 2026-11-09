"""T06 edit-plan contract: wire model, canonical view, unchanged hashes/domain."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from catia_mcp.contracts.edit_plan import (
    AcceptanceWire,
    EditPlanWire,
    canonicalize_edit_plan,
)
from catia_mcp.drawing.plan import _plan_id
from catia_mcp.editing.validation import plan_hash, validate_plan

REPO_ROOT = Path(__file__).resolve().parents[2]
PLANS = REPO_ROOT / "contracts" / "catia" / "v1" / "fixtures" / "plans"
SESSION = "sess0000abcd"


def load(name: str) -> dict:
    return json.loads((PLANS / name).read_text(encoding="utf-8"))


def oracles() -> dict:
    return json.loads((PLANS / "hash-oracles.json").read_text(encoding="utf-8"))


def plan() -> dict:
    return load("edit-plan-v1.json")


def snapshot() -> dict:
    return load("edit-snapshot-v1.json")


def codes(value: dict) -> list[str]:
    return [problem["code"] for problem in validate_plan(value, snapshot(), session_id=SESSION)]


def test_wire_model_accepts_the_frozen_plan_and_keeps_the_hash():
    original = plan()
    frozen = copy.deepcopy(original)
    wire = EditPlanWire.model_validate(original)
    canonical = canonicalize_edit_plan(wire)
    assert wire.editSessionId == SESSION
    assert canonical.changes[0].new_value == 200.0
    # The legacy hash is computed from the untouched wire dict, not the model.
    assert plan_hash(original) == oracles()["edit_plan"]["expected_digest"]
    assert original == frozen
    assert canonical.model_dump()["changes"][0]["new_value"] == 200.0


def test_the_other_hash_families_are_untouched():
    frozen = oracles()
    build = load("build-plan-v1.json")
    feature = load("feature-plan-v1.json")
    assert _plan_id(build) == frozen["build_plan"]["expected_digest"]
    payload = {key: value for key, value in feature.items() if key != "planId"}
    assert "sha256:" + hashlib.sha256(
        json.dumps(payload, sort_keys=True, allow_nan=False).encode()
    ).hexdigest() == frozen["feature_plan"]["expected_digest"]


# -- numeric policy ----------------------------------------------------------


def test_expected_revision_accepts_integer_valued_float():
    value = plan()
    value["expectedRevision"] = 3.0
    assert EditPlanWire.model_validate(value).expectedRevision == 3


@pytest.mark.parametrize("value", [True, False, "3", 3.5, float("nan"), float("inf")])
def test_expected_revision_rejects_boolean_string_fraction_and_non_finite(value):
    payload = plan()
    payload["expectedRevision"] = value
    with pytest.raises(ValidationError):
        EditPlanWire.model_validate(payload)


@pytest.mark.parametrize("field", ["oldValue", "newValue"])
@pytest.mark.parametrize("value", [True, False, "200", float("nan"), float("inf")])
def test_change_values_reject_boolean_string_and_non_finite(field, value):
    payload = plan()
    payload["changes"][0][field] = value
    with pytest.raises(ValidationError):
        EditPlanWire.model_validate(payload)


def test_degree_change_is_not_reinterpreted_as_length():
    value = plan()
    value["changes"] = [{"parameter": "Part2\\Angle", "oldValue": 45.0,
                         "newValue": 60.0, "unit": "deg", "reason": "open the angle"}]
    wire = EditPlanWire.model_validate(value)
    canonical = canonicalize_edit_plan(wire)
    assert canonical.changes[0].unit == "deg"
    assert canonical.changes[0].new_value == 60.0
    assert codes(value) == []


# -- acceptance and preserve unions -----------------------------------------


@pytest.mark.parametrize("item,expected", [
    ({"type": "bbox_range", "axis": "x", "min": -1, "max": 1}, "bbox_range"),
    ({"type": "bbox_dimension", "axis": "y", "expected": 20.0}, "bbox_dimension"),
    ({"type": "volume", "expected": 1000.0}, "volume"),
    ({"type": "hole_positions", "holes": [{"x": 1, "y": 2, "r": 3}]}, "hole_positions"),
    ({"type": "hole_world_positions", "holes": [{"x": 1, "y": 2, "z": 3}]}, "hole_world_positions"),
    ({"type": "hole_radius", "expected": 5.0, "parameter": "H"}, "hole_radius"),
    ({"type": "unchanged_dimension", "parameter": "Part2\\W", "expected": 100.0}, "unchanged_dimension"),
    ({"type": "feature_count", "expected": 2, "absTolerance": 0.5}, "feature_count"),
])
def test_each_acceptance_type_is_typed(item, expected):
    owner = AnnotatedHolder.model_validate({"type": "x", "acceptance": [item]})
    assert owner.acceptance[0].type == expected


def test_feature_count_uses_mathematical_integer_semantics():
    value = plan()
    value["acceptance"] = [{"type": "feature_count", "expected": 2.0}]
    assert EditPlanWire.model_validate(value).acceptance[0].expected == 2
    value["acceptance"] = [{"type": "feature_count", "expected": True}]
    with pytest.raises(ValidationError):
        EditPlanWire.model_validate(value)


def test_unknown_acceptance_type_is_structurally_rejected():
    value = plan()
    value["acceptance"] = [{"type": "made_up", "expected": 1}]
    with pytest.raises(ValidationError):
        EditPlanWire.model_validate(value)


def test_preserve_union_and_unknown_keys_are_kept():
    value = plan()
    value["preserve"] = [
        {"type": "holes_edge_distance", "holes": [1, 2], "legacyNote": "kept"},
        {"type": "hole_world_position", "holes": [{"x": 1, "y": 2, "z": 0}]},
    ]
    value["legacyTop"] = {"kept": True}
    value["changes"][0]["legacyChange"] = "kept"
    canonical = canonicalize_edit_plan(EditPlanWire.model_validate(value))
    assert canonical.preserve[0].type == "holes_edge_distance"
    assert canonical.preserve[0].fields["holes"] == [1, 2]
    assert canonical.preserve[0].extra["legacyNote"] == "kept"
    assert canonical.extra["legacyTop"] == {"kept": True}
    assert canonical.changes[0].extra["legacyChange"] == "kept"


# -- domain judgement stays in validate_plan ---------------------------------


def test_multi_error_aggregation_is_unchanged():
    value = plan()
    value["expectedRevision"] = 99
    value["changes"] = [
        {"parameter": "Part2\\Missing", "oldValue": 1.0, "newValue": 2.0, "unit": "mm"},
        {"parameter": "Part2\\L", "oldValue": 160.0, "newValue": 200.0, "unit": "mm"},
        {"parameter": "Part2\\L", "oldValue": 160.0, "newValue": 210.0, "unit": "mm"},
        {"parameter": "Part2\\PartBody\\Sketch.1\\Length.1", "oldValue": 160.0,
         "newValue": 200.0, "unit": "mm"},
    ]
    problems = validate_plan(value, snapshot(), session_id=SESSION)
    assert [problem["code"] for problem in problems] == [
        "STALE_REVISION", "PARAM_UNKNOWN", "INVALID_PLAN", "PARAM_NOT_WRITABLE",
    ]
    assert problems[0]["path"] == "expectedRevision"
    assert problems[1]["path"] == "changes[0].parameter"
    assert problems[2]["path"] == "changes[2].parameter"


def test_stale_old_value_path_is_unchanged():
    value = plan()
    value["changes"][0]["oldValue"] = 150.0
    problems = validate_plan(value, snapshot(), session_id=SESSION)
    assert [(problem["code"], problem["path"]) for problem in problems] == [
        ("STALE_PLAN", "changes[0].oldValue"),
    ]


def test_a_canonical_model_is_not_an_execution_trust_token():
    canonical = canonicalize_edit_plan(EditPlanWire.model_validate(plan()))
    problems = validate_plan(canonical, snapshot(), session_id=SESSION)
    assert problems == [{"code": "INVALID_PLAN", "message": "plan must be a JSON object", "path": ""}]


# A minimal holder so an acceptance item can be validated through the union.
from typing import Annotated  # noqa: E402

from pydantic import BaseModel, Field  # noqa: E402


class AnnotatedHolder(BaseModel):
    type: str
    acceptance: list[AcceptanceWire] = Field(default_factory=list)
