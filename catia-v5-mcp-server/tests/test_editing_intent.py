"""Offline semantic tests for cad-intent-draft/1 compilation.

These tests use frozen snapshots. They establish binding, arithmetic, unit and
preserve semantics; they do not establish natural-language understanding or
CATIA behaviour. The P0 experiment report keeps those layers separate.
"""

from __future__ import annotations

from catia_mcp.editing.intent import compile_intent


def _param(handle, value, unit="mm", kind="driver", writable=True):
    return {
        "handle": handle,
        "name": handle,
        "value": value,
        "unit": unit,
        "kind": kind,
        "writable": writable,
    }


def _impact(
    *,
    probe_delta=1.0,
    x=0.0,
    y=0.0,
    z=0.0,
    edge=None,
    hole_centers=False,
    hole_radius_delta=0.0,
    geometry=True,
    edge_preserved=True,
    write_stuck=True,
):
    return {
        "handle": "unused",
        "original": 0.0,
        "probeValue": probe_delta,
        "probeDelta": probe_delta,
        "writeStuck": write_stuck,
        "bboxDeltaX": abs(x),
        "bboxDeltaY": abs(y),
        "bboxDeltaZ": abs(z),
        "bboxEdgeDeltas": edge
        or {
            "x": {"min": 0.0, "max": 0.0},
            "y": {"min": 0.0, "max": 0.0},
            "z": {"min": 0.0, "max": 0.0},
        },
        "holeCentersMoved": hole_centers,
        "holeRadiiChanged": abs(hole_radius_delta) > 1e-12,
        "holeRadiusDelta": hole_radius_delta,
        "geometryChanged": geometry,
        "edgeDistancesPreserved": edge_preserved,
        "restored": True,
    }


def _snapshot(impacts, *, holes=None, capability=None, driver_values=None):
    driver_values = driver_values or {}
    params = [_param(handle, value) for handle, value in driver_values.items()]
    return {
        "schema": "catia-model-snapshot/1",
        "editSessionId": "sess123",
        "modelRevision": 0,
        "parameters": params,
        "holes": holes
        if holes is not None
        else [
            {"center": {"x": -65.0, "y": 35.0, "z": 0.0}, "radius": 5.0},
            {"center": {"x": 65.0, "y": 35.0, "z": 0.0}, "radius": 5.0},
        ],
        "geometry": {
            "volumeMm3": 190115.044408,
            "bbox": {
                "min": {"x": -80.0, "y": -50.0, "z": 0.0},
                "max": {"x": 80.0, "y": 50.0, "z": 12.0},
                "dimensions": {"x": 160.0, "y": 100.0, "z": 12.0},
            },
        },
        "impacts": impacts,
        "capabilities": {
            "invariants": {
                "holes_edge_distance": capability
                or {"supported": True, "reason": "measured boundary drivers keep the margins"}
            }
        },
        "limitations": [],
    }


def fixture_a():
    impacts = {
        "Part1\\L": _impact(
            x=1.0,
            edge={"x": {"min": -0.5, "max": 0.5}, "y": {"min": 0.0, "max": 0.0}, "z": {"min": 0.0, "max": 0.0}},
            hole_centers=True,
            edge_preserved=True,
        ),
        "Part1\\W": _impact(
            y=1.0,
            edge={"x": {"min": 0.0, "max": 0.0}, "y": {"min": -0.5, "max": 0.5}, "z": {"min": 0.0, "max": 0.0}},
            hole_centers=True,
            edge_preserved=True,
        ),
        "Part1\\T": _impact(
            z=1.0,
            edge={"x": {"min": 0.0, "max": 0.0}, "y": {"min": 0.0, "max": 0.0}, "z": {"min": 0.0, "max": 1.0}},
            hole_centers=False,
            edge_preserved=True,
        ),
        "Part1\\D": _impact(
            hole_centers=False,
            hole_radius_delta=0.5,
            geometry=True,
            edge_preserved=True,
        ),
        "Part1\\E": _impact(hole_centers=True, geometry=True, edge_preserved=False),
    }
    return _snapshot(
        impacts,
        driver_values={"Part1\\L": 160.0, "Part1\\W": 100.0, "Part1\\T": 12.0, "Part1\\D": 10.0, "Part1\\E": 15.0},
    )


