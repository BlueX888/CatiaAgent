import copy
import math
import sys

import pytest

bd = pytest.importorskip("build123d")
pytest.importorskip("cadgen")
from test_feature_model import design, polygon_design, wire_design, sector_design
from catia_mcp.feature_geometry import evaluate_features


def test_concave_polygon_and_blind_triangle_cut(polygon_design):
    req, _ = polygon_design
    result = evaluate_features({"requirements": req})
    assert result["expected"]["volumeMm3"] == pytest.approx(7050, abs=.001)
    assert result["expected"]["bbox"]["min"] == pytest.approx([0, -20, -15])
    assert result["expected"]["bbox"]["max"] == pytest.approx([8, 20, 15])


def test_line_arc_slot_has_exact_curved_material(wire_design):
    req, _ = wire_design
    result = evaluate_features({"requirements": req})
    assert result["expected"]["volumeMm3"] == pytest.approx(9600 - (20 * 6 + math.pi * 9) * 8, abs=.001)


@pytest.mark.parametrize("clockwise,large,area_factor", [
    (False, False, math.pi / 4), (True, False, 1 - math.pi / 4),
    (False, True, 1 + 3 * math.pi / 4), (True, True, 3 * math.pi / 4),
])
def test_arc_direction_and_major_sweep(clockwise, large, area_factor):
    req, _ = sector_design(clockwise, large)
    result = evaluate_features({"requirements": req})
    assert result["expected"]["volumeMm3"] == pytest.approx(100 * area_factor, abs=.001)


@pytest.mark.parametrize("plane,dimensions", [("xy", [40, 30, 12]), ("yz", [12, 40, 30]), ("zx", [30, 12, 40])])
def test_composed_geometry_and_coordinates(design, plane, dimensions):
    req, _ = design
    for f in req["features"]:
        f["sketch"]["plane"] = plane
    result = evaluate_features({"requirements": req})
    assert result["expected"]["volumeMm3"] == pytest.approx(9600 + 36 * math.pi, abs=.001)
    assert result["expected"]["bbox"]["max"] == pytest.approx(dimensions)
    assert result["expected"]["solidCount"] == 1


def test_declared_through_cut_covers_all_material(design):
    req, _ = design
    bore = req["features"][2]
    bore["through"] = True
    assert evaluate_features({"requirements": req})["verdict"] == "PASS"


def test_each_disjoint_cutting_profile_must_remove_material():
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "parameters": [{"name": n, "value": v} for n, v in (("L", 40), ("W", 30), ("T", 8), ("R", 2))],
           "features": [
               {"id": "base", "operation": "pad", "length": "T",
                "sketch": {"plane": "xy", "profiles": [{"type": "rectangle", "width": "L", "height": "W"}]}},
               {"id": "holes", "operation": "pocket", "dependsOn": ["base"], "length": "T",
                "sketch": {"plane": "xy", "profiles": [
                    {"type": "circle", "x": 10, "y": 15, "radius": "R"},
                    {"type": "circle", "x": 50, "y": 15, "radius": "R"}]}}]}
    with pytest.raises(ValueError, match="holes.*PROFILE_NO_EFFECT"):
        evaluate_features({"requirements": req})
    req["features"][1]["sketch"]["profiles"][1]["x"] = 30
    assert evaluate_features({"requirements": req})["verdict"] == "PASS"
    req["features"][1]["sketch"]["profiles"] = [
        {"type": "circle", "x": x, "y": 15, "radius": radius}
        for x in (10, 30) for radius in ("R * 2", "R")]
    req["features"][1]["length"] = "T / 2"
    assert evaluate_features({"requirements": req})["verdict"] == "PASS"


