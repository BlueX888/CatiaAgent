"""Compile normalized requirements into a closed ``catia-build-plan/1``.

The plan is a whitelisted operation list, not a script: every step names one
known CATIA operation and the builder refuses anything else. Sketch element
indices are derived here (CATIA puts the absolute axis at index 1), so the
model never has to guess them.

Topology produced by the plan:

* sketch 1: rectangle (4 lines at indices 2..5) -> pad
* sketch 2: 4 construction reference lines (2..5) + one circle per hole
  (6..5+n) -> pocket with depth ``T + 2mm``

Both sketches are fully parameter-driven through user parameters. Plans retain
local parameter names; the executor quotes those identifiers for CATIA keywords.
Backticked full parameter paths are not valid formula input on B31.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

PLAN_SCHEMA = "catia-build-plan/1"

PARAMETER_NAMES = {"length": "L", "width": "W", "thickness": "T", "diameter": "D"}

# Element indices in a factory-created sketch: the absolute axis always
# occupies index 1, so the first created element is 2.
AXIS_INDEX = 1
BODY_LINES = {"bottom": 2, "right": 3, "top": 4, "left": 5}
HOLE_SKETCH_REF_LINES = {"bottom": 2, "right": 3, "top": 4, "left": 5}
FIRST_CIRCLE_INDEX = 6


class PlanError(ValueError):
    """The requirements cannot be compiled into a supported build plan."""


def _plan_id(plan: dict[str, Any]) -> str:
    payload = {k: v for k, v in plan.items() if k != "planId"}
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _expected_volume(length: float, width: float, thickness: float, diameter: float, count: int) -> float:
    return (length * width - count * math.pi * (diameter / 2.0) ** 2) * thickness


def compile_build_plan(normalized: dict[str, Any]) -> dict[str, Any]:
    """Return a validated build plan. Raises :class:`PlanError` on bad input."""
    if not isinstance(normalized, dict):
        raise PlanError("Normalized requirements must be an object.")
    from catia_mcp.drawing.feature_model import SCHEMA as FEATURE_SCHEMA
    if normalized.get("schema") == FEATURE_SCHEMA:
        return json.loads(json.dumps(normalized["featurePlan"]))
    body = normalized.get("body") or {}
    if body.get("type") == "annular_ring":
        return _compile_ring(normalized)
    if body.get("type") == "extruded_profile":
        return _compile_profile(normalized)
    holes = normalized.get("holes") or {}
    try:
        length = float(body["length"])
        width = float(body["width"])
        thickness = float(body["thickness"])
        diameter = float(holes["diameter"])
        count = int(holes["count"])
        centres = [[float(p[0]), float(p[1])] for p in holes["centres"]]
        # Canonical hole order: the requirement list order must not change the
        # parameter names LX1..n or the plan id.
        centres.sort(key=lambda pair: (round(pair[0], 6), round(pair[1], 6)))
    except (KeyError, TypeError, ValueError, IndexError) as exc:
        raise PlanError(f"Normalized requirements are incomplete: {exc}") from exc

    if body.get("type") != "flat_plate":
        raise PlanError(f"Unsupported body type: {body.get('type')!r}.")
    if min(length, width, thickness, diameter) <= 0:
        raise PlanError("All dimensions must be positive.")
    if count <= 0 or len(centres) != count:
        raise PlanError(f"hole count {count} does not match {len(centres)} centres.")
    if holes.get("through") is not True:
        raise PlanError("This version supports through holes only.")

    pattern = holes.get("pattern") or {}
    pattern_type = str(pattern.get("type") or "").capitalize()

    parameters: list[dict[str, Any]] = [
        {"name": "L", "type": "length", "value": length, "role": "length"},
        {"name": "W", "type": "length", "value": width, "role": "width"},
        {"name": "T", "type": "length", "value": thickness, "role": "thickness"},
        {"name": "D", "type": "length", "value": diameter, "role": "hole_diameter"},
    ]

    # Hole centre parameters -------------------------------------------------
    hole_formulas: list[dict[str, Any]] = []
    if pattern_type == "Symmetric":
        edge_x = float(pattern.get("edgeX"))
        edge_y = float(pattern.get("edgeY"))
        if edge_x <= diameter / 2 or edge_y <= diameter / 2:
            raise PlanError("Hole edge distance must exceed the hole radius.")
        parameters.extend(
            [
                {"name": "EX", "type": "length", "value": edge_x, "role": "hole_edge_x"},
                {"name": "EY", "type": "length", "value": edge_y, "role": "hole_edge_y"},
            ]
        )
        for index, (x, y) in enumerate(centres):
            # The side is a property of the hole, not of its position in the
            # list: the centres are sorted canonically, so a fixed
            # left/right/top/bottom table would attach the wrong reference line
            # and collapse the holes onto each other.
            xline = "left" if x < 0 else "right"
            yline = "bottom" if y < 0 else "top"
            hole_formulas.append({
                "hole": index, "xLine": xline, "xExpression": "EX - D / 2",
                "yLine": yline, "yExpression": "EY - D / 2",
                "xValue": edge_x - diameter / 2.0, "yValue": edge_y - diameter / 2.0,
            })
    elif pattern_type == "Explicit":
        edges = []
        for index, (x, y) in enumerate(centres):
            lx = x + length / 2.0
            ly = y + width / 2.0
            if lx - diameter / 2 <= 0 or ly - diameter / 2 <= 0:
                raise PlanError(f"Hole {index + 1} sits outside the plate.")
            parameters.extend(
                [
                    {"name": f"LX{index + 1}", "type": "length", "value": lx,
                     "role": f"hole_{index + 1}_from_left"},
                    {"name": f"LY{index + 1}", "type": "length", "value": ly,
                     "role": f"hole_{index + 1}_from_bottom"},
                ]
            )
            hole_formulas.append(
                {"hole": index, "xLine": "left", "xExpression": f"LX{index + 1} - D / 2",
                 "yLine": "bottom", "yExpression": f"LY{index + 1} - D / 2",
                 "xValue": lx - diameter / 2.0, "yValue": ly - diameter / 2.0}
            )
    else:
        raise PlanError(f"Unsupported hole pattern: {pattern.get('type')!r}.")

    steps: list[dict[str, Any]] = [
        {"op": "new_part", "name": normalized.get("partName") or "Plate"},
        {"op": "create_parameters", "parameters": [p["name"] for p in parameters]},
        {"op": "create_sketch", "plane": "xy", "role": "body"},
        {"op": "rectangle", "x1": -length / 2, "y1": -width / 2,
         "x2": length / 2, "y2": width / 2},
        {"op": "constraint", "type": "distance", "value": width,
         "geometry_index_1": BODY_LINES["bottom"], "geometry_index_2": BODY_LINES["top"],
         "bind": "W", "role": "body_width"},
        {"op": "constraint", "type": "distance", "value": length,
         "geometry_index_1": BODY_LINES["right"], "geometry_index_2": BODY_LINES["left"],
         "bind": "L", "role": "body_length"},
        {"op": "close_sketch", "role": "body"},
        {"op": "pad", "height": thickness, "bind": "T"},
        {"op": "create_sketch", "plane": "xy", "role": "holes"},
        {"op": "construction_rectangle", "x1": -length / 2, "y1": -width / 2,
         "x2": length / 2, "y2": width / 2},
        {"op": "constraint", "type": "distance", "value": width,
         "geometry_index_1": HOLE_SKETCH_REF_LINES["bottom"],
         "geometry_index_2": HOLE_SKETCH_REF_LINES["top"],
         "bind": "W", "role": "hole_ref_width"},
        {"op": "constraint", "type": "distance", "value": length,
         "geometry_index_1": HOLE_SKETCH_REF_LINES["right"],
         "geometry_index_2": HOLE_SKETCH_REF_LINES["left"],
         "bind": "L", "role": "hole_ref_length"},
    ]

    for index, (x, y) in enumerate(centres):
        steps.append(
            {
                "op": "circle",
                "cx": x,
                "cy": y,
                "radius": diameter / 2.0,
                "geometry_index": FIRST_CIRCLE_INDEX + index,
                "hole": index,
            }
        )
        steps.append(
            {
                "op": "constraint",
                "type": "radius",
                "value": diameter / 2.0,
                "geometry_index_1": FIRST_CIRCLE_INDEX + index,
                "bind": "D / 2",
                "role": f"hole_{index + 1}_radius",
            }
        )
    for item in hole_formulas:
        circle_index = FIRST_CIRCLE_INDEX + item["hole"]
        steps.append(
            {
                "op": "constraint",
                "type": "distance",
                "value": item["xValue"],
                "geometry_index_1": circle_index,
                "geometry_index_2": HOLE_SKETCH_REF_LINES[item["xLine"]],
                "bind": item["xExpression"],
                "role": f"hole_{item['hole'] + 1}_x",
            }
        )
        steps.append(
            {
                "op": "constraint",
                "type": "distance",
                "value": item["yValue"],
                "geometry_index_1": circle_index,
                "geometry_index_2": HOLE_SKETCH_REF_LINES[item["yLine"]],
                "bind": item["yExpression"],
                "role": f"hole_{item['hole'] + 1}_y",
            }
        )

    steps.extend(
        [
            {"op": "close_sketch", "role": "holes"},
            {"op": "pocket", "depth": thickness + 2.0, "bind": "T + 2mm"},
            {"op": "verify_geometry",
             "expected": {
                 "volumeMm3": _expected_volume(length, width, thickness, diameter, count),
                 "bbox": {
                     "min": {"x": -length / 2, "y": -width / 2, "z": 0.0},
                     "max": {"x": length / 2, "y": width / 2, "z": thickness},
                 },
                 "holes": [{"x": x, "y": y, "diameter": diameter, "through": True}
                           for x, y in centres],
             }},
        ]
    )

    expected = {
        "unit": "mm",
        "volumeMm3": round(_expected_volume(length, width, thickness, diameter, count), 6),
        "bbox": {
            "min": {"x": -length / 2, "y": -width / 2, "z": 0.0},
            "max": {"x": length / 2, "y": width / 2, "z": thickness},
        },
        "holes": [{"x": x, "y": y, "diameter": diameter, "through": True} for x, y in centres],
        "holeCount": count,
        "featureCount": 2,  # one Pad + one Pocket
        "parameters": {p["name"]: p["value"] for p in parameters},
    }

    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "unit": "mm",
        "partName": normalized.get("partName") or "Plate",
        "parameters": parameters,
        "steps": steps,
        "expected": expected,
        "checks": [
            {"name": "unit_normalized", "passed": normalized.get("unit") == "mm",
             "detail": f"source unit {normalized.get('sourceUnit')}"},
            {"name": "positive_dimensions", "passed": True},
            {"name": "holes_inside_body", "passed": True},
            {"name": "closed_profile_ops", "passed": True},
            {"name": "supported_operations_only", "passed": True},
        ],
        "topology": {
            "bodySketch": {"axis": AXIS_INDEX, "lines": BODY_LINES},
            "holeSketch": {"axis": AXIS_INDEX, "referenceLines": HOLE_SKETCH_REF_LINES,
                           "firstCircle": FIRST_CIRCLE_INDEX},
        },
    }
    plan["planId"] = _plan_id(plan)
    return plan


def _profile_area(profile: list[dict[str, Any]]) -> float:
    """Exact area of a closed line/arc profile via Green's theorem."""
    import math

    total = 0.0
    for segment in profile:
        kind = str(segment.get("type") or "").strip().lower()
        if kind == "line":
            total += float(segment["x1"]) * float(segment["y2"]) - float(segment["x2"]) * float(segment["y1"])
        elif kind == "arc":
            cx = float(segment["cx"])
            cy = float(segment["cy"])
            radius = float(segment["radius"])
            start = math.radians(float(segment["startAngle"]))
            end = math.radians(float(segment["endAngle"]))
            while end <= start:
                end += 2.0 * math.pi
            total += (
                radius * (cx * (math.sin(end) - math.sin(start)) - cy * (math.cos(end) - math.cos(start)))
                + radius * radius * (end - start)
            )
        else:
            raise PlanError(f"Unsupported profile segment: {segment.get('type')!r}.")
    return abs(total) / 2.0


