"""Offline P1 semantics: measured circle roles, sketches and frames.

These use a frozen FixtureF-shaped snapshot. They establish the compiler's
deterministic decisions; the real-agent runs establish the language layer.
"""

from __future__ import annotations

from catia_mcp.editing.intent import compile_intent


def _param(handle, value, unit="mm"):
    return {"handle": handle, "name": handle, "value": value, "unit": unit, "kind": "driver", "writable": True}


def _impact(*, x=0.0, y=0.0, z=0.0, edge=None, radius_deltas=None, volume=0.0, world_moved=False, edge_margins=None, edge_preserved=True):
    return {
        "handle": "unused",
        "probeDelta": 1.0,
        "probeValue": 1.0,
        "writeStuck": True,
        "bboxDeltaX": x,
        "bboxDeltaY": y,
        "bboxDeltaZ": z,
        "bboxEdgeDeltas": edge
        or {
            "x": {"min": -x / 2, "max": x / 2},
            "y": {"min": -y / 2, "max": y / 2},
            "z": {"min": 0.0, "max": z},
        },
        "holeCentersMoved": world_moved,
        "holeWorldCentersMoved": world_moved,
        "holeRadiiChanged": bool(radius_deltas),
        "holeRadiusDelta": 0.5 if radius_deltas and any(abs(v) > 1e-9 for v in radius_deltas) else 0.0,
        "holeRadiusDeltas": radius_deltas or [0.0, 0.0, 0.0, 0.0],
        "volumeDelta": volume,
        "geometryChanged": True,
        "edgeDistancesPreserved": edge_preserved,
        "edgeMargins": edge_margins,
        "restored": True,
    }


def fixture_f_like():
    holes = [
        {
            "sketch": "Sketch.A",
            "center": {"x": -65.0, "y": 35.0, "z": 0.0},
            "centerWorld": {"x": -65.0, "y": 35.0, "z": 0.0},
            "radius": 5.0,
            "role": "hole",
            "radiusDrivers": ["Part1\\D"],
        },
        {
            "sketch": "Sketch.A",
            "center": {"x": 65.0, "y": 35.0, "z": 0.0},
            "centerWorld": {"x": 65.0, "y": 35.0, "z": 0.0},
            "radius": 5.0,
            "role": "hole",
            "radiusDrivers": ["Part1\\D"],
        },
        {
            "sketch": "Sketch.B",
            "center": {"x": 0.0, "y": 0.0, "z": 0.0},
            "centerWorld": {"x": 0.0, "y": 0.0, "z": 0.0},
            "radius": 12.0,
            "role": "additive_feature",
            "radiusDrivers": ["Part1\\R"],
        },
        {
            "sketch": "Sketch.C",
            "center": {"x": 0.0, "y": 6.0, "z": 0.0},
            "centerWorld": {"x": 0.0, "y": 0.0, "z": 6.0},
            "radius": 5.0,
            "role": "hole",
            "radiusDrivers": ["Part1\\S"],
        },
    ]
    impacts = {
        "Part1\\L": _impact(x=1.0, world_moved=True),
        "Part1\\W": _impact(y=1.0, world_moved=True),
        "Part1\\D": _impact(radius_deltas=[0.5, 0.5, 0.0, 0.0], volume=-395.0),
        "Part1\\R": _impact(radius_deltas=[0.0, 0.0, 0.5, 0.0], volume=307.0),
        "Part1\\S": _impact(radius_deltas=[0.0, 0.0, 0.0, 0.5], volume=-2638.0),
        "Part1\\Y1": _impact(
            y=1.0,
            edge={"x": {"min": 0.0, "max": 0.0}, "y": {"min": 0.0, "max": 1.0}, "z": {"min": 0.0, "max": 0.0}},
        ),
    }
    return {
        "schema": "catia-model-snapshot/1",
        "editSessionId": "sess-structure",
        "modelRevision": 0,
        "parameters": [
            _param("Part1\\L", 160.0),
            _param("Part1\\W", 100.0),
            _param("Part1\\D", 10.0),
            _param("Part1\\R", 24.0),
            _param("Part1\\S", 10.0),
            _param("Part1\\Y1", 30.0),
        ],
        "holes": holes,
        "impacts": impacts,
        "structure": {
            "features": [
                {"name": "Pad.1", "kind": "pad", "materialRole": "additive", "sketch": "Sketch.A"},
                {"name": "Pad.2", "kind": "pad", "materialRole": "additive", "sketch": "Sketch.B"},
                {"name": "Pocket.1", "kind": "pocket", "materialRole": "subtractive", "sketch": "Sketch.C"},
            ],
            "sketches": [
                {"name": "Sketch.A", "frame": {"plane": "XY", "horizontal": {"x": 1, "y": 0, "z": 0}, "vertical": {"x": 0, "y": 1, "z": 0}}},
                {"name": "Sketch.B", "frame": {"plane": "XY", "horizontal": {"x": 1, "y": 0, "z": 0}, "vertical": {"x": 0, "y": 1, "z": 0}}},
                {"name": "Sketch.C", "frame": {"plane": "YZ", "horizontal": {"x": 0, "y": 1, "z": 0}, "vertical": {"x": 0, "y": 0, "z": 1}}},
            ],
        },
        "geometry": {
            "volumeMm3": 181167.78853,
            "bbox": {
                "min": {"x": -80.0, "y": -50.0, "z": 0.0},
                "max": {"x": 80.0, "y": 50.0, "z": 20.0},
                "dimensions": {"x": 160.0, "y": 100.0, "z": 20.0},
            },
        },
        "capabilities": {"invariants": {"holes_edge_distance": {"supported": True, "reason": "measured"}}},
        "preserves": {"hole_centers": {"supported": True, "worldSupported": True}},
        "limitations": [],
    }


