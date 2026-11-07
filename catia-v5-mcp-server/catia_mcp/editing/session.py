"""Edit sessions: working-copy creation, document binding, snapshots, impact map."""

from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from catia_mcp.editing.graph import build_relations_graph
from catia_mcp.editing.probe import (
    approx_equal,
    read_bbox,
    read_model,
    read_parameters,
    read_holes,
    read_volume_area,
)

SCHEMA = "catia-edit/1"
_SYSTEM_DRIVER_MAX = 1


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def new_session_id() -> str:
    return uuid.uuid4().hex[:12]


def resolve_document(app: Any, full_path: str):
    """Find the open document whose FullName is exactly this path.

    Binding is by absolute path, never by name and never via ActiveDocument:
    two files with the same base name must stay distinguishable. If any open
    document refuses to report its FullName the scan is inconclusive and raises,
    so callers never treat "could not look" as "not open".
    """
    target = norm(full_path)
    docs = app.Documents
    unreadable = 0
    for i in range(1, docs.Count + 1):
        doc = docs.Item(i)
        try:
            name = doc.FullName
        except Exception:
            unreadable += 1
            continue
        if name and os.path.isabs(str(name)) and norm(str(name)) == target:
            return doc
    if unreadable:
        raise RuntimeError(f"DOCUMENT_SCAN_INCOMPLETE: {unreadable} open document(s) did not report a path")
    return None


def _activate(app: Any, doc: Any) -> None:
    try:
        doc.Activate()
    except Exception:
        pass


def create_working_copy(
    app: Any, source_path: str, work_dir: str, session_id: str
) -> tuple[Any, str, bool]:
    """Make the server-owned working copy and bind it to an open document.

    Returns ``(doc, working_path, copied_by_server)``. ``copied_by_server`` is True when
    CATIA refused ``SaveAs`` and the copy was produced by a byte copy instead (踩坑 66);
    the caller must surface that, because delivery saving later needs the same authority.

    Documents.NewFrom gives the copy its own identity, so a copy never aliases
    an open original (踩坑报告 12). If NewFrom is unavailable, a byte copy plus
    Documents.Open is used and the resulting FullName must match exactly.
    """
    source_path = os.path.abspath(source_path)
    # CATIA SaveAs rejects forward-slash paths with a bare E_FAIL on Windows;
    # normalize the caller-supplied work directory before joining/opening it.
    work_dir = os.path.abspath(work_dir)
    os.makedirs(work_dir, exist_ok=True)
    # The session id keeps two sources that share a base name apart, and makes a
    # retry after a failed begin_edit impossible to collide with its leftovers.
    working_path = os.path.join(work_dir, f"{Path(source_path).stem}-{session_id}.CATPart")
    if os.path.exists(working_path):
        raise FileExistsError(f"working copy path already exists: {working_path}")

    app.DisplayFileAlerts = False
    copied_by_server = False
    try:
        doc = None
        try:
            doc = app.Documents.NewFrom(source_path)
        except Exception:
            doc = None
        if doc is not None:
            try:
                doc.SaveAs(working_path)
            except Exception as save_error:  # noqa: BLE001 - COM raises bare Exception
                # NewFrom+SaveAs needs CATIA's own write authorization. When the driven
                # CATIA instance cannot create files (踩坑 66: sandboxed instance or an
                # endpoint policy), the byte copy still yields an identical working copy.
                try:
                    doc.Close()
                except Exception:
                    pass
                doc = None
                shutil.copy2(source_path, working_path)
                copied_by_server = True
                try:
                    doc = app.Documents.Open(working_path)
                except Exception as open_error:
                    raise RuntimeError(
                        "CATIA refused SaveAs ({0}) and the byte-copy fallback could not "
                        "open '{1}': {2}".format(save_error, working_path, open_error)
                    ) from open_error
        else:
            shutil.copy2(source_path, working_path)
            copied_by_server = True
            doc = app.Documents.Open(working_path)
        try:
            actual = str(doc.FullName)
        except Exception:
            actual = ""
        if norm(actual) != norm(working_path):
            raise RuntimeError(
                f"working copy binding failed: CATIA reports '{actual}', expected '{working_path}'"
            )
    finally:
        app.DisplayFileAlerts = True
    _activate(app, doc)
    return doc, working_path, copied_by_server