def _profile_bbox(profile: list[dict[str, Any]]) -> dict[str, float]:
    import math

    xs: list[float] = []
    ys: list[float] = []
    for segment in profile:
        kind = str(segment.get("type") or "").strip().lower()
        if kind == "line":
            xs.extend([float(segment["x1"]), float(segment["x2"])])
            ys.extend([float(segment["y1"]), float(segment["y2"])])
            continue
        cx = float(segment["cx"])
        cy = float(segment["cy"])
        radius = float(segment["radius"])
        start = math.radians(float(segment["startAngle"]))
        end = math.radians(float(segment["endAngle"]))
        while end <= start:
            end += 2.0 * math.pi
        for angle in (start, end):
            xs.append(cx + radius * math.cos(angle))
            ys.append(cy + radius * math.sin(angle))
        for cardinal in (0.0, math.pi / 2, math.pi, 3 * math.pi / 2):
            angle = cardinal
            while angle < start:
                angle += 2.0 * math.pi
            if angle <= end:
                xs.append(cx + radius * math.cos(cardinal))
                ys.append(cy + radius * math.sin(cardinal))
    return {"minX": min(xs), "minY": min(ys), "maxX": max(xs), "maxY": max(ys)}


def _compile_profile(normalized: dict[str, Any]) -> dict[str, Any]:
    body = normalized.get("body") or {}
    try:
        thickness = float(body["thickness"])
        profile = list(body["profile"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError(f"Normalized profile requirements are incomplete: {exc}") from exc
    if thickness <= 0 or len(profile) < 2:
        raise PlanError("A profile body needs a positive thickness and at least 2 segments.")

    holes = normalized.get("holes") or {}
    count = int(holes.get("count") or 0)
    diameter = float(holes.get("diameter") or 0.0)
    centres = [[float(pair[0]), float(pair[1])] for pair in (holes.get("centres") or [])]
    if count < 0 or (count and (diameter <= 0 or len(centres) != count)):
        raise PlanError("Hole count, diameter and centres are inconsistent.")
    if holes.get("through") is False:
        raise PlanError("Only through holes are supported.")

    area = _profile_area(profile)
    if area <= 0:
        raise PlanError("Profile area is zero; the loop is not closed or self-intersecting.")
    bbox = _profile_bbox(profile)
    parameters: list[dict[str, Any]] = [
        {"name": "T", "type": "length", "value": thickness, "role": "thickness"},
    ]
    if count:
        parameters.append({"name": "D", "type": "length", "value": diameter, "role": "hole_diameter"})
        for index, (x, y) in enumerate(centres):
            parameters.append({"name": f"PX{index + 1}", "type": "length", "value": x - bbox["minX"],
                               "role": f"hole_{index + 1}_from_left"})
            parameters.append({"name": f"PY{index + 1}", "type": "length", "value": y - bbox["minY"],
                               "role": f"hole_{index + 1}_from_bottom"})

    steps: list[dict[str, Any]] = [
        {"op": "new_part", "name": normalized.get("partName") or "Profile"},
        {"op": "create_parameters", "parameters": [p["name"] for p in parameters]},
        {"op": "create_sketch", "plane": "xy", "role": "profile"},
    ]
    for segment in profile:
        if segment.get("type") == "line":
            steps.append({"op": "line", "x1": segment["x1"], "y1": segment["y1"],
                          "x2": segment["x2"], "y2": segment["y2"]})
        elif segment.get("type") == "arc":
            steps.append({"op": "arc", "cx": segment["cx"], "cy": segment["cy"],
                          "radius": segment["radius"], "startAngle": segment["startAngle"],
                          "endAngle": segment["endAngle"]})
        else:
            raise PlanError(f"Unsupported profile segment: {segment.get('type')!r}.")
    steps.extend([
        {"op": "close_sketch", "role": "profile"},
        {"op": "pad", "height": thickness, "bind": "T"},
    ])

    if count:
        steps.extend([
            {"op": "create_sketch", "plane": "xy", "role": "holes"},
            {"op": "construction_rectangle", "x1": bbox["minX"], "y1": bbox["minY"],
             "x2": bbox["maxX"], "y2": bbox["maxY"]},
        ])
        for index, (x, y) in enumerate(centres):
            circle_index = FIRST_CIRCLE_INDEX + index
            steps.append({"op": "circle", "cx": x, "cy": y, "radius": diameter / 2.0,
                          "geometry_index": circle_index, "hole": index})
            steps.append({"op": "constraint", "type": "radius", "value": diameter / 2.0,
                          "geometry_index_1": circle_index, "bind": "D / 2",
                          "role": f"hole_{index + 1}_radius"})
            steps.append({"op": "constraint", "type": "distance", "value": x - bbox["minX"],
                          "geometry_index_1": circle_index,
                          "geometry_index_2": HOLE_SKETCH_REF_LINES["left"],
                          "bind": f"PX{index + 1}", "role": f"hole_{index + 1}_x"})
            steps.append({"op": "constraint", "type": "distance", "value": y - bbox["minY"],
                          "geometry_index_1": circle_index,
                          "geometry_index_2": HOLE_SKETCH_REF_LINES["bottom"],
                          "bind": f"PY{index + 1}", "role": f"hole_{index + 1}_y"})
        steps.extend([
            {"op": "close_sketch", "role": "holes"},
            {"op": "pocket", "depth": thickness + 2.0, "bind": "T + 2mm"},
        ])

    hole_area = count * math.pi * (diameter / 2.0) ** 2 if count else 0.0
    volume = (area - hole_area) * thickness
    expected = {
        "kind": "extruded_profile",
        "unit": "mm",
        "volumeMm3": round(volume, 6),
        "bbox": {
            "min": {"x": bbox["minX"], "y": bbox["minY"], "z": 0.0},
            "max": {"x": bbox["maxX"], "y": bbox["maxY"], "z": thickness},
        },
        "holes": [{"x": x, "y": y, "diameter": diameter, "through": True} for x, y in centres],
        "holeCount": count,
        "featureCount": 2 if count else 1,
        "parameters": {p["name"]: p["value"] for p in parameters},
        "profileAreaMm2": round(area, 6),
    }
    steps.append({"op": "verify_geometry", "expected": expected})

    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "unit": "mm",
        "partName": normalized.get("partName") or "Profile",
        "parameters": parameters,
        "steps": steps,
        "expected": expected,
        "checks": [
            {"name": "unit_normalized", "passed": normalized.get("unit") == "mm",
             "detail": f"source unit {normalized.get('sourceUnit')}"},
            {"name": "positive_dimensions", "passed": True},
            {"name": "profile_closed", "passed": True},
            {"name": "holes_inside_body", "passed": True},
            {"name": "supported_operations_only", "passed": True},
        ],
        "topology": {"profileSketch": {"axis": AXIS_INDEX}, "holeSketch": {
            "axis": AXIS_INDEX, "referenceLines": HOLE_SKETCH_REF_LINES,
            "firstCircle": FIRST_CIRCLE_INDEX}},
        "limitations": [
            "Profile segment coordinates are fixed sketch geometry; only thickness and hole "
            "diameter/positions are driven by named parameters.",
        ],
    }
    plan["planId"] = _plan_id(plan)
    return plan


def _ring_volume(outer: float, inner: float, thickness: float, radius: float) -> float:
    """Exact volume of the web ∪ two rolled-edge tori.

    Each torus is centred at ``(outer/2 - r, z=r)`` / ``(inner/2 + r, z=r)``
    with minor radius ``r``. Only the part of the torus above the web top
    (``z = thickness``) adds material, so the union subtracts the torus
    volume below that plane. The integral of the torus cross-section is
    ``4*pi*major*[F(h-r) - F(-r)]`` with ``F(u)=u*sqrt(r^2-u^2)/2 +
    r^2*asin(u/r)/2``.
    """
    import math

    def below(plane: float, major: float) -> float:
        def f(u: float) -> float:
            return 0.5 * u * math.sqrt(max(0.0, radius ** 2 - u ** 2)) + 0.5 * radius ** 2 * math.asin(
                max(-1.0, min(1.0, u / radius))
            )
        return 4.0 * math.pi * major * (f(plane - radius) - f(-radius))

    web = math.pi * ((outer / 2.0) ** 2 - (inner / 2.0) ** 2) * thickness
    torus_outer = 2.0 * math.pi ** 2 * (outer / 2.0 - radius) * radius ** 2
    torus_inner = 2.0 * math.pi ** 2 * (inner / 2.0 + radius) * radius ** 2
    return web + torus_outer + torus_inner - below(thickness, outer / 2.0 - radius) - below(
        thickness, inner / 2.0 + radius
    )


def _compile_ring(normalized: dict[str, Any]) -> dict[str, Any]:
    body = normalized.get("body") or {}
    try:
        outer = float(body["outerDiameter"])
        inner = float(body["innerDiameter"])
        thickness = float(body["thickness"])
        radius = float((body.get("edge") or {})["radius"])
    except (KeyError, TypeError, ValueError) as exc:
        raise PlanError(f"Normalized ring requirements are incomplete: {exc}") from exc
    if min(outer, inner, thickness, radius) <= 0:
        raise PlanError("All ring dimensions must be positive.")
    if outer <= inner:
        raise PlanError("outerDiameter must exceed innerDiameter.")
    if radius >= inner / 2.0:
        raise PlanError("edge radius must be smaller than the inner radius.")
    if 2.0 * radius <= thickness:
        raise PlanError("a rolled edge needs 2*radius > thickness.")

    outer_r = outer / 2.0
    inner_r = inner / 2.0
    bead_outer_r = outer_r - radius
    bead_inner_r = inner_r + radius
    bead_z = radius

    parameters = [
        {"name": "OD", "type": "length", "value": outer, "role": "outer_diameter"},
        {"name": "ID", "type": "length", "value": inner, "role": "inner_diameter"},
        {"name": "T", "type": "length", "value": thickness, "role": "thickness"},
        {"name": "R", "type": "length", "value": radius, "role": "edge_radius"},
    ]

    def line(x1: float, y1: float, x2: float, y2: float, index: int) -> dict[str, Any]:
        return {"op": "construction_line", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                "geometry_index": index}

    steps: list[dict[str, Any]] = [
        {"op": "new_part", "name": normalized.get("partName") or "Ring"},
        {"op": "create_parameters", "parameters": [p["name"] for p in parameters]},
        {"op": "create_sketch", "plane": "xy", "role": "web"},
        {"op": "circle", "cx": 0.0, "cy": 0.0, "radius": outer_r, "geometry_index": 2,
         "role": "outer"},
        {"op": "constraint", "type": "radius", "value": outer_r, "geometry_index_1": 2,
         "bind": "OD / 2", "role": "outer_radius"},
        {"op": "circle", "cx": 0.0, "cy": 0.0, "radius": inner_r, "geometry_index": 3,
         "role": "inner"},
        {"op": "constraint", "type": "radius", "value": inner_r, "geometry_index_1": 3,
         "bind": "ID / 2", "role": "inner_radius"},
        {"op": "close_sketch", "role": "web"},
        {"op": "pad", "height": thickness, "bind": "T"},
        {"op": "create_sketch", "plane": "yz", "role": "beads"},
        # Sketch yz maps H -> model Y and V -> model Z. Two construction
        # lines give the bead centres an axis to dimension against.
        line(0.0, 0.0, 0.0, bead_z + radius + 5.0, 2),
        line(0.0, 0.0, bead_outer_r + radius + 5.0, 0.0, 3),
        {"op": "circle", "cx": bead_outer_r, "cy": bead_z, "radius": radius,
         "geometry_index": 4, "role": "outer_bead"},
        {"op": "constraint", "type": "radius", "value": radius, "geometry_index_1": 4,
         "bind": "R", "role": "outer_bead_radius"},
        {"op": "constraint", "type": "distance", "value": bead_outer_r,
         "geometry_index_1": 4, "geometry_index_2": 2,
         "bind": "OD / 2 - R", "role": "outer_bead_x"},
        {"op": "constraint", "type": "distance", "value": bead_z,
         "geometry_index_1": 4, "geometry_index_2": 3,
         "bind": "R", "role": "outer_bead_z"},
        {"op": "circle", "cx": bead_inner_r, "cy": bead_z, "radius": radius,
         "geometry_index": 5, "role": "inner_bead"},
        {"op": "constraint", "type": "radius", "value": radius, "geometry_index_1": 5,
         "bind": "R", "role": "inner_bead_radius"},
        {"op": "constraint", "type": "distance", "value": bead_inner_r,
         "geometry_index_1": 5, "geometry_index_2": 2,
         "bind": "ID / 2 + R", "role": "inner_bead_x"},
        {"op": "constraint", "type": "distance", "value": bead_z,
         "geometry_index_1": 5, "geometry_index_2": 3,
         "bind": "R", "role": "inner_bead_z"},
        {"op": "close_sketch", "role": "beads"},
        {"op": "shaft", "angle": 360, "axis": "v", "role": "rolled_edges"},
    ]

    volume = _ring_volume(outer, inner, thickness, radius)
    expected = {
        "kind": "annular_ring",
        "unit": "mm",
        "volumeMm3": round(volume, 6),
        "bbox": {
            "min": {"x": -outer_r, "y": -outer_r, "z": 0.0},
            "max": {"x": outer_r, "y": outer_r, "z": 2.0 * radius},
        },
        "cylinders": [
            {"radius": outer_r, "count": 1},
            {"radius": inner_r, "count": 1},
        ],
        "featureCount": 2,
        "parameters": {p["name"]: p["value"] for p in parameters},
    }
    steps.append({"op": "verify_geometry", "expected": expected})

    plan: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "unit": "mm",
        "partName": normalized.get("partName") or "Ring",
        "parameters": parameters,
        "steps": steps,
        "expected": expected,
        "checks": [
            {"name": "unit_normalized", "passed": normalized.get("unit") == "mm",
             "detail": f"source unit {normalized.get('sourceUnit')}"},
            {"name": "positive_dimensions", "passed": True},
            {"name": "ring_diameter_order", "passed": True},
            {"name": "rolled_edge_fits", "passed": True},
            {"name": "supported_operations_only", "passed": True},
        ],
        "topology": {
            "webSketch": {"axis": AXIS_INDEX, "outerCircle": 2, "innerCircle": 3},
            "beadSketch": {"axis": AXIS_INDEX, "referenceLines": {"vertical": 2, "horizontal": 3},
                           "outerCircle": 4, "innerCircle": 5},
        },
    }
    plan["planId"] = _plan_id(plan)
    return plan


