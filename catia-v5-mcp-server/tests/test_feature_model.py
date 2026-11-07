"""Compiler boundary tests; these do not claim CATIA geometry correctness."""
import copy

import pytest

from catia_mcp.drawing.feature_model import SCHEMA, compile_features, validate_features, expression, FeatureError, arc_geometry


def test_semicircle_does_not_amplify_coordinate_roundoff():
    start, end, radius = (2670.58, 406.0), (2670.58, 854.8399999999999), 224.42
    for clockwise in (False, True):
        center, _, sweep = arc_geometry(start, end, radius, clockwise)
        assert center == pytest.approx((2670.58, 630.42), abs=1e-12)
        assert abs(sweep) == pytest.approx(3.141592653589793)
    # A resolvable near-semicircle retains its distinct centre and arc choice.
    near_center, _, _ = arc_geometry(start, (start[0], end[1] - 1e-7), radius)
    assert abs(near_center[0] - start[0]) > 1e-3
    # A huge coordinate shared by both endpoints must not erase a genuine arc.
    translated_center, _, _ = arc_geometry((1e12, 0.0), (1e12, 199.9999), 100.0)
    assert abs(translated_center[0] - 1e12) > .01
    # A tiny, nonzero component on the translated axis has little effect on chord length.
    start, end = (1e12, 0.0), (1e12 + .001, 199.9999)
    oblique_center, _, _ = arc_geometry(start, end, 100.0)
    assert abs(oblique_center[0] - (start[0] + end[0]) / 2) > .01


def test_quoted_zero_is_a_valid_zero_coordinate():
    for text in ("0", "-0", "+0.00", ".0"):
        assert expression(text, {}, "length")["value"] == 0
        assert expression(text, {}, "length")["formula"] == "0mm"
    with pytest.raises(FeatureError):
        expression("0.01", {}, "length")


@pytest.fixture
def design():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": ["p1:u"],
           "parameters": [{"name": n, "value": v, "evidence": ["p1:" + n]}
                          for n, v in [("L", 40), ("W", 30), ("T", 8), ("R", 3)]],
           "features": [
               {"id": "base", "operation": "pad", "length": "T",
                "sketch": {"plane": "xy", "profiles": [{"type": "rectangle", "width": "L", "height": "W"}]}},
               {"id": "boss", "operation": "pad", "dependsOn": ["base"], "length": "T / 2",
                "sketch": {"plane": "xy", "offset": "T", "profiles": [{"type": "circle", "x": "L / 2", "y": "W / 2", "radius": "R * 2"}]}},
               {"id": "bore", "operation": "pocket", "dependsOn": ["boss"], "length": "T * 2", "direction": "reverse",
                "sketch": {"plane": "xy", "offset": "T * 1.5", "profiles": [{"type": "circle", "x": "L / 2", "y": "W / 2", "radius": "R"}]}}]}
    manifest = {"source": {"sha256": "test"}, "pages": [{"page": 1, "spans": [
        {"id": "p1:u", "text": "UNITS mm"}, *[{"id": "p1:" + p["name"], "text": str(p["value"])} for p in req["parameters"]]]}]}
    return req, manifest


@pytest.fixture
def polygon_design():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": ["p1:u"],
           "parameters": [{"name": n, "value": v, "evidence": ["p1:" + n]}
                          for n, v in [("L", 40), ("W", 30), ("T", 8)]],
           "features": [
               {"id": "base", "operation": "pad", "length": "T",
                "sketch": {"plane": "yz", "profiles": [{"type": "polygon", "points": [
                    {"x": "-L / 2", "y": "-W / 2"}, {"x": "L / 2", "y": "-W / 2"},
                    {"x": "L / 2", "y": 0}, {"x": 0, "y": 0},
                    {"x": 0, "y": "W / 2"}, {"x": "-L / 2", "y": "W / 2"}]}]}},
               {"id": "triangle", "operation": "pocket", "length": "T / 2", "direction": "reverse", "dependsOn": ["base"],
                "sketch": {"plane": "yz", "offset": "T", "profiles": [{"type": "polygon", "points": [
                    {"x": "-L * 3 / 8", "y": "-W * 3 / 8"},
                    {"x": "-L / 8", "y": "-W * 3 / 8"},
                    {"x": "-L * 3 / 8", "y": "-W / 8"}]}]}}]}
    manifest = {"pages": [{"spans": [{"id": "p1:u", "text": "UNITS mm"},
        *[{"id": "p1:" + p["name"], "text": str(p["value"])} for p in req["parameters"]]]}]}
    return req, manifest


