"""Evidence-backed, composable single-body feature requirements.

The agent chooses features, not raw COM calls. Dimensions are named parameters;
expressions are a small dimensional arithmetic language, never Python eval.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import math
import re
from typing import Any

SCHEMA = "catia-feature-requirements/1"
PLAN_SCHEMA = "catia-feature-plan/1"
OPERATIONS = {"pad", "pocket", "shaft", "groove", "fillet", "chamfer"}
EDGE_OPERATIONS = {"fillet", "chamfer"}
PLANES = {"xy", "yz", "zx"}
NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
IDENTIFIER = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")


def sketch_plane(value):
    """A fixed orthonormal frame, positioned by the existing signed offset."""
    if isinstance(value, str) and value in PLANES:
        return value
    if not isinstance(value, dict):
        raise FeatureError("SKETCH_PLANE_UNSUPPORTED", "Expected xy/yz/zx or a fixed normal/xDirection frame.")
    _object(value, {"normal", "xDirection"}, "sketch.plane")
    axes = []
    for key in ("normal", "xDirection"):
        axis = value.get(key)
        if (not isinstance(axis, list) or len(axis) != 3
                or any(type(x) not in (int, float) or abs(x)>1.000001 or not math.isfinite(x) for x in axis)
                or abs(sum(x*x for x in axis)-1) > 1e-7):
            raise FeatureError("SKETCH_FRAME_INVALID", f"{key} must be a finite unit vector.")
        axes.append([float(x) for x in axis])
    if abs(sum(a*b for a,b in zip(*axes))) > 1e-7:
        raise FeatureError("SKETCH_FRAME_INVALID", "normal and xDirection must be perpendicular.")
    return dict(zip(("normal", "xDirection"), axes))


class FeatureError(ValueError):
    def __init__(self, code: str, message: str, path: str = ""):
        super().__init__(message)
        self.code, self.path = code, path


def _object(value, allowed, path):
    if not isinstance(value, dict):
        raise FeatureError("STRUCTURE_INVALID", "Expected an object.", path)
    unknown = set(value) - set(allowed)
    if unknown:
        raise FeatureError("FIELD_UNSUPPORTED", f"Unknown fields: {sorted(unknown, key=str)}", path)


def _references(value, path):
    if (not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value)
            or len(set(value)) != len(value)):
        raise FeatureError("REFERENCES_INVALID", "Expected unique nonempty string references.", path)


def _checked_evidence(manifest):
    from catia_mcp.drawing.requirements import _is_context_evidence

    if manifest is None:
        return {}
    if not isinstance(manifest, dict) or not isinstance(manifest.get("pages"), list):
        raise FeatureError("EVIDENCE_INVALID", "Evidence must contain a pages array.")
    index = {}
    for page in manifest["pages"]:
        if not isinstance(page, dict) or not isinstance(page.get("spans"), list):
            raise FeatureError("EVIDENCE_INVALID", "Every evidence page needs a spans array.")
        for span in page["spans"]:
            if (not isinstance(span, dict) or not isinstance(span.get("id"), str)
                    or not span["id"] or not isinstance(span.get("text"), str)
                    or span["id"].startswith("ref3d:") or span["id"] in index):
                raise FeatureError("EVIDENCE_INVALID", "Invalid or duplicate evidence span.")
            index[span["id"]] = span
    context = manifest.get("contextEvidence", [])
    if not isinstance(context, list):
        raise FeatureError("EVIDENCE_INVALID", "Context evidence must be an array.")
    context_hashes = (manifest.get("contextSource") or {}).get("sources") or {}
    for span in context:
        if not _is_context_evidence(span) or span["id"] in index:
            raise FeatureError("EVIDENCE_INVALID", "Invalid or duplicate context evidence.")
        if span.get("sourceKind") == "referenceGeometry":
            expected = context_hashes.get("referenceGeometry")
            if expected and span.get("sourceSha256") != expected:
                raise FeatureError(
                    "EVIDENCE_INVALID",
                    "Reference geometry evidence hash does not match the context source.",
                )
        index[span["id"]] = span
    return index


def _dimension_evidence_matches(span, number, kind):
    """Accept a source-declared raw measurement only alongside its rounded display."""
    from catia_mcp.drawing.requirements import (
        _contains_number, _without_multiplicity, _block_definition_text,
        _reference_geometry_number_matches,
    )
    if _block_definition_text(span):
        return False
    if _reference_geometry_number_matches(span, number):
        return True
    if _contains_number(_without_multiplicity(span["text"]), number):
        return True
    raw = span.get("rawMeasurement")
    if (kind != "length" or span.get("sourceKind") != "dimensions"
            or not isinstance(raw, dict) or raw.get("basis") != "declared_drawing_unit"
            or raw.get("unit") != "drawing_unit" or raw.get("dimensionType") not in (0, 1, 3, 4)
            or type(raw.get("dimensionType")) is not int
            or type(raw.get("lengthScaleFactor")) not in (int, float)
            or not math.isfinite(raw["lengthScaleFactor"])
            or not (0 < raw["lengthScaleFactor"] <= 1e6 or raw["lengthScaleFactor"] == -1)):
        return False
    override = raw.get("textOverride")
    if override is None and raw.get("textOverridePresent") is True:
        override = ""
    if not isinstance(override, str):
        return False
    diameter_template = override.lower() == "%%c<>"
    multiple_template = re.fullmatch(r"\(\s*([1-9]\d{0,2})\s*[xX]\s*\)\s*<>", override)
    if override not in ("", "<>") and not diameter_template and not multiple_template:
        return False
    if diameter_template and raw["dimensionType"] not in (0, 1, 3):
        return False
    value = raw.get("value")
    prefix = (r"[Ø⌀∅]\s*" if diameter_template else
              {0: "", 1: "", 3: r"[Ø⌀∅]\s*", 4: r"[Rr]\s*"}[raw["dimensionType"]])
    if multiple_template:
        prefix = r"\(\s*" + re.escape(multiple_template.group(1)) + r"\s*[xX]\s*\)\s*" + prefix
    match = re.fullmatch(r"\s*" + prefix + r"([+-]?\d+(?:[.,]\d+)?)\s*", span["text"])
    if type(value) not in (int, float) or not math.isfinite(value) or not match:
        return False
    scale = abs(raw["lengthScaleFactor"])
    scaled_value = value * scale
    # Some converted dimensions already store the scaled value. Require
    # agreement with the dimension geometry before using that interpretation.
    points = raw.get("definitionPoints")
    if (raw.get("coordinateSystem") == "layout_wcs" and isinstance(points, list)
            and all(isinstance(p, list) and len(p) == 3 and all(
                type(v) in (int, float) and math.isfinite(v) for v in p) for p in points)):
        geometric_value = None
        if raw["dimensionType"] == 4 and len(points) == 2:
            geometric_value = math.dist(*points)
        elif (raw["dimensionType"] == 0 and len(points) == 3
              and raw.get("definitionPointNames") == ["defpoint", "defpoint2", "defpoint3"]
              and abs(points[1][2] - points[2][2]) < 1e-8
              and raw.get("dimensionExtrusion") == [0, 0, 1]
              and type(raw.get("dimensionRotationDegrees")) in (int, float)
              and math.isfinite(raw["dimensionRotationDegrees"])):
            angle = math.radians(raw["dimensionRotationDegrees"])
            geometric_value = abs((points[2][0] - points[1][0]) * math.cos(angle)
                                  + (points[2][1] - points[1][1]) * math.sin(angle))
        if geometric_value is not None and math.isclose(geometric_value * scale, value, rel_tol=1e-10, abs_tol=1e-8):
            scaled_value = value
    value = scaled_value
    if not math.isfinite(value) or not math.isclose(value, number, rel_tol=1e-10, abs_tol=1e-8):
        return False
    displayed = match.group(1).replace(",", ".")
    precision = len(displayed.partition(".")[2])
    return abs(value - float(displayed)) <= 0.5 * 10 ** -precision + 1e-8


def _direct_dimension_span(span):
    """Extract an unambiguous nominal, excluding supported tolerances."""
    from catia_mcp.drawing.requirements import _without_multiplicity
    text = _without_multiplicity(span.get("text", "").replace("%%C", "Ø").replace("%%c", "Ø"))
    number = r"[+-]?(?:\d+(?:[.,]\d+)?|\.\d+)"
    magnitude = r"(?:\d+(?:[.,]\d+)?|\.\d+)"
    match = re.fullmatch(r"\s*(?P<nominal>(?:[RrØ⌀∅]\s*)?" + number + r")"
                         r"(?:\s*(?:(?:±|\+/-)\s*" + magnitude
                         + r"|\+\s*" + magnitude + r"\s*/\s*-\s*" + magnitude
                         + r"))?\s*(?:mm|in|°)?\s*",
                         text, flags=re.IGNORECASE)
    return {**span, "text": match.group("nominal")} if match else None


def expression(value: Any, parameters: dict[str, dict], kind: str = "length") -> dict:
    """Return value, canonical CATIA formula and parameter dependencies."""
    expected = {"length": (1, 0), "angle": (0, 1), "real": (0, 0)}[kind]
    if ((value == 0 and not isinstance(value, bool))
            or (isinstance(value, str) and len(value) <= 256
                and re.fullmatch(r"[+-]?(?:0+(?:\.0*)?|\.0+)", value.strip()))):
        return {"value": 0.0, "formula": "0" + {"length": "mm", "angle": "deg", "real": ""}[kind], "parameters": []}
    if not isinstance(value, str) or len(value) > 256:
        raise FeatureError("EXPRESSION_INVALID", "Use named dimensions and arithmetic; only literal zero is allowed.")
    try:
        tree = ast.parse(value, mode="eval")
    except SyntaxError as exc:
        raise FeatureError("EXPRESSION_INVALID", str(exc)) from exc
    if len(list(ast.walk(tree))) > 256:
        raise FeatureError("EXPRESSION_INVALID", "Expression is too complex.")
    names: set[str] = set()

    def visit(node):
        if isinstance(node, ast.Name) and node.id in parameters:
            p = parameters[node.id]
            names.add(node.id)
            return float(p["value"]), {"length": (1, 0), "angle": (0, 1)}[p["type"]], node.id
        if isinstance(node, ast.Constant) and type(node.value) in (float, int):
            number = float(node.value)
            if not math.isfinite(number) or abs(number) > 1e6:
                raise FeatureError("EXPRESSION_INVALID", "Numeric coefficient is outside the supported range.")
            return number, (0, 0), repr(number)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            n, dimension, formula = visit(node.operand)
            return (-n if isinstance(node.op, ast.USub) else n), dimension, ("-" if isinstance(node.op, ast.USub) else "+") + f"({formula})"
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"sin", "cos", "tan"} and len(node.args) == 1 and not node.keywords):
            angle, dimension, formula = visit(node.args[0])
            if dimension != (0, 1):
                raise FeatureError("UNIT_MISMATCH", "Trigonometric functions require an angle.")
            radians = math.radians(angle)
            if node.func.id == "tan" and abs(math.cos(radians)) < 1e-12:
                raise FeatureError("EXPRESSION_INVALID", "Tangent is singular at this angle.")
            result = {"sin": math.sin, "cos": math.cos, "tan": math.tan}[node.func.id](radians)
            return result, (0, 0), f"{node.func.id}({formula})"
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
            a, da, fa = visit(node.left)
            b, db, fb = visit(node.right)
            if isinstance(node.op, (ast.Add, ast.Sub)):
                # Eliminate a literal additive zero rather than embedding a
                # unit-tagged zero, which the plan recompiler cannot parse.
                units = {(1, 0), (0, 1)}
                if da == (0, 0) and isinstance(node.left, ast.Constant) and node.left.value == 0 and db in units:
                    return (-b, db, f"-({fb})") if isinstance(node.op, ast.Sub) else (b, db, fb)
                if db == (0, 0) and isinstance(node.right, ast.Constant) and node.right.value == 0 and da in units:
                    return a, da, fa
                if da != db:
                    raise FeatureError("UNIT_MISMATCH", f"Addition/subtraction requires matching dimensions in {value!r}.")
                subtract = isinstance(node.op, ast.Sub)
                return a - b if subtract else a + b, da, f"({fa} {'-' if subtract else '+'} {fb})"
            if isinstance(node.op, ast.Mult):
                return a * b, tuple(x + y for x, y in zip(da, db)), f"({fa} * {fb})"
            if b == 0:
                raise FeatureError("EXPRESSION_INVALID", "Division by zero.")
            return a / b, tuple(x - y for x, y in zip(da, db)), f"({fa} / {fb})"
        raise FeatureError("EXPRESSION_INVALID", "Only known parameters, +, -, *, /, and sin/cos/tan of angles are allowed.")

    result, dimension, formula = visit(tree.body)
    if dimension != expected or not names:
        raise FeatureError("UNIT_MISMATCH", f"Expression must yield a named {kind} dimension.")
    if not math.isfinite(result) or abs(result) > 1e6:
        raise FeatureError("EXPRESSION_INVALID", "Expression result is nonfinite or exceeds the supported range.")
    return {"value": result, "formula": formula, "parameters": sorted(names)}


def coord_expression(value: Any, parameters: dict[str, dict], kind: str = "length") -> dict:
    """A sketch coordinate: a literal number is allowed, a string stays symbolic.

    Only main dimensions must be evidence-backed named parameters. A coordinate
    transcribed from a drawing schedule can be literal; the independent OCCT
    comparison still checks the resulting geometry.
    """
    if type(value) in (int, float) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number) or abs(number) > 1e6:
            raise FeatureError("EXPRESSION_INVALID", "Coordinate is outside the supported range.")
        unit = {"length": "mm", "angle": "deg", "real": ""}[kind]
        return {"value": number, "formula": f"{repr(number)}{unit}", "parameters": []}
    return expression(value, parameters, kind)


def _simple_polygon(points):
    """Reject zero edges, retraced adjacent edges, and nonadjacent intersections."""
    xy = [(p["x"]["value"], p["y"]["value"]) for p in points]
    edges = list(zip(xy, xy[1:] + xy[:1]))
    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    def touches(a, b, c, d):
        if any(max(a[k], b[k]) < min(c[k], d[k]) - 1e-8 or
               max(c[k], d[k]) < min(a[k], b[k]) - 1e-8 for k in (0, 1)):
            return False
        return cross(a, b, c) * cross(a, b, d) <= 1e-16 and cross(c, d, a) * cross(c, d, b) <= 1e-16
    if abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in edges)) < 1e-8:
        raise FeatureError("PROFILE_INVALID", "Polygon must enclose a nonzero area.")
    for i, (a, b) in enumerate(edges):
        if math.dist(a, b) < 1e-8:
            raise FeatureError("PROFILE_INVALID", "Polygon has a zero-length edge.")
        c = edges[(i + 1) % len(edges)][1]
        if abs(cross(a, b, c)) < 1e-8 and sum((b[k] - a[k]) * (c[k] - b[k]) for k in (0, 1)) < 0:
            raise FeatureError("PROFILE_INVALID", "Polygon retraces an adjacent edge.")
        for j in range(i + 1, len(edges)):
            if j == i + 1 or (i == 0 and j == len(edges) - 1):
                continue
            if touches(a, b, *edges[j]):
                raise FeatureError("PROFILE_INVALID", "Polygon crosses or touches itself.")


def _number_vector(value, label):
    if (not isinstance(value, list) or len(value) != 3
            or any(type(v) not in (int, float) or not math.isfinite(v) or abs(v) > 1e6 for v in value)):
        raise FeatureError("EDGE_SELECTOR_INVALID", f"{label} must be three finite numbers.", label)
    numbers = [float(v) for v in value]
    if all(abs(v) < 1e-12 for v in numbers):
        raise FeatureError("EDGE_SELECTOR_INVALID", f"{label} must be a nonzero direction.", label)
    return numbers


def _point_vector(value, parameters, label):
    if not isinstance(value, list) or len(value) != 3:
        raise FeatureError("EDGE_SELECTOR_INVALID", f"{label} must be three length expressions.", label)
    return [coord_expression(item, parameters) for item in value]


def edge_selector(value, parameters, fid):
    """Compile an exact edge selector resolved identically by CATIA and OCCT.

    ``linear`` matches a line by direction and one point it passes through, so
    the match survives a neighbouring fillet shortening the edge. ``circular``
    matches a circle by radius, centre and axis. Both sides require exactly one
    match; an index-based edge reference would not survive a topology change.
    """
    _object(value, {"type", "direction", "through", "radius", "centre", "axis", "withinSegment"}, f"{fid}.edges")
    kind = value.get("type")
    if kind == "linear":
        _object(value, {"type", "direction", "through", "withinSegment"}, f"{fid}.edges")
        result = {"type": "linear",
                "direction": _number_vector(value.get("direction"), f"{fid}.edges.direction"),
                "through": _point_vector(value.get("through"), parameters, f"{fid}.edges.through")}
        if "withinSegment" in value:
            if type(value["withinSegment"]) is not bool:
                raise FeatureError("EDGE_SELECTOR_INVALID", "withinSegment must be boolean.", fid)
            result["withinSegment"] = value["withinSegment"]
        return result
    if kind == "circular":
        _object(value, {"type", "radius", "centre", "axis"}, f"{fid}.edges")
        radius = expression(value.get("radius"), parameters)
        if radius["value"] <= 0:
            raise FeatureError("DIMENSION_INVALID", f"{fid} needs a positive edge radius.", fid)
        return {"type": "circular", "radius": radius,
                "centre": _point_vector(value.get("centre"), parameters, f"{fid}.edges.centre"),
                "axis": _number_vector(value.get("axis"), f"{fid}.edges.axis")}
    raise FeatureError("EDGE_SELECTOR_INVALID", "Edge type must be 'linear' or 'circular'.", fid)


def selector_dimensions(selector):
    if isinstance(selector,list):
        return [dimension for item in selector for dimension in selector_dimensions(item)]
    if selector["type"] == "circular":
        return [selector["radius"], *selector["centre"]]
    return list(selector["through"])


def arc_geometry(start, end, radius, clockwise=False, large_arc=False):
    """Resolve an endpoint/radius arc to its centre and signed sweep in radians."""
    dx, dy = end[0] - start[0], end[1] - start[1]
    chord = math.hypot(dx, dy)
    if radius <= 0 or chord < 1e-8 or chord > 2 * radius + 1e-8:
        raise FeatureError("PROFILE_INVALID", "Arc needs distinct endpoints and radius at least half the chord.")
    # Near a semicircle, subtracting nearly equal squares amplifies the
    # rounding already present in endpoint coordinates into a false offset.
    errors = [(math.ulp(start[i]) + math.ulp(end[i])) if start[i] != end[i] else 0.0 for i in (0, 1)]
    rounding = sum(2 * abs(delta) * error + error * error for delta, error in zip((dx, dy), errors)) / chord
    rounding += 4 * (math.ulp(radius) + math.ulp(chord))
    height = (0.0 if abs(2 * radius - chord) <= rounding else
              math.sqrt(max(0, (radius - chord / 2) * (radius + chord / 2))))
    side = (-1 if clockwise else 1) * (-1 if large_arc else 1)
    center = ((start[0] + end[0]) / 2 - side * dy * height / chord,
              (start[1] + end[1]) / 2 + side * dx * height / chord)
    first = math.atan2(start[1] - center[1], start[0] - center[0])
    last = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = -((first - last) % math.tau) if clockwise else (last - first) % math.tau
    return center, first, sweep


def _compact_point(value):
    """A 2D point may be {"x":..,"y":..} or the compact [x, y]."""
    if isinstance(value, list) and len(value) == 2:
        return {"x": value[0], "y": value[1]}
    return value


def _compact_segment(value):
    """A wire segment may be an object or ["line",x,y] / ["arc",x,y,r,cw,large]."""
    if isinstance(value, list) and value:
        kind = value[0]
        if kind == "line" and len(value) == 3:
            return {"type": "line", "end": {"x": value[1], "y": value[2]}}
        if kind == "arc" and len(value) >= 4:
            return {"type": "arc", "end": {"x": value[1], "y": value[2]},
                    "radius": value[3],
                    "clockwise": bool(value[4]) if len(value) > 4 else False,
                    "largeArc": bool(value[5]) if len(value) > 5 else False}
    return value


def _compact_profile(value):
    """Accept [kind, ...] arrays for the four profile kinds.

    The derived schedule emits exactly these arrays (START/SEGS rows) so an
    agent can copy a complex arc profile without re-deriving endpoints.
    """
    if not isinstance(value, list) or not value:
        return value
    kind = value[0]
    if kind == "circle" and len(value) == 4:
        return {"type": "circle", "x": value[1], "y": value[2], "radius": value[3]}
    if kind == "rectangle" and len(value) == 5:
        return {"type": "rectangle", "x": value[1], "y": value[2],
                "width": value[3], "height": value[4]}
    if kind == "polygon" and len(value) == 2 and isinstance(value[1], list):
        return {"type": "polygon", "points": [_compact_point(point) for point in value[1]]}
    if kind == "wire" and len(value) == 3:
        return {"type": "wire", "start": _compact_point(value[1]),
                "segments": [_compact_segment(segment) for segment in value[2]]}
    return value


def compile_features(req: dict, manifest: dict | None, *, require_evidence: bool = True,
                     reference_evidence: dict | None = None, _recompile_plan: bool = False) -> dict:
    from catia_mcp.drawing.requirements import UNIT_TO_MM
    from catia_mcp.drawing.units import unit_evidence_matches

    _object(req, {"schema", "unit", "unitEvidence", "parameters", "features", "partName",
                  "profiles"}, "requirements")
    part_name = req.get("partName", "PDFPart")
    if not isinstance(part_name, str) or not IDENTIFIER.fullmatch(part_name):
        raise FeatureError("PART_NAME_INVALID", "partName must be a simple identifier.", "partName")
    if req.get("schema") != SCHEMA:
        raise FeatureError("SCHEMA_MISMATCH", f"Expected {SCHEMA}.")
    scale = UNIT_TO_MM.get(str(req.get("unit", "")).lower())
    if scale is None:
        raise FeatureError("UNIT_MISSING", "The drawing must declare mm or inch.", "unit")
    index = _checked_evidence(manifest)
    unit_refs = req.get("unitEvidence", [])
    _references(unit_refs, "unitEvidence")
    if require_evidence:
        if not unit_refs or any(ref not in index for ref in unit_refs):
            raise FeatureError("EVIDENCE_MISSING", "Cite the drawing's unit declaration.", "unitEvidence")
        unit_text = " ".join(index[ref]["text"] for ref in unit_refs).lower()
        if not unit_evidence_matches(unit_text, scale):
            raise FeatureError("EVIDENCE_VALUE_MISMATCH", "Unit citation does not state the selected unit.", "unitEvidence")
    declarations = req.get("parameters")
    if not isinstance(declarations, list) or not 1 <= len(declarations) <= 512:
        raise FeatureError("PARAMETERS_INVALID", "Declare between 1 and 512 named dimensions.")
    parameters = {}
    for p in declarations:
        _object(p, {"name", "type", "value", "evidence", "source"}, "parameters")
        name = p.get("name", "")
        if not isinstance(name, str) or not NAME.fullmatch(name) or name in parameters:
            raise FeatureError("PARAMETER_NAME_INVALID", f"Invalid or duplicate parameter {name!r}.")
        kind = p.get("type", "length")
        number = p.get("value")
        if not isinstance(kind, str) or kind not in {"length", "angle"} or type(number) not in (int, float) or not math.isfinite(number) or abs(number) > 1e6:
            raise FeatureError("PARAMETER_INVALID", f"Invalid dimension {name}.", name)
        refs = p.get("evidence", [])
        _references(refs, f"parameters.{name}.evidence")
        reference_refs = [ref for ref in refs if ref.startswith("ref3d:")]
        source = p.get("source", "drawing")
        if source not in {"drawing", "reference_inference"} or bool(reference_refs) != (source == "reference_inference"):
            raise FeatureError("EVIDENCE_SOURCE_INVALID", "Mark reference-derived parameters as reference_inference.", name)
        if require_evidence or (reference_refs and not _recompile_plan):
            reference_index = reference_evidence or {}
            if not refs or any(ref not in (reference_index if ref.startswith("ref3d:") else index) for ref in refs):
                raise FeatureError("EVIDENCE_MISSING", f"Dimension {name} needs valid drawing citations.", name)
            drawing_refs = [ref for ref in refs if not ref.startswith("ref3d:")]
            drawing_matches = any(_dimension_evidence_matches(index[ref], number, kind) for ref in drawing_refs)
            reference_matches = all(kind == "length" and math.isclose(reference_index[ref]["value"], number * scale,
                                    rel_tol=1e-10, abs_tol=1e-8) for ref in reference_refs)
            # Every explicit drawing value cited alongside a 3D inference must
            # agree; one matching citation cannot erase another contradiction.
            if reference_refs:
                for ref in drawing_refs:
                    direct = _direct_dimension_span(index[ref])
                    if direct and not _dimension_evidence_matches(direct, number, kind):
                        raise FeatureError("EVIDENCE_CONFLICT", f"Cited drawing and reference values conflict for {name}.", name)
            if (drawing_refs and not drawing_matches) or not reference_matches:
                raise FeatureError("EVIDENCE_VALUE_MISMATCH", f"No cited annotation contains {name}={number}.", name)
        parameters[name] = {"name": name, "type": kind,
                            "value": number * (scale if kind == "length" else 1), "evidence": list(refs),
                            **({"source": source} if reference_refs else {})}
    profile_table = req.get("profiles") or {}
    if not isinstance(profile_table, dict):
        raise FeatureError("PROFILES_INVALID", "profiles must be an object of named profiles.")
    if len(profile_table) > 512:
        raise FeatureError("PROFILES_INVALID", "At most 512 named profiles are supported.")
    for table_name, table_value in profile_table.items():
        if not isinstance(table_name, str) or not re.fullmatch(r"P[0-9]{1,4}", table_name):
            raise FeatureError("PROFILES_INVALID",
                               "Profile names use the drawing ids P<number>.", "profiles")
        _compact_profile(table_value)
    declared_features = req.get("features")
    if not isinstance(declared_features, list) or not 1 <= len(declared_features) <= 256:
        raise FeatureError("FEATURES_INVALID", "Declare between 1 and 256 ordered features.")
    features = []
    ids = set()
    used = set()
    for i, feature in enumerate(declared_features):
        _object(feature, {"id", "operation", "dependsOn", "sketch", "length", "angle", "direction",
                          "radius", "edges", "axisX", "through"}, f"features[{i}]")
        fid, op = feature.get("id", ""), feature.get("operation")
        if not isinstance(fid, str) or not IDENTIFIER.fullmatch(fid) or fid in ids:
            raise FeatureError("FEATURE_ID_INVALID", f"Invalid or duplicate feature {fid!r}.")
        if (not isinstance(op, str) or op not in OPERATIONS
                or (i == 0 and op in {"pocket", "groove", "fillet", "chamfer"})):
            raise FeatureError("FEATURE_OPERATION_UNSUPPORTED", f"Invalid operation {op!r} at {fid}.", fid)
        if "through" in feature and (op != "pocket" or type(feature["through"]) is not bool):
            raise FeatureError("FIELD_UNSUPPORTED", f"{fid}.through must be a boolean on a pocket.", fid)
        dependencies = feature.get("dependsOn", [])
        _references(dependencies, f"{fid}.dependsOn")
        if not isinstance(dependencies, list) or any(d not in ids for d in dependencies):
            raise FeatureError("FEATURE_DEPENDENCY_INVALID", f"Dependencies of {fid} must name earlier features.", fid)
        if i and declared_features[i - 1]["id"] not in dependencies:
            raise FeatureError("FEATURE_DEPENDENCY_INVALID", "Each single-body feature must depend on the previous solid state.", fid)
        if op in EDGE_OPERATIONS:
            requested_edges = feature.get("edges")
            if isinstance(requested_edges,list):
                if op != "fillet":
                    raise FeatureError("EDGE_SELECTOR_INVALID","Edge groups are supported only for fillet.",fid)
                if not 2 <= len(requested_edges) <= 64:
                    raise FeatureError("EDGE_SELECTOR_INVALID","A fillet group needs 2-64 selectors.",fid)
                selector = [edge_selector(item,parameters,fid) for item in requested_edges]
                signatures = [json.dumps(item,sort_keys=True) for item in selector]
                if len(set(signatures)) != len(signatures):
                    raise FeatureError("EDGE_SELECTOR_INVALID","A fillet group contains duplicate selectors.",fid)
            else:
                selector = edge_selector(requested_edges, parameters, fid)
            if op == "fillet":
                _object(feature, {"id", "operation", "dependsOn", "radius", "edges"}, f"features[{i}]")
                size = expression(feature.get("radius"), parameters)
                angle = None
            else:
                _object(feature, {"id", "operation", "dependsOn", "length", "edges", "angle"},
                        f"features[{i}]")
                size = expression(feature.get("length"), parameters)
                angle = feature.get("angle", 45)
                if (type(angle) not in (int, float) or not math.isfinite(angle)
                        or not 1 <= float(angle) <= 89):
                    raise FeatureError("DIMENSION_INVALID", f"{fid}.angle must be between 1 and 89 degrees.", fid)
                if float(angle) != 45:
                    # The independent OCCT evaluator applies a symmetric chamfer;
                    # an asymmetric CATIA chamfer would have no matching expectation.
                    raise FeatureError(
                        "EDGE_OPERATION_UNSUPPORTED",
                        f"{fid}: only 45-degree chamfers are implemented.", fid)
                angle = float(angle)
            if size["value"] <= 0:
                raise FeatureError("DIMENSION_INVALID", f"{fid} needs a positive finishing size.", fid)
            edge_dimensions = [size, *selector_dimensions(selector)]
            used.update(dimension_name for dimension in edge_dimensions
                        for dimension_name in dimension["parameters"])
            refs = set()
            for dimension in edge_dimensions:
                for name in dimension["parameters"]:
                    refs.update(parameters[name]["evidence"])
            features.append({"id": fid, "operation": op, "dependsOn": list(dependencies),
                             "edges": selector, "size": size, "angle": angle,
                             "evidence": sorted(refs)})
            ids.add(fid)
            continue
        unused_extent = "length" if op in {"shaft", "groove"} else "angle"
        if unused_extent in feature:
            raise FeatureError("FIELD_UNSUPPORTED", f"{op} does not consume {unused_extent}.", fid)
        if "radius" in feature or "edges" in feature:
            raise FeatureError("FIELD_UNSUPPORTED", f"{op} does not consume finishing fields.", fid)
        if "axisX" in feature and op not in {"shaft", "groove"}:
            raise FeatureError("FIELD_UNSUPPORTED", f"{op} does not consume axisX.", fid)
        sketch = feature.get("sketch", {})
        _object(sketch, {"plane", "offset", "profiles"}, f"{fid}.sketch")
        plane = sketch_plane(sketch.get("plane"))
        offset = coord_expression(sketch.get("offset", 0), parameters)
        profiles = sketch.get("profiles")
        if not isinstance(profiles, list) or not 1 <= len(profiles) <= 256:
            raise FeatureError("PROFILE_INVALID", "A sketch needs 1–256 closed profiles.", fid)
        resolved = []
        for item in profiles:
            if isinstance(item, str):
                if item not in profile_table:
                    raise FeatureError("PROFILE_REFERENCE_INVALID",
                                       f"Unknown profile reference {item!r}.", fid)
                resolved.append(copy.deepcopy(profile_table[item]))
            else:
                resolved.append(item)
        profiles = resolved
        compiled_profiles = []
        dimensions = [offset]
        axis_x = None
        if "axisX" in feature:
            axis_x = coord_expression(feature["axisX"], parameters)
            dimensions.append(axis_x)
        for profile in profiles:
            profile = _compact_profile(profile)
            _object(profile, {"type", "x", "y", "width", "height", "radius", "points", "start", "segments"}, f"{fid}.profiles")
            ptype = profile.get("type")
            if not isinstance(ptype, str):
                raise FeatureError("PROFILE_INVALID", "Profile type must be a string.", fid)
            if ptype == "wire":
                _object(profile, {"type", "start", "segments"}, f"{fid}.profiles")
                def point(value):
                    value = _compact_point(value)
                    _object(value, {"x", "y"}, f"{fid}.wire.point")
                    compiled = {key: coord_expression(value.get(key), parameters) for key in ("x", "y")}
                    dimensions.extend(compiled.values())
                    return compiled
                def xy(value):
                    return value["x"]["value"], value["y"]["value"]
                start = point(profile.get("start"))
                current = start
                segments = profile.get("segments")
                if not isinstance(segments, list) or not 2 <= len(segments) <= 256:
                    raise FeatureError("PROFILE_INVALID", "Wire needs 2–256 ordered line/arc segments.", fid)
                compiled_segments = []
                for segment in segments:
                    segment = _compact_segment(segment)
                    _object(segment, {"type", "end", "radius", "clockwise", "largeArc"}, f"{fid}.segments")
                    kind = segment.get("type")
                    if kind not in ("line", "arc"):
                        raise FeatureError("PROFILE_INVALID", "Wire segment must be line or arc.", fid)
                    _object(segment, {"type", "end"} if kind == "line" else {"type", "end", "radius", "clockwise", "largeArc"}, f"{fid}.segments")
                    end = point(segment.get("end"))
                    if math.dist(xy(current), xy(end)) < 1e-8:
                        raise FeatureError("PROFILE_INVALID", "Wire has a zero-length segment.", fid)
                    compiled = {"type": kind, "end": end}
                    if kind == "arc":
                        radius = coord_expression(segment.get("radius"), parameters)
                        clockwise, large = segment.get("clockwise", False), segment.get("largeArc", False)
                        if type(clockwise) is not bool or type(large) is not bool:
                            raise FeatureError("PROFILE_INVALID", "Arc direction/largeArc must be booleans.", fid)
                        arc_geometry(xy(current), xy(end), radius["value"], clockwise, large)
                        dimensions.append(radius)
                        compiled.update(radius=radius, clockwise=clockwise, largeArc=large)
                    compiled_segments.append(compiled)
                    current = end
                if math.dist(xy(current), xy(start)) > 1e-8:
                    raise FeatureError("PROFILE_INVALID", "Last wire endpoint must equal its start point.", fid)
                compiled_profiles.append({"type": "wire", "start": start, "segments": compiled_segments})
                continue
            if ptype == "polygon":
                _object(profile, {"type", "points"}, f"{fid}.profiles")
                points = profile.get("points")
                if not isinstance(points, list) or not 3 <= len(points) <= 256:
                    raise FeatureError("PROFILE_INVALID", "Polygon needs 3–256 ordered vertices; closure is implicit.", fid)
                compiled_points = []
                for point in points:
                    point = _compact_point(point)
                    _object(point, {"x", "y"}, f"{fid}.points")
                    vertex = {key: coord_expression(point.get(key), parameters) for key in ("x", "y")}
                    dimensions.extend(vertex.values())
                    compiled_points.append(vertex)
                _simple_polygon(compiled_points)
                compiled_profiles.append({"type": ptype, "points": compiled_points})
                continue
            fields = {"rectangle": ("x", "y", "width", "height"), "circle": ("x", "y", "radius")}.get(ptype)
            if fields is None:
                raise FeatureError("PROFILE_UNSUPPORTED", f"Unknown profile {ptype!r}.", fid)
            _object(profile, {"type", *fields}, f"{fid}.profiles")
            compiled = {"type": ptype}
            for field in fields:
                compiled[field] = (coord_expression(profile.get(field, 0), parameters)
                                   if field in {"x", "y"}
                                   else expression(profile.get(field), parameters))
                if field not in {"x", "y"} and compiled[field]["value"] <= 0:
                    raise FeatureError("DIMENSION_INVALID", f"{fid}.{field} must be positive.", fid)
                dimensions.append(compiled[field])
            compiled_profiles.append(compiled)
        raw_extent = feature.get("angle") if op in {"shaft", "groove"} else feature.get("length")
        full_revolution = op in {"shaft", "groove"} and (
            (type(raw_extent) in (int, float) and raw_extent == 360)
            or (isinstance(raw_extent, str) and re.fullmatch(r"\s*360(?:\.0+)?\s*", raw_extent) is not None))
        amount = ({"value": 360.0, "formula": "360deg", "parameters": []} if full_revolution else
                  expression(raw_extent, parameters, "angle" if op in {"shaft", "groove"} else "length"))
        if amount["value"] <= 0 or (op in {"shaft", "groove"} and amount["value"] > 360):
            raise FeatureError("DIMENSION_INVALID", f"Invalid extent for {fid}.", fid)
        direction = feature.get("direction", "normal")
        if not isinstance(direction, str) or direction not in {"normal", "reverse"}:
            raise FeatureError("DIRECTION_INVALID", f"Invalid direction for {fid}.", fid)
        if op in {"shaft", "groove"} and direction != "normal":
            raise FeatureError("DIRECTION_INVALID", "Revolutions use the sketch vertical axis in the positive direction.", fid)
        dimensions.append(amount)
        refs = set()
        for dimension in dimensions:
            used.update(dimension["parameters"])
            for name in dimension["parameters"]:
                refs.update(parameters[name]["evidence"])
        features.append({"id": fid, "operation": op, "dependsOn": list(dependencies),
                         "sketch": {"plane": plane, "offset": offset, "profiles": compiled_profiles},
                         "extent": amount, "direction": direction, "evidence": sorted(refs)})
        if "through" in feature:
            features[-1]["through"] = feature["through"]
        if axis_x is not None:
            features[-1]["axisX"] = axis_x
        ids.add(fid)
    if set(parameters) - used:
        raise FeatureError("UNUSED_PARAMETERS", f"Unused dimensions: {sorted(set(parameters) - used)}")
    plan = {"schema": PLAN_SCHEMA, "partName": req.get("partName", "PDFPart"),
            "parameters": list(parameters.values()), "features": features,
            "source": (manifest or {}).get("source"), "unit": "mm"}
    plan["planId"] = "sha256:" + hashlib.sha256(json.dumps(plan, sort_keys=True, allow_nan=False).encode()).hexdigest()
    return plan


def validate_features(req, evidence=None, *, require_evidence=True, reference_evidence=None):
    from catia_mcp.drawing.requirements import _load_evidence

    try:
        plan = compile_features(req, _load_evidence(evidence), require_evidence=require_evidence,
                                reference_evidence=reference_evidence)
    except (FeatureError, TypeError, ValueError, KeyError, RuntimeError) as exc:
        code = getattr(exc, "code", "FEATURE_REQUIREMENTS_INVALID")
        missing = code in {"EVIDENCE_MISSING", "UNIT_MISSING"}
        problem = {"code": code, "message": str(exc), "path": getattr(exc, "path", "")}
        return {"decision": "needs_clarification" if missing else "invalid", "problems": [problem],
                "questions": [{"topic": problem["path"], "text": str(exc), "blocking": True}] if missing else [],
                "warnings": [], "evidenceChecks": [], "normalized": None}
    warnings = [{"code": "FULL_REVOLUTION_COUPLED", "feature": feature["id"],
                 "message": "A complete 360-degree revolution is driven by angle parameters; verify that the drawing intends this coupling. Use literal 360 for an independent full turn."}
                for feature in plan["features"] if feature["operation"] in {"shaft", "groove"}
                and math.isclose(feature["extent"]["value"], 360, rel_tol=0, abs_tol=1e-9)
                and feature["extent"]["parameters"]]
    return {"decision": "ready", "problems": [], "questions": [], "warnings": warnings,
            "evidenceChecks": [{"feature": f["id"], "references": f["evidence"]} for f in plan["features"]],
            "normalized": {"schema": SCHEMA, "featurePlan": plan}}