def draft(text, handle, attribute, amount, *, unit="mm", kind=None, sketch=None, preserve=None, frame=None, direction_text=None):
    target = {"parameter": handle, "attribute": attribute}
    if kind:
        target["kind"] = kind
    if sketch:
        target["sketch"] = sketch
    reference = {"basis": "current"}
    if frame:
        reference["frame"] = frame
    if direction_text:
        reference["directionText"] = direction_text
    payload = {
        "schema": "cad-intent-draft/1",
        "text": text,
        "target": target,
        "operation": {"type": "increase", "amount": amount, "unit": unit, "scope": "overall"},
        "reference": reference,
        "preserve": preserve or [],
    }
    return payload


def test_a_bare_hole_diameter_request_is_ambiguous_between_the_two_hole_groups():
    result = compile_intent(draft("把孔径改为 12 mm", "Part1\\D", "diameter", 2), fixture_f_like(), session_id="s")
    assert result["decision"] == "needs_clarification"
    assert "target.parameter.ambiguous" in result["unresolved"]
    handles = {row["handle"] for row in result["candidates"]}
    assert {"Part1\\D", "Part1\\S"} <= handles


def test_hole_count_binds_the_group_that_actually_changes_that_many_holes():
    payload = draft("把两个安装孔的直径改为 12 mm", "Part1\\D", "diameter", 2)
    payload["target"]["holeCount"] = 2
    result = compile_intent(payload, fixture_f_like(), session_id="s")
    assert result["decision"] == "planned", result
    assert result["plan"]["changes"][0]["parameter"] == "Part1\\D"


def test_a_boss_request_binds_the_additive_profile_not_a_hole():
    result = compile_intent(
        draft("把凸台的直径改为 30 mm", "Part1\\R", "diameter", 6, kind="additive_feature"),
        fixture_f_like(),
        session_id="s",
    )
    assert result["decision"] == "planned", result
    assert result["plan"]["changes"][0]["parameter"] == "Part1\\R"
    assert result["checks"][-1]["id"] == "plan-validation"


def test_a_hole_request_cannot_edit_the_boss_parameter():
    result = compile_intent(
        draft("把孔径改为 12 mm", "Part1\\R", "diameter", 2, kind="hole"),
        fixture_f_like(),
        session_id="s",
    )
    assert result["decision"] == "unsupported"
    assert "not a hole" in result["message"]


def test_sketch_filter_binds_the_side_hole():
    result = compile_intent(
        draft("把侧面孔的直径增加 2 mm", "Part1\\S", "diameter", 2, sketch="Sketch.C"),
        fixture_f_like(),
        session_id="s",
    )
    assert result["decision"] == "planned", result
    assert result["plan"]["changes"][0]["parameter"] == "Part1\\S"


def test_world_hole_position_preserve_adds_the_world_assertion():
    result = compile_intent(
        draft(
            "把侧面孔的直径增加 2 mm，孔心模型坐标不动",
            "Part1\\S",
            "diameter",
            2,
            sketch="Sketch.C",
            preserve=["hole_world_position"],
        ),
        fixture_f_like(),
        session_id="s",
    )
    assert result["decision"] == "planned", result
    assert any(
        item["type"] == "hole_world_position" for item in result["plan"]["preserve"]
    )
    world_acceptance = [
        item for item in result["plan"]["acceptance"] if item["type"] == "hole_world_positions"
    ]
    assert world_acceptance and len(world_acceptance[0]["holes"]) == 3


def _without_w(snapshot):
    snapshot = {**snapshot, "parameters": [p for p in snapshot["parameters"] if p["handle"] != "Part1\\W"]}
    snapshot["impacts"] = {k: v for k, v in snapshot["impacts"].items() if k != "Part1\\W"}
    return snapshot


def test_a_sketch_frame_maps_right_to_the_measured_y_axis():
    payload = draft("只向右侧增加 10 mm", "Part1\\Y1", "width", 10, frame="sketch:Sketch.C", direction_text="right")
    payload["operation"]["scope"] = "single_side"
    result = compile_intent(payload, _without_w(fixture_f_like()), session_id="s")
    assert result["decision"] == "planned", result
    assert result["binding"]["axis"] == "y"
    assert result["plan"]["changes"][0]["newValue"] == 40.0