def fixture_b():
    impacts = {
        "Part2\\L": _impact(
            x=1.0,
            edge={"x": {"min": -0.5, "max": 0.5}, "y": {"min": 0.0, "max": 0.0}, "z": {"min": 0.0, "max": 0.0}},
            hole_centers=False,
            edge_preserved=False,
        ),
        "Part2\\D": _impact(hole_centers=False, hole_radius_delta=0.5, geometry=True, edge_preserved=True),
        "Part2\\UnusedParam": _impact(geometry=False, edge_preserved=True),
    }
    return _snapshot(
        impacts,
        driver_values={"Part2\\L": 160.0, "Part2\\D": 10.0, "Part2\\UnusedParam": 42.0},
    )


def fixture_c():
    impacts = {
        "Part4\\L": _impact(
            x=1.0,
            edge={"x": {"min": -0.5, "max": 0.5}, "y": {"min": 0.0, "max": 0.0}, "z": {"min": 0.0, "max": 0.0}},
            hole_centers=False,
            edge_preserved=True,
        )
    }
    return _snapshot(
        impacts,
        holes=[],
        capability={"supported": False, "reason": "model has no hole geometry"},
        driver_values={"Part4\\L": 160.0},
    )


def fixture_d():
    axis_edge = {"x": {"min": -0.5, "max": 0.5}, "y": {"min": 0.0, "max": 0.0}, "z": {"min": 0.0, "max": 0.0}}
    impacts = {
        "Part1\\P1": _impact(x=1.0, edge=axis_edge, hole_centers=True, edge_preserved=True),
        "Part1\\P2": _impact(x=1.0, edge=axis_edge, hole_centers=True, edge_preserved=True),
        "Part1\\W": _impact(y=1.0, edge={"x": {"min": 0.0, "max": 0.0}, "y": {"min": -0.5, "max": 0.5}, "z": {"min": 0.0, "max": 0.0}}, hole_centers=True, edge_preserved=True),
    }
    return _snapshot(
        impacts,
        driver_values={"Part1\\P1": 80.0, "Part1\\P2": 80.0, "Part1\\W": 100.0},
    )


def intent(handle, attribute, op_type, amount, *, unit="mm", scope="overall", basis="current", preserve=None, binding=None):
    target = {"parameter": handle, "attribute": attribute}
    if binding:
        target["binding"] = binding
    payload = {
        "schema": "cad-intent-draft/1",
        "text": f"{attribute} {op_type} {amount} {unit}",
        "target": target,
        "operation": {"type": op_type, "amount": amount, "unit": unit, "scope": scope},
        "reference": {"basis": basis},
        "preserve": preserve or [],
    }
    return payload


def compile_ok(payload, snapshot, baseline=None):
    result = compile_intent(payload, snapshot, session_id="sess123", baseline_parameters=baseline)
    assert result["decision"] == "planned", result
    assert result["plan"]["schema"] == "catia-edit-plan/1"
    return result


def test_each_side_doubles_the_delta():
    result = compile_ok(intent("Part1\\L", "length", "increase", 50, scope="each_side", preserve=["center_position"]), fixture_a())
    change = result["plan"]["changes"][0]
    assert change["oldValue"] == 160.0
    assert change["newValue"] == 260.0
    assert change["unit"] == "mm"
    acceptance = result["plan"]["acceptance"]
    assert {"type": "bbox_dimension", "axis": "x", "expected": 260.0} in acceptance
    assert {"type": "bbox_range", "axis": "x", "min": -130.0, "max": 130.0} in acceptance


