"""T12 per-variant feature structure: batches are isolated, domain unchanged."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from catia_mcp.contracts.features import (
    BATCHES,
    FeatureRequirementsWire,
    FeatureWire,
    structure_problems,
    variant_batch,
)
from catia_mcp.drawing.feature_executor import BuildError, validate_compiled_plan
from catia_mcp.drawing.feature_model import (
    SCHEMA,
    FeatureError,
    clear_feature_shadow_differences,
    compile_features,
    feature_shadow_differences,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
PLANS = REPO_ROOT / "contracts" / "catia" / "v1" / "fixtures" / "plans"


VALUES = {"T": (8, None), "R": (3, None), "A": (30, "angle")}


def parameters(names=("T", "R")) -> list[dict]:
    return [{"name": name, "value": VALUES[name][0], "evidence": [],
             **({"type": VALUES[name][1]} if VALUES[name][1] else {})} for name in names]


def base_pad() -> dict:
    return {"id": "base", "operation": "pad", "length": "T",
            "sketch": {"plane": "xy",
                       "profiles": [{"type": "rectangle", "width": "T", "height": "T"}]}}


def requirements(*features, params=("T", "R")) -> dict:
    return {"schema": SCHEMA, "unit": "mm", "unitEvidence": [], "parameters": parameters(params),
            "features": list(features)}


def valid_by_batch() -> dict[str, dict]:
    base = base_pad()
    return {
        "pad_pocket": requirements(base, {
            "id": "pocket1", "operation": "pocket", "dependsOn": ["base"],
            "length": "T / 2", "direction": "reverse", "through": True,
            "sketch": {"plane": "xy", "offset": "T",
                       "profiles": [{"type": "circle", "x": "T / 2", "y": "T / 2", "radius": "R"}]},
        }),
        "shaft_groove": requirements({
            "id": "shaft1", "operation": "shaft", "angle": "A", "direction": "normal", "axisX": 0,
            "sketch": {"plane": "xy",
                       "profiles": [{"type": "circle", "x": "0", "y": "0", "radius": "R"}]},
        }, {
            "id": "groove1", "operation": "groove", "dependsOn": ["shaft1"], "angle": "A",
            "direction": "normal",
            "sketch": {"plane": "xy", "profiles": [{"type": "circle", "x": "0", "y": "0", "radius": "R"}]},
        }, params=("R", "A")),
        "fillet_chamfer": requirements(base, {
            "id": "fillet1", "operation": "fillet", "dependsOn": ["base"], "radius": "R",
            "edges": {"type": "linear", "direction": [1, 0, 0], "through": [0, 0, 0]},
        }, {
            "id": "chamfer1", "operation": "chamfer", "dependsOn": ["fillet1"], "length": "T",
            "angle": 45,
            "edges": {"type": "circular", "radius": "R", "centre": [0, 0, 0], "axis": [0, 0, 1]},
        }),
    }


@pytest.fixture(autouse=True)
def clean_shadow():
    clear_feature_shadow_differences()
    yield
    clear_feature_shadow_differences()


@pytest.mark.parametrize("batch", sorted(BATCHES))
def test_each_batch_is_structurally_valid_and_compiles(batch):
    value = valid_by_batch()[batch]
    assert structure_problems(value) == []
    plan = compile_features(value, None, require_evidence=False)
    assert plan["schema"] == "catia-feature-plan/1"
    assert [variant_batch(feature) for feature in value["features"]] == [
        batch if feature["operation"] in BATCHES[batch] else variant_batch(feature)
        for feature in value["features"]
    ]
    assert feature_shadow_differences() == []


def test_variant_structure_is_closed_per_operation():
    value = valid_by_batch()["pad_pocket"]
    value["features"][0]["through"] = True  # through belongs to pocket only
    problems = structure_problems(value)
    assert any("through" in problem["path"] for problem in problems)
    with pytest.raises(FeatureError) as caught:
        compile_features(value, None, require_evidence=False)
    assert caught.value.code == "FIELD_UNSUPPORTED"


def test_one_variant_failure_does_not_disable_another_batch():
    broken = valid_by_batch()["shaft_groove"]
    broken["features"][0]["direction"] = "reverse"
    assert structure_problems(broken)
    with pytest.raises(FeatureError):
        compile_features(broken, None, require_evidence=False)
    # The fillet/chamfer batch still compiles.
    assert compile_features(valid_by_batch()["fillet_chamfer"], None, require_evidence=False)


# -- domain rules stay in the compiler ---------------------------------------


def test_dependency_and_solid_rules_are_unchanged():
    duplicate = requirements(base_pad(), {**base_pad(), "id": "base"})
    with pytest.raises(FeatureError) as caught:
        compile_features(duplicate, None, require_evidence=False)
    assert caught.value.code == "FEATURE_ID_INVALID"

    first_pocket = requirements({
        "id": "cut", "operation": "pocket", "length": "T",
        "sketch": {"plane": "xy", "profiles": [["circle", 0, 0, "R"]]}})
    with pytest.raises(FeatureError) as caught:
        compile_features(first_pocket, None, require_evidence=False)
    assert caught.value.code == "FEATURE_OPERATION_UNSUPPORTED"

    forward = requirements(base_pad(), {
        "id": "boss", "operation": "pad", "dependsOn": ["future"], "length": "T",
        "sketch": {"plane": "xy", "profiles": [["circle", 0, 0, "R"]]}})
    with pytest.raises(FeatureError) as caught:
        compile_features(forward, None, require_evidence=False)
    assert caught.value.code == "FEATURE_DEPENDENCY_INVALID"

    missing_previous = requirements(base_pad(), {
        "id": "boss", "operation": "pad", "length": "T",
        "sketch": {"plane": "xy", "profiles": [["circle", 0, 0, "R"]]}})
    with pytest.raises(FeatureError) as caught:
        compile_features(missing_previous, None, require_evidence=False)
    assert caught.value.code == "FEATURE_DEPENDENCY_INVALID"


def test_fillet_group_uniqueness_remains_a_domain_rejection():
    selector = {"type": "linear", "direction": [1, 0, 0], "through": [0, 0, 0]}
    value = requirements(base_pad(), {
        "id": "fillet1", "operation": "fillet", "dependsOn": ["base"], "radius": "R",
        "edges": [selector, dict(selector)],
    })
    assert structure_problems(value)
    with pytest.raises(FeatureError) as caught:
        compile_features(value, None, require_evidence=False)
    assert caught.value.code == "EDGE_SELECTOR_INVALID"


def test_chamfer_angle_is_structurally_45_and_domain_checked():
    value = requirements(base_pad(), {
        "id": "chamfer1", "operation": "chamfer", "dependsOn": ["base"], "length": "T",
        "angle": 30, "edges": {"type": "linear", "direction": [1, 0, 0], "through": [0, 0, 0]},
    })
    assert structure_problems(value)
    with pytest.raises(FeatureError):
        compile_features(value, None, require_evidence=False)


# -- shadow transparency and frozen plans ------------------------------------


def test_shadow_records_differences_without_changing_the_error():
    value = valid_by_batch()["pad_pocket"]
    value["features"][0]["through"] = True
    with pytest.raises(FeatureError) as first:
        compile_features(value, None, require_evidence=False)
    assert feature_shadow_differences()
    assert feature_shadow_differences()[0]["kind"] == "feature_structure"
    # Same domain error as without shadow.
    assert first.value.code == "FIELD_UNSUPPORTED"


def test_frozen_feature_plan_hash_is_unchanged():
    oracles = json.loads((PLANS / "hash-oracles.json").read_text(encoding="utf-8"))
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": ["p1:u"],
           "parameters": [{"name": n, "value": v, "evidence": ["p1:" + n]}
                          for n, v in [("L", 40), ("W", 30), ("T", 8), ("R", 3)]],
           "features": [
               {"id": "base", "operation": "pad", "length": "T",
                "sketch": {"plane": "xy",
                           "profiles": [{"type": "rectangle", "width": "L", "height": "W"}]}},
               {"id": "boss", "operation": "pad", "dependsOn": ["base"], "length": "T / 2",
                "sketch": {"plane": "xy", "offset": "T",
                           "profiles": [{"type": "circle", "x": "L / 2", "y": "W / 2", "radius": "R * 2"}]}},
               {"id": "bore", "operation": "pocket", "dependsOn": ["boss"], "length": "T * 2",
                "direction": "reverse", "sketch": {"plane": "xy", "offset": "T * 1.5",
                    "profiles": [{"type": "circle", "x": "L / 2", "y": "W / 2", "radius": "R"}]}}]}
    manifest = {"source": {"sha256": "test"}, "pages": [{"page": 1, "spans": [
        {"id": "p1:u", "text": "UNITS mm"},
        *[{"id": "p1:" + p["name"], "text": str(p["value"])} for p in req["parameters"]]]}]}
    plan = compile_features(req, manifest)
    assert plan["planId"] == oracles["feature_plan"]["expected_digest"]


def test_pre_execution_recompile_rejects_tampered_dimensions():
    plan = compile_features(valid_by_batch()["pad_pocket"], None, require_evidence=False)
    plan["features"][0]["extent"]["value"] += 1
    with pytest.raises(BuildError):
        validate_compiled_plan(plan)
