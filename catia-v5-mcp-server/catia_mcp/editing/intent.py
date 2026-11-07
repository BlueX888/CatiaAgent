"""Deterministic intent compilation for the catia-edit/1 workflow.

The model is good at turning a sentence into a *structured* intent, but it is
not a reliable calculator or a reliable judge of which side a dimension moves.
This module takes a small ``cad-intent-draft/1`` object and, using only the
current model snapshot and its measured perturbation evidence:

* binds the requested attribute to a real writable driver handle,
* rejects ambiguous bindings instead of picking one,
* converts units and diameter/radius quantities with plain arithmetic,
* checks that the requested scope (overall / each side / one side) is something
  the measured driver can actually do,
* checks every preserve request against capability and impact evidence, and
* compiles a validated ``catia-edit-plan/1``.

It never talks to CATIA and never writes a model. The compiled plan still goes
through ``validate_plan`` and the normal coordinator; nothing here bypasses the
existing server-side checks.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from catia_mcp.editing.validation import PLAN_SCHEMA, validate_plan

INTENT_SCHEMA = "cad-intent-draft/1"
COMPILE_SCHEMA = "cad-intent-compile/1"

_LINEAR_ATTRS = {"length", "width", "height", "thickness", "depth", "distance", "size"}
_HOLE_ATTRS = {"diameter", "radius"}
_ATTR_ALIASES = {
    "长": "length",
    "长度": "length",
    "长边": "length",
    "宽": "width",
    "宽度": "width",
    "高": "height",
    "高度": "height",
    "厚": "thickness",
    "厚度": "thickness",
    "深度": "depth",
    "距离": "distance",
    "尺寸": "size",
    "直径": "diameter",
    "孔径": "diameter",
    "半径": "radius",
    "角度": "angle",
    "未知": "unknown",
}
_UNIT_ALIASES = {
    "mm": "mm",
    "millimeter": "mm",
    "millimetre": "mm",
    "毫米": "mm",
    "cm": "cm",
    "centimeter": "cm",
    "centimetre": "cm",
    "厘米": "cm",
    "m": "m",
    "meter": "m",
    "metre": "m",
    "米": "m",
    "in": "in",
    "inch": "in",
    "inches": "in",
    "英寸": "in",
    "deg": "deg",
    "degree": "deg",
    "degrees": "deg",
    "度": "deg",
    "none": "none",
    "unitless": "none",
    "": "none",
}
_UNIT_TO_MM = {"mm": 1.0, "cm": 10.0, "m": 1000.0, "in": 25.4}
_SCOPE_ALIASES = {
    "overall": "overall",
    "total": "overall",
    "total_length": "overall",
    "whole": "overall",
    "总长": "overall",
    "总共": "overall",
    "整体": "overall",
    "each_side": "each_side",
    "eachside": "each_side",
    "both_sides": "each_side",
    "both": "each_side",
    "two_sides": "each_side",
    "每侧": "each_side",
    "两侧": "each_side",
    "左右各": "each_side",
    "single_side": "single_side",
    "one_side": "single_side",
    "left": "single_side",
    "right": "single_side",
    "单侧": "single_side",
    "左侧": "single_side",
    "右侧": "single_side",
}
_PRESERVE_ALIASES = {
    "center_position": "center_position",
    "center": "center_position",
    "centre": "center_position",
    "center_fixed": "center_position",
    "中心": "center_position",
    "保持中心": "center_position",
    "hole_edge_distance": "hole_edge_distance",
    "holes_edge_distance": "hole_edge_distance",
    "edge_distance": "hole_edge_distance",
    "hole_margin": "hole_edge_distance",
    "孔边距": "hole_edge_distance",
    "边距": "hole_edge_distance",
    "hole_centers": "hole_centers",
    "hole_center": "hole_centers",
    "hole_centres": "hole_centers",
    "hole_position": "hole_centers",
    "hole_positions": "hole_centers",
    "孔心": "hole_centers",
    "孔心坐标": "hole_centers",
    "hole_world_position": "hole_world_position",
    "hole_world_positions": "hole_world_position",
    "hole_world_centers": "hole_world_position",
    "孔心世界坐标": "hole_world_position",
    "孔心模型坐标": "hole_world_position",
    "other_dimensions": "other_dimensions",
    "other_dims": "other_dimensions",
    "其它尺寸": "other_dimensions",
}
_PRESERVE_TYPES = (
    "center_position",
    "hole_edge_distance",
    "hole_centers",
    "hole_world_position",
    "other_dimensions",
)
_DIRECTION_ALIASES = {
    "positive": "positive",
    "+": "positive",
    "plus": "positive",
    "increase": "positive",
    "正向": "positive",
    "负向": "negative",
    "negative": "negative",
    "-": "negative",
    "minus": "negative",
    "decrease": "negative",
}


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _round(value: float) -> float:
    return round(float(value), 9)


def _tail(handle: str) -> str:
    return str(handle).split("\\")[-1]


def _provenance_key(text: str) -> str:
    """Whitespace/punctuation-insensitive key for verbatim-text checks."""
    return re.sub(r"[\s　]+", "", str(text or "")).lower()


def _base_result(decision: str) -> dict[str, Any]:
    return {
        "schema": COMPILE_SCHEMA,
        "decision": decision,
        "intent": None,
        "plan": None,
        "binding": None,
        "checks": [],
        "unresolved": [],
        "candidates": [],
        "notes": [],
        "operationId": None,
        "message": "",
        "textSource": None,
        "clarificationSource": None,
    }


def _check(result: dict[str, Any], identifier: str, status: str, detail: str, **evidence: Any) -> None:
    item = {"id": identifier, "status": status, "detail": detail}
    if evidence:
        item["evidence"] = evidence
    result["checks"].append(item)


def _invalid(message: str, path: str = "") -> dict[str, Any]:
    result = _base_result("invalid")
    result["message"] = message
    result["unresolved"] = [path] if path else []
    return result


def _clarify(message: str, unresolved: list[str], candidates: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    result = _base_result("needs_clarification")
    result["message"] = message
    result["unresolved"] = unresolved
    result["candidates"] = candidates or []
    return result


def _unsupported(message: str, unresolved: list[str] | None = None) -> dict[str, Any]:
    result = _base_result("unsupported")
    result["message"] = message
    result["unresolved"] = unresolved or []
    return result


def _norm_attr(value: Any) -> str:
    text = str(value or "unknown").strip().lower()
    return _ATTR_ALIASES.get(text, text)


def _norm_unit(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return _UNIT_ALIASES.get(text, text)


def _norm_scope(value: Any) -> str | None:
    if value is None:
        return "overall"
    text = str(value).strip().lower()
    return _SCOPE_ALIASES.get(text, text)


def _norm_preserve(value: Any) -> str | None:
    text = str(value or "").strip().lower()
    return _PRESERVE_ALIASES.get(text, text)


def _probe_delta(impact: dict[str, Any]) -> float:
    value = _num(impact.get("probeDelta"))
    if value is not None and value != 0:
        return value
    probe = _num(impact.get("probeValue"))
    original = _num(impact.get("original"))
    if probe is not None and original is not None and probe != original:
        return probe - original
    return 1.0


def _axis_ratio(impact: dict[str, Any], axis: str) -> float | None:
    delta = _num(impact.get(f"bboxDelta{axis.upper()}"))
    probe = _probe_delta(impact)
    if delta is None or probe == 0:
        return None
    return delta / probe


def _edge_ratios(impact: dict[str, Any], axis: str) -> tuple[float, float] | None:
    edges = (impact.get("bboxEdgeDeltas") or {}).get(axis) if isinstance(impact.get("bboxEdgeDeltas"), dict) else None
    if not isinstance(edges, dict):
        return None
    low = _num(edges.get("min"))
    high = _num(edges.get("max"))
    probe = _probe_delta(impact)
    if low is None or high is None or probe == 0:
        return None
    return (low / probe, high / probe)


def _numeric_equal(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def _infer_interpretation(param: dict[str, Any], impact: dict[str, Any], attribute: str) -> dict[str, Any]:
    """What does this parameter physically change?

    Evidence comes from the +1-unit perturbation stored in the snapshot: which
    bbox axis moved 1:1, and/or how much the hole radius moved.
    """
    probe = _probe_delta(impact)
    linear: list[dict[str, Any]] = []
    for axis in ("x", "y", "z"):
        ratio = _axis_ratio(impact, axis)
        if ratio is None or abs(ratio) < 1e-9:
            continue
        linear.append({"axis": axis, "ratio": ratio})
    # A driver may change only a subset of circles (e.g. the two mounting holes
    # but not the side hole or the boss). Per-hole deltas keep that evidence;
    # the scalar ``holeRadiusDelta`` is null when the circles disagree.
    per_hole = impact.get("holeRadiusDeltas")
    radius_delta = _num(impact.get("holeRadiusDelta"))
    if isinstance(per_hole, list):
        changed = [abs(float(delta)) for delta in per_hole if _num(delta) is not None and abs(float(delta)) > 1e-9]
        if changed and all(abs(value - changed[0]) <= 1e-6 for value in changed):
            radius_delta = changed[0]
    radius_ratio = radius_delta / probe if radius_delta is not None and probe != 0 else None
    hole_kind = None
    if radius_ratio is not None:
        if _numeric_equal(abs(radius_ratio), 0.5):
            hole_kind = "diameter"
        elif _numeric_equal(abs(radius_ratio), 1.0):
            hole_kind = "radius"
        elif abs(radius_ratio) > 1e-9:
            hole_kind = "unknown_hole_quantity"
    one_to_one = [item for item in linear if _numeric_equal(item["ratio"], 1.0)]
    return {
        "probeDelta": probe,
        "linear": linear,
        "oneToOne": one_to_one,
        "holeKind": hole_kind,
        "holeRadiusRatio": radius_ratio,
        "requestedAttribute": attribute,
    }


def _driver_candidates(
    snapshot: dict[str, Any],
    interpretation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    impacts = snapshot.get("impacts") or {}
    rows = []
    for param in snapshot.get("parameters", []):
        if param.get("kind") != "driver" or param.get("writable") is not True:
            continue
        impact = impacts.get(param.get("handle")) or {}
        meaning = _infer_interpretation(param, impact, "unknown")
        rows.append(
            {
                "handle": param.get("handle"),
                "name": param.get("name"),
                "value": param.get("value"),
                "unit": param.get("unit"),
                "writable": param.get("writable"),
                "kind": param.get("kind"),
                "axis": meaning["oneToOne"][0]["axis"] if len(meaning["oneToOne"]) == 1 else None,
                "holeQuantity": meaning["holeKind"],
                "circleRoles": _driver_circle_roles(snapshot, str(param.get("handle"))),
                "circleSketches": _driver_circle_sketches(snapshot, str(param.get("handle"))),
                "circleCount": sum(
                    1
                    for hole in snapshot.get("holes") or []
                    if param.get("handle") in (hole.get("radiusDrivers") or [])
                ),
                "geometryChanged": impact.get("geometryChanged"),
                "edgeDistancesPreserved": impact.get("edgeDistancesPreserved"),
                "holeCentersMoved": impact.get("holeCentersMoved"),
                "holeWorldCentersMoved": impact.get("holeWorldCentersMoved"),
            }
        )
    return rows


def _unit_factor(param_unit: str, operation_unit: str | None) -> tuple[float | None, str | None]:
    """Multiplier from the user's unit into the parameter's unit."""
    if operation_unit is None:
        return None, "unit_missing"
    if param_unit == "unknown":
        return None, "parameter unit is not reported by CATIA"
    if operation_unit == "none" and param_unit in ("mm", "deg"):
        # An empty/omitted unit normalises to "none"; for a dimensional
        # parameter that is a missing unit, not a unitless quantity.
        return None, "unit_missing"
    if param_unit == "mm":
        if operation_unit not in _UNIT_TO_MM:
            return None, f"unit {operation_unit!r} cannot be applied to a length parameter"
        return _UNIT_TO_MM[operation_unit], None
    if param_unit == "deg":
        if operation_unit != "deg":
            return None, f"unit {operation_unit!r} cannot be applied to an angle parameter"
        return 1.0, None
    if param_unit == "none":
        if operation_unit != "none":
            return None, f"parameter has no physical unit but the intent declares {operation_unit!r}"
        return 1.0, None
    return None, f"parameter unit {param_unit!r} has no supported conversion"


def _edge_margin(holes: list[dict[str, Any]], bbox: dict[str, Any]) -> float | None:
    try:
        x0, x1 = float(bbox["min"]["x"]), float(bbox["max"]["x"])
        y0, y1 = float(bbox["min"]["y"]), float(bbox["max"]["y"])
    except Exception:
        return None
    if not holes:
        return None
    values = []
    for hole in holes:
        x = float(hole["center"]["x"])
        y = float(hole["center"]["y"])
        values.append(min(x - x0, x1 - x, y - y0, y1 - y))
    return round(min(values), 6)


def _find_edge_parameter(snapshot: dict[str, Any], margin: float | None) -> str | None:
    """Prefer a driver that looks like the edge-distance parameter, only as a label.

    The server evaluates holes_edge_distance globally and does not read this
    handle, so this is explanatory metadata, not a write target.
    """
    if margin is None:
        return None
    for param in snapshot.get("parameters", []):
        if param.get("kind") != "driver" or param.get("writable") is not True:
            continue
        value = _num(param.get("value"))
        if value is None or not _numeric_equal(value, margin, 1e-6):
            continue
        lowered = str(param.get("name") or "").lower()
        if any(marker in lowered for marker in ("edge", "边距", "e")):
            return str(param.get("handle"))
    return None


def _confirmed_holes(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    """Only measured holes; older snapshots without roles keep every circle."""
    holes = snapshot.get("holes") or []
    if any(hole.get("role") for hole in holes):
        return [hole for hole in holes if hole.get("role") == "hole"]
    return list(holes)


def _scope_hole_indices(snapshot: dict[str, Any], scope: dict[str, Any] | None) -> list[int] | None:
    """Resolve a preserve scope to indices into ``snapshot['holes']``.

    ``scope`` is ``{"sketch": name}`` or ``{"holes": [1-based, ...]}``. ``None``
    means the conservative default (every measured hole). A scope that resolves
    to no measured hole returns an empty list, never an implicit all-holes pass.
    """
    holes = snapshot.get("holes") or []
    has_roles = any(hole.get("role") for hole in holes)
    confirmed = [
        index
        for index, hole in enumerate(holes)
        if not has_roles or hole.get("role") == "hole"
    ]
    if not scope:
        return confirmed
    if scope.get("sketch"):
        name = str(scope["sketch"]).strip()
        return [
            index
            for index in confirmed
            if str(holes[index].get("sketch") or "") == name
        ]
    if scope.get("holes"):
        indices = []
        for value in scope["holes"]:
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                return None
            index = value - 1
            if index not in confirmed:
                return None
            indices.append(index)
        return sorted(set(indices))
    return confirmed


def _driver_circle_roles(snapshot: dict[str, Any], handle: str) -> list[str]:
    roles = []
    for hole in snapshot.get("holes") or []:
        if handle in (hole.get("radiusDrivers") or []):
            role = hole.get("role")
            if role:
                roles.append(str(role))
    return roles


def _driver_circle_sketches(snapshot: dict[str, Any], handle: str) -> list[str]:
    sketches = []
    for hole in snapshot.get("holes") or []:
        if handle in (hole.get("radiusDrivers") or []):
            sketch = hole.get("sketch")
            if sketch:
                sketches.append(str(sketch))
    return sorted(set(sketches))


def _text_circle_kind(text: str) -> str | None:
    """What kind of circular feature does the user name, if any."""
    lowered = str(text or "").lower()
    if any(word in lowered for word in ("凸台", "圆柱", "凸起", "boss", "cylinder", "additive")):
        return "additive_feature"
    if any(word in lowered for word in ("孔", "hole")):
        return "hole"
    return None


def _find_sketch(snapshot: dict[str, Any], name: str) -> dict[str, Any] | None:
    for sketch in (snapshot.get("structure") or {}).get("sketches") or []:
        if str(sketch.get("name")) == name:
            return sketch
    return None


def _axis_of_vector(vector: dict[str, Any] | None) -> tuple[str, float] | None:
    """Dominant model axis of a unit sketch axis vector, with its sign."""
    if not isinstance(vector, dict):
        return None
    values = {axis: _num(vector.get(axis)) for axis in ("x", "y", "z")}
    if any(value is None for value in values.values()):
        return None
    components = [(axis, float(values[axis])) for axis in ("x", "y", "z")]
    axis, value = max(components, key=lambda item: abs(item[1]))
    if abs(abs(value) - 1.0) > 1e-6:
        return None
    return axis, (1.0 if value > 0 else -1.0)


def _format_number(value: float) -> str:
    return f"{value:g}"


_CN_NUMBERS = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _text_hole_count(text: str) -> int | None:
    # "两个安装孔" is a count too; allow a short qualifier between 个 and 孔.
    match = re.search(r"([一二两三四五六七八九十]|\d+)\s*(?:个|只)\s*[一-鿿]{0,4}?孔", text)
    if not match:
        return None
    token = match.group(1)
    if token.isdigit():
        return int(token)
    return _CN_NUMBERS.get(token)


def _out_of_scope_reason(text: str) -> str | None:
    """High-confidence phrases for requests catia-edit/1 cannot express."""
    if re.search(r"(增加|新增|添加|加)\s*(?:一个|1\s*个|个)?\s*孔(?!径)", text):
        return "adding a hole/feature is outside catia-edit/1; only existing driver dimensions can be changed"
    if re.search(r"(移动|挪动|平移).{0,8}孔|孔.{0,8}(移动|挪动|平移)", text):
        return (
            "moving an individual hole centre is not available: hole positions are driven by "
            "edge-distance constraints, not by an independent centre parameter"
        )
    if re.search(r"装配|product|装配体", text, re.IGNORECASE):
        return "assembly/Product edits are outside catia-edit/1"
    if re.search(r"自由曲面|\bgsd\b|曲面特征", text, re.IGNORECASE):
        return "free-surface/GSD edits are outside catia-edit/1"
    return None


def _text_consistency(
    text: str,
    *,
    attribute: str,
    scope: str,
    basis: str,
    preserve_types: list[str],
    holes: list[dict[str, Any]],
    requested_holes: Any,
    operation_unit: str | None = None,
    param_unit: str = "mm",
    target_handle: str | None = None,
    target_binding: str | None = None,
    requested_sketch: str | None = None,
    requested_kind: str | None = None,
    sketch_discriminator: bool = False,
    hole_count_discriminator: bool = False,
    kind_discriminator: bool = False,
    check_provenance: bool = True,
) -> list[dict[str, str]]:
    """Catch contradictions between the text and its structured fields.

    This is not a second NLU layer: it only checks that a high-confidence phrase
    in the user text is reflected in the field the model chose. A mismatch means
    the parse is internally inconsistent, so the safe outcome is clarification.

    ``check_provenance`` stays on when the text came from the model. When the
    host injected the real user message, the text is already authoritative and
    the paraphrase heuristic must not fire on the user's own wording.
    """
    issues: list[dict[str, str]] = []
    if not text:
        return issues
    lowered = text.lower()

    # The text field must be the user's words. A model that rewrites it can
    # defeat every consistency check below (observed in the P0 experiment:
    # "长度改成 20" was rewritten to add an assumed "mm"). Meta-commentary is
    # evidence of a paraphrase, so the safe outcome is to ask the user.
    meta_markers = (
        "原话",
        "按模型单位",
        "假设",
        "解释为",
        "未给出",
        "推测",
        "按默认",
        "默认单位",
        "不含孔",
        "interpreted as",
        "assuming",
    )
    if check_provenance and any(marker in lowered for marker in meta_markers):
        issues.append(
            {
                "path": "text.provenance",
                "reason": "intent.text contains an assumption or paraphrase; copy the user's words verbatim and ask instead",
            }
        )

    def has(*words: str) -> bool:
        return any(word in lowered for word in words)

    if has("左右各", "两侧各", "两边各", "每侧", "向左右各", "向左向右", "each side", "both sides") and scope != "each_side":
        issues.append(
            {
                "path": "operation.scope",
                "reason": "text says both sides change, but operation.scope is not each_side",
            }
        )
    if has("总长", "总长度", "整体长度", "total length", "overall length") and scope == "each_side":
        issues.append(
            {
                "path": "operation.scope",
                "reason": "text says total length, but operation.scope is each_side",
            }
        )
    if has("原始", "最初", "original", "baseline") and basis != "original":
        issues.append(
            {
                "path": "reference.basis",
                "reason": "text refers to the original model, but reference.basis is not original",
            }
        )
    part_center = bool(
        re.search(r"(?<!孔)中心|零件中心|part\s+cent(?:er|re)", text, re.IGNORECASE)
    )
    if part_center and has("不动", "不变", "保持", "fixed", "unchanged") and "center_position" not in preserve_types:
        issues.append(
            {
                "path": "preserve",
                "reason": "text requires the part centre to stay fixed, but center_position is not preserved",
            }
        )
    if (
        has("孔心", "孔中心", "hole centre", "hole center")
        and has("不动", "不变", "保持", "固定", "fixed", "unchanged")
        and "hole_centers" not in preserve_types
        and "hole_world_position" not in preserve_types
    ):
        issues.append(
            {
                "path": "preserve",
                "reason": "text requires hole centres to stay fixed, but hole_centers is not preserved",
            }
        )
    if (
        has("边距", "孔到边", "到边的距离", "孔距上边", "孔距下边", "距上边", "距下边", "edge distance", "hole margin")
        and has("保持", "不变", "仍然", "仍", "别变", "keep", "unchanged")
        and "hole_edge_distance" not in preserve_types
    ):
        issues.append(
            {
                "path": "preserve",
                "reason": "text requires the hole edge distance to stay fixed, but hole_edge_distance is not preserved",
            }
        )
    if (
        has("只", "仅", "only")
        and has("右侧", "左侧", "单侧", "一侧", "right side", "left side", "one side")
        and has("不动", "保持", "fixed", "unchanged")
        and scope != "single_side"
    ):
        issues.append(
            {
                "path": "operation.scope",
                "reason": "text asks for a single-side change, but operation.scope is not single_side",
            }
        )
    if str(target_binding or "").strip().lower() in ("user_explicit", "explicit", "named") and target_handle:
        leaf = _tail(target_handle).lower()
        if leaf and leaf not in lowered and str(target_handle).lower() not in lowered:
            issues.append(
                {
                    "path": "target.binding",
                    "reason": "binding is user_explicit, but the user text does not name this parameter",
                }
            )
    if has("直径", "diameter") and attribute == "radius":
        issues.append({"path": "target.attribute", "reason": "text says diameter, but target.attribute is radius"})
    if has("半径", "radius") and attribute == "diameter":
        issues.append({"path": "target.attribute", "reason": "text says radius, but target.attribute is diameter"})

    if operation_unit in ("mm", "cm", "m", "in") and param_unit == "mm" and re.search(r"\d", text):
        if not re.search(r"\d+(?:\.\d+)?\s*(?:mm|毫米|cm|厘米|m|米|英寸|in(?:ch(?:es)?)?)", text, re.IGNORECASE):
            issues.append(
                {
                    "path": "operation.unit",
                    "reason": "the text states a number without a unit; confirm mm/cm/inch instead of assuming one",
                }
            )
    if operation_unit == "deg" and re.search(r"\d", text):
        if not re.search(r"\d+(?:\.\d+)?\s*(?:deg|度|°)", text, re.IGNORECASE):
            issues.append(
                {
                    "path": "operation.unit",
                    "reason": "the text states a number without an angle unit; confirm degrees",
                }
            )

    # Structural discriminators are only legitimate when the user's words
    # support them. A model that invents "sketch=草图.1" or "holeCount=2" to
    # break a tie is making the choice the user did not make.
    if sketch_discriminator and requested_sketch:
        sketch_tail = str(requested_sketch).split("\\")[-1].lower()
        descriptors = (
            "侧面", "侧孔", "顶面", "上面", "底面", "下面", "正面", "背面",
            "安装", "主孔", "side", "top", "bottom", "face",
        )
        if sketch_tail not in lowered and not any(word in lowered for word in descriptors):
            issues.append(
                {
                    "path": "target.sketch.provenance",
                    "reason": "target.sketch was used to break a tie, but the user's words do not identify the sketch or hole group",
                }
            )
    if hole_count_discriminator and requested_holes is not None:
        if _text_hole_count(text) is None:
            issues.append(
                {
                    "path": "target.holeCount.provenance",
                    "reason": "target.holeCount was used to break a tie, but the user did not state a hole count",
                }
            )
    if kind_discriminator and requested_kind:
        kind_key = {
            "孔": "hole",
            "hole": "hole",
            "holes": "hole",
            "凸台": "additive_feature",
            "additive": "additive_feature",
            "additive_feature": "additive_feature",
        }.get(str(requested_kind).strip().lower(), str(requested_kind).strip().lower())
        kind_words = {
            "hole": ("孔", "hole"),
            "additive_feature": ("凸台", "圆柱", "凸起", "boss", "cylinder"),
        }.get(kind_key, ())
        if kind_words and not any(word in lowered for word in kind_words):
            issues.append(
                {
                    "path": "target.kind.provenance",
                    "reason": "target.kind was used to break a tie, but the user did not say whether the profile is a hole or a boss",
                }
            )

    mentioned = _text_hole_count(text)
    relevant_holes = holes
    if target_handle:
        driven = [hole for hole in holes if target_handle in (hole.get("radiusDrivers") or [])]
        if driven:
            relevant_holes = driven
    if mentioned is not None:
        declared = requested_holes if isinstance(requested_holes, int) and not isinstance(requested_holes, bool) else None
        if declared is not None and declared != mentioned:
            issues.append(
                {
                    "path": "target.holeCount",
                    "reason": f"target.holeCount is {declared}, but the text mentions {mentioned} holes",
                }
            )
        elif declared is None and mentioned != len(relevant_holes):
            issues.append(
                {
                    "path": "target.holeCount",
                    "reason": (
                        f"text mentions {mentioned} holes, but the bound parameter changes "
                        f"{len(relevant_holes)} measured holes"
                    ),
                }
            )
    return issues


def compile_intent(
    intent: Any,
    snapshot: dict[str, Any],
    *,
    session_id: str,
    operation_id: str | None = None,
    baseline_parameters: dict[str, Any] | None = None,
    user_text: str | None = None,
) -> dict[str, Any]:
    """Compile a structured draft into a validated catia-edit-plan/1.

    ``user_text`` is the host-injected current user message. When present it is
    authoritative: ``intent.text`` must be a verbatim fragment of it, and every
    phrase check runs against it instead of trusting the model's copy. Without
    it the compiler falls back to ``intent.text`` and marks the result
    ``textSource="model"`` so the evidence never overstates provenance.

    The return value is a *decision*, not an execution result:
    ``planned``, ``needs_clarification``, ``unsupported`` or ``invalid``.
    """
    if not isinstance(intent, dict):
        return _invalid("intent must be a JSON object")
    if intent.get("schema") != INTENT_SCHEMA:
        return _invalid(f"intent.schema must be {INTENT_SCHEMA}", "schema")
    result = _base_result("planned")
    result["intent"] = intent

    model_text = str(intent.get("text") or "")
    platform_text = user_text.strip() if isinstance(user_text, str) and user_text.strip() else None
    text_source = "platform" if platform_text else "model"
    text = platform_text or model_text
    result["textSource"] = text_source
    if platform_text is not None:
        # The model's "user words" field must be an exact fragment of the
        # host-injected message. A paraphrase is what defeated the P0 checks.
        if _provenance_key(model_text) and _provenance_key(model_text) not in _provenance_key(platform_text):
            result["decision"] = "needs_clarification"
            result["unresolved"] = ["text.provenance"]
            result["message"] = (
                "intent.text is not a verbatim fragment of the user's message injected by the "
                "host; copy the user's words exactly or ask instead"
            )
            _check(
                result,
                "text-provenance",
                "fail",
                "intent.text differs from the host-injected user message",
                intentText=model_text,
                userText=platform_text,
            )
            return result
        _check(result, "text-provenance", "pass", "intent.text is contained in the host-injected user message")

    model_decision = str(intent.get("decision") or "planned").strip().lower()
    if model_decision in ("needs_clarification", "clarify", "clarification"):
        result["decision"] = "needs_clarification"
        result["unresolved"] = list(intent.get("unresolved") or ["model requested clarification"])
        result["candidates"] = _driver_candidates(snapshot)
        result["message"] = "the model asked for clarification before proposing a plan"
        result["clarificationSource"] = "model"
        return result
    # A model cannot answer a clarification by itself. When the host injected
    # the same user message that triggered the pending question, any attempt to
    # plan is converted back to a clarification by the coordinator's gate.
    if model_decision in ("unsupported", "reject", "rejected", "refuse"):
        result["decision"] = "unsupported"
        result["unresolved"] = list(intent.get("unresolved") or [])
        result["message"] = str(intent.get("message") or "the model declared the request unsupported")
        return result
    if model_decision not in ("planned", "plan", "propose"):
        return _invalid(f"unknown intent.decision {model_decision!r}", "decision")

    out_of_scope = _out_of_scope_reason(text)
    if out_of_scope:
        result["decision"] = "unsupported"
        result["unresolved"] = ["request.scope"]
        result["message"] = out_of_scope
        return result

    target = intent.get("target")
    operation = intent.get("operation")
    reference = intent.get("reference") or {}
    if not isinstance(target, dict):
        return _invalid("target must be an object", "target")
    if not isinstance(operation, dict):
        return _invalid("operation must be an object", "operation")
    if not isinstance(reference, dict):
        return _invalid("reference must be an object", "reference")

    params = {p["handle"]: p for p in snapshot.get("parameters", [])}
    handle = target.get("parameter")
    if not isinstance(handle, str) or not handle.strip():
        return _clarify(
            "target.parameter is missing; bind the requested attribute to one handle from the snapshot",
            ["target.parameter"],
            _driver_candidates(snapshot),
        )
    handle = handle.strip()
    param = params.get(handle)
    if param is None:
        return _clarify(
            f"parameter handle {handle!r} does not exist in the current snapshot; use an exact handle",
            ["target.parameter"],
            _driver_candidates(snapshot),
        )
    if param.get("kind") != "driver":
        return _unsupported(
            f"{handle} is not a driver parameter (kind={param.get('kind')}); only writable drivers may be changed",
            ["target.parameter"],
        )
    if param.get("writable") is not True:
        return _unsupported(
            f"{handle} is not proven writable (writable={param.get('writable')}); the dependency probe "
            f"did not show a stable write, so a plan on it cannot be trusted",
            ["target.parameter"],
        )
    current = _num(param.get("value"))
    if current is None:
        return _unsupported(f"{handle} has no numeric value in the snapshot", ["target.parameter"])
    param_unit = _norm_unit(param.get("unit")) or "unknown"
    if param_unit not in ("mm", "deg", "none"):
        return _unsupported(
            f"{handle} unit is {param.get('unit')!r}; no safe unit conversion is available",
            ["target.unit"],
        )

    attribute = _norm_attr(target.get("attribute"))
    impact = (snapshot.get("impacts") or {}).get(handle) or {}
    if not isinstance(impact, dict) or impact.get("writeStuck") is not True:
        return _unsupported(
            f"no successful perturbation evidence for {handle}; re-run catia_begin_edit so the snapshot "
            f"contains measured impacts",
            ["snapshot.impacts"],
        )
    interpretation = _infer_interpretation(param, impact, attribute)
    one_to_one = interpretation["oneToOne"]
    hole_kind = interpretation["holeKind"]

    if attribute in _HOLE_ATTRS:
        if param_unit != "mm":
            return _unsupported(f"{attribute} requires a length parameter, but {handle} is {param_unit}")
        if hole_kind not in ("diameter", "radius"):
            return _unsupported(
                f"cannot prove whether {handle} is a diameter or a radius: the +1-unit probe did not "
                f"change the hole radius in a 1:2 or 1:1 ratio",
                ["target.attribute"],
            )
        meaning = "hole_dimension"
        axis = None
        ratio = interpretation["holeRadiusRatio"]
    elif attribute in _LINEAR_ATTRS:
        if len(one_to_one) != 1:
            return _unsupported(
                f"{handle} does not drive exactly one bounding-box axis 1:1 "
                f"(measured axes: {interpretation['linear']})",
                ["target.attribute"],
            )
        meaning = "linear_dimension"
        axis = one_to_one[0]["axis"]
        ratio = one_to_one[0]["ratio"]
    elif attribute == "angle":
        if param_unit != "deg":
            return _unsupported(f"attribute 'angle' requires deg, but {handle} is {param_unit}")
        meaning = "angle"
        axis = None
        ratio = 1.0
    elif attribute == "unknown":
        if hole_kind in ("diameter", "radius"):
            meaning, axis, ratio = "hole_dimension", None, interpretation["holeRadiusRatio"]
            attribute = hole_kind
        elif len(one_to_one) == 1:
            meaning, axis, ratio = "linear_dimension", one_to_one[0]["axis"], one_to_one[0]["ratio"]
        elif param_unit == "deg":
            meaning, axis, ratio = "angle", None, 1.0
        else:
            return _unsupported(
                f"cannot infer what {handle} changes from the measured evidence; state target.attribute",
                ["target.attribute"],
            )
    else:
        return _clarify(
            f"unsupported target.attribute {attribute!r}; use length/width/thickness/diameter/radius/angle",
            ["target.attribute"],
            _driver_candidates(snapshot),
        )

    if meaning == "hole_dimension":
        roles = _driver_circle_roles(snapshot, handle)
        target_sketch = target.get("sketch")
        if isinstance(target_sketch, str) and target_sketch.strip():
            target_sketch = target_sketch.strip()
            driven_sketches = _driver_circle_sketches(snapshot, handle)
            if driven_sketches and target_sketch not in driven_sketches:
                return _clarify(
                    f"{handle} drives circles in {', '.join(driven_sketches)}, not in sketch "
                    f"{target_sketch!r}; bind the sketch that owns the intended circle",
                    ["target.sketch"],
                    _driver_candidates(snapshot),
                )
            _check(result, "target-sketch", "pass", f"the driver's circles live in sketch {target_sketch}")
        requested_kind = str(target.get("kind") or "").strip().lower() or _text_circle_kind(text)
        if requested_kind in ("boss", "additive", "additive_feature", "凸台"):
            requested_kind = "additive_feature"
        elif requested_kind in ("孔", "hole", "holes"):
            requested_kind = "hole"
        if requested_kind == "hole" and roles and "hole" not in roles:
            return _unsupported(
                f"{handle} changes a circular profile measured as "
                f"{', '.join(roles)}, not a hole; a hole-size change would edit the wrong feature",
                ["target.kind"],
            )
        if requested_kind == "additive_feature" and roles and "additive_feature" not in roles:
            return _unsupported(
                f"{handle} changes a circular profile measured as "
                f"{', '.join(roles)}, not an additive feature",
                ["target.kind"],
            )
        if requested_kind is None and len(set(roles)) > 1:
            return _clarify(
                f"{handle} changes {len(set(roles))} kinds of circular profile "
                f"({', '.join(sorted(set(roles)))}); say whether the user means the holes or the "
                f"additive circular feature",
                ["target.kind"],
                _driver_candidates(snapshot),
            )
        if "insufficient_evidence" in roles and "hole" not in roles:
            return _unsupported(
                f"the circular profile driven by {handle} has insufficient evidence to be called a "
                f"hole (radius change coupled with a boundary change); confirm the feature first",
                ["target.kind"],
            )
        _check(
            result,
            "circle-role",
            "pass",
            f"measured circle role(s): {', '.join(sorted(set(roles))) or 'unclassified'}",
            requestedKind=requested_kind,
        )

    requested_holes = target.get("holeCount")
    if requested_holes is not None:
        if (
            not isinstance(requested_holes, int)
            or isinstance(requested_holes, bool)
            or requested_holes <= 0
        ):
            return _invalid("target.holeCount must be a positive integer", "target.holeCount")
        driven_holes = [
            hole
            for hole in _confirmed_holes(snapshot)
            if handle in (hole.get("radiusDrivers") or [])
        ]
        actual_holes = len(driven_holes) if driven_holes else len(_confirmed_holes(snapshot))
        if requested_holes != actual_holes:
            circles = len(snapshot.get("holes") or [])
            return _clarify(
                f"the user referred to {requested_holes} holes but the bound parameter changes "
                f"{actual_holes} measured holes ({circles} circular profiles in the model); bind the "
                f"intended hole group before changing it",
                ["target.holeCount"],
                _driver_candidates(snapshot),
            )

    binding = {
        "handle": handle,
        "name": param.get("name"),
        "currentValue": current,
        "unit": param_unit,
        "meaning": meaning,
        "axis": axis,
        "ratio": ratio,
        "attribute": attribute,
        "parameterHoleQuantity": hole_kind,
        "evidence": {
            "probeDelta": interpretation["probeDelta"],
            "bboxAxisRatios": {item["axis"]: item["ratio"] for item in interpretation["linear"]},
            "holeRadiusRatio": interpretation["holeRadiusRatio"],
            "edgeRatios": _edge_ratios(impact, axis) if axis else None,
            "source": "measured at begin_edit on the server working copy",
        },
    }
    result["binding"] = binding
    _check(result, "binding", "pass", f"{handle} is a writable driver proven to change {meaning}")
    if meaning in ("linear_dimension", "hole_dimension") and impact.get("geometryChanged") is not True:
        return _unsupported(
            f"{handle} does not change measurable geometry (geometryChanged is not true); "
            f"a plan on it would be NO_EFFECT",
            ["target.parameter"],
        )

    # Ambiguity: another writable driver changes the same dimension the same way.
    # Unless the user named the parameter explicitly, choosing one would be a guess.
    sketch_discriminator = False
    hole_count_discriminator = False
    kind_discriminator = False
    if str(target.get("binding") or "").strip().lower() not in ("user_explicit", "explicit", "named"):
        if meaning == "linear_dimension":
            similar = [row for row in _driver_candidates(snapshot) if row["axis"] == axis]
        elif meaning == "hole_dimension":
            similar = [row for row in _driver_candidates(snapshot) if row["holeQuantity"] in ("diameter", "radius")]
            if requested_kind in ("hole", "additive_feature"):
                filtered = [row for row in similar if requested_kind in (row.get("circleRoles") or [])]
                if filtered and len(filtered) < len(similar):
                    kind_discriminator = True
                if filtered:
                    similar = filtered
            if isinstance(target_sketch, str) and target_sketch:
                filtered = [row for row in similar if target_sketch in (row.get("circleSketches") or [])]
                if filtered and len(filtered) < len(similar):
                    sketch_discriminator = True
                if filtered:
                    similar = filtered
            if isinstance(requested_holes, int) and not isinstance(requested_holes, bool):
                filtered = [row for row in similar if row.get("circleCount") == requested_holes]
                if filtered and len(filtered) < len(similar):
                    hole_count_discriminator = True
                if filtered:
                    similar = filtered
        else:
            similar = []
        if len(similar) > 1:
            return _clarify(
                f"{len(similar)} writable drivers change the same {attribute}; a single parameter cannot be "
                f"bound without a user choice",
                ["target.parameter.ambiguous"],
                similar,
            )
        _check(result, "binding-unique", "pass", "no other writable driver changes the same dimension")

    # ---- operation ---------------------------------------------------
    op_type = str(operation.get("type") or "").strip().lower()
    op_type = {
        "set": "set",
        "set_to": "set",
        "absolute": "set",
        "改为": "set",
        "设为": "set",
        "increase": "increase",
        "add": "increase",
        "增加": "increase",
        "加": "increase",
        "decrease": "decrease",
        "reduce": "decrease",
        "减少": "decrease",
        "减": "decrease",
    }.get(op_type)
    if op_type not in ("set", "increase", "decrease"):
        return _invalid(f"unsupported operation.type {operation.get('type')!r}", "operation.type")
    amount = _num(operation.get("amount"))
    if amount is None:
        try:
            amount = float(str(operation.get("amount")).strip())
        except Exception:
            return _invalid("operation.amount must be a number", "operation.amount")
    if op_type in ("increase", "decrease") and amount <= 0:
        return _invalid("increase/decrease amount must be positive; use set for an absolute value", "operation.amount")

    scope = _norm_scope(operation.get("scope"))
    if scope not in ("overall", "each_side", "single_side"):
        return _clarify(
            f"unsupported operation.scope {operation.get('scope')!r}; use overall / each_side / single_side",
            ["operation.scope"],
        )
    if op_type == "set" and scope != "overall":
        return _invalid("set is absolute; scope must be overall", "operation.scope")
    if scope == "single_side" and meaning != "linear_dimension":
        return _unsupported("single_side only applies to a linear dimension, not to a hole size or angle")
    if scope == "each_side" and meaning != "linear_dimension":
        return _unsupported("each_side only applies to a linear dimension", ["operation.scope"])

    raw_unit = operation.get("unit")
    operation_unit = None if raw_unit is None or not str(raw_unit).strip() else _norm_unit(raw_unit)
    factor, unit_error = _unit_factor(param_unit, operation_unit)
    if factor is None:
        if unit_error == "unit_missing":
            return _clarify(
                "operation.unit is missing; do not guess mm/cm/inch for a dimensional change",
                ["operation.unit"],
            )
        return _unsupported(f"cannot apply operation.unit: {unit_error}", ["operation.unit"])
    amount_in_param = amount * factor
    _check(
        result,
        "unit",
        "pass",
        f"{_format_number(amount)} {operation_unit} -> {_format_number(amount_in_param)} {param_unit}",
        factor=factor,
    )

    # ---- reference / basis -------------------------------------------
    basis = str(reference.get("basis") or "current").strip().lower()
    if basis in ("current", "current_model", "now", "当前"):
        basis = "current"
        base_value = current
    elif basis in ("original", "source", "baseline", "原始", "最初"):
        basis = "original"
        baseline = (baseline_parameters or {}).get(handle)
        base_value = _num(baseline.get("value")) if isinstance(baseline, dict) else None
        if base_value is None:
            return _clarify(
                f"reference.basis=original but no recorded original value exists for {handle}; "
                f"the session baseline is unavailable",
                ["reference.basis"],
            )
        _check(result, "basis-original", "pass", f"original recorded value is {_format_number(base_value)} {param_unit}")
    else:
        return _invalid(f"unsupported reference.basis {reference.get('basis')!r}", "reference.basis")

    revision = reference.get("revision")
    if isinstance(revision, (int, float)) and not isinstance(revision, bool):
        if int(revision) != int(snapshot.get("modelRevision", -1)):
            return _clarify(
                f"reference.revision {revision} is stale; the snapshot is at revision "
                f"{snapshot.get('modelRevision')}; re-read before planning",
                ["reference.revision"],
            )

    # ---- frame and direction -----------------------------------------
    # A direction is only meaningful inside a named frame. "Model" is the
    # default. A sketch frame is accepted only when its measured absolute axis
    # maps the requested left/right/up/down onto one model axis; the view frame
    # is never accepted because it changes with the camera.
    ref_axis = reference.get("axis")
    if ref_axis is not None:
        ref_axis = str(ref_axis).strip().lower()
        if ref_axis not in ("x", "y", "z"):
            return _invalid(f"reference.axis {ref_axis!r} must be x, y or z", "reference.axis")
    direction = reference.get("direction")
    direction = _DIRECTION_ALIASES.get(str(direction).strip().lower()) if direction is not None else None

    frame = reference.get("frame")
    frame_name = str(frame).strip().lower() if frame is not None else "model"
    if frame_name in ("", "model", "模型"):
        if ref_axis is not None and axis is not None and ref_axis != axis:
            return _clarify(
                f"reference.axis {ref_axis} does not match the measured axis {axis} for {handle}; "
                f"confirm which dimension the user means",
                ["reference.axis"],
            )
    elif frame_name.startswith("sketch:") or frame_name.startswith("草图:"):
        sketch_name = str(frame).split(":", 1)[1].strip()
        sketch = _find_sketch(snapshot, sketch_name)
        if sketch is None:
            return _clarify(
                f"reference.frame names sketch {sketch_name!r}, but the snapshot has no such sketch",
                ["reference.frame"],
                _driver_candidates(snapshot),
            )
        sketch_frame = sketch.get("frame")
        if not isinstance(sketch_frame, dict):
            return _unsupported(
                f"sketch {sketch_name} exists but its absolute axis could not be read "
                f"({sketch.get('frameError') or 'unknown error'}); left/right cannot be converted",
                ["reference.frame"],
            )
        direction_text = str(reference.get("directionText") or "").strip().lower()
        direction_text = {
            "left": "left", "左": "left", "左侧": "left",
            "right": "right", "右": "right", "右侧": "right",
            "up": "up", "上": "up", "上方": "up",
            "down": "down", "下": "down", "下方": "down",
        }.get(direction_text, direction_text)
        if direction_text in ("left", "right", "up", "down"):
            vector = sketch_frame.get("horizontal" if direction_text in ("left", "right") else "vertical")
            mapped = _axis_of_vector(vector)
            if mapped is None:
                return _unsupported(
                    f"sketch {sketch_name} plane {sketch_frame.get('plane')} is not axis-aligned; "
                    f"{direction_text} cannot be converted to a single model axis",
                    ["reference.frame"],
                )
            mapped_axis, sign = mapped
            if direction_text in ("left", "down"):
                sign = -sign
            mapped_direction = "positive" if sign > 0 else "negative"
            if axis is not None and mapped_axis != axis:
                return _clarify(
                    f"sketch {sketch_name} maps {direction_text} to {mapped_axis}{'+' if sign > 0 else '-'}, "
                    f"but the driver that changes {handle} moves the {axis} axis; confirm the dimension",
                    ["reference.frame"],
                )
            axis = mapped_axis
            direction = mapped_direction
            _check(
                result,
                "frame-sketch",
                "pass",
                f"{direction_text} in sketch {sketch_name} = {mapped_direction} along model {mapped_axis}",
                frame=sketch_frame,
            )
        elif ref_axis is not None:
            mapped_axes = {
                entry[0]
                for entry in (
                    _axis_of_vector(sketch_frame.get("horizontal")),
                    _axis_of_vector(sketch_frame.get("vertical")),
                )
                if entry
            }
            if ref_axis not in mapped_axes:
                return _clarify(
                    f"reference.axis {ref_axis} is not an axis of sketch {sketch_name} "
                    f"(its plane is {sketch_frame.get('plane')})",
                    ["reference.axis"],
                )
            axis = ref_axis
            _check(result, "frame-sketch", "pass", f"axis {ref_axis} is an axis of sketch {sketch_name}")
        elif scope == "single_side":
            return _clarify(
                f"a single-side change in sketch {sketch_name} needs directionText "
                f"(left/right/up/down); the sketch plane {sketch_frame.get('plane')} cannot be assumed",
                ["reference.directionText"],
                _driver_candidates(snapshot),
            )
        else:
            _check(
                result,
                "frame-sketch",
                "pass",
                f"sketch {sketch_name} frame is known (plane {sketch_frame.get('plane')})",
                frame=sketch_frame,
            )
    else:
        return _clarify(
            f"reference.frame {frame!r} is not a stable frame; use model or sketch:<name>. "
            f"View-relative directions change with the camera and cannot be reused",
            ["reference.frame"],
        )

    if scope == "single_side" and direction is None:
        return _clarify(
            "a single-side change needs an explicit direction (positive/negative, or "
            "directionText inside a named sketch frame)",
            ["reference.direction"],
        )

    # ---- scope feasibility from measured edge movement ---------------
    edge = _edge_ratios(impact, axis) if axis else None
    if scope == "each_side":
        if edge is None:
            return _unsupported(
                f"no per-edge bbox evidence for {handle}; re-run catia_begin_edit before using each_side",
                ["snapshot.impacts.bboxEdgeDeltas"],
            )
        low, high = edge
        if not (_numeric_equal(low, -0.5, 1e-3) and _numeric_equal(high, 0.5, 1e-3)):
            return _unsupported(
                f"{handle} does not grow both sides symmetrically (measured per-unit edge movement "
                f"min={low:g}, max={high:g}); 'each side' cannot be achieved by changing this parameter",
                ["operation.scope"],
            )
        _check(result, "scope-each-side", "pass", "the driver moves both boundaries outward symmetrically")
    elif scope == "single_side":
        if edge is None:
            return _unsupported(
                f"no per-edge bbox evidence for {handle}; re-run catia_begin_edit before using single_side",
                ["snapshot.impacts.bboxEdgeDeltas"],
            )
        low, high = edge
        if direction == "positive":
            feasible = _numeric_equal(high, 1.0, 1e-3) and _numeric_equal(low, 0.0, 1e-3)
            wanted = "+axis"
        else:
            feasible = _numeric_equal(low, -1.0, 1e-3) and _numeric_equal(high, 0.0, 1e-3)
            wanted = "-axis"
        if not feasible:
            return _unsupported(
                f"{handle} cannot extend only the {wanted} side (measured per-unit edge movement "
                f"min={low:g}, max={high:g}); this single-side request is not expressible with the "
                f"available parameters",
                ["operation.scope"],
            )
        _check(result, "scope-single-side", "pass", f"the driver moves only the {wanted} boundary")

    # ---- compute the absolute target ---------------------------------
    if op_type == "set":
        if meaning == "hole_dimension":
            target_radius = amount_in_param if attribute == "radius" else amount_in_param / 2.0
            new_value = target_radius if hole_kind == "radius" else target_radius * 2.0
            resolved = (
                f"{handle}: set {attribute} to {_format_number(amount)} {operation_unit} "
                f"= radius {_format_number(target_radius)} {param_unit} "
                f"= {_format_number(new_value)} {param_unit} on a {hole_kind} parameter"
            )
        else:
            new_value = amount_in_param
            resolved = f"{handle}: set to {_format_number(amount)} {operation_unit} = {_format_number(new_value)} {param_unit}"
    else:
        sign = 1.0 if op_type == "increase" else -1.0
        factor_scope = 2.0 if scope == "each_side" else 1.0
        delta_param = amount_in_param * factor_scope * sign
        if meaning == "hole_dimension":
            delta_radius = delta_param if attribute == "radius" else delta_param / 2.0
            base_radius = base_value if hole_kind == "radius" else base_value / 2.0
            target_radius = base_radius + delta_radius
            new_value = target_radius if hole_kind == "radius" else target_radius * 2.0
            resolved = (
                f"{handle}: {op_type} {attribute} by {_format_number(amount)} {operation_unit} "
                f"({'+' if sign > 0 else '-'}{_format_number(amount_in_param * factor_scope)} {param_unit} on the parameter) "
                f"from {_format_number(base_value)} to {_format_number(new_value)} {param_unit}"
            )
        else:
            new_value = base_value + delta_param
            scope_note = " x2 (each side)" if factor_scope == 2.0 else ""
            resolved = (
                f"{handle}: {_format_number(base_value)} {param_unit} "
                f"{'+' if sign > 0 else '-'} {_format_number(amount_in_param)}{scope_note} "
                f"= {_format_number(new_value)} {param_unit} (basis={basis})"
            )
    new_value = _round(new_value)
    if param_unit == "mm" and new_value <= 0:
        return _unsupported(f"the computed target {new_value} mm is not a positive length", ["operation.amount"])
    if abs(new_value) > 1e6:
        return _unsupported(f"the computed target {new_value} is outside the supported range", ["operation.amount"])
    _check(result, "arithmetic", "pass", resolved, newValue=new_value, basis=basis, baseValue=base_value)

    # ---- preserve ----------------------------------------------------
    preserve_raw = intent.get("preserve") or []
    if not isinstance(preserve_raw, list):
        return _invalid("preserve must be a list", "preserve")
    preserve_types: list[str] = []
    preserve_scopes: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(preserve_raw):
        value = item.get("type") if isinstance(item, dict) else item
        normalized = _norm_preserve(value)
        if normalized is None:
            return _invalid(f"preserve[{index}] is empty", f"preserve[{index}]")
        if normalized not in _PRESERVE_TYPES:
            return _unsupported(
                f"preserve[{index}] type {value!r} is not implemented; it cannot be dropped silently",
                [f"preserve[{index}]"],
            )
        if isinstance(item, dict):
            scope: dict[str, Any] = {}
            if item.get("sketch"):
                scope["sketch"] = str(item["sketch"]).strip()
            if item.get("holes"):
                if not isinstance(item["holes"], list):
                    return _invalid(f"preserve[{index}].holes must be a list", f"preserve[{index}].holes")
                scope["holes"] = list(item["holes"])
            if scope:
                preserve_scopes[normalized] = scope
        if normalized not in preserve_types:
            preserve_types.append(normalized)

    consistency = _text_consistency(
        text,
        attribute=attribute,
        scope=scope,
        basis=basis,
        preserve_types=preserve_types,
        holes=_confirmed_holes(snapshot),
        requested_holes=target.get("holeCount"),
        operation_unit=operation_unit,
        param_unit=param_unit,
        target_handle=handle,
        target_binding=target.get("binding"),
        requested_sketch=target.get("sketch"),
        requested_kind=target.get("kind"),
        sketch_discriminator=sketch_discriminator,
        hole_count_discriminator=hole_count_discriminator,
        kind_discriminator=kind_discriminator,
        check_provenance=text_source == "model",
    )
    if consistency:
        result["decision"] = "needs_clarification"
        result["unresolved"] = [issue["path"] for issue in consistency]
        result["message"] = "; ".join(issue["reason"] for issue in consistency)
        result["candidates"] = _driver_candidates(snapshot)
        for issue in consistency:
            _check(result, "text-consistency", "fail", issue["reason"], path=issue["path"])
        return result
    _check(result, "text-consistency", "pass", "the structured fields agree with high-confidence phrases in the text")

    preserve_plan: list[dict[str, Any]] = []
    object_edges = (snapshot.get("geometry") or {}).get("bbox") or {}
    confirmed_holes = _confirmed_holes(snapshot)
    margin = _edge_margin(confirmed_holes, object_edges)
    if "center_position" in preserve_types:
        if meaning != "linear_dimension" or edge is None:
            return _unsupported(
                "center_position can only be preserved for a linear dimension with measured per-edge movement",
                ["preserve.center_position"],
            )
        low, high = edge
        if not _numeric_equal(low + high, 0.0, 1e-3):
            return _unsupported(
                f"{handle} grows asymmetrically (per-unit edge movement min={low:g}, max={high:g}); "
                f"the part centre cannot stay fixed",
                ["preserve.center_position"],
            )
        if scope == "single_side":
            return _unsupported(
                "a single-side change necessarily moves the part centre; center_position contradicts it",
                ["preserve.center_position"],
            )
        _check(result, "preserve-center", "pass", "measured symmetric growth keeps the bbox centre fixed")
    if "hole_edge_distance" in preserve_types:
        capability = ((snapshot.get("capabilities") or {}).get("invariants") or {}).get("holes_edge_distance") or {}
        scope = preserve_scopes.get("hole_edge_distance")
        scoped_indices = _scope_hole_indices(snapshot, scope)
        if not confirmed_holes:
            return _unsupported(
                "the model has no measured hole geometry; hole edge distance cannot be verified",
                ["preserve.hole_edge_distance"],
            )
        if scoped_indices is None:
            return _invalid(
                "preserve.hole_edge_distance scope must name a sketch or list 1-based measured hole indices",
                "preserve.hole_edge_distance",
            )
        if not scoped_indices:
            return _unsupported(
                "the requested hole-edge-distance scope contains no measured hole",
                ["preserve.hole_edge_distance"],
            )
        scoped_holes = [snapshot["holes"][index] for index in scoped_indices]
        scoped_margin = _edge_margin(scoped_holes, object_edges)
        if scoped_margin is None:
            return _unsupported(
                "the current hole edge distance cannot be measured from the snapshot",
                ["preserve.hole_edge_distance"],
            )
        margins = impact.get("edgeMargins")
        if scope is not None:
            # A scoped preserve is checked against this driver's per-hole
            # margins, even when the global invariant is false because another
            # hole group (e.g. a central side hole) does move.
            if not isinstance(margins, dict):
                return _unsupported(
                    f"no per-hole edge-margin evidence for {handle}; re-run catia_begin_edit",
                    ["preserve.hole_edge_distance"],
                )
            before = margins.get("before") or []
            after = margins.get("after") or []
            if len(before) != len(after) or len(before) != len(snapshot.get("holes") or []):
                return _unsupported(
                    "per-hole edge-margin evidence is incomplete for this snapshot",
                    ["preserve.hole_edge_distance"],
                )
            deviation = max(
                max(
                    abs(float(after[index][0]) - float(before[index][0])),
                    abs(float(after[index][1]) - float(before[index][1])),
                )
                for index in scoped_indices
            )
            if deviation > 1e-6:
                return _unsupported(
                    f"changing {handle} changes the edge margin of the requested hole scope by "
                    f"{round(deviation, 6)} mm",
                    ["preserve.hole_edge_distance"],
                )
            _check(
                result,
                "preserve-edge-distance-scoped",
                "pass",
                f"the scoped holes keep their margins (max deviation {round(deviation, 6)} mm)",
                holes=scoped_indices,
            )
        else:
            if not capability.get("supported"):
                return _unsupported(
                    str(capability.get("reason") or "hole edge distance is not provable on this model"),
                    ["preserve.hole_edge_distance"],
                )
            if impact.get("geometryChanged") and impact.get("edgeDistancesPreserved") is not True:
                return _unsupported(
                    f"changing {handle} moves the boundary without keeping the hole edge distance",
                    ["preserve.hole_edge_distance"],
                )
        preserve_item = {
            "type": "holes_edge_distance",
            "parameter": _find_edge_parameter(snapshot, scoped_margin) or handle,
            "targetValue": scoped_margin,
        }
        if scope is not None:
            # An explicit scope is what makes this a per-hole assertion. The
            # implicit "all measured holes" case stays global so older snapshots
            # without per-hole margins keep their existing behaviour.
            preserve_item["holes"] = [index + 1 for index in scoped_indices]
            if scope.get("sketch"):
                preserve_item["sketch"] = scope["sketch"]
        preserve_plan.append(preserve_item)
        _check(
            result,
            "preserve-edge-distance",
            "pass",
            f"current edge margin {scoped_margin} mm is measurable for {len(scoped_indices)} hole(s)",
        )
    if "hole_centers" in preserve_types:
        if not confirmed_holes:
            return _unsupported(
                "the model has no measured holes; hole centres cannot be preserved",
                ["preserve.hole_centers"],
            )
        if impact.get("holeCentersMoved") is True:
            return _unsupported(
                f"changing {handle} moves the hole centres; keeping them fixed contradicts the requested change",
                ["preserve.hole_centers"],
            )
        _check(result, "preserve-hole-centers", "pass", "the measured change does not move hole centres")
        if any(isinstance(hole.get("centerWorld"), dict) for hole in confirmed_holes):
            preserve_plan.append(
                {
                    "type": "hole_world_position",
                    "holes": [
                        {
                            "x": round(float(hole["centerWorld"]["x"]), 6),
                            "y": round(float(hole["centerWorld"]["y"]), 6),
                            "z": round(float(hole["centerWorld"]["z"]), 6),
                        }
                        for hole in confirmed_holes
                        if isinstance(hole.get("centerWorld"), dict)
                    ],
                }
            )
    if "hole_world_position" in preserve_types:
        if not confirmed_holes:
            return _unsupported(
                "the model has no measured holes; world hole positions cannot be preserved",
                ["preserve.hole_world_position"],
            )
        if any(not isinstance(hole.get("centerWorld"), dict) for hole in confirmed_holes):
            return _unsupported(
                "world coordinates are unavailable for at least one hole (sketch frame readback failed); "
                "the condition cannot be asserted",
                ["preserve.hole_world_position"],
            )
        if impact.get("holeWorldCentersMoved") is True or impact.get("holeCentersMoved") is True:
            return _unsupported(
                f"changing {handle} moves the hole centres in model coordinates; keeping them fixed "
                f"contradicts the requested change",
                ["preserve.hole_world_position"],
            )
        if not any(item.get("type") == "hole_world_position" for item in preserve_plan):
            preserve_plan.append(
                {
                    "type": "hole_world_position",
                    "holes": [
                        {
                            "x": round(float(hole["centerWorld"]["x"]), 6),
                            "y": round(float(hole["centerWorld"]["y"]), 6),
                            "z": round(float(hole["centerWorld"]["z"]), 6),
                        }
                        for hole in confirmed_holes
                    ],
                }
            )
        _check(
            result,
            "preserve-hole-world",
            "pass",
            "the measured change does not move hole centres in model coordinates",
        )
    if "other_dimensions" in preserve_types:
        for param in snapshot.get("parameters", []):
            other = param.get("handle")
            if other == handle or param.get("kind") != "driver" or param.get("writable") is not True:
                continue
            value = _num(param.get("value"))
            if value is None:
                continue
            preserve_plan_other = {
                "type": "unchanged_dimension",
                "parameter": other,
                "expected": value,
            }
            # unchanged_dimension is an acceptance assertion, not a preserve type.
            result.setdefault("_extra_acceptance", []).append(preserve_plan_other)
        _check(result, "preserve-other-dimensions", "pass", "other writable drivers will be asserted unchanged")

    # ---- acceptance --------------------------------------------------
    acceptance: list[dict[str, Any]] = []
    if meaning == "linear_dimension":
        current_dim = _num(((snapshot.get("geometry") or {}).get("bbox") or {}).get("dimensions", {}).get(axis))
        if current_dim is None:
            return _unsupported(
                f"the current bbox dimension on {axis} is unavailable; the effect cannot be asserted",
                ["geometry.bbox"],
            )
        expected_dim = _round(current_dim + (new_value - current) * float(ratio or 1.0))
        acceptance.append({"type": "bbox_dimension", "axis": axis, "expected": expected_dim})
        if "center_position" in preserve_types or scope == "single_side":
            if edge is None:
                return _unsupported(
                    "per-edge bbox evidence is unavailable; the requested boundary condition cannot be asserted",
                    ["preserve"],
                )
            low, high = edge
            delta = new_value - current
            try:
                new_min = round(float(object_edges["min"][axis]) + delta * low, 6)
                new_max = round(float(object_edges["max"][axis]) + delta * high, 6)
            except Exception:
                return _unsupported("the current bbox min/max is unavailable", ["geometry.bbox"])
            acceptance.append({"type": "bbox_range", "axis": axis, "min": new_min, "max": new_max})
    elif meaning == "hole_dimension":
        # The radius assertion must target the circles this parameter drives,
        # including an additive boss. ``_confirmed_holes`` deliberately excludes
        # the boss, so using it here compared the boss target (15 mm) with a
        # measured hole's radius (5 mm) and produced a deterministic
        # WRONG_EFFECT (observed on FixtureF F02).
        all_circles = snapshot.get("holes") or []
        hole_circles = _confirmed_holes(snapshot)
        driven = [
            hole for hole in all_circles if handle in (hole.get("radiusDrivers") or [])
        ] or hole_circles or all_circles
        if not driven:
            return _unsupported(
                "the model has no circular profile; a hole-size effect cannot be asserted",
                ["holes"],
            )
        current_radius = _num(driven[0].get("radius"))
        if current_radius is None:
            return _unsupported("the current hole radius is unavailable", ["holes"])
        radius_per_param = 0.5 if hole_kind == "diameter" else 1.0
        expected_radius = _round(current_radius + (new_value - current) * radius_per_param)
        if expected_radius <= 0:
            return _unsupported(f"the computed hole radius {expected_radius} is not positive", ["operation.amount"])
        acceptance.append({"type": "hole_radius", "expected": expected_radius, "parameter": handle})
        if "hole_centers" in preserve_types:
            acceptance.append(
                {
                    "type": "hole_positions",
                    "holes": [
                        {"x": round(float(hole["center"]["x"]), 6), "y": round(float(hole["center"]["y"]), 6)}
                        for hole in hole_circles
                    ],
                }
            )
        if hole_circles and all(isinstance(hole.get("centerWorld"), dict) for hole in hole_circles):
            acceptance.append(
                {
                    "type": "hole_world_positions",
                    "holes": [
                        {
                            "x": round(float(hole["centerWorld"]["x"]), 6),
                            "y": round(float(hole["centerWorld"]["y"]), 6),
                            "z": round(float(hole["centerWorld"]["z"]), 6),
                        }
                        for hole in hole_circles
                    ],
                }
            )
    for extra in result.get("_extra_acceptance") or []:
        acceptance.append(extra)
    result.pop("_extra_acceptance", None)

    # ---- plan --------------------------------------------------------
    op_id = operation_id or intent.get("operationId")
    if not isinstance(op_id, str) or not op_id.strip():
        seed = json.dumps(
            {
                "session": session_id,
                "revision": snapshot.get("modelRevision"),
                "handle": handle,
                "newValue": new_value,
                "preserve": preserve_types,
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        op_id = "intent-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    op_id = op_id.strip()
    plan = {
        "schema": PLAN_SCHEMA,
        "editSessionId": session_id,
        "operationId": op_id,
        "expectedRevision": snapshot.get("modelRevision"),
        "intent": {
            "text": text,
            "resolved": resolved,
            "textSource": text_source,
        },
        "changes": [
            {
                "parameter": handle,
                "oldValue": current,
                "newValue": new_value,
                "unit": param_unit,
                "reason": resolved,
            }
        ],
        "preserve": preserve_plan,
        "acceptance": acceptance,
    }
    problems = validate_plan(plan, snapshot, session_id=session_id)
    if problems:
        stale = [p for p in problems if p.get("code") == "STALE_REVISION"]
        if stale:
            return _clarify(
                "the snapshot revision changed while compiling; re-read the model before planning",
                ["expectedRevision"],
            )
        result["decision"] = "invalid"
        result["problems"] = problems
        result["message"] = "compiled plan failed server-side static validation"
        result["plan"] = plan
        return result
    result["plan"] = plan
    result["operationId"] = op_id
    _check(result, "plan-validation", "pass", "compiled plan passes validate_plan unchanged")
    result["message"] = resolved
    return result