def test_overall_adds_once():
    result = compile_ok(intent("Part1\\L", "length", "increase", 50), fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 210.0
    assert {"type": "bbox_dimension", "axis": "x", "expected": 210.0} in result["plan"]["acceptance"]
    assert all(item["type"] != "bbox_range" for item in result["plan"]["acceptance"])


def test_set_is_absolute():
    result = compile_ok(intent("Part1\\L", "length", "set", 50), fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 50.0


def test_single_side_on_symmetric_driver_is_refused():
    result = compile_intent(
        intent("Part1\\L", "length", "increase", 50, scope="single_side"),
        fixture_a(),
        session_id="sess123",
    )
    assert result["decision"] == "needs_clarification"
    assert "reference.direction" in result["unresolved"]


def test_single_side_with_direction_on_anchored_driver_is_planned():
    snapshot = fixture_a()
    snapshot["impacts"]["Part1\\L"]["bboxEdgeDeltas"]["x"] = {"min": 0.0, "max": 1.0}
    payload = intent("Part1\\L", "length", "increase", 50, scope="single_side")
    payload["reference"]["direction"] = "positive"
    result = compile_ok(payload, snapshot)
    assert result["plan"]["changes"][0]["newValue"] == 210.0
    assert {"type": "bbox_range", "axis": "x", "min": -80.0, "max": 130.0} in result["plan"]["acceptance"]


def test_single_side_on_wrong_side_is_refused():
    snapshot = fixture_a()
    snapshot["impacts"]["Part1\\L"]["bboxEdgeDeltas"]["x"] = {"min": 0.0, "max": 1.0}
    payload = intent("Part1\\L", "length", "increase", 50, scope="single_side")
    payload["reference"]["direction"] = "negative"
    result = compile_intent(payload, snapshot, session_id="sess123")
    assert result["decision"] == "unsupported"


def test_diameter_increase_on_diameter_parameter():
    result = compile_ok(intent("Part1\\D", "diameter", "increase", 2), fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 12.0
    assert {"type": "hole_radius", "expected": 6.0, "parameter": "Part1\\D"} in result["plan"]["acceptance"]


def test_radius_increase_on_diameter_parameter():
    result = compile_ok(intent("Part1\\D", "radius", "increase", 2), fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 14.0
    assert {"type": "hole_radius", "expected": 7.0, "parameter": "Part1\\D"} in result["plan"]["acceptance"]


def test_edge_distance_preserve_compiles_the_condition():
    result = compile_ok(
        intent("Part1\\L", "length", "increase", 40, preserve=["hole_edge_distance"]),
        fixture_a(),
    )
    assert result["plan"]["preserve"] == [
        {"type": "holes_edge_distance", "parameter": "Part1\\E", "targetValue": 15.0}
    ]
    assert {"type": "bbox_dimension", "axis": "x", "expected": 200.0} in result["plan"]["acceptance"]


def test_fixture_b_edge_preserve_is_refused_before_planning():
    result = compile_intent(
        intent("Part2\\L", "length", "increase", 40, preserve=["hole_edge_distance"]),
        fixture_b(),
        session_id="sess123",
    )
    assert result["decision"] == "unsupported"
    assert "edgeDistancesPreserved" in result["message"] or "edge" in result["message"].lower()


def test_hole_centers_conflict_when_driver_moves_holes():
    result = compile_intent(
        intent("Part1\\L", "length", "increase", 40, preserve=["hole_centers"]),
        fixture_a(),
        session_id="sess123",
    )
    assert result["decision"] == "unsupported"
    assert "preserve.hole_centers" in result["unresolved"]


def test_hole_centers_allowed_when_driver_does_not_move_them():
    result = compile_ok(
        intent("Part1\\D", "diameter", "increase", 2, preserve=["hole_centers"]),
        fixture_a(),
    )
    assert {"type": "hole_radius", "expected": 6.0, "parameter": "Part1\\D"} in result["plan"]["acceptance"]
    assert {
        "type": "hole_positions",
        "holes": [{"x": -65.0, "y": 35.0}, {"x": 65.0, "y": 35.0}],
    } in result["plan"]["acceptance"]


def test_current_basis_uses_current_value():
    result = compile_ok(intent("Part1\\L", "length", "increase", 10, basis="current"), fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 170.0


def test_original_basis_uses_recorded_baseline():
    result = compile_ok(
        intent("Part1\\L", "length", "increase", 10, basis="original"),
        fixture_a(),
        baseline={"Part1\\L": {"value": 200.0, "unit": "mm"}},
    )
    assert result["plan"]["changes"][0]["newValue"] == 210.0


def test_original_basis_without_baseline_asks_for_clarification():
    result = compile_intent(
        intent("Part1\\L", "length", "increase", 10, basis="original"),
        fixture_a(),
        session_id="sess123",
    )
    assert result["decision"] == "needs_clarification"
    assert "reference.basis" in result["unresolved"]


def test_ambiguous_drivers_ask_instead_of_picking_one():
    result = compile_intent(
        intent("Part1\\P1", "length", "set", 100),
        fixture_d(),
        session_id="sess123",
    )
    assert result["decision"] == "needs_clarification"
    handles = {candidate["handle"] for candidate in result["candidates"]}
    assert handles == {"Part1\\P1", "Part1\\P2"}


def test_explicitly_named_parameter_is_allowed_to_be_ambiguous():
    payload = intent("Part1\\P1", "length", "set", 100, binding="user_explicit")
    payload["text"] = "把 P1 改成 100 mm"
    result = compile_ok(payload, fixture_d())
    assert result["plan"]["changes"][0]["newValue"] == 100.0


def test_user_explicit_binding_must_be_named_in_the_text():
    payload = intent("Part1\\P1", "length", "set", 100, binding="user_explicit")
    payload["text"] = "把这个长度改成 200 mm"
    result = compile_intent(payload, fixture_d(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "target.binding" in result["unresolved"]


def test_missing_unit_asks_instead_of_guessing():
    payload = intent("Part1\\L", "length", "set", 20)
    payload["operation"].pop("unit")
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "operation.unit" in result["unresolved"]

    payload["operation"]["unit"] = "none"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "operation.unit" in result["unresolved"]


def test_unit_conversion_cm_and_inch():
    result = compile_ok(intent("Part1\\L", "length", "increase", 2, unit="cm"), fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 180.0
    result = compile_ok(intent("Part1\\L", "length", "increase", 1, unit="in"), fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 185.4


def test_unknown_preserve_is_refused_not_dropped():
    result = compile_intent(
        intent("Part1\\L", "length", "increase", 10, preserve=["banana"]),
        fixture_a(),
        session_id="sess123",
    )
    assert result["decision"] == "unsupported"
    assert "preserve[0]" in result["unresolved"]


def test_no_holes_edge_preserve_is_refused():
    result = compile_intent(
        intent("Part4\\L", "length", "increase", 40, preserve=["hole_edge_distance"]),
        fixture_c(),
        session_id="sess123",
    )
    assert result["decision"] == "unsupported"


def test_non_geometry_parameter_is_refused():
    result = compile_intent(
        intent("Part2\\UnusedParam", "length", "set", 50, binding="user_explicit"),
        fixture_b(),
        session_id="sess123",
    )
    assert result["decision"] == "unsupported"


def test_old_value_and_revision_are_copied_from_snapshot():
    result = compile_ok(intent("Part1\\L", "length", "increase", 40), fixture_a())
    plan = result["plan"]
    assert plan["expectedRevision"] == 0
    assert plan["editSessionId"] == "sess123"
    assert plan["changes"][0]["oldValue"] == 160.0


def test_compiled_plan_passes_server_validation():
    from catia_mcp.editing.validation import validate_plan

    snapshot = fixture_a()
    result = compile_ok(intent("Part1\\L", "length", "increase", 40), snapshot)
    assert validate_plan(result["plan"], snapshot, session_id="sess123") == []


def test_text_each_side_with_overall_scope_is_caught():
    payload = intent("Part1\\L", "length", "increase", 50, scope="overall")
    payload["text"] = "左右各延长 50 mm"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "operation.scope" in result["unresolved"]


def test_text_total_with_each_side_scope_is_caught():
    payload = intent("Part1\\L", "length", "increase", 50, scope="each_side")
    payload["text"] = "总长加 50 mm"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "operation.scope" in result["unresolved"]


def test_text_original_with_current_basis_is_caught():
    payload = intent("Part1\\L", "length", "increase", 10)
    payload["text"] = "比原始长度增加 10 mm"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "reference.basis" in result["unresolved"]


def test_text_center_without_preserve_is_caught():
    payload = intent("Part1\\L", "length", "increase", 50, scope="each_side")
    payload["text"] = "左右各延长 50 mm，中心不动"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "preserve" in result["unresolved"]


def test_text_hole_centers_without_preserve_is_caught():
    payload = intent("Part1\\L", "length", "increase", 10)
    payload["text"] = "长度加 10 mm，孔心坐标保持不变"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "preserve" in result["unresolved"]


def test_text_edge_distance_without_preserve_is_caught():
    payload = intent("Part1\\L", "length", "increase", 40)
    payload["text"] = "长度增加 40 mm，孔到边的距离保持不变"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "preserve" in result["unresolved"]


def test_text_single_side_with_overall_scope_is_caught():
    payload = intent("Part1\\L", "length", "increase", 50, scope="overall")
    payload["text"] = "只把右侧加长 50 mm，左边不动"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "operation.scope" in result["unresolved"]


def test_text_three_holes_on_two_hole_model_is_caught_without_hole_count():
    payload = intent("Part1\\D", "diameter", "increase", 2)
    payload["text"] = "把三个孔都改为直径 12 mm"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "target.holeCount" in result["unresolved"]


def test_text_diameter_with_radius_attribute_is_caught():
    payload = intent("Part1\\D", "radius", "increase", 2)
    payload["text"] = "把孔直径增加 2 mm"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "target.attribute" in result["unresolved"]


def test_consistent_text_each_side_plans():
    payload = intent("Part1\\L", "length", "increase", 50, scope="each_side", preserve=["center_position"])
    payload["text"] = "左右各延长 50 mm，中心不动"
    result = compile_ok(payload, fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 260.0


def test_hole_count_mismatch_asks_instead_of_changing_the_wrong_group():
    payload = intent("Part1\\D", "diameter", "increase", 2)
    payload["target"]["holeCount"] = 3
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "target.holeCount" in result["unresolved"]

    payload["target"]["holeCount"] = 2
    result = compile_ok(payload, fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 12.0


def test_adding_a_hole_is_refused_by_text_even_if_the_model_routes_it_to_compile():
    payload = intent("Part1\\D", "diameter", "increase", 2)
    payload["text"] = "增加一个孔"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "unsupported"
    assert "adding a hole" in result["message"]


def test_moving_one_hole_centre_is_refused_by_text():
    payload = intent("Part1\\E", "distance", "increase", 10)
    payload["text"] = "把左侧的孔向左移动 10 mm"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "unsupported"
    assert "hole centre" in result["message"]


def test_growing_a_hole_is_not_mistaken_for_adding_one():
    payload = intent("Part1\\D", "diameter", "increase", 2)
    payload["text"] = "把两个孔加大到直径 12 mm"
    result = compile_ok(payload, fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 12.0


def test_unitless_text_with_a_supplied_unit_asks_instead_of_assuming():
    payload = intent("Part1\\L", "length", "set", 20)
    payload["text"] = "长度改成 20"
    payload["operation"]["unit"] = "mm"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "operation.unit" in result["unresolved"]

    payload["text"] = "长度改成 20 毫米"
    result = compile_ok(payload, fixture_a())
    assert result["plan"]["changes"][0]["newValue"] == 20.0


def test_paraphrased_text_with_an_assumption_is_rejected():
    payload = intent("Part1\\L", "length", "set", 20)
    payload["text"] = "长度改成 20（原话未给出单位，按模型单位 mm 解释为 20 mm，不换算）"
    result = compile_intent(payload, fixture_a(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "text.provenance" in result["unresolved"]


def test_dropped_preserve_cannot_be_hidden_in_a_rewritten_text():
    payload = intent("Part4\\L", "length", "increase", 40)
    payload["text"] = "长度增加 40 mm（不含孔边距约束）"
    result = compile_intent(payload, fixture_c(), session_id="sess123")
    assert result["decision"] == "needs_clarification"
    assert "text.provenance" in result["unresolved"]


def test_tool_is_registered_as_read_only():
    from catia_mcp.tools.editing import READ_ONLY_TOOLS, EditingTools

    assert "catia_compile_intent" in READ_ONLY_TOOLS
    names = {definition["name"] for definition in EditingTools.get_tool_definitions(None)}
    assert "catia_compile_intent" in names


# ---------------------------------------------------------------------------
# host-injected user text (P0 report section 8, item 1)
# ---------------------------------------------------------------------------


def test_platform_user_text_is_used_instead_of_the_model_copy():
    # The model omitted the unit in its copy; the host says the user did give one.
    payload = intent("Part1\\L", "length", "set", 20)
    payload["text"] = "长度改成 20"
    result = compile_intent(
        payload,
        fixture_a(),
        session_id="sess123",
        user_text="把零件 长度改成 20 mm。验证后另存为 out.CATPart。",
    )
    assert result["decision"] == "planned", result
    assert result["textSource"] == "platform"
    assert result["plan"]["changes"][0]["newValue"] == 20.0
    assert result["plan"]["intent"]["textSource"] == "platform"


def test_model_cannot_add_a_unit_the_user_never_said():
    # The P0 Q02 bypass: the model rewrote its own text to add "mm".
    payload = intent("Part1\\L", "length", "set", 20)
    payload["text"] = "长度改成 20（原话未给出单位，按模型单位 mm 解释为 20 mm）"
    result = compile_intent(
        payload,
        fixture_a(),
        session_id="sess123",
        user_text="长度改成 20",
    )
    assert result["decision"] == "needs_clarification"
    assert "text.provenance" in result["unresolved"]
    assert result["textSource"] == "platform"


def test_platform_text_without_unit_still_asks_for_the_unit():
    payload = intent("Part1\\L", "length", "set", 20)
    payload["text"] = "长度改成 20"
    result = compile_intent(
        payload,
        fixture_a(),
        session_id="sess123",
        user_text="长度改成 20",
    )
    assert result["decision"] == "needs_clarification"
    assert "operation.unit" in result["unresolved"]


def test_platform_text_drives_the_consistency_phrases():
    # The host text (authoritative) says "左右各"; the structured scope says overall.
    payload = intent("Part1\\L", "length", "increase", 50, scope="overall")
    payload["text"] = "左右各增加 50 mm"
    result = compile_intent(
        payload,
        fixture_a(),
        session_id="sess123",
        user_text="长度左右各增加 50 mm，中心不动",
    )
    assert result["decision"] == "needs_clarification"
    assert "operation.scope" in result["unresolved"]


def test_without_platform_text_the_result_is_marked_model_sourced():
    payload = intent("Part1\\L", "length", "increase", 50)
    result = compile_ok(payload, fixture_a())
    assert result["textSource"] == "model"
    assert result["plan"]["intent"]["textSource"] == "model"