def test_each_disjoint_groove_profile_must_remove_material():
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "parameters": [{"name": "R", "value": 5}, {"name": "L", "value": 10}],
           "features": [
               {"id": "shaft", "operation": "shaft", "angle": 360,
                "sketch": {"plane": "xy", "profiles": [{"type": "rectangle", "width": "R * 2", "height": "L"}]}},
               {"id": "grooves", "operation": "groove", "dependsOn": ["shaft"], "angle": 360,
                "sketch": {"plane": "xy", "profiles": [
                    {"type": "circle", "x": 5, "y": 3, "radius": "R / 5"},
                    {"type": "circle", "x": 20, "y": 7, "radius": "R / 5"}]}}]}
    with pytest.raises(ValueError, match="grooves.*PROFILE_NO_EFFECT"):
        evaluate_features({"requirements": req})
    req["features"][1]["sketch"]["profiles"][1]["x"] = 5
    assert evaluate_features({"requirements": req})["verdict"] == "PASS"


@pytest.mark.parametrize("plane", ["xy", "yz", "zx"])
@pytest.mark.parametrize("direction", ["normal", "reverse"])
def test_through_cut_tracks_axis_and_driving_thickness(design, plane, direction):
    req, _ = design
    req["features"] = [req["features"][0], req["features"][2]]
    req["features"][1]["dependsOn"] = ["base"]
    for feature in req["features"]:
        feature["sketch"]["plane"] = plane
    bore = req["features"][1]
    bore.update(through=True, length="T", direction=direction)
    bore["sketch"]["offset"] = 0 if direction == "normal" else "T"
    for thickness in (8, 11):
        next(p for p in req["parameters"] if p["name"] == "T")["value"] = thickness
        assert evaluate_features({"requirements": req})["verdict"] == "PASS"


def test_through_cut_on_inclined_sketch_checks_material_below_profile(design):
    req, _ = design
    req["features"] = [req["features"][0], req["features"][2]]
    bore = req["features"][1]
    bore.update(dependsOn=["base"], through=True, length="T * 8", direction="normal")
    bore["sketch"].update(plane={"normal": [0, 0.6, 0.8], "xDirection": [1, 0, 0]}, offset="-T * 2")
    bore["sketch"]["profiles"][0].update(x="L / 2", y="W * 0.4")
    assert evaluate_features({"requirements": req})["verdict"] == "PASS"
    bore["sketch"]["offset"] = "T * 1.5"
    with pytest.raises(ValueError, match="THROUGH_CUT_INCOMPLETE"):
        evaluate_features({"requirements": req})


def test_incomplete_through_cut_fails_before_catia_execution(design, tmp_path, monkeypatch):
    from catia_mcp.drawing import feature_workflow
    from catia_mcp.drawing.plan import compile_build_plan
    from catia_mcp.drawing.requirements import validate_requirements

    req, manifest = design
    bore = req["features"][2]
    bore.update(through=True, length="T")
    bore["sketch"]["offset"] = "T"
    plan = compile_build_plan(validate_requirements(req, manifest)["normalized"])
    class NoCatiaExecutor:
        def __init__(self, connection): pass
        def close(self): pass
        def execute(self, plan): raise AssertionError("CATIA must not be touched")
    monkeypatch.setattr(feature_workflow, "FeatureExecutor", NoCatiaExecutor)
    monkeypatch.setenv("CATIA_CAD_SUPPORT_PYTHON", sys.executable)
    target = tmp_path / "part.CATPart"
    result = feature_workflow.build_features(None, req, plan, target, [], {})
    assert result["ok"] is False
    assert result["effects"]["modified"] is False
    assert "THROUGH_CUT_INCOMPLETE" in result["message"]
    assert not target.exists()
    # The hole starts inside the boss and cuts only toward the bottom. The
    # existing geometry/effect checks accept it, but material remains above.
    bore["sketch"]["offset"] = "T"
    bore["length"] = "T"
    with pytest.raises(ValueError, match="THROUGH_CUT_INCOMPLETE"):
        evaluate_features({"requirements": req})
    del bore["through"]
    assert evaluate_features({"requirements": req})["verdict"] == "PASS"