def test_polygon_points_keep_formulas_and_citations(polygon_design):
    plan = compile_features(*polygon_design)
    polygon = plan["features"][0]["sketch"]["profiles"][0]
    assert polygon["points"][0]["x"]["value"] == -20
    assert polygon["points"][0]["x"]["parameters"] == ["L"]
    assert plan["features"][1]["evidence"] == ["p1:L", "p1:T", "p1:W"]


def test_through_declaration_is_only_valid_for_pockets(design):
    from catia_mcp.drawing.feature_executor import validate_compiled_plan
    req, manifest = design
    req["features"][-1]["through"] = True
    plan = compile_features(req, manifest)
    assert plan["features"][-1]["through"] is True
    assert validate_compiled_plan(plan)["features"][-1]["through"] is True
    req["features"][0]["through"] = True
    with pytest.raises(FeatureError, match="through"):
        compile_features(req, manifest)


@pytest.mark.parametrize("angle", [360, "360", "360.0"])
def test_full_revolution_is_a_literal_modeling_operation(angle):
    from jsonschema import validate as validate_schema
    from catia_mcp.drawing.feature_schema import FEATURE_REQUIREMENTS_JSON
    from catia_mcp.drawing.feature_executor import validate_compiled_plan
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "R", "value": 5, "evidence": []}, {"name": "L", "value": 10, "evidence": []}],
           "features": [{"id": "body", "operation": "shaft", "angle": angle,
                         "sketch": {"plane": "xy", "profiles": [{"type": "rectangle", "width": "R", "height": "L"}]}}]}
    validate_schema(req, FEATURE_REQUIREMENTS_JSON)
    plan = compile_features(req, None, require_evidence=False)
    assert plan["features"][0]["extent"]["value"] == 360
    assert plan["features"][0]["extent"]["parameters"] == []
    assert validate_compiled_plan(plan)["features"][0]["extent"]["value"] == 360


def test_partial_revolution_still_requires_an_evidenced_angle():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "R", "value": 5}, {"name": "L", "value": 10}],
           "features": [{"id": "body", "operation": "shaft", "angle": "90",
                         "sketch": {"plane": "xy", "profiles": [{"type": "rectangle", "width": "R", "height": "L"}]}}]}
    with pytest.raises(FeatureError, match="named angle"):
        compile_features(req, None, require_evidence=False)


def test_full_revolution_coupled_to_an_angle_parameter_warns():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "R", "value": 5}, {"name": "L", "value": 10},
                          {"name": "A", "type": "angle", "value": 30}],
           "features": [{"id": "body", "operation": "shaft", "angle": "A * 12",
                         "sketch": {"plane": "xy", "profiles": [{"type": "rectangle", "width": "R", "height": "L"}]}}]}
    result = validate_features(req, None, require_evidence=False)
    assert result["decision"] == "ready"
    assert any(w["code"] == "FULL_REVOLUTION_COUPLED" for w in result["warnings"])
    req["features"][0]["angle"] = 360
    req["parameters"].pop()
    assert validate_features(req, None, require_evidence=False)["warnings"] == []


def test_angle_trigonometry_keeps_dimensions_and_parameter_dependencies():
    import math
    params = {"CH": {"type": "length", "value": 10}, "A": {"type": "angle", "value": 30}}
    result = expression("CH * tan(A)", params)
    assert result["value"] == pytest.approx(10 * math.tan(math.radians(30)))
    assert result["parameters"] == ["A", "CH"]
    assert "tan(A)" in result["formula"]
    assert expression("CH * sin(A)", params)["value"] == pytest.approx(5)
    assert expression("CH * cos(A)", params)["value"] == pytest.approx(10 * math.cos(math.radians(30)))
    with pytest.raises(FeatureError, match="angle"):
        expression("CH * tan(CH)", params)
    params["A"]["value"] = 90
    with pytest.raises(FeatureError, match="singular"):
        expression("CH * tan(A)", params)
    with pytest.raises(FeatureError):
        expression("CH * __import__(A)", params)