def test_a_sketch_frame_that_maps_to_the_wrong_axis_asks_instead_of_guessing():
    payload = draft("只向右侧增加 10 mm", "Part1\\L", "length", 10, frame="sketch:Sketch.C", direction_text="right")
    payload["operation"]["scope"] = "single_side"
    result = compile_intent(payload, _without_w(fixture_f_like()), session_id="s")
    assert result["decision"] == "needs_clarification"
    assert "reference.frame" in result["unresolved"]


def test_an_oblique_sketch_frame_is_refused():
    snapshot = _without_w(fixture_f_like())
    snapshot["structure"]["sketches"][2]["frame"] = {
        "plane": "oblique",
        "horizontal": {"x": 0.7071, "y": 0.7071, "z": 0.0},
        "vertical": {"x": -0.7071, "y": 0.7071, "z": 0.0},
    }
    payload = draft("只向右侧增加 10 mm", "Part1\\Y1", "width", 10, frame="sketch:Sketch.C", direction_text="right")
    payload["operation"]["scope"] = "single_side"
    result = compile_intent(payload, snapshot, session_id="s")
    assert result["decision"] == "unsupported"
    assert "not axis-aligned" in result["message"]


def _edge_scoped_snapshot():
    snapshot = _without_w(fixture_f_like())
    # L moves the base holes with the boundary (margins constant) but the
    # central side hole's x margin grows 80 -> 100: the global invariant is
    # false, while the installation-hole group is still preservable.
    snapshot["impacts"]["Part1\L"] = _impact(
        x=1.0,
        world_moved=True,
        edge_preserved=False,
        edge_margins={
            "before": [[15.0, 15.0], [15.0, 15.0], [12.0, 12.0], [80.0, 44.0]],
            "after": [[15.0, 15.0], [15.0, 15.0], [12.0, 12.0], [100.0, 44.0]],
        },
    )
    snapshot["capabilities"]["invariants"]["holes_edge_distance"] = {
        "supported": False,
        "reason": "the central side hole margin changes",
    }
    return snapshot


def test_an_unscoped_edge_preserve_is_refused_when_another_hole_group_moves():
    payload = draft("长度增加 40 mm，孔到边的距离保持 15 mm", "Part1\L", "length", 40, preserve=["hole_edge_distance"])
    result = compile_intent(payload, _edge_scoped_snapshot(), session_id="s")
    assert result["decision"] == "unsupported"
    assert "preserve.hole_edge_distance" in result["unresolved"]


def test_a_sketch_scoped_edge_preserve_uses_per_hole_evidence():
    payload = draft("长度增加 40 mm，安装孔边距保持 15 mm", "Part1\L", "length", 40,
                    preserve=[{"type": "hole_edge_distance", "sketch": "Sketch.A"}])
    result = compile_intent(payload, _edge_scoped_snapshot(), session_id="s")
    assert result["decision"] == "planned", result
    preserve = result["plan"]["preserve"][0]
    assert preserve["type"] == "holes_edge_distance"
    assert preserve["holes"] == [1, 2]
    assert preserve["sketch"] == "Sketch.A"
    assert preserve["targetValue"] == 15.0


def test_a_sketch_discriminator_needs_text_evidence():
    # The model invented sketch=Sketch.A + holeCount=2 for a bare "孔径" request.
    payload = draft("把孔径改为 12 mm", "Part1\D", "diameter", 2, sketch="Sketch.A")
    payload["target"]["holeCount"] = 2
    result = compile_intent(payload, fixture_f_like(), session_id="s")
    assert result["decision"] == "needs_clarification"
    assert "target.sketch.provenance" in result["unresolved"]

    # The same discriminator is legitimate when the user says "安装孔".
    payload["text"] = "把两个安装孔的直径改为 12 mm"
    result = compile_intent(payload, fixture_f_like(), session_id="s")
    assert result["decision"] == "planned", result


def test_text_hole_count_is_compared_to_the_bound_drivers_holes():
    payload = draft("把两个安装孔的直径改为 12 mm，孔心不动", "Part1\D", "diameter", 2, sketch="Sketch.A")
    payload["preserve"] = ["hole_centers"]
    result = compile_intent(payload, fixture_f_like(), session_id="s")
    assert result["decision"] == "planned", result


def test_a_boss_diameter_acceptance_uses_the_boss_circle_not_a_hole():
    result = compile_intent(
        draft("把凸台的直径改为 30 mm", "Part1\R", "diameter", 6, kind="additive_feature"),
        fixture_f_like(),
        session_id="s",
    )
    assert result["decision"] == "planned", result
    radius = [item for item in result["plan"]["acceptance"] if item["type"] == "hole_radius"]
    assert radius == [{"type": "hole_radius", "expected": 15.0, "parameter": "Part1\R"}]