@pytest.mark.parametrize("failure", ["disconnected", "no_effect", "overlap"])
def test_bad_feature_is_identified(design, failure):
    req, _ = design
    if failure == "disconnected":
        req["features"][1]["sketch"]["offset"] = "T * 2"
        expected = "boss.*SINGLE_SOLID_REQUIRED"
    elif failure == "no_effect":
        req["features"][2]["sketch"]["offset"] = 0
        expected = "bore.*FEATURE_NO_EFFECT"
    else:
        profile = copy.deepcopy(req["features"][0]["sketch"]["profiles"][0])
        profile["x"] = "L / 2"
        req["features"][0]["sketch"]["profiles"].append(profile)
        expected = "base.*PROFILE_INTERSECTION"
    with pytest.raises(ValueError, match=expected):
        evaluate_features({"requirements": req})


def test_equal_volume_wrong_bore_fails_material_comparison(design, tmp_path):
    req, _ = design
    block = bd.Box(40, 30, 8, align=(bd.Align.MIN, bd.Align.MIN, bd.Align.MIN))
    boss = bd.Pos(20, 15, 8) * bd.Cylinder(6, 4, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    correct_hole = bd.Pos(20, 15, 0) * bd.Cylinder(3, 12, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    wrong_hole = bd.Pos(22, 15, 0) * bd.Cylinder(3, 12, align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN))
    for hole, verdict in [(correct_hole, "PASS"), (wrong_hole, "FAIL")]:
        actual = tmp_path / f"{verdict}.step"
        bd.export_step(block + boss - hole, str(actual))
        result = evaluate_features({"requirements": req, "actual": str(actual)})
        assert result["verdict"] == verdict


def test_nested_profiles_form_an_annulus():
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "parameters": [{"name": "R", "value": 5}, {"name": "T", "value": 10}],
           "features": [{"id": "ring", "operation": "pad", "length": "T",
                         "sketch": {"plane": "xy", "profiles": [
                             {"type": "circle", "radius": "R * 2"}, {"type": "circle", "radius": "R"}]}}]}
    result = evaluate_features({"requirements": req})
    assert result["expected"]["volumeMm3"] == pytest.approx(math.pi * 75 * 10)


def test_shaft_and_groove_evaluation():
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "parameters": [{"name": "R", "value": 5}, {"name": "T", "value": 10},
                          {"name": "A", "value": 360, "type": "angle"}],
           "features": [
               {"id": "shaft", "operation": "shaft", "angle": "A",
                "sketch": {"plane": "yz", "profiles": [{"type": "rectangle", "width": "R * 2", "height": "T"}]}},
               {"id": "groove", "operation": "groove", "angle": "A", "dependsOn": ["shaft"],
                "sketch": {"plane": "yz", "profiles": [{"type": "rectangle", "x": "R", "y": "T / 4", "width": "R", "height": "T / 2"}]}}]}
    result = evaluate_features({"requirements": req})
    assert result["expected"]["volumeMm3"] == pytest.approx(math.pi * (100 * 10 - 75 * 5))
    req["features"][0]["sketch"]["profiles"][0]["x"] = "-R"
    with pytest.raises(ValueError, match="REVOLUTION_AXIS_SIDE"):
        evaluate_features({"requirements": req})


def test_literal_full_revolution_builds_expected_geometry():
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "parameters": [{"name": "R", "value": 5}, {"name": "L", "value": 10}],
           "features": [{"id": "body", "operation": "shaft", "angle": 360,
               "sketch": {"plane": "xy", "profiles": [{"type": "rectangle", "width": "R", "height": "L"}]}}]}
    result = evaluate_features({"requirements": req})
    assert result["verdict"] == "PASS"
    assert result["expected"]["volumeMm3"] == pytest.approx(math.pi * 25 * 10)