@pytest.fixture
def wire_design(design):
    req, manifest = design
    req["features"] = [req["features"][0], {
        "id": "slot", "operation": "pocket", "length": "T", "direction": "reverse", "dependsOn": ["base"],
        "sketch": {"plane": "xy", "offset": "T", "profiles": [{"type": "wire",
            "start": {"x": "L / 4", "y": "W / 2 - R"},
            "segments": [
                {"type": "line", "end": {"x": "L * 3 / 4", "y": "W / 2 - R"}},
                {"type": "arc", "end": {"x": "L * 3 / 4", "y": "W / 2 + R"}, "radius": "R"},
                {"type": "line", "end": {"x": "L / 4", "y": "W / 2 + R"}},
                {"type": "arc", "end": {"x": "L / 4", "y": "W / 2 - R"}, "radius": "R"}
            ]}]}}]
    return req, manifest


def test_wire_compiles_cited_arcs(wire_design):
    plan = compile_features(*wire_design)
    wire = plan["features"][1]["sketch"]["profiles"][0]
    assert wire["segments"][1]["radius"]["value"] == 3
    assert wire["segments"][1]["clockwise"] is False
    assert plan["features"][1]["evidence"] == ["p1:L", "p1:R", "p1:T", "p1:W"]


def sector_design(clockwise=False, large=False):
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": ["p1:u"],
           "parameters": [{"name": "R", "value": 5, "evidence": ["p1:R"]},
                          {"name": "T", "value": 4, "evidence": ["p1:T"]}],
           "features": [{"id": "sector", "operation": "pad", "length": "T",
               "sketch": {"plane": "xy", "profiles": [{"type": "wire", "start": {"x": 0, "y": 0},
                   "segments": [{"type": "line", "end": {"x": "R", "y": 0}},
                                {"type": "arc", "end": {"x": 0, "y": "R"}, "radius": "R", "clockwise": clockwise, "largeArc": large},
                                {"type": "line", "end": {"x": 0, "y": 0}}]}]}}]}
    return req, {"pages": [{"spans": [{"id": "p1:u", "text": "mm"}, {"id": "p1:R", "text": "R5"}, {"id": "p1:T", "text": "4"}]}]}


@pytest.mark.parametrize("mutation", ["open", "small_radius", "zero_edge", "extra_field", "bad_direction"])
def test_bad_wire_rejected(wire_design, mutation):
    req, manifest = wire_design
    wire = req["features"][1]["sketch"]["profiles"][0]
    if mutation == "open": wire["segments"][-1]["end"]["x"] = "L / 8"
    if mutation == "small_radius": wire["segments"][1]["radius"] = "R / 2"
    if mutation == "zero_edge": wire["segments"][0]["end"] = copy.deepcopy(wire["start"])
    if mutation == "extra_field": wire["segments"][0]["radius"] = "R"
    if mutation == "bad_direction": wire["segments"][1]["clockwise"] = "yes"
    with pytest.raises(FeatureError): compile_features(req, manifest)


@pytest.mark.parametrize("points", [[], [None] * 3,
    [{"x": 0, "y": 0}] * 3,
    [{"x": 0, "y": 0}, {"x": "L", "y": "W"}, {"x": 0, "y": "W"}, {"x": "L", "y": 0}],
    [{"x": 0, "y": 0}, {"x": "L", "y": "W"}, {"x": 0, "y": "W"}, {"x": "L/2", "y": 0}],
    [{"x": 0, "y": 0}, {"x": "L", "y": 0}, {"x": "L/2", "y": 0}, {"x": 0, "y": "W"}],
])
def test_invalid_polygon_rejected(polygon_design, points):
    req, manifest = polygon_design
    req["features"][0]["sketch"]["profiles"][0]["points"] = points
    with pytest.raises(FeatureError):
        compile_features(req, manifest)


def test_composition_preserves_dimensions_dependencies_and_evidence(design):
    req, manifest = design
    before = copy.deepcopy(req)
    plan = compile_features(req, manifest)
    assert plan["features"][1]["sketch"]["offset"]["value"] == 8
    assert plan["features"][2]["extent"]["value"] == 16
    assert plan["features"][2]["dependsOn"] == ["boss"]
    assert plan["features"][2]["evidence"] == ["p1:L", "p1:R", "p1:T", "p1:W"]
    assert compile_features(req, manifest)["planId"] == plan["planId"]
    assert req == before
    req["unit"] = "inch"
    manifest["pages"][0]["spans"][0]["text"] = "UNITS inches"
    assert compile_features(req, manifest)["features"][0]["extent"]["value"] == pytest.approx(203.2)