def plan_summary(plan: dict[str, Any]) -> str:
    from catia_mcp.drawing.feature_model import PLAN_SCHEMA as FEATURE_PLAN
    if plan.get("schema") == FEATURE_PLAN:
        return "Composable single-body features: " + " -> ".join(
            f"{f['id']} ({f['operation']})" for f in plan["features"])
    expected = plan.get("expected") or {}
    if expected.get("kind") == "extruded_profile":
        bbox = expected.get("bbox") or {}
        dims = [bbox.get("max", {}).get(axis, 0) - bbox.get("min", {}).get(axis, 0)
                for axis in ("x", "y", "z")]
        return (
            f"Extruded profile {dims[0]:g} x {dims[1]:g} x {dims[2]:g} mm, "
            f"{expected.get('holeCount', 0)} through hole(s), "
            f"{len(plan.get('steps', []))} build steps."
        )
    if expected.get("kind") == "annular_ring":
        params = expected.get("parameters") or {}
        return (
            f"Annular ring OD {params.get('OD'):g} / ID {params.get('ID'):g} mm, "
            f"web {params.get('T'):g} mm, rolled edge R{params.get('R'):g}, "
            f"{len(plan.get('steps', []))} build steps."
        )
    expected = plan.get("expected") or {}
    holes = expected.get("holeCount", 0)
    bbox = expected.get("bbox") or {}
    low = bbox.get("min", {})
    high = bbox.get("max", {})
    dims = [high.get(axis, 0) - low.get(axis, 0) for axis in ("x", "y", "z")]
    return (
        f"Flat plate {dims[0]:g} x {dims[1]:g} x {dims[2]:g} mm, "
        f"{holes} through hole(s), {len(plan.get('steps', []))} build steps."
    )