def _param_index(part: Any) -> dict[str, Any]:
    out = {}
    params = part.Parameters
    for i in range(1, params.Count + 1):
        p = params.Item(i)
        out[str(p.Name)] = p
    return out


def get_param(part: Any, handle: str):
    try:
        return part.Parameters.Item(handle)
    except Exception:
        pass
    index = _param_index(part)
    if handle not in index:
        raise KeyError(handle)
    return index[handle]


def unit_of(param: Any) -> str:
    try:
        symbol = str(param.Unit.Symbol)
    except Exception:
        return ""
    return symbol


def assign_value(param: Any, value: float, unit: str) -> None:
    if unit in ("mm", "deg"):
        param.ValuateFromString(f"{value}{unit}")
    else:
        param.Value = value


def _is_numeric(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def driver_handles(snapshot_model: dict[str, Any]) -> list[str]:
    return [
        p["handle"]
        for p in snapshot_model["parameters"]
        if p.get("kind") == "driver" and _is_numeric(p.get("value"))
    ]


def _diff_parameters(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    after_map = {p["handle"]: p for p in after}
    changes = []
    for p in before:
        other = after_map.get(p["handle"])
        if other is None or not _is_numeric(p.get("value")) or not _is_numeric(other.get("value")):
            continue
        if not approx_equal(float(p["value"]), float(other["value"]), rel=1e-9, abs_tol=1e-6):
            changes.append({"handle": p["handle"], "before": p["value"], "after": other["value"]})
    return changes


def probe_dependencies(app: Any, part: Any, baseline: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Measure, by controlled perturbation, which parameters actually drive what.

    Runs only on the server's own working copy, restores every perturbed value,
    then verifies the restored model matches the baseline. A parameter whose
    write does not stick is formula-driven; a parameter that changes nothing is
    reported as such instead of being assumed meaningful.
    """
    impacts: dict[str, Any] = {}
    limitations: list[str] = []
    base_params = baseline["parameters"]
    base_geom = baseline["geometry"]
    base_holes = baseline["holes"]

    for handle in driver_handles(baseline):
        param = get_param(part, handle)
        try:
            original = float(param.Value)
        except Exception as exc:
            limitations.append(f"driver {handle}: value unreadable ({exc})")
            continue
        unit = unit_of(param)
        delta = 1.0 if unit == "mm" else (1.0 if unit == "deg" else max(abs(original) * 0.01, 0.01))
        target = original + delta
        record: dict[str, Any] = {
            "handle": handle,
            "original": original,
            "probeValue": target,
            "probeDelta": delta,
            "unit": unit,
        }
        try:
            assign_value(param, target, unit)
            part.Update()
            readback = float(param.Value)
            record["writeStuck"] = approx_equal(readback, target, rel=1e-9, abs_tol=1e-6)
            doc = _doc_of(part, app)
            after_params = read_parameters(part)
            volume = read_volume_area(doc, part)
            bbox = read_bbox(app, doc, part)
            holes = read_holes(app, part)[0]
            record["affectedParameters"] = _diff_parameters(base_params, after_params)
            record["volumeDelta"] = (
                round(volume.get("volumeMm3", 0.0) - base_geom.get("volumeMm3", 0.0), 6)
                if "volumeMm3" in volume and "volumeMm3" in base_geom
                else None
            )
            bbox_delta = _bbox_delta(base_geom.get("bbox", {}), bbox)
            record["bboxDelta"] = bbox_delta
            record["bboxAfter"] = bbox
            record["bboxDeltaX"] = _axis_delta(base_geom.get("bbox", {}), bbox, "x")
            record["bboxDeltaY"] = _axis_delta(base_geom.get("bbox", {}), bbox, "y")
            record["bboxDeltaZ"] = _axis_delta(base_geom.get("bbox", {}), bbox, "z")
            record["bboxEdgeDeltas"] = _bbox_edge_deltas(base_geom.get("bbox", {}), bbox)
            record["holeCentersMoved"] = _hole_centers_moved(base_holes, holes)
            record["holeWorldCentersMoved"] = _hole_world_centers_moved(base_holes, holes)
            record["holeCenterDeltas"] = _hole_center_deltas(base_holes, holes)
            record["holeWorldCenterDeltas"] = _hole_world_center_deltas(base_holes, holes)
            record["holeRadiiChanged"] = _hole_radii_changed(base_holes, holes)
            record["holeRadiusDelta"] = _hole_radius_delta(base_holes, holes)
            record["holeRadiusDeltas"] = _hole_radius_deltas(base_holes, holes)
            record["holeCountAfter"] = len(holes)
            record["geometryChanged"] = bool(
                (record["volumeDelta"] is not None and abs(record["volumeDelta"]) > 1e-6)
                or (bbox_delta is not None and bbox_delta > 1e-6)
                or record["holeCentersMoved"]
                or record["holeRadiiChanged"]
            )
            base_signature = _edge_signature(base_holes, base_geom.get("bbox", {}))
            after_signature = _edge_signature(holes, bbox)
            record["edgeMargins"] = {"before": base_signature, "after": after_signature}
            deviation = _signature_deviation(base_signature, after_signature)
            record["edgeDistancesPreserved"] = deviation is not None and deviation <= 1e-6
            if not record["edgeDistancesPreserved"]:
                record["edgeDistances"] = {
                    "before": base_signature,
                    "after": after_signature,
                    "deviation": deviation,
                    "bboxAfter": (bbox or {}).get("dimensions"),
                    "holesAfter": holes,
                }
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                assign_value(param, original, unit)
                part.Update()
            except Exception as exc:
                record["restoreError"] = f"{type(exc).__name__}: {exc}"
        # verify restoration against the baseline
        try:
            after_params = read_parameters(part)
            after_volume = read_volume_area(_doc_of(part, app), part).get("volumeMm3")
            after_bbox = read_bbox(app, _doc_of(part, app), part)
            evidence: list[bool] = []
            base_volume = base_geom.get("volumeMm3")
            if after_volume is not None and base_volume is not None:
                evidence.append(abs(after_volume - base_volume) <= 1e-6)
            bbox_delta = _bbox_delta(base_geom.get("bbox", {}), after_bbox)
            if bbox_delta is not None:
                evidence.append(bbox_delta <= 1e-6)
            restored = not _diff_parameters(base_params, after_params) and bool(evidence) and all(evidence)
        except Exception:
            restored = False
        record["restored"] = restored
        if not restored:
            limitations.append(f"driver {handle}: perturbation could not be proven restored")
        impacts[handle] = record

    # Circle roles are only known after every driver was probed. The global
    # invariant is conservative: if the XY boundary moves, or a measured hole
    # centre moves, *every* measured hole's nearest-edge margin is compared.
    # Fixed holes + a moving boundary therefore correctly break the invariant
    # (Fixture B), while a driver that only changes a radius or thickness does
    # not. Scoped preserves (one hole group) are evaluated by the compiler from
    # the same per-hole margins.
    roles = _decorate_holes(base_holes, impacts)
    confirmed = [index for index, hole in enumerate(roles) if hole.get("role") == "hole"]
    for record in impacts.values():
        margins = record.get("edgeMargins")
        if not isinstance(margins, dict):
            continue
        if not confirmed:
            record["edgeDistancesPreserved"] = None
            continue
        edges = record.get("bboxEdgeDeltas") or {}
        boundary_moved = any(
            isinstance(edges.get(axis), dict)
            and (
                abs(float(edges[axis].get("min") or 0.0)) > 1e-6
                or abs(float(edges[axis].get("max") or 0.0)) > 1e-6
            )
            for axis in ("x", "y")
        )
        center_deltas = record.get("holeCenterDeltas")
        centers_moved = any(
            isinstance(center_deltas, list)
            and index < len(center_deltas)
            and (
                abs(float(center_deltas[index].get("x") or 0.0)) > 1e-6
                or abs(float(center_deltas[index].get("y") or 0.0)) > 1e-6
            )
            for index in confirmed
        )
        if not boundary_moved and not centers_moved:
            record["edgeDistancesPreserved"] = True
            record["edgeDistanceEvidence"] = {
                "holesConsidered": 0,
                "deviation": 0.0,
                "reason": "no measured hole centre or XY boundary moved under this driver",
            }
            continue
        before = margins.get("before") or []
        after = margins.get("after") or []
        if len(before) != len(after) or len(before) != len(base_holes):
            record["edgeDistancesPreserved"] = None
            continue
        deltas = [
            [round(float(after[i][0]) - float(before[i][0]), 6), round(float(after[i][1]) - float(before[i][1]), 6)]
            for i in range(len(before))
        ]
        deviation = max(
            max(abs(deltas[index][0]), abs(deltas[index][1])) for index in confirmed
        )
        record["edgeMarginDeltas"] = deltas
        record["edgeDistancesPreserved"] = deviation <= 1e-6
        record["edgeDistanceEvidence"] = {
            "holesConsidered": len(confirmed),
            "deviation": deviation,
            "boundaryMoved": boundary_moved,
            "centersMoved": centers_moved,
            "filter": "all measured holes when the XY boundary or a measured centre moves",
        }
    return impacts, limitations


def _doc_of(part: Any, app: Any):
    try:
        return part.Parent
    except Exception:
        return app.ActiveDocument


def _hole_centers_moved(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    """True only when a hole centre moved.

    Radius changes are reported separately: conflating them made a diameter
    change look like a centre move, which made "keep hole centres" look
    impossible when it was actually safe.
    """
    if len(before) != len(after):
        return True
    for a, b in zip(before, after):
        if not approx_equal(a["center"]["x"], b["center"]["x"], abs_tol=1e-6) or not approx_equal(
            a["center"]["y"], b["center"]["y"], abs_tol=1e-6
        ):
            return True
    return False


def _hole_radii_changed(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool:
    if len(before) != len(after):
        return True
    return any(not approx_equal(a["radius"], b["radius"], abs_tol=1e-6) for a, b in zip(before, after))


def _hole_radius_delta(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> float | None:
    """Radius change per probe unit, when every hole changed by the same amount."""
    deltas = _hole_radius_deltas(before, after)
    if deltas is None or not deltas:
        return None
    if all(abs(value - deltas[0]) <= 1e-6 for value in deltas):
        return deltas[0]
    return None


def _hole_radius_deltas(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[float] | None:
    """Per-circle radius change, index-aligned with the sorted hole list.

    The order is stable because ``read_holes`` sorts by (sketch, x, y) and a
    radius change does not move a centre. A length change that moves both the
    centre and the radius still keeps per-circle identity through the sketch and
    ordering; if the counts differ this returns None instead of guessing.
    """
    if len(before) != len(after):
        return None
    return [round(float(b["radius"]) - float(a["radius"]), 6) for a, b in zip(before, after)]


def _hole_world_centers_moved(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> bool | None:
    """Did any circle move in model coordinates? None when a frame is missing."""
    deltas = _hole_world_center_deltas(before, after)
    if deltas is None:
        return None
    return any(any(abs(value) > 1e-6 for value in row.values()) for row in deltas)


def _hole_center_deltas(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, float]] | None:
    if len(before) != len(after):
        return None
    return [
        {
            "x": round(float(b["center"]["x"]) - float(a["center"]["x"]), 6),
            "y": round(float(b["center"]["y"]) - float(a["center"]["y"]), 6),
        }
        for a, b in zip(before, after)
    ]


def _hole_world_center_deltas(
    before: list[dict[str, Any]], after: list[dict[str, Any]]
) -> list[dict[str, float]] | None:
    if len(before) != len(after):
        return None
    rows = []
    for a, b in zip(before, after):
        wa, wb = a.get("centerWorld"), b.get("centerWorld")
        if not isinstance(wa, dict) or not isinstance(wb, dict):
            return None
        rows.append(
            {
                axis: round(float(wb.get(axis, 0.0)) - float(wa.get(axis, 0.0)), 6)
                for axis in ("x", "y", "z")
            }
        )
    return rows


def _edge_signature(holes: list[dict[str, Any]], bbox: dict[str, Any]) -> list[list[float]] | None:
    """Nearest-edge margin per hole (x and y), compared with the solid's bbox.

    The far-side distances are intentionally excluded: a plate that grows moves
    its far edge, which is not an edge-margin violation.
    """
    try:
        x0, x1 = float(bbox["min"]["x"]), float(bbox["max"]["x"])
        y0, y1 = float(bbox["min"]["y"]), float(bbox["max"]["y"])
    except Exception:
        return None
    return [
        [
            round(min(h["center"]["x"] - x0, x1 - h["center"]["x"]), 6),
            round(min(h["center"]["y"] - y0, y1 - h["center"]["y"]), 6),
        ]
        for h in holes
    ]


def _signature_deviation(before: list[list[float]] | None, after: list[list[float]] | None) -> float | None:
    if before is None or after is None or len(before) != len(after):
        return None
    worst = 0.0
    for row_a, row_b in zip(before, after):
        for a, b in zip(row_a, row_b):
            worst = max(worst, abs(a - b))
    return round(worst, 6)


def _bbox_delta(before: dict[str, Any], after: dict[str, Any]) -> float | None:
    try:
        return round(
            max(
                abs(float(after["dimensions"][axis]) - float(before["dimensions"][axis]))
                for axis in ("x", "y", "z")
            ),
            6,
        )
    except Exception:
        return None


def _axis_delta(before: dict[str, Any], after: dict[str, Any], axis: str) -> float | None:
    try:
        return round(abs(float(after["dimensions"][axis]) - float(before["dimensions"][axis])), 6)
    except Exception:
        return None


def _bbox_edge_deltas(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    """Signed min/max movement per axis (after - before).

    A symmetric length driver moves min by -0.5 and max by +0.5 per unit; an
    anchored driver moves only one side. Intent compilation needs this to tell
    "each side" from "one side" without guessing from parameter names.
    """
    out: dict[str, Any] = {}
    for axis in ("x", "y", "z"):
        try:
            out[axis] = {
                "min": round(float(after["min"][axis]) - float(before["min"][axis]), 6),
                "max": round(float(after["max"][axis]) - float(before["max"][axis]), 6),
            }
        except Exception:
            out[axis] = None
    return out


def build_snapshot(
    app: Any,
    doc: Any,
    part: Any,
    session: dict[str, Any],
    *,
    with_impacts: bool,
) -> dict[str, Any]:
    model = read_model(app, doc, part)
    limitations = list(model["limitations"])
    drivers = driver_handles(model)
    impacts: dict[str, Any] = session.get("impacts", {})
    if with_impacts:
        impacts, impact_limitations = probe_dependencies(app, part, model)
        limitations.extend(impact_limitations)
        # re-read after probes so the snapshot is the restored state
        model = read_model(app, doc, part)

    model_holes = _decorate_holes(model.get("holes") or [], impacts)
    model = {**model, "holes": model_holes}
    if model.get("structure"):
        model["structure"] = _attach_hole_roles(model["structure"], model_holes)
    capabilities = _capabilities(model, impacts)
    graph = build_relations_graph(model, impacts)
    snapshot = {
        "schema": "catia-model-snapshot/1",
        "editSessionId": session["editSessionId"],
        "modelRevision": session["modelRevision"],
        "source": session["source"],
        "workingCopy": session["workingCopy"],
        "document": {
            "name": str(doc.Name),
            "fullName": str(doc.FullName),
            "saved": bool(doc.Saved),
        },
        "part": {
            "name": model["part"],
            "mainBody": model["features"]["mainBody"],
            "featureCount": len(model["features"]["shapes"]),
            "bodyNames": model["features"]["bodies"],
        },
        "structure": model.get("structure")
        or {"features": [], "sketches": [], "bodies": [], "limitations": ["structure readback unavailable"]},
        "parameters": [
            _decorate_parameter(p, impacts) for p in model["parameters"]
        ],
        "relations": model["relations"],
        "relationGraph": graph,
        "geometry": model["geometry"],
        "holes": model_holes,
        "preserves": _preserve_capabilities(model, impacts),
        "drivers": drivers,
        "impacts": impacts,
        "capabilities": capabilities,
        "limitations": limitations,
        "capturedUtc": now(),
    }
    return snapshot


def _decorate_holes(holes: list[dict[str, Any]], impacts: dict[str, Any]) -> list[dict[str, Any]]:
    """Add measured role evidence to each circular profile.

    A circle is not a hole (guide 3.1). The measured discriminator is the
    volume sign when a writable driver grows the radius: removing more material
    is a hole, adding material is an additive circular feature. A radius change
    coupled with a bounding-box change is not clean evidence, and is reported as
    insufficient rather than guessed.
    """
    out = [dict(hole) for hole in holes]
    for index, hole in enumerate(out):
        drivers: list[str] = []
        evidence: list[dict[str, Any]] = []
        roles: set[str] = set()
        for handle, impact in (impacts or {}).items():
            deltas = impact.get("holeRadiusDeltas") if isinstance(impact, dict) else None
            if not isinstance(deltas, list) or len(deltas) != len(out):
                continue
            delta = deltas[index]
            if delta is None or abs(float(delta)) <= 1e-9:
                continue
            drivers.append(handle)
            volume = impact.get("volumeDelta")
            boundary_changed = any(
                abs(float(impact.get(f"bboxDelta{axis.upper()}") or 0.0)) > 1e-6 for axis in ("x", "y", "z")
            )
            if boundary_changed or volume is None:
                roles.add("insufficient_evidence")
                evidence.append(
                    {
                        "driver": handle,
                        "radiusDelta": delta,
                        "volumeDelta": volume,
                        "boundaryChanged": boundary_changed,
                        "result": "insufficient_evidence",
                        "reason": "the radius change is coupled with a boundary change or volume is unreadable",
                    }
                )
                continue
            volume_sign = 1 if volume > 0 else (-1 if volume < 0 else 0)
            removed_per_radius = -volume_sign if delta > 0 else volume_sign
            role = "hole" if removed_per_radius > 0 else "additive_feature"
            roles.add(role)
            evidence.append(
                {
                    "driver": handle,
                    "radiusDelta": delta,
                    "volumeDelta": volume,
                    "result": role,
                    "method": "perturbation_volume_sign",
                    "reason": "growing the radius removes material" if role == "hole" else "growing the radius adds material",
                }
            )
        if len(roles) == 1:
            role = next(iter(roles))
        elif roles:
            role = "insufficient_evidence"
        else:
            role = "observed_circle"
            evidence.append({"result": role, "reason": "no writable driver changed this circle's radius"})
        hole["role"] = role
        hole["radiusDrivers"] = sorted(set(drivers))
        hole["roleEvidence"] = evidence
    return out


def _attach_hole_roles(structure: dict[str, Any], holes: list[dict[str, Any]]) -> dict[str, Any]:
    by_sketch: dict[str, list[dict[str, Any]]] = {}
    for hole in holes:
        by_sketch.setdefault(str(hole.get("sketch")), []).append(hole)
    sketches = []
    for sketch in structure.get("sketches") or []:
        row = dict(sketch)
        row["circles"] = [
            {
                "radius": hole.get("radius"),
                "center": hole.get("center"),
                "centerWorld": hole.get("centerWorld"),
                "role": hole.get("role"),
                "roleEvidence": hole.get("roleEvidence"),
            }
            for hole in by_sketch.get(str(sketch.get("name")), [])
        ]
        sketches.append(row)
    return {**structure, "sketches": sketches}


def _scoped_edge_groups(model: dict[str, Any], impacts: dict[str, Any]) -> list[dict[str, Any]]:
    """Per-sketch edge-margin groups and the drivers that keep them.

    The global invariant is conservative (any measured hole whose margin moves
    breaks it). A user who names one hole group ("安装孔") can preserve that
    group only; this is the evidence the compiler uses for a scoped preserve.
    """
    holes = model.get("holes") or []
    confirmed = [
        index for index, hole in enumerate(holes) if hole.get("role") == "hole"
    ]
    sketches = sorted({str(holes[index].get("sketch")) for index in confirmed})
    groups = []
    for sketch in sketches:
        indices = [index for index in confirmed if str(holes[index].get("sketch")) == sketch]
        drivers = []
        for handle, impact in (impacts or {}).items():
            margins = impact.get("edgeMargins") if isinstance(impact, dict) else None
            if not isinstance(margins, dict):
                continue
            before = margins.get("before") or []
            after = margins.get("after") or []
            if len(before) != len(after) or len(before) != len(holes):
                continue
            try:
                deviation = max(
                    max(
                        abs(float(after[index][0]) - float(before[index][0])),
                        abs(float(after[index][1]) - float(before[index][1])),
                    )
                    for index in indices
                )
            except Exception:
                continue
            if deviation <= 1e-6:
                drivers.append(handle)
        groups.append(
            {
                "sketch": sketch,
                "holes": [index + 1 for index in indices],
                "preservedBy": drivers,
                "supported": bool(drivers),
            }
        )
    return groups


def _preserve_capabilities(model: dict[str, Any], impacts: dict[str, Any]) -> dict[str, Any]:
    """What can be preserved *with evidence*, for both the compiler and the model."""
    holes = model.get("holes") or []
    world_complete = bool(holes) and all(isinstance(h.get("centerWorld"), dict) for h in holes)
    edge_capability = ((_capabilities(model, impacts).get("invariants") or {}).get("holes_edge_distance"))
    return {
        "hole_centers": {
            "supported": bool(holes),
            "worldSupported": world_complete,
            "reason": (
                "circle centres are measured in sketch coordinates"
                + (" and model coordinates" if world_complete else "; world coordinates are incomplete")
            ),
        },
        "hole_edge_distance": {
            **(edge_capability or {}),
            "scopedGroups": _scoped_edge_groups(model, impacts),
        },
        "center_position": {
            "supported": any(
                isinstance(impact.get("bboxEdgeDeltas"), dict)
                and any(
                    isinstance(edges, dict)
                    and abs(float(edges.get("min") or 0.0) + float(edges.get("max") or 0.0)) <= 1e-3
                    and abs(float(edges.get("max") or 0.0) - float(edges.get("min") or 0.0)) > 1e-3
                    for edges in impact["bboxEdgeDeltas"].values()
                )
                for impact in (impacts or {}).values()
            ),
            "reason": "requires a writable driver with symmetric measured bbox growth (min + max = 0)",
        },
        "measuredRoles": {
            "hole": sum(1 for h in holes if h.get("role") == "hole"),
            "additiveFeature": sum(1 for h in holes if h.get("role") == "additive_feature"),
            "observedCircle": sum(1 for h in holes if h.get("role") == "observed_circle"),
            "insufficientEvidence": sum(1 for h in holes if h.get("role") == "insufficient_evidence"),
        },
    }


def _decorate_parameter(param: dict[str, Any], impacts: dict[str, Any]) -> dict[str, Any]:
    out = dict(param)
    impact = impacts.get(param["handle"])
    if param.get("kind") == "driver" and _is_numeric(param.get("value")):
        out["writable"] = bool(impact.get("writeStuck")) if impact else None
        out["dependencies"] = {
            "affectedParameters": [c["handle"] for c in impact.get("affectedParameters", [])] if impact else [],
            "geometryChanged": impact.get("geometryChanged") if impact else None,
            "holeCentersMoved": impact.get("holeCentersMoved") if impact else None,
            "holeWorldCentersMoved": impact.get("holeWorldCentersMoved") if impact else None,
            "holeRadiiChanged": impact.get("holeRadiiChanged") if impact else None,
            "holeRadiusDelta": impact.get("holeRadiusDelta") if impact else None,
            "holeRadiusDeltas": impact.get("holeRadiusDeltas") if impact else None,
            "bboxEdgeDeltas": impact.get("bboxEdgeDeltas") if impact else None,
        }
        if impact and impact.get("error"):
            out["writable"] = False
            out["notes"] = [f"probe failed: {impact['error']}"]
            out["dependencies"]["probeFailed"] = True
    else:
        out.setdefault("writable", False)
    if param.get("unit") in ("", "unknown"):
        out.setdefault("notes", []).append("unit not reported by CATIA")
    return out


def _holes_for_margin(holes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Only measured holes count for an edge-margin assertion.

    Snapshots from older builds have no ``role``; they keep the old behaviour
    (every circle). Once roles exist, an additive circular feature must never be
    treated as a hole, and a model with only additive circles has no hole margin.
    """
    if any(hole.get("role") for hole in holes):
        return [hole for hole in holes if hole.get("role") == "hole"]
    return list(holes)


def _capabilities(model: dict[str, Any], impacts: dict[str, Any]) -> dict[str, Any]:
    written = [h for h, imp in impacts.items() if imp.get("writeStuck")]
    geometry_drivers = [h for h in written if impacts[h].get("geometryChanged")]
    non_driving = [h for h in written if not impacts[h].get("geometryChanged")]
    hole_followers = [h for h, imp in impacts.items() if imp.get("holeCentersMoved")]
    holes = _holes_for_margin(model.get("holes") or [])
    # A hole margin can only be maintained by a parameter that moves the XY
    # boundary; a thickness driver moves only z and says nothing about margins.
    xy_boundary_drivers = [
        h
        for h in geometry_drivers
        if (abs(impacts[h].get("bboxDeltaX") or 0.0) > 1e-6 or abs(impacts[h].get("bboxDeltaY") or 0.0) > 1e-6)
    ]
    bad = [h for h in xy_boundary_drivers if not impacts[h].get("edgeDistancesPreserved")]
    if not holes:
        supported, reason = False, "model has no confirmed hole geometry; edge distance does not apply"
    elif not xy_boundary_drivers:
        supported, reason = False, "no writable parameter moves the XY boundary; nothing to preserve"
    elif bad:
        supported, reason = False, "boundary moves without the holes for: " + ", ".join(bad)
    else:
        supported = True
        reason = "hole edge margins stay constant when these drivers change: " + ", ".join(xy_boundary_drivers)
    return {
        "writableDrivers": written,
        "geometryDrivers": geometry_drivers,
        "nonsemanticDrivers": non_driving,
        "xyBoundaryDrivers": xy_boundary_drivers,
        "holeFollowers": hole_followers,
        "invariants": {"holes_edge_distance": {"supported": supported, "reason": reason}},
        "unsupported": [] if holes else ["holes: no confirmed hole geometry found"],
    }
