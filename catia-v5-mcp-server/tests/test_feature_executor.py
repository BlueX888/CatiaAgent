import copy
import math

import pytest

from test_feature_model import design, polygon_design, wire_design
from catia_mcp.drawing.builder import BuildError
from catia_mcp.drawing.feature_model import compile_features
from catia_mcp.drawing.feature_executor import (
    FeatureExecutor, material_effect, validate_compiled_plan)


@pytest.mark.parametrize("operation,before,after,code", [
    ("pad", 100, 100, "FEATURE_NO_EFFECT"),
    ("pocket", 100, 101, "WRONG_GEOMETRY_EFFECT"),
    ("shaft", 100, 99, "WRONG_GEOMETRY_EFFECT"),
    ("groove", 100, math.nan, "MEASUREMENT_FAILED"),
    ("pad", 0, math.inf, "MEASUREMENT_FAILED"),
    ("pocket", 100, 0, "MEASUREMENT_FAILED"),
])
def test_bad_material_effect_stops_build(operation, before, after, code):
    with pytest.raises(BuildError, match=code):
        material_effect(operation, before, after)


def test_add_and_remove_are_measured():
    assert material_effect("pad", 0, 10)["deltaMm3"] == 10
    assert material_effect("pocket", 10, 8)["deltaMm3"] == -2


def test_executor_recomputes_plan_before_com(design):
    plan = compile_features(*design)
    assert len(validate_compiled_plan(plan)["features"]) == 3
    corrupt = copy.deepcopy(plan)
    corrupt["features"][0]["extent"]["value"] += 1
    with pytest.raises(BuildError, match="do not match"):
        validate_compiled_plan(corrupt)
    corrupt = copy.deepcopy(plan)
    corrupt["features"][0]["extent"]["formula"] = "__import__('os')"
    with pytest.raises(BuildError):
        validate_compiled_plan(corrupt)


def test_zero_offset_expression_survives_plan_recompilation(design):
    req, manifest = design
    req["features"][1]["sketch"]["profiles"][0]["x"] = "0 - L / 2"
    plan = compile_features(req, manifest)
    coordinate = plan["features"][1]["sketch"]["profiles"][0]["x"]
    assert coordinate["value"] == -20
    assert "0mm" not in coordinate["formula"]
    checked = validate_compiled_plan(plan)
    assert checked["features"][1]["sketch"]["profiles"][0]["x"]["value"] == -20


def test_polygon_coordinate_plan_is_recomputed(polygon_design):
    plan = compile_features(*polygon_design)
    assert validate_compiled_plan(plan)["features"][0]["sketch"]["profiles"][0]["type"] == "polygon"
    plan["features"][0]["sketch"]["profiles"][0]["points"][0]["x"]["value"] += 1
    with pytest.raises(BuildError, match="do not match"):
        validate_compiled_plan(plan)


def test_arc_plan_is_recomputed(wire_design):
    plan = compile_features(*wire_design)
    assert validate_compiled_plan(plan)["features"][1]["sketch"]["profiles"][0]["type"] == "wire"
    plan["features"][1]["sketch"]["profiles"][0]["segments"][1]["radius"]["value"] += 1
    with pytest.raises(BuildError, match="do not match"):
        validate_compiled_plan(plan)


def _circular(index, radius, centre, axis=(0, 0, 1)):
    return {"index": index, "kind": "circular", "length": 2 * 3.141592653589793 * radius,
            "radius": radius, "centre": list(centre), "axis": list(axis)}


def _linear(index, midpoint, direction=(1, 0, 0)):
    return {"index": index, "kind": "linear", "length": 10.0,
            "midpoint": list(midpoint), "direction": list(direction)}


def test_coincident_edge_references_group_for_finishing():
    # A rim cut flush with a face is exposed as two topology cells.
    a = _circular(2, 5.0, (30, 20, 18))
    b = _circular(7, 5.0, (30, 20, 18))
    other = _circular(9, 5.0, (30, 20, 0))
    groups = FeatureExecutor._edge_groups([a, b, other])
    assert sorted(len(group) for group in groups) == [1, 2]


def test_opposite_axis_represents_the_same_circular_edge():
    a = _circular(1, 5.0, (0, 0, 10), axis=(0, 0, 1))
    b = _circular(2, 5.0, (0, 0, 10), axis=(0, 0, -1))
    assert len(FeatureExecutor._edge_groups([a, b])) == 1


def test_linear_edges_group_by_midpoint():
    a = _linear(1, (0, -15, 8))
    b = _linear(2, (0, -15, 8))
    c = _linear(3, (0, 15, 8))
    groups = FeatureExecutor._edge_groups([a, b, c])
    assert sorted(len(group) for group in groups) == [1, 2]


def test_two_arcs_of_one_circle_are_not_merged():
    half = math.pi * 5.0
    a = {"index": 1, "kind": "circular", "length": half, "radius": 5.0,
         "centre": [0, 0, 10], "axis": [0, 0, 1]}
    b = {"index": 2, "kind": "circular", "length": half, "radius": 5.0,
         "centre": [0, 0, 10], "axis": [0, 0, 1]}
    assert len(FeatureExecutor._edge_groups([a, b])) == 2


def test_literal_coordinate_plan_round_trips():
    req = {"schema": "catia-feature-requirements/1", "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "T", "value": 4, "evidence": []}],
           "features": [{"id": "base", "operation": "pad", "length": "T",
                         "sketch": {"plane": "xy", "offset": 0, "profiles": [
                             {"type": "polygon", "points": [
                                 {"x": -10, "y": -5}, {"x": 10, "y": -5},
                                 {"x": 10, "y": 5}, {"x": -10, "y": 5}]}]}}]}
    plan = compile_features(req, None, require_evidence=False)
    checked = validate_compiled_plan(plan)
    assert checked["planId"] == plan["planId"]
    assert checked["features"][0]["sketch"]["profiles"][0]["points"][0]["x"]["value"] == -10