@pytest.mark.parametrize("text,unit,accepted", [
    ("ALL DIMENSIONS ARE IN MILLIMITERS", "mm", True),
    ("DIMENSIONSAREINMILLIMITERS", "mm", True),
    ("NODIMENSIONSAREINMILLIMITERS", "mm", False),
    ("ALL DIMENSIONS ARE IN MILLIMITER", "inch", False),
    ("ALL DIMENSIONS ARE IN MILLIMETERS", "inch", False),
    ("ALL DIMENSIONS ARE IN", "inch", False),
    ("UNITS: IN", "inch", True),
    ("UNITS: INCHES", "inch", True),
    ("UNITS mm AND inches", "mm", False),
    ("UNITS mm AND inches", "inch", False),
])
def test_unit_evidence_does_not_confuse_preposition_and_units(design, text, unit, accepted):
    req, manifest = design
    req["unit"] = unit
    manifest["pages"][0]["spans"][0]["text"] = text
    result = validate_features(req, manifest)
    mismatch = any(p.get("code") == "EVIDENCE_VALUE_MISMATCH" for p in result["problems"])
    assert mismatch is not accepted, result


@pytest.mark.parametrize("path,value", [
    ((), []), (("parameters", 0), None), (("features", 0), None),
    (("features", 0, "sketch"), []), (("features", 0, "sketch", "profiles", 0), None),
    (("unitEvidence",), "p1:u"), (("parameters", 0, "evidence"), [{}]),
    (("features", 1, "dependsOn"), ["base", "base"]),
    (("features", 0, "dependsOn"), ["bore"]),
    (("features", 1, "dependsOn"), ["boss"]),
    (("features", 0, "angle"), "T"), (("features", 0, "sketch", "typo"), 1),
    (("parameters", 0, "value"), 41), (("parameters", 0, "value"), True),
    (("parameters", 0, "name"), []), (("features", 0, "operation"), []),
    (("features", 0, "length"), "__import__('os').system('echo bad')"),
    (("features", 0, "length"), "L / (T - T)"),
    (("features", 0, "length"), "L * T"),
    (("features", 0, "sketch", "profiles", 0, "width"), "-L"),
])
def test_malformed_or_unsupported_input_is_never_ready(design, path, value):
    req, manifest = design
    if not path:
        req = value
    else:
        target = req
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value
    result = validate_features(req, manifest)
    assert result["decision"] != "ready"
    assert result["normalized"] is None


def test_dimension_mixing_rejected():
    parameters = {"L": {"value": 10, "type": "length"}, "A": {"value": 90, "type": "angle"}}
    with pytest.raises(FeatureError, match="matching dimensions"):
        expression("L + A", parameters)
    with pytest.raises(FeatureError, match="matching dimensions"):
        expression("1 - L", parameters)


def test_literal_zero_adopts_the_other_operand_unit():
    parameters = {"L": {"value": 10, "type": "length"}, "A": {"value": 90, "type": "angle"}}
    assert expression("0 - L", parameters)["value"] == -10
    assert expression("0 - L", parameters)["formula"] == "-(L)"
    assert expression("L + 0", parameters)["formula"] == "L"
    assert expression("0 - A", parameters, "angle")["formula"] == "-(A)"


def test_duplicate_evidence_ids_are_not_silently_rebound(design):
    req, manifest = design
    manifest["pages"][0]["spans"].append({"id": "p1:L", "text": "40"})
    assert validate_features(req, manifest)["decision"] == "invalid"


@pytest.mark.parametrize("manifest", [[], {"pages": None}, {"pages": [None]},
    {"pages": [{"spans": [None]}]}, {"pages": [{"spans": [{"id": [], "text": "40"}]}]}])
def test_malformed_evidence_fails_closed(design, manifest):
    assert validate_features(design[0], manifest)["decision"] == "invalid"


def test_formula_preserves_numeric_precision():
    evaluated = expression("L * 1.23456789", {"L": {"value": 40, "type": "length"}})
    assert "1.23456789" in evaluated["formula"]
    assert evaluated["value"] == pytest.approx(49.3827156)


def test_output_does_not_alias_requirement_references(design):
    req, evidence = design
    plan = compile_features(req, evidence)
    req["features"][1]["dependsOn"].append("changed")
    req["parameters"][0]["evidence"].append("changed")
    assert plan["features"][1]["dependsOn"] == ["base"]
    assert plan["parameters"][0]["evidence"] == ["p1:L"]