def test_angle_parameter_drives_sloped_profile_geometry():
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "parameters": [{"name": "L", "value": 20}, {"name": "H", "value": 10},
                          {"name": "T", "value": 3}, {"name": "A", "type": "angle", "value": 30}],
           "features": [{"id": "body", "operation": "pad", "length": "T", "sketch": {"plane": "xy",
               "profiles": [{"type": "polygon", "points": [{"x": 0, "y": 0}, {"x": "L", "y": 0},
                   {"x": "L", "y": "H"}, {"x": 0, "y": "H - T * tan(A)"}]}]}}]}
    result = evaluate_features({"requirements": req})
    expected_area = 20 * (10 - 3 * math.tan(math.radians(30)) / 2)
    assert result["expected"]["volumeMm3"] == pytest.approx(expected_area * 3, abs=.001)


def finish_requirements():
    return {"schema": "catia-feature-requirements/1", "unit": "mm",
            "parameters": [{"name": "L", "value": 40}, {"name": "W", "value": 30},
                           {"name": "T", "value": 8}, {"name": "DH", "value": 6},
                           {"name": "HX", "value": 10}, {"name": "FR", "value": 2},
                           {"name": "CL", "value": 2}],
            "features": [
                {"id": "base", "operation": "pad", "length": "T",
                 "sketch": {"plane": "xy", "profiles": [
                     {"type": "rectangle", "x": "-L / 2", "y": "-W / 2", "width": "L", "height": "W"}]}},
                {"id": "hole", "operation": "pocket", "length": "T * 3", "dependsOn": ["base"],
                 "sketch": {"plane": "xy", "profiles": [
                     {"type": "circle", "x": "HX", "y": 0, "radius": "DH / 2"}]}},
                {"id": "round", "operation": "fillet", "radius": "FR", "dependsOn": ["hole"],
                 "edges": {"type": "circular", "radius": "DH / 2", "centre": ["HX", 0, "T"],
                           "axis": [0, 0, 1]}},
                {"id": "break", "operation": "chamfer", "length": "CL", "angle": 45,
                 "dependsOn": ["round"],
                 "edges": {"type": "linear", "direction": [1, 0, 0], "through": [0, "-W / 2", "T"]}},
            ]}


def test_fillet_and_chamfer_remove_exact_material():
    result = evaluate_features({"requirements": finish_requirements()})
    volumes = [step["afterMm3"] for step in result["steps"]]
    assert volumes[1] == pytest.approx(9600 - math.pi * 9 * 8, abs=.001)
    assert volumes[2] == pytest.approx(9355.215247, abs=.001)
    assert volumes[3] == pytest.approx(9275.215247, abs=.001)


def test_edge_selector_rejects_zero_matches():
    req = finish_requirements()
    req["features"][2]["edges"] = {
        "type": "circular", "radius": "DH / 2", "centre": ["HX", 0, "T * 5"], "axis": [0, 0, 1]}
    with pytest.raises(ValueError, match="matched 0 edges"):
        evaluate_features({"requirements": req})


def test_edge_selector_rejects_two_collinear_edges():
    # A notch splits the top edge into two collinear segments; the selector
    # must refuse rather than pick one.
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "parameters": [{"name": "L", "value": 40}, {"name": "W", "value": 30},
                          {"name": "T", "value": 8}, {"name": "N", "value": 5},
                          {"name": "FR", "value": 1}],
           "features": [
               {"id": "base", "operation": "pad", "length": "T",
                "sketch": {"plane": "xy", "profiles": [
                    {"type": "rectangle", "x": "-L / 2", "y": "-W / 2", "width": "L", "height": "W"}]}},
               {"id": "notch", "operation": "pocket", "length": "T * 3", "dependsOn": ["base"],
                "sketch": {"plane": "xy", "profiles": [
                    {"type": "rectangle", "x": "-N", "y": "-W", "width": "N * 2", "height": "W"}]}},
               {"id": "round", "operation": "fillet", "radius": "FR", "dependsOn": ["notch"],
                "edges": {"type": "linear", "direction": [1, 0, 0], "through": [0, "-W / 2", "T"]}},
           ]}
    with pytest.raises(ValueError, match="matched 2 edges"):
        evaluate_features({"requirements": req})
