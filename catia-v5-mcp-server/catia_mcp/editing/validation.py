"""Deterministic plan validation. The LLM may propose; only this module decides."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from catia_mcp.editing.probe import approx_equal

PLAN_SCHEMA = "catia-edit-plan/1"
ACCEPTANCE_TYPES = {
    "bbox_range",
    "bbox_dimension",
    "volume",
    "hole_positions",
    "hole_world_positions",
    "hole_radius",
    "unchanged_dimension",
    "feature_count",
}
REQUIRED_FIELDS = {
    "bbox_range": ("axis", "min", "max"),
    "bbox_dimension": ("axis", "expected"),
    "volume": ("expected",),
    "hole_positions": ("holes",),
    "hole_world_positions": ("holes",),
    "hole_radius": ("expected",),
    "unchanged_dimension": ("parameter", "expected"),
    "feature_count": ("expected",),
}
PRESERVE_TYPES = {"holes_edge_distance", "hole_world_position"}
_UNIT_ALIASES = {"mm": "mm", "millimeter": "mm", "毫米": "mm", "deg": "deg", "degree": "deg", "度": "deg", "": "none", "none": "none"}


def plan_hash(plan: dict[str, Any]) -> str:
    canonical = json.dumps(plan, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _norm_unit(unit: Any) -> str:
    return _UNIT_ALIASES.get(str(unit or "").strip().lower(), str(unit))


def _reject(code: str, message: str, path: str = "") -> dict[str, Any]:
    return {"code": code, "message": message, "path": path}


def validate_plan(
    plan: Any, snapshot: dict[str, Any], *, session_id: str
) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if not isinstance(plan, dict):
        return [_reject("INVALID_PLAN", "plan must be a JSON object")]
    if plan.get("schema") != PLAN_SCHEMA:
        problems.append(_reject("INVALID_PLAN", f"schema must be {PLAN_SCHEMA}", "schema"))
    if plan.get("editSessionId") != session_id:
        problems.append(_reject("INVALID_PLAN", "editSessionId does not match this session", "editSessionId"))
    operation_id = plan.get("operationId")
    if not isinstance(operation_id, str) or not operation_id.strip():
        problems.append(_reject("INVALID_PLAN", "operationId is required", "operationId"))
    if plan.get("expectedRevision") != snapshot.get("modelRevision"):
        problems.append(
            _reject(
                "STALE_REVISION",
                f"expectedRevision {plan.get('expectedRevision')} != current {snapshot.get('modelRevision')}",
                "expectedRevision",
            )
        )

    params = {p["handle"]: p for p in snapshot.get("parameters", [])}
    impacts = snapshot.get("impacts", {})
    changes = plan.get("changes")
    if not isinstance(changes, list) or not changes:
        problems.append(_reject("INVALID_PLAN", "changes must be a non-empty list", "changes"))
        changes = []
    seen: set[str] = set()
    for index, change in enumerate(changes):
        path = f"changes[{index}]"
        if not isinstance(change, dict):
            problems.append(_reject("INVALID_PLAN", "change must be an object", path))
            continue
        handle = change.get("parameter")
        if handle not in params:
            problems.append(_reject("PARAM_UNKNOWN", f"unknown parameter handle: {handle!r}", f"{path}.parameter"))
            continue
        if handle in seen:
            problems.append(_reject("INVALID_PLAN", "duplicate parameter in one plan", f"{path}.parameter"))
            continue
        seen.add(handle)
        param = params[handle]
        if param.get("kind") != "driver" or param.get("writable") is not True:
            problems.append(
                _reject(
                    "PARAM_NOT_WRITABLE",
                    f"{handle} is not a writable driver (kind={param.get('kind')}, writable={param.get('writable')})",
                    f"{path}.parameter",
                )
            )
        unit = _norm_unit(change.get("unit"))
        param_unit = _norm_unit(param.get("unit"))
        if unit not in ("mm", "deg", "none"):
            problems.append(_reject("UNIT_INCOMPATIBLE", f"unsupported unit {unit!r}", f"{path}.unit"))
        elif param_unit == "unknown":
            problems.append(
                _reject(
                    "UNIT_INCOMPATIBLE",
                    f"parameter unit is not reported by CATIA; a {unit!r} value cannot be applied safely",
                    f"{path}.unit",
                )
            )
        elif param_unit not in ("mm", "deg", "none"):
            problems.append(
                _reject("UNIT_INCOMPATIBLE", f"parameter unit {param.get('unit')!r} has no comparable unit", f"{path}.unit")
            )
        elif param_unit == "none" and unit != "none":
            problems.append(
                _reject(
                    "UNIT_INCOMPATIBLE",
                    f"parameter has no physical unit but the change declares {unit!r}",
                    f"{path}.unit",
                )
            )
        elif param_unit in ("mm", "deg") and unit != param_unit:
            problems.append(
                _reject("UNIT_INCOMPATIBLE", f"change unit {unit!r} != parameter unit {param_unit!r}", f"{path}.unit")
            )
        new_value = change.get("newValue")
        if not isinstance(new_value, (int, float)) or isinstance(new_value, bool):
            problems.append(_reject("INVALID_PLAN", "newValue must be a number", f"{path}.newValue"))
        elif param_unit == "mm" and float(new_value) <= 0:
            problems.append(_reject("OUT_OF_RANGE", "length must be > 0", f"{path}.newValue"))
        elif abs(float(new_value)) > 1e6:
            problems.append(_reject("OUT_OF_RANGE", "value out of supported range (|v| <= 1e6)", f"{path}.newValue"))
        old_value = change.get("oldValue")
        if not isinstance(old_value, (int, float)) or isinstance(old_value, bool):
            problems.append(_reject("INVALID_PLAN", "oldValue is required", f"{path}.oldValue"))
        elif isinstance(param.get("value"), (int, float)) and not approx_equal(
            float(old_value), float(param["value"]), rel=1e-9, abs_tol=1e-6
        ):
            problems.append(
                _reject(
                    "STALE_PLAN",
                    f"oldValue {old_value} != current {param.get('value')} for {handle}",
                    f"{path}.oldValue",
                )
            )

    invariants = snapshot.get("capabilities", {}).get("invariants", {})
    preserve = plan.get("preserve") or []
    if not isinstance(preserve, list):
        problems.append(_reject("INVALID_PLAN", "preserve must be a list", "preserve"))
        preserve = []
    for index, item in enumerate(preserve):
        path = f"preserve[{index}]"
        if not isinstance(item, dict) or item.get("type") not in PRESERVE_TYPES:
            problems.append(_reject("INVALID_PLAN", "unsupported preserve type", path))
            continue
        if item.get("type") == "holes_edge_distance":
            scope = item.get("holes") if isinstance(item.get("holes"), list) else None
            if scope is not None:
                if not scope or any(
                    not isinstance(value, int) or isinstance(value, bool) or value <= 0 for value in scope
                ):
                    problems.append(
                        _reject("INVALID_PLAN", "preserve.holes must be 1-based positive hole indices", f"{path}.holes")
                    )
                    continue
                for change in changes:
                    impact = impacts.get(change.get("parameter"), {})
                    margins = impact.get("edgeMargins") if isinstance(impact, dict) else None
                    if not isinstance(margins, dict):
                        problems.append(
                            _reject(
                                "INVARIANT_UNSUPPORTED",
                                f"no per-hole edge-margin evidence for {change.get('parameter')}",
                                path,
                            )
                        )
                        continue
                    before = margins.get("before") or []
                    after = margins.get("after") or []
                    deviation = None
                    try:
                        deviation = max(
                            max(
                                abs(float(after[index - 1][0]) - float(before[index - 1][0])),
                                abs(float(after[index - 1][1]) - float(before[index - 1][1])),
                            )
                            for index in scope
                        )
                    except Exception:
                        deviation = None
                    if deviation is None or deviation > 1e-6:
                        problems.append(
                            _reject(
                                "INVARIANT_UNSUPPORTED",
                                f"changing {change.get('parameter')} changes the scoped hole edge margin",
                                path,
                            )
                        )
            else:
                capability = invariants.get("holes_edge_distance", {})
                if not capability.get("supported"):
                    problems.append(
                        _reject("INVARIANT_UNSUPPORTED", capability.get("reason", "hole edge distance is not provable"), path)
                    )
                for change in changes:
                    impact = impacts.get(change.get("parameter"), {})
                    if impact.get("geometryChanged") and not impact.get("edgeDistancesPreserved", False):
                        problems.append(
                            _reject(
                                "INVARIANT_UNSUPPORTED",
                                f"changing {change.get('parameter')} moves a boundary without moving the holes",
                                path,
                            )
                        )
        if item.get("type") == "hole_world_position":
            holes = item.get("holes")
            if not isinstance(holes, list) or not holes:
                problems.append(_reject("INVALID_PLAN", "hole_world_position requires holes", path))
                continue
            capability = (snapshot.get("preserves") or {}).get("hole_centers") or {}
            if not capability.get("worldSupported"):
                problems.append(
                    _reject(
                        "INVARIANT_UNSUPPORTED",
                        "world hole coordinates are not available for every hole; the condition cannot be asserted",
                        path,
                    )
                )
            for hole_index, hole in enumerate(holes):
                if not isinstance(hole, dict) or not all(
                    _is_number(hole.get(axis)) for axis in ("x", "y", "z")
                ):
                    problems.append(
                        _reject("INVALID_PLAN", "each world hole needs numeric x, y and z", f"{path}.holes[{hole_index}]")
                    )

    acceptance = plan.get("acceptance") or []
    if not isinstance(acceptance, list):
        problems.append(_reject("INVALID_PLAN", "acceptance must be a list", "acceptance"))
        acceptance = []
    for index, item in enumerate(acceptance):
        path = f"acceptance[{index}]"
        if not isinstance(item, dict) or item.get("type") not in ACCEPTANCE_TYPES:
            problems.append(_reject("INVALID_PLAN", f"unsupported acceptance type: {item!r}", path))
            continue
        kind = item["type"]
        missing = [field for field in REQUIRED_FIELDS[kind] if field not in item]
        if missing:
            problems.append(_reject("INVALID_PLAN", f"{kind} requires {', '.join(missing)}", path))
            continue
        problems.extend(_check_acceptance_shape(kind, item, path))
        for field, limit in (("absTolerance", 1.0), ("relTolerance", 1e-3)):
            if field in item:
                try:
                    value = float(item[field])
                except Exception:
                    problems.append(_reject("INVALID_PLAN", f"{field} must be a number", f"{path}.{field}"))
                    continue
                if value <= 0 or value > limit:
                    problems.append(
                        _reject("INVALID_PLAN", f"{field} must be in (0, {limit}]", f"{path}.{field}")
                    )
    return problems


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _check_acceptance_shape(kind: str, item: dict[str, Any], path: str) -> list[dict[str, Any]]:
    problems: list[dict[str, Any]] = []
    if kind == "bbox_range":
        if item["axis"] not in ("x", "y", "z"):
            problems.append(_reject("INVALID_PLAN", "axis must be x, y or z", f"{path}.axis"))
        if not (_is_number(item["min"]) and _is_number(item["max"])):
            problems.append(_reject("INVALID_PLAN", "min/max must be numbers", path))
        elif float(item["min"]) >= float(item["max"]):
            problems.append(_reject("INVALID_PLAN", "min must be below max", path))
    elif kind == "bbox_dimension":
        if item["axis"] not in ("x", "y", "z"):
            problems.append(_reject("INVALID_PLAN", "axis must be x, y or z", f"{path}.axis"))
        if not _is_number(item["expected"]):
            problems.append(_reject("INVALID_PLAN", "expected must be a number", f"{path}.expected"))
    elif kind in ("volume", "hole_radius"):
        if not _is_number(item["expected"]):
            problems.append(_reject("INVALID_PLAN", "expected must be a number", f"{path}.expected"))
        if kind == "hole_radius" and "parameter" in item and not isinstance(item["parameter"], str):
            problems.append(_reject("INVALID_PLAN", "parameter must be a handle string", f"{path}.parameter"))
    elif kind == "hole_positions":
        holes = item["holes"]
        if not isinstance(holes, list) or not holes:
            problems.append(_reject("INVALID_PLAN", "holes must be a non-empty list", f"{path}.holes"))
            return problems
        for hole_index, hole in enumerate(holes):
            hole_path = f"{path}.holes[{hole_index}]"
            if not isinstance(hole, dict) or not (_is_number(hole.get("x")) and _is_number(hole.get("y"))):
                problems.append(_reject("INVALID_PLAN", "each hole needs numeric x and y", hole_path))
            elif "r" in hole and not _is_number(hole["r"]):
                problems.append(_reject("INVALID_PLAN", "r must be a number", f"{hole_path}.r"))
    elif kind == "hole_world_positions":
        holes = item["holes"]
        if not isinstance(holes, list) or not holes:
            problems.append(_reject("INVALID_PLAN", "holes must be a non-empty list", f"{path}.holes"))
            return problems
        for hole_index, hole in enumerate(holes):
            hole_path = f"{path}.holes[{hole_index}]"
            if not isinstance(hole, dict) or not all(_is_number(hole.get(axis)) for axis in ("x", "y", "z")):
                problems.append(_reject("INVALID_PLAN", "each world hole needs numeric x, y and z", hole_path))
    elif kind == "unchanged_dimension":
        if not isinstance(item["parameter"], str) or not item["parameter"]:
            problems.append(_reject("INVALID_PLAN", "parameter must be a handle string", f"{path}.parameter"))
        if not _is_number(item["expected"]):
            problems.append(_reject("INVALID_PLAN", "expected must be a number", f"{path}.expected"))
    elif kind == "feature_count":
        expected = item["expected"]
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            problems.append(_reject("INVALID_PLAN", "expected must be a non-negative integer", f"{path}.expected"))
    return problems


def acceptance_tolerance(item: dict[str, Any]) -> float:
    if "absTolerance" in item:
        return float(item["absTolerance"])
    return 0.01