def test_missing_evidence_requires_clarification(design):
    result = validate_features(design[0])
    assert result["decision"] == "needs_clarification"
    assert result["questions"][0]["blocking"] is True


@pytest.fixture
def finish_design(design):
    req, manifest = design
    req["parameters"].extend([{"name": "FR", "value": 2, "evidence": ["p1:FR"]},
                              {"name": "CL", "value": 2, "evidence": ["p1:CL"]}])
    manifest["pages"][0]["spans"].extend([{"id": "p1:FR", "text": "R2"}, {"id": "p1:CL", "text": "2 X 45"}])
    req["features"] = [req["features"][0], {
        "id": "round", "operation": "fillet", "radius": "FR", "dependsOn": ["base"],
        "edges": {"type": "circular", "radius": "R", "centre": ["L / 2", "W / 2", "T"],
                  "axis": [0, 0, 1]}}, {
        "id": "break", "operation": "chamfer", "length": "CL", "dependsOn": ["round"],
        "edges": {"type": "linear", "direction": [1, 0, 0], "through": [0, "-W / 2", "T"]}}]
    return req, manifest


def test_finish_features_compile_with_geometric_selectors(finish_design):
    req, manifest = finish_design
    plan = compile_features(req, manifest)
    fillet, chamfer = plan["features"][1], plan["features"][2]
    assert fillet["operation"] == "fillet" and fillet["size"]["value"] == 2
    assert fillet["edges"]["type"] == "circular"
    assert fillet["edges"]["centre"][0]["value"] == 20
    assert fillet["edges"]["radius"]["formula"] == "R"
    assert chamfer["operation"] == "chamfer" and chamfer["angle"] == 45
    assert chamfer["edges"]["through"][1]["value"] == -15
    assert chamfer["evidence"] == ["p1:CL", "p1:T", "p1:W"]


@pytest.mark.parametrize("mutation, message", [
    (lambda r: r["features"][1].pop("edges"), "Expected an object"),
    (lambda r: r["features"][1]["edges"].update(type="face"), "linear"),
    (lambda r: r["features"][2]["edges"].update(direction=[0, 0, 0]), "nonzero"),
    (lambda r: r["features"][1]["edges"].update(centre=[0, 0]), "three length"),
    (lambda r: r["features"][2].update(angle=90), "between 1 and 89"),
    (lambda r: r["features"][2].update(angle=30), "only 45-degree chamfers"),
    (lambda r: r["features"][2].update(angle="C"), "between 1 and 89"),
    (lambda r: r["features"][1].update(sketch={"plane": "xy", "profiles": []}), "Unknown fields"),
    (lambda r: r["features"][0].update(edges={"type": "linear", "direction": [1, 0, 0],
                                              "through": [0, 0, 0]}), "finishing fields"),
])
def test_invalid_finish_input_is_rejected(finish_design, mutation, message):
    req, _ = finish_design
    mutation(req)
    with pytest.raises(FeatureError, match=message):
        compile_features(req, None, require_evidence=False)


def test_finish_requires_previous_solid(finish_design):
    req, _ = finish_design
    req["features"] = [{"id": "round", "operation": "fillet", "radius": "FR",
                        "edges": {"type": "circular", "radius": "R", "centre": [0, 0, 0],
                                  "axis": [0, 0, 1]}}]
    with pytest.raises(FeatureError, match="Invalid operation"):
        compile_features(req, None, require_evidence=False)


def test_literal_coordinates_need_no_parameter_but_sizes_stay_symbolic():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "T", "value": 5, "evidence": []},
                          {"name": "D", "value": 6, "evidence": []}],
           "features": [
               {"id": "base", "operation": "pad", "length": "T",
                "sketch": {"plane": "xy", "offset": 0, "profiles": [
                    {"type": "rectangle", "x": -20, "y": -10.5, "width": "T * 8", "height": "T * 4.2"}]}},
               {"id": "hole", "operation": "pocket", "length": "T * 3", "dependsOn": ["base"],
                "sketch": {"plane": "xy", "offset": 5, "profiles": [
                    {"type": "circle", "x": 7.25, "y": -3, "radius": "D / 2"}]}}]}
    plan = compile_features(req, None, require_evidence=False)
    rectangle = plan["features"][0]["sketch"]["profiles"][0]
    assert rectangle["x"]["value"] == -20 and rectangle["x"]["parameters"] == []
    assert rectangle["x"]["formula"] == "-20.0mm"
    assert plan["features"][1]["sketch"]["offset"]["value"] == 5
    assert plan["features"][1]["sketch"]["offset"]["parameters"] == []


def test_literal_feature_size_is_still_rejected():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "T", "value": 5, "evidence": []}],
           "features": [{"id": "base", "operation": "pad", "length": 12,
                         "sketch": {"plane": "xy", "profiles": [
                             {"type": "circle", "radius": "T"}]}}]}
    with pytest.raises(FeatureError):
        compile_features(req, None, require_evidence=False)


def test_compact_array_profiles_compile():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "T", "value": 4, "evidence": []}],
           "features": [
               {"id": "base", "operation": "pad", "length": "T",
                "sketch": {"plane": "xy", "offset": 0, "profiles": [
                    ["polygon", [[0, 0], [20, 0], [20, 10], [0, 10]]]]}},
               {"id": "slot", "operation": "pocket", "length": "T * 2", "dependsOn": ["base"],
                "sketch": {"plane": "xy", "profiles": [
                    ["wire", [5, 5], [["line", 15, 5], ["arc", 15, 7, 1, False, False],
                                      ["line", 5, 7], ["arc", 5, 5, 1, False, False]]]]}},
               {"id": "hole", "operation": "pocket", "length": "T * 2", "dependsOn": ["slot"],
                "sketch": {"plane": "xy", "profiles": [["circle", 10, 2.5, "T / 4"]]}}]}
    plan = compile_features(req, None, require_evidence=False)
    assert plan["features"][1]["sketch"]["profiles"][0]["segments"][1]["type"] == "arc"
    assert plan["features"][1]["sketch"]["profiles"][0]["start"]["x"]["value"] == 5
    assert plan["features"][2]["sketch"]["profiles"][0]["radius"]["value"] == 1


def test_public_schema_accepts_compact_and_named_profiles():
    """The MCP input schema gates every call before compilation; it must not
    reject what compile_features accepts."""
    from jsonschema import validate as validate_schema
    from catia_mcp.drawing.feature_schema import FEATURE_REQUIREMENTS_JSON
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "T", "value": 4, "evidence": []}],
           "profiles": {"P1": ["circle", 0, 0, "T / 2"],
                        "P2": ["wire", [0, 0], [["line", 10, 0], ["arc", 0, 0, "T * 2", True, False]]],
                        "P3": {"type": "rectangle", "width": "T", "height": "T"}},
           "features": [{"id": "base", "operation": "pad", "length": "T",
                         "sketch": {"plane": "xy", "offset": 0, "profiles": ["P1", "P2", "P3"]}},
                        {"id": "slot", "operation": "pocket", "length": "T * 2", "through": True, "dependsOn": ["base"],
                         "sketch": {"plane": "xy", "offset": "T",
                                    "profiles": [["circle", 2, 2, "T / 4"]]}}]}
    validate_schema(req, FEATURE_REQUIREMENTS_JSON)
    plan = compile_features(req, None, require_evidence=False)
    assert len(plan["features"][0]["sketch"]["profiles"]) == 3
    assert plan["features"][0]["sketch"]["profiles"][1]["segments"][1]["type"] == "arc"
    bad = copy.deepcopy(req)
    bad["features"][0]["sketch"]["profiles"] = ["P9"]
    validate_schema(bad, FEATURE_REQUIREMENTS_JSON)  # shape is fine; the table check is compile-time
    with pytest.raises(FeatureError, match="Unknown profile"):
        compile_features(bad, None, require_evidence=False)


def test_named_profile_table_and_references():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "T", "value": 4, "evidence": []}],
           "profiles": {"P1": ["circle", 0, 0, "T / 2"], "P2": ["circle", 10, 0, "T / 4"]},
           "features": [
               {"id": "base", "operation": "pad", "length": "T",
                "sketch": {"plane": "xy", "profiles": ["P1", "P2"]}}]}
    plan = compile_features(req, None, require_evidence=False)
    assert len(plan["features"][0]["sketch"]["profiles"]) == 2
    req["features"][0]["sketch"]["profiles"] = ["P9"]
    with pytest.raises(FeatureError, match="Unknown profile"):
        compile_features(req, None, require_evidence=False)
