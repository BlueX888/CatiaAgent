#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Independent acceptance verification for one delivered CATIA V5 .CATPart.

This is the SECOND evidence chain.  It deliberately shares NO code with the
``catia_mcp`` package: it does not import it, does not read any JSON report the
editing pipeline produced, and does not reuse its helpers or constants.  Every
number in the verdict is measured directly from CATIA through late-bound COM.

Binding rules (踩坑报告 items 45 / 46 / 49):
  * late binding only - ``GetActiveObject`` / ``Dispatch``, never
    ``win32com.client.gencache`` / ``EnsureDispatch`` (that poisons the global
    ``%TEMP%\\gen_py`` cache for every other client on the machine);
  * a document that is already open is never opened again and never closed;
  * only documents this script opened itself (throwaway probe copies) are
    closed, and only its own ``<name>-probe*.CATPart`` copies are deleted;
  * out-parameter arrays are read through ``SystemService.Evaluate`` VBScript
    because pywin32 cannot marshal them.

Usage
-----
    <venv python> independent_verify.py <spec.json> [--json-out <path>]

Prints one compact JSON object ``{"passed": bool, "checks": [...], "errors": [...]}``
with ``checks[i] = {"id", "status", "evidence"}`` and
``status in {"pass", "fail", "skipped", "unavailable"}``.  Exit code is 0 only
when no check is "fail" or "unavailable" (2 = spec/output error).

Spec (JSON object)
------------------
    catpart            delivered .CATPart (opened, never modified or saved)
    source_catpart     the file the edit started from (hashed only)
    source_sha256      expected hash of source_catpart
    expected.drivers   {"<full parameter name or last path segment>": value}
    expected.volume_mm3
    expected.bbox      {"min": [x, y, z], "max": [x, y, z]}
    expected.holes     [{"x", "y", "r"}, ...]   (sketch-local 2D is compared to
                        global x/y; the sketch name is reported per circle)
    expected.feature_count
    tolerances         {"length_mm", "volume_abs_mm3", "volume_rel"}
    probe (optional)   {"half_mm": 2.5, "z_margin_mm": 1.0, "tolerance_mm3": ..}

COM calls relied on, per check
------------------------------
  source_sha256       hashlib only (no COM)
  reopen              Documents.Count, Documents.Item(i).FullName, Documents.Open,
                      DisplayFileAlerts, Document.Part, Document.Name
  volume              GetWorkbench("SPAWorkbench").GetMeasurable(ref).Volume (*1e9)
  bbox                Selection.Add / Search("Type=顶点,sel") / Item(i).Reference,
                      GetMeasurable(ref), SystemService.Evaluate(GetPoint out-array)
  holes               Body.Sketches, sketch.GeometricElements, GeometricType==5,
                      Circle2D.GetCenter out-array + .Radius, via Evaluate
  drivers             Part.Parameters.Item(i).Name/.Value/.Unit.Symbol
  feature_count       Part.MainBody.Shapes.Count
  physical_hole_probe copy on disk -> Documents.Open -> Part.Bodies.Add,
                      Part.InWorkObject, Body.Sketches.Add, Sketch.OpenEdition,
                      Factory2D.CreateLine x4, Sketch.CloseEdition, Part.UpdateObject,
                      ShapeFactory.AddNewPad (+ IsSymmetric),
                      ShapeFactory.AddNewRemove | AddNewIntersect,
                      GetMeasurable(body).Volume, vertex sweep of the probe body
  original_untouched  Documents.Item(i).FullName (look only, no touch) + hashlib
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shutil
import struct
import sys
import time
import traceback
from collections import defaultdict
from typing import Any

try:  # pywin32 is the only third-party dependency.
    import pythoncom
    import win32com.client

    HAS_COM = True
    _COM_IMPORT_ERROR = ""
except Exception as _com_import_exception:  # pragma: no cover - Windows only
    pythoncom = None  # type: ignore[assignment]
    win32com = None  # type: ignore[assignment]
    HAS_COM = False
    _COM_IMPORT_ERROR = f"{type(_com_import_exception).__name__}: {_com_import_exception}"

PROGID = "CATIA.Application"

# Selection.Search only accepts the current UI language's type names.  The
# CATIA UI on this machine is Chinese (V5 R32): "顶点" works, "Topology.Vertex"
# does not.  The English spelling is kept as a fallback for other installs.
VERTEX_SEARCH_CANDIDATES = ("Type=顶点,sel", "Type=Vertex,sel")

FAILING_STATUSES = ("fail", "unavailable")
MAX_PROBE_HOLES = 16

# VBScript function names must not collide with any Dim name inside the body.
_VBS_GET_POINT = r"""
Public Function iv_getpoint(m)
    Dim a(2)
    m.GetPoint a
    iv_getpoint = a(0) & "," & a(1) & "," & a(2)
End Function
"""

_VBS_SKETCH_CIRCLES = r"""
Public Function iv_circles(sk)
    Dim g, i, x(1), s
    s = ""
    For i = 1 To sk.GeometricElements.Count
        Set g = sk.GeometricElements.Item(i)
        If g.GeometricType = 5 Then
            g.GetCenter x
            s = s & x(0) & "," & x(1) & "," & g.Radius & ";"
        End If
    Next
    iv_circles = s
End Function
"""

# GetAbsoluteAxisData returns origin + H + V through an out-array that pywin32
# cannot marshal (踩坑 45), so the verifier reads it through VBScript. This is a
# separate implementation from the MCP server's: the two evidence chains must
# not share code.
_VBS_SKETCH_AXIS = r"""
Public Function iv_axis(sk)
    Dim a(8), i, s
    sk.GetAbsoluteAxisData a
    s = ""
    For i = 0 To 8
        s = s & a(i) & ","
    Next
    iv_axis = s
End Function
"""


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _err(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _tb() -> str:
    lines = [line.strip() for line in traceback.format_exc(limit=4).strip().splitlines() if line.strip()]
    return " | ".join(lines[-4:])


def _num(value: Any, digits: int = 6) -> Any:
    """Round for the verdict while keeping non-finite values out of the JSON."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return value
    if not math.isfinite(number):
        return str(number)
    return round(number, digits)


def _sha256(path: str, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_name(obj: Any) -> str:
    try:
        return str(obj.Name)
    except Exception:
        return "<unnamed>"


def _load_spec(path: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        spec = json.load(handle)
    if not isinstance(spec, dict):
        raise ValueError("spec.json must contain a JSON object")
    return spec


def _tolerances(spec: dict[str, Any]) -> dict[str, float]:
    raw = spec.get("tolerances") if isinstance(spec.get("tolerances"), dict) else {}
    return {
        "length_mm": float(raw.get("length_mm", 0.01)),
        "volume_abs_mm3": float(raw.get("volume_abs_mm3", 0.01)),
        "volume_rel": float(raw.get("volume_rel", 1e-6)),
    }


def _circle_square_overlap(half: float, radius: float, slices: int = 2000) -> float:
    """Area shared by a circle centred in an axis-aligned square of half-size `half`."""
    if radius <= 0.0 or half <= 0.0:
        return 0.0
    full = 4.0 * half * half
    if radius >= half * math.sqrt(2.0):
        return full  # the square sits inside the circle
    if radius <= half:
        return math.pi * radius * radius  # the circle sits inside the square
    step = half / slices
    area = 0.0
    for index in range(slices):
        x = (index + 0.5) * step
        if x >= radius:
            continue
        area += min(half, math.sqrt(radius * radius - x * x)) * step
    return 4.0 * area


# --------------------------------------------------------------------------- #
# CATIA session
# --------------------------------------------------------------------------- #
class CatiaSession:
    """Late-bound CATIA handle plus the document etiquette this script must obey."""

    def __init__(self) -> None:
        self.app: Any = None
        self.how = "none"
        self.caption = ""
        self.alerts_original: Any = True
        self._to_close: list[Any] = []

    # -- connection ---------------------------------------------------------
    def connect(self) -> None:
        if not HAS_COM:
            raise RuntimeError(f"pywin32 is unavailable: {_COM_IMPORT_ERROR}")
        try:
            pythoncom.CoInitialize()
        except Exception:
            pass  # already initialized on this thread
        try:
            self.app = win32com.client.GetActiveObject(PROGID)
            self.how = "GetActiveObject"
        except Exception:
            # Late binding only; gencache/EnsureDispatch is forbidden here.
            self.app = win32com.client.Dispatch(PROGID)
            self.how = "Dispatch"
            try:
                self.app.Visible = True
            except Exception:
                pass
        try:
            self.caption = str(self.app.Caption)
        except Exception:
            self.caption = ""
        try:
            self.alerts_original = self.app.DisplayFileAlerts
        except Exception:
            self.alerts_original = True

    def ensure_connected(self) -> Any:
        if self.app is None:
            self.connect()
        return self.app

    # -- documents ----------------------------------------------------------
    @staticmethod
    def norm(path: str) -> str:
        return os.path.normcase(os.path.abspath(str(path)))

    def document_count(self) -> int:
        return int(self.app.Documents.Count)

    def documents(self) -> list[Any]:
        collection = self.app.Documents
        return [collection.Item(i) for i in range(1, int(collection.Count) + 1)]

    def open_paths(self) -> list[str]:
        paths: list[str] = []
        for doc in self.documents():
            try:
                paths.append(str(doc.FullName))
            except Exception:
                paths.append("<unreadable FullName>")
        return paths

    def find_open(self, path: str) -> Any | None:
        target = self.norm(path)
        for doc in self.documents():
            try:
                if self.norm(str(doc.FullName)) == target:
                    return doc
            except Exception:
                continue
        return None

    def _restore_alerts(self) -> None:
        try:
            self.app.DisplayFileAlerts = self.alerts_original
        except Exception:
            pass

    def open_document(self, path: str, *, close_at_end: bool = False) -> tuple[Any, dict[str, Any]]:
        """Open `path` unless it is already open.  Never re-open an open file."""
        absolute = os.path.abspath(str(path))
        existing = self.find_open(absolute)
        if existing is not None:
            return existing, {
                "already_open": True,
                "documents_before": self.document_count(),
                "documents_after": self.document_count(),
            }
        before = self.document_count()
        doc = None
        try:
            self.app.DisplayFileAlerts = False
            doc = self.app.Documents.Open(absolute)
        finally:
            self._restore_alerts()
        after = self.document_count()
        if close_at_end:
            self._to_close.append(doc)
        return doc, {"already_open": False, "documents_before": before, "documents_after": after}

    def close_document(self, doc: Any) -> str:
        """Close a document this script opened itself; never someone else's."""
        status = "closed"
        try:
            self.app.DisplayFileAlerts = False
            try:
                doc.Close()
            finally:
                self._restore_alerts()
        except Exception as exc:
            status = f"close failed: {_err(exc)}"
        for index, tracked in enumerate(self._to_close):
            if tracked is doc:
                del self._to_close[index]
                break
        return status

    def shutdown(self) -> list[str]:
        """Safety net: close leftover probe copies.  The delivered file stays open."""
        notes: list[str] = []
        for doc in list(self._to_close):
            note = self.close_document(doc)
            if note != "closed":
                notes.append(f"cleanup: {note}")
        return notes


# --------------------------------------------------------------------------- #
# measurement
# --------------------------------------------------------------------------- #
def spa_workbench(session: CatiaSession, doc: Any) -> Any:
    errors = []
    for getter in (
        lambda: doc.GetWorkbench("SPAWorkbench"),
        lambda: session.app.GetWorkbench("SPAWorkbench"),
    ):
        try:
            return getter()
        except Exception as exc:
            errors.append(_err(exc))
    raise RuntimeError("SPAWorkbench is unavailable: " + "; ".join(errors))


def measure_volume_mm3(session: CatiaSession, doc: Any, part: Any, obj: Any) -> dict[str, Any]:
    workbench = spa_workbench(session, doc)
    reference = part.CreateReferenceFromObject(obj)
    volume_m3 = float(workbench.GetMeasurable(reference).Volume)
    return {"volume_m3": volume_m3, "volume_mm3": volume_m3 * 1e9}


def sketch_frame(session: CatiaSession, sketch: Any) -> dict[str, Any]:
    raw = str(session.app.SystemService.Evaluate(_VBS_SKETCH_AXIS, 0, "iv_axis", [sketch]))
    values = [float(token) for token in raw.split(",") if token.strip()]
    if len(values) != 9:
        raise RuntimeError(f"GetAbsoluteAxisData returned {len(values)} values, expected 9")
    origin = values[0:3]
    horizontal = values[3:6]
    vertical = values[6:9]
    plane = "oblique"
    canonical = {
        "XY": ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]),
        "XZ": ([1.0, 0.0, 0.0], [0.0, 0.0, 1.0]),
        "YZ": ([0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
    }
    for name, (h, v) in canonical.items():
        if all(abs(a - b) <= 1e-6 for a, b in zip(horizontal, h)) and all(
            abs(a - b) <= 1e-6 for a, b in zip(vertical, v)
        ):
            plane = name
            break
    return {
        "origin": origin,
        "horizontal": horizontal,
        "vertical": vertical,
        "plane": plane,
    }


def to_world(frame: dict[str, Any], x: float, y: float) -> list[float]:
    origin = frame["origin"]
    h = frame["horizontal"]
    v = frame["vertical"]
    return [
        origin[axis] + x * h[axis] + y * v[axis]
        for axis in range(3)
    ]


def _stl_points(path: str) -> list[tuple[float, float, float]]:
    data = open(path, "rb").read()
    if data[:5] == b"solid" and b"facet" in data[:4096]:
        points = []
        for line in data.decode("ascii", "replace").splitlines():
            line = line.strip()
            if line.startswith("vertex "):
                try:
                    values = [float(token) for token in line.split()[1:4]]
                except Exception:
                    continue
                if len(values) == 3:
                    points.append((values[0], values[1], values[2]))
        return points
    if len(data) < 84:
        return []
    count = struct.unpack("<I", data[80:84])[0]
    points = []
    offset = 84
    for _ in range(count):
        if offset + 50 > len(data):
            break
        values = struct.unpack("<12fH", data[offset : offset + 50])
        offset += 50
        points.append((values[3], values[4], values[5]))
        points.append((values[6], values[7], values[8]))
        points.append((values[9], values[10], values[11]))
    return points


def stl_bbox(session: CatiaSession, doc: Any) -> dict[str, Any]:
    """Tessellated BRep bounding box: includes a cylinder top a vertex sweep misses.

    Exports to the delivered file's directory (CATIA must have write authority
    there; a redirected TEMP made ExportData block) and deletes the temp file in
    a finally block. The delivered document is not saved or modified.
    """
    full_name = str(getattr(doc, "FullName", "") or "")
    base = os.path.dirname(os.path.abspath(full_name)) if os.path.isabs(full_name) else os.getcwd()
    path = os.path.join(base, f".iv-bbox-{os.getpid()}-{time.time_ns()}.stl")
    try:
        doc.ExportData(path, "stl")
        points = _stl_points(path)
        if not points:
            raise RuntimeError("STL export produced no vertices")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    mins = [min(point[axis] for point in points) for axis in range(3)]
    maxs = [max(point[axis] for point in points) for axis in range(3)]
    return {
        "min": mins,
        "max": maxs,
        "dimensions": [maxs[axis] - mins[axis] for axis in range(3)],
        "vertex_count": len(points),
        "triangle_count": len(points) // 3,
        "method": "STL tessellation (Document.ExportData)",
        "attempts": [],
    }


def vertex_bbox(session: CatiaSession, doc: Any, part: Any, body: Any = None) -> dict[str, Any]:
    """Sweep every vertex of `body` (default MainBody) and return its min/max."""
    body = part.MainBody if body is None else body
    label = _safe_name(body)
    count = int(body.Shapes.Count)
    if count <= 0:
        raise RuntimeError(f"body '{label}' has no solid shape to sweep")
    last_shape = body.Shapes.Item(count)
    selection = doc.Selection
    attempts: list[str] = []
    used = None
    found = 0
    try:
        for candidate in VERTEX_SEARCH_CANDIDATES:
            try:
                selection.Clear()
                selection.Add(last_shape)
                selection.Search(candidate)
                found = int(selection.Count)
            except Exception as exc:
                attempts.append(f"{candidate}: {_err(exc)}")
                found = 0
                continue
            if found:
                used = candidate
                break
            attempts.append(f"{candidate}: 0 vertices")
        if not used:
            raise RuntimeError(
                f"no vertex search candidate worked for body '{label}': " + "; ".join(attempts)
            )
        workbench = spa_workbench(session, doc)
        points: list[tuple[float, float, float]] = []
        for index in range(1, found + 1):
            reference = selection.Item(index).Reference
            measurable = workbench.GetMeasurable(reference)
            raw = str(session.app.SystemService.Evaluate(_VBS_GET_POINT, 0, "iv_getpoint", [measurable]))
            x, y, z = (float(token) for token in raw.split(","))
            points.append((x, y, z))
    finally:
        try:
            selection.Clear()
        except Exception:
            pass
    mins = [min(point[axis] for point in points) for axis in range(3)]
    maxs = [max(point[axis] for point in points) for axis in range(3)]
    return {
        "min": mins,
        "max": maxs,
        "dimensions": [maxs[axis] - mins[axis] for axis in range(3)],
        "vertex_count": found,
        "method": f"vertex sweep ({used})",
        "attempts": attempts,
    }


def sketch_circles(session: CatiaSession, sketch: Any) -> dict[str, Any]:
    raw = str(session.app.SystemService.Evaluate(_VBS_SKETCH_CIRCLES, 0, "iv_circles", [sketch]))
    circles: list[dict[str, float]] = []
    unparsable: list[str] = []
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            x, y, radius = (float(token) for token in chunk.split(","))
        except Exception:
            unparsable.append(chunk[:80])
            continue
        circles.append({"x": x, "y": y, "r": radius})
    return {"circles": circles, "unparsable": unparsable}


def read_all_circles(session: CatiaSession, part: Any) -> dict[str, Any]:
    """Circle centres/radii of every sketch in every body, deterministically sorted."""
    sketches: list[dict[str, Any]] = []
    errors: list[str] = []
    flat: list[dict[str, Any]] = []
    seen: set[str] = set()
    bodies: list[Any] = []
    try:
        bodies.append(part.MainBody)
    except Exception as exc:
        errors.append(f"MainBody: {_err(exc)}")
    try:
        collection = part.Bodies
        for index in range(1, int(collection.Count) + 1):
            bodies.append(collection.Item(index))
    except Exception as exc:
        errors.append(f"Bodies: {_err(exc)}")
    for body in bodies:
        try:
            owner = _safe_name(body)
            sketch_collection = body.Sketches
            total = int(sketch_collection.Count)
        except Exception as exc:
            errors.append(f"body sketches: {_err(exc)}")
            continue
        for index in range(1, total + 1):
            try:
                sketch = sketch_collection.Item(index)
            except Exception as exc:
                errors.append(f"{owner} sketch {index}: {_err(exc)}")
                continue
            label = f"{owner}/{_safe_name(sketch)}"
            if label in seen:
                continue
            seen.add(label)
            try:
                read = sketch_circles(session, sketch)
            except Exception as exc:
                message = _err(exc)
                errors.append(f"{label}: {message}")
                sketches.append({"sketch": label, "circle_count": 0, "error": message})
                continue
            frame = None
            frame_error = None
            try:
                frame = sketch_frame(session, sketch)
            except Exception as exc:  # noqa: BLE001 - report, never invent a frame
                frame_error = _err(exc)
                errors.append(f"{label}: absolute axis readback failed ({frame_error})")
            sketches.append(
                {
                    "sketch": label,
                    "circle_count": len(read["circles"]),
                    "unparsable": read["unparsable"],
                    "frame": frame,
                    "frame_error": frame_error,
                }
            )
            for circle in read["circles"]:
                row = {"x": circle["x"], "y": circle["y"], "r": circle["r"], "sketch": label}
                if frame is not None:
                    world = to_world(frame, circle["x"], circle["y"])
                    row["world"] = {"x": world[0], "y": world[1], "z": world[2]}
                    row["plane"] = frame["plane"]
                flat.append(row)
    flat.sort(key=lambda circle: (round(circle["x"], 6), round(circle["y"], 6), round(circle["r"], 6), circle["sketch"]))
    return {"circles": flat, "sketches": sketches, "errors": errors}


def list_parameters(part: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    collection = part.Parameters
    for index in range(1, int(collection.Count) + 1):
        item = collection.Item(index)
        row: dict[str, Any] = {"name": "", "value": None, "unit": ""}
        try:
            row["name"] = str(item.Name)
        except Exception as exc:
            row["name_error"] = _err(exc)
        try:
            value = item.Value
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                row["value"] = float(value)
            else:
                row["raw_value"] = repr(value)[:80]
        except Exception as exc:
            row["value_error"] = _err(exc)
        try:
            row["unit"] = str(item.Unit.Symbol)
        except Exception:
            pass
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# matching helpers
# --------------------------------------------------------------------------- #
def match_holes(
    expected_holes: list[Any],
    measured_circles: list[dict[str, Any]],
    tolerance_mm: float,
    *,
    space: str = "sketch",
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Nearest-centre matching of expected holes against measured sketch circles.

    ``space="world"`` compares the sketch-local circle transformed by the
    measured absolute axis with the expected model coordinates. A circle whose
    frame could not be read is skipped rather than compared in the wrong frame.
    """
    rows: list[dict[str, Any]] = []
    used: set[int] = set()
    world = space == "world"
    for position, expected_hole in enumerate(expected_holes, start=1):
        row: dict[str, Any] = {"expected_index": position}
        try:
            expected_x = float(expected_hole["x"])
            expected_y = float(expected_hole["y"])
            expected_z = float(expected_hole.get("z", 0.0)) if world else None
        except Exception as exc:
            row.update(
                {
                    "status": "invalid_spec",
                    "error": _err(exc),
                    "within_tolerance": False,
                }
            )
            rows.append(row)
            continue
        expected_radius = expected_hole.get("r")
        best_index = None
        best_distance = None
        for index, circle in enumerate(measured_circles):
            if index in used:
                continue
            if world:
                centre = circle.get("world")
                if not isinstance(centre, dict):
                    continue
                dx = centre["x"] - expected_x
                dy = centre["y"] - expected_y
                dz = centre["z"] - float(expected_z)
                distance = math.sqrt(dx * dx + dy * dy + dz * dz)
            else:
                dx = circle["x"] - expected_x
                dy = circle["y"] - expected_y
                dz = None
                distance = math.hypot(dx, dy)
            if best_distance is None or distance < best_distance:
                best_index, best_distance = index, distance
        if best_index is None:
            row.update(
                {
                    "expected": {"x": expected_x, "y": expected_y, "r": expected_radius},
                    "status": "missing",
                    "within_tolerance": False,
                    "note": "no sketch circle with a readable frame was measured" if world else "no sketch circle was measured at all",
                }
            )
            rows.append(row)
            continue
        circle = measured_circles[best_index]
        radius_delta = (
            abs(circle["r"] - float(expected_radius)) if expected_radius is not None else 0.0
        )
        within = best_distance <= tolerance_mm and radius_delta <= tolerance_mm
        matched = {
            "x": _num(circle["x"]),
            "y": _num(circle["y"]),
            "r": _num(circle["r"]),
            "sketch": circle.get("sketch"),
        }
        if world and isinstance(circle.get("world"), dict):
            matched["world"] = {axis: _num(circle["world"][axis]) for axis in ("x", "y", "z")}
        row.update(
            {
                "expected": {"x": _num(expected_x), "y": _num(expected_y), "r": _num(expected_radius)},
                "matched": matched,
                "dx": _num(dx),
                "dy": _num(dy),
                "distance_mm": _num(best_distance),
                "radius_delta_mm": _num(radius_delta),
                "status": "ok" if within else "mismatch",
                "within_tolerance": within,
            }
        )
        if within:
            used.add(best_index)
        rows.append(row)
    leftovers = [circle for index, circle in enumerate(measured_circles) if index not in used]
    return rows, leftovers


def _span_check(
    box_bbox: dict[str, Any],
    cx: float,
    cy: float,
    half: float,
    z_low: float,
    z_high: float,
    tolerance_mm: float,
) -> dict[str, Any]:
    """Prove the probe box really sits where it was requested."""
    wanted = {"x": (cx - half, cx + half), "y": (cy - half, cy + half), "z": (z_low, z_high)}
    axes: dict[str, Any] = {}
    errors: list[str] = []
    for axis, index in (("x", 0), ("y", 1), ("z", 2)):
        low, high = wanted[axis]
        got_low = float(box_bbox["min"][index])
        got_high = float(box_bbox["max"][index])
        ok = abs(got_low - low) <= tolerance_mm and abs(got_high - high) <= tolerance_mm
        axes[axis] = {"expected": [_num(low), _num(high)], "actual": [_num(got_low), _num(got_high)], "ok": ok}
        if not ok:
            errors.append(f"{axis}: expected [{low:.4f},{high:.4f}] measured [{got_low:.4f},{got_high:.4f}]")
    return {"ok": not errors, "axes": axes, "errors": errors, "tolerance_mm": _num(tolerance_mm)}


# --------------------------------------------------------------------------- #
# physical hole probe (geometric second opinion)
# --------------------------------------------------------------------------- #
def _build_probe_box(
    session: CatiaSession,
    doc: Any,
    part: Any,
    *,
    cx: float,
    cy: float,
    half: float,
    pad_height: float,
    symmetric: bool,
) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    shape_factory = part.ShapeFactory
    plane_reference = part.CreateReferenceFromObject(part.OriginElements.PlaneXY)
    body = part.Bodies.Add()
    info: dict[str, Any] = {"body": _safe_name(body)}
    try:
        part.InWorkObject = body  # the pad must land here, not in MainBody
    except Exception as exc:
        info["in_work_error"] = _err(exc)
    sketch = body.Sketches.Add(plane_reference)
    info["sketch"] = _safe_name(sketch)
    editor = sketch.OpenEdition()
    x_low, x_high = cx - half, cx + half
    y_low, y_high = cy - half, cy + half
    editor.CreateLine(x_low, y_low, x_high, y_low)
    editor.CreateLine(x_high, y_low, x_high, y_high)
    editor.CreateLine(x_high, y_high, x_low, y_high)
    editor.CreateLine(x_low, y_high, x_low, y_low)
    sketch.CloseEdition()
    part.UpdateObject(sketch)
    pad = shape_factory.AddNewPad(sketch, pad_height)
    try:
        pad.IsSymmetric = bool(symmetric)
    except Exception as exc:
        info["is_symmetric_error"] = _err(exc)
    part.UpdateObject(pad)
    info["pad"] = _safe_name(pad)
    shapes = int(body.Shapes.Count)
    info["shapes_in_body"] = shapes
    if shapes < 1:
        raise RuntimeError("the pad did not land in the probe body (Shapes.Count == 0)")
    info["box_volume_mm3"] = measure_volume_mm3(session, doc, part, body)["volume_mm3"]
    box_bbox = vertex_bbox(session, doc, part, body=body)
    info["box_bbox"] = {"min": [_num(v) for v in box_bbox["min"]], "max": [_num(v) for v in box_bbox["max"]]}
    return body, info, box_bbox


def _boolean_with_main(part: Any, body: Any, operand: Any) -> tuple[Any, str, str, list[str]]:
    """Subtract/intersect the delivered solid in the throwaway copy.

    Remove is tried first: box-minus-solid never degenerates to an empty solid
    while the box pokes out of the plate on both sides, whereas an intersection
    that lands entirely inside a hole can be rejected by CATIA.
    """
    shape_factory = part.ShapeFactory
    attempts: list[str] = []
    try:
        part.InWorkObject = body  # the boolean must land in the probe body
    except Exception as exc:
        attempts.append(f"InWorkObject: {_err(exc)}")
    for method in ("AddNewRemove", "AddNewIntersect"):
        for kind in ("body", "reference"):
            try:
                argument = operand if kind == "body" else part.CreateReferenceFromObject(operand)
                feature = getattr(shape_factory, method)(argument)
                part.UpdateObject(feature)
                return method, kind, _safe_name(feature), attempts
            except Exception as exc:
                attempts.append(f"{method}({kind}): {_err(exc)}")
    return None, "", "", attempts


def _probe_one(
    session: CatiaSession,
    doc: Any,
    part: Any,
    row: dict[str, Any],
    cx: float,
    cy: float,
    radius: float,
    *,
    half: float,
    pad_height: float,
    symmetric: bool,
    z_low: float,
    z_high: float,
    thickness: float,
    full_material: float,
    plate_xy: tuple[float, float, float, float],
    tolerance_mm: float,
    tolerance_mm3: float,
) -> None:
    # The expected material assumes the whole footprint sits in plate material.
    # A box hanging over the plate edge would make that formula lie, so such a
    # hole is reported as not applicable instead of failing on a wrong number.
    x_low, x_high, y_low, y_high = plate_xy
    if not (
        x_low - tolerance_mm <= cx - half
        and cx + half <= x_high + tolerance_mm
        and y_low - tolerance_mm <= cy - half
        and cy + half <= y_high + tolerance_mm
    ):
        row["status"] = "probe_not_applicable"
        row["reason"] = (
            "the probe footprint leaves the plate bounding box; the expected material "
            "formula (full footprint inside material) would be wrong"
        )
        row["footprint"] = {"x": [_num(cx - half), _num(cx + half)], "y": [_num(cy - half), _num(cy + half)]}
        row["plate_bbox_xy"] = {"x": [_num(x_low), _num(x_high)], "y": [_num(y_low), _num(y_high)]}
        return

    body, info, box_bbox = _build_probe_box(
        session, doc, part, cx=cx, cy=cy, half=half, pad_height=pad_height, symmetric=symmetric
    )
    row["box"] = info
    placement = _span_check(box_bbox, cx, cy, half, z_low, z_high, tolerance_mm)
    row["box_placement"] = placement
    if not placement["ok"]:
        raise RuntimeError("the probe box is not where it was requested: " + "; ".join(placement["errors"]))

    method, operand_kind, feature, attempts = _boolean_with_main(part, body, part.MainBody)
    row["boolean"] = {"method": method, "operand_kind": operand_kind, "feature": feature, "failures": attempts}
    if method is None:
        raise RuntimeError("no Part Design boolean API worked: " + "; ".join(attempts))

    residual = measure_volume_mm3(session, doc, part, body)["volume_mm3"]
    box_volume = float(info["box_volume_mm3"])
    material = residual if method == "AddNewIntersect" else box_volume - residual
    material = max(0.0, material)

    overlap = _circle_square_overlap(half, radius)
    expected_material = max(0.0, thickness * (4.0 * half * half - overlap))
    formula_box_minus_cylinder = full_material - math.pi * radius * radius * thickness
    delta = material - expected_material
    within = abs(delta) <= tolerance_mm3

    row.update(
        {
            "expected_material_mm3": _num(expected_material),
            "expected_material_formula_box_minus_cylinder_mm3": _num(formula_box_minus_cylinder),
            "actual_material_mm3": _num(material),
            "material_if_no_hole_mm3": _num(full_material),
            "residual_after_boolean_mm3": _num(residual),
            "delta_mm3": _num(delta),
            "within_tolerance": within,
        }
    )
    if abs(formula_box_minus_cylinder - expected_material) > 1e-6:
        row["formula_note"] = (
            "box-minus-cylinder value clamped: the (2*half) x (2*half) footprint lies inside the hole "
            "circle, so the physical material left inside the box is the rectangle/circle overlap (>= 0)"
        )
    if within:
        row["inferred"] = "hole present at the claimed centre"
    elif abs(material - full_material) <= tolerance_mm3:
        row["inferred"] = "no hole at the claimed centre (the probe measured solid material)"
    else:
        row["inferred"] = "unexpected material fraction (hole moved or only partly present)"


def _probe_status(evidence: dict[str, Any], expected_count: int) -> str:
    rows = evidence["holes"]
    if evidence["com_errors"]:
        return "unavailable"
    if len(rows) < expected_count:
        return "unavailable"
    if not rows:
        return "unavailable"
    main_body = evidence.get("main_body_volume") or {}
    if main_body.get("unchanged") is False:
        return "unavailable"
    if all(row.get("within_tolerance") is True for row in rows):
        return "pass"
    if any(row.get("within_tolerance") is False for row in rows):
        return "fail"
    return "unavailable"


def _physical_hole_probe(
    session: CatiaSession,
    spec: dict[str, Any],
    tolerance: dict[str, float],
    measured_bbox: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    expected = spec.get("expected") if isinstance(spec.get("expected"), dict) else {}
    holes = expected.get("holes") or []
    if not isinstance(holes, list):
        return "fail", {"reason": "expected.holes must be a list", "value": str(holes)[:120]}
    if not holes:
        return "skipped", {"reason": "spec lists no expected.holes to probe"}
    for position, hole in enumerate(holes, start=1):
        numeric = (
            isinstance(hole, dict)
            and isinstance(hole.get("x"), (int, float))
            and not isinstance(hole.get("x"), bool)
            and isinstance(hole.get("y"), (int, float))
            and not isinstance(hole.get("y"), bool)
        )
        if not numeric:
            return "fail", {
                "reason": f"expected.holes[{position - 1}] needs numeric x and y",
                "holes": holes[:6],
            }

    configuration = spec.get("probe") if isinstance(spec.get("probe"), dict) else {}
    if configuration.get("skip") is True:
        return "skipped", {"reason": configuration.get("reason") or "spec opts out of the Z through-hole probe"}
    only = configuration.get("only")
    if isinstance(only, list) and only:
        try:
            holes = [holes[int(index) - 1] for index in only]
        except Exception as exc:
            return "fail", {"reason": f"probe.only must be 1-based indices into expected.holes ({exc})"}
    half = float(configuration.get("half_mm", 2.5))
    margin = float(configuration.get("z_margin_mm", 1.0))
    length_tolerance = max(tolerance["length_mm"], 0.01)

    if measured_bbox is not None:
        source_box = measured_bbox
        bbox_source = "measured in this run (MainBody vertex sweep)"
    elif isinstance(expected.get("bbox"), dict):
        source_box = expected["bbox"]
        bbox_source = "spec expected.bbox (vertex sweep unavailable)"
    else:
        return "unavailable", {"reason": "no measured or expected bbox; the probe box cannot be placed in z"}
    plate_min = [float(value) for value in source_box["min"]]
    plate_max = [float(value) for value in source_box["max"]]
    z_min, z_max = plate_min[2], plate_max[2]

    thickness = z_max - z_min
    if thickness <= 0:
        return "unavailable", {
            "reason": f"non-positive plate thickness in z ({_num(thickness)} mm)",
            "z_range": [_num(z_min), _num(z_max)],
            "bbox_source": bbox_source,
        }

    # PlaneXY is z=0.  A pad grows along +Z, so a plate resting at z>=0 is
    # covered by one pad from 0; anything reaching below z=0 needs a symmetric pad.
    if z_min >= 0.0:
        pad_height = z_max + margin
        symmetric = False
        z_low, z_high = 0.0, pad_height
    else:
        reach = max(-z_min, z_max) + margin
        pad_height = 2.0 * reach
        symmetric = True
        z_low, z_high = -reach, reach

    footprint = 4.0 * half * half
    full_material = thickness * footprint
    probe_tolerance = float(configuration.get("tolerance_mm3", 0.0) or 0.0) or max(
        1.0, full_material * 0.005
    )

    catpart = os.path.abspath(str(spec.get("catpart") or ""))
    if not spec.get("catpart") or not os.path.isfile(catpart):
        return "unavailable", {"reason": f"delivered .CATPart not readable: {catpart}"}
    directory = os.path.dirname(catpart)
    stem = os.path.splitext(os.path.basename(catpart))[0]
    probe_path = os.path.join(directory, f"{stem}-probe-{os.getpid()}.CATPart")
    suffix = 1
    while (os.path.exists(probe_path) or session.find_open(probe_path) is not None) and suffix < 50:
        probe_path = os.path.join(directory, f"{stem}-probe-{os.getpid()}-{suffix}.CATPart")
        suffix += 1

    evidence: dict[str, Any] = {
        "probe_copy": probe_path,
        "copied_from": catpart,
        "half_mm": _num(half),
        "z_margin_mm": _num(margin),
        "plate": {
            "min": [_num(value) for value in plate_min],
            "max": [_num(value) for value in plate_max],
            "thickness_mm": _num(thickness),
            "bbox_source": bbox_source,
        },
        "probe_box": {
            "pad_height_mm": _num(pad_height),
            "symmetric": symmetric,
            "z_span": [_num(z_low), _num(z_high)],
            "footprint_mm2": _num(footprint),
        },
        "expected_material_formula": "thickness * (footprint - circle/square overlap); "
                                     "the plain boxVolume - pi*r^2*thickness value is reported per hole",
        "tolerance_mm3": _num(probe_tolerance),
        "holes": [],
        "com_errors": [],
    }
    rows: list[dict[str, Any]] = evidence["holes"]
    copy_doc = None
    delete_error = ""
    try:
        shutil.copy2(catpart, probe_path)
        evidence["probe_copy_bytes"] = os.path.getsize(probe_path)
        copy_doc, open_evidence = session.open_document(probe_path, close_at_end=True)
        evidence["open"] = open_evidence
        if open_evidence.get("already_open"):
            raise RuntimeError("the freshly written probe copy reported as already open; refusing to reuse it")
        probe_part = copy_doc.Part
        evidence["probe_part"] = _safe_name(probe_part)
        main_before = measure_volume_mm3(session, copy_doc, probe_part, probe_part.MainBody)["volume_mm3"]
        for index, hole in enumerate(holes[:MAX_PROBE_HOLES], start=1):
            row: dict[str, Any] = {
                "index": index,
                "expected": {"x": _num(hole.get("x")), "y": _num(hole.get("y")), "r": _num(hole.get("r"))},
            }
            rows.append(row)
            try:
                _probe_one(
                    session,
                    copy_doc,
                    probe_part,
                    row,
                    float(hole["x"]),
                    float(hole["y"]),
                    float(hole.get("r", 0.0) or 0.0),
                    half=half,
                    pad_height=pad_height,
                    symmetric=symmetric,
                    z_low=z_low,
                    z_high=z_high,
                    thickness=thickness,
                    full_material=full_material,
                    plate_xy=(plate_min[0], plate_max[0], plate_min[1], plate_max[1]),
                    tolerance_mm=length_tolerance,
                    tolerance_mm3=probe_tolerance,
                )
            except Exception as exc:
                message = _err(exc)
                row["error"] = message
                evidence["com_errors"].append({"hole": index, "error": message})
                break
        if len(holes) > MAX_PROBE_HOLES:
            evidence["holes_not_probed"] = len(holes) - MAX_PROBE_HOLES
        main_after = measure_volume_mm3(session, copy_doc, probe_part, probe_part.MainBody)["volume_mm3"]
        evidence["main_body_volume"] = {
            "before_mm3": _num(main_before),
            "after_mm3": _num(main_after),
            "unchanged": abs(main_after - main_before)
            <= max(tolerance["volume_abs_mm3"], abs(main_before) * tolerance["volume_rel"]),
        }
    finally:
        if copy_doc is not None:
            evidence["close"] = session.close_document(copy_doc)
        if os.path.isfile(probe_path):
            deleted = False
            for _attempt in range(3):
                try:
                    os.remove(probe_path)
                    deleted = True
                    break
                except Exception as exc:
                    delete_error = _err(exc)
                    time.sleep(0.4)
            evidence["probe_copy_deleted"] = deleted
            if not deleted:
                evidence["probe_copy_delete_error"] = delete_error
    return _probe_status(evidence, len(holes[:MAX_PROBE_HOLES])), evidence


# --------------------------------------------------------------------------- #
# orchestration
# --------------------------------------------------------------------------- #
def run(spec_path: str) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    try:
        spec = _load_spec(spec_path)
    except Exception as exc:
        return {
            "passed": False,
            "checks": [],
            "errors": [{"check": "spec", "error": _err(exc), "spec_path": str(spec_path)}],
        }

    tolerance = _tolerances(spec)
    expected = spec.get("expected") if isinstance(spec.get("expected"), dict) else {}
    session = CatiaSession()
    state: dict[str, Any] = {"doc": None, "part": None, "bbox": None}

    def record(check_id: str, function) -> None:
        try:
            status, evidence = function()
        except Exception as exc:
            status = "fail"
            evidence = {"error": _err(exc), "traceback": _tb()}
            errors.append({"check": check_id, "error": _err(exc)})
        checks.append({"id": check_id, "status": status, "evidence": evidence})

    def require_document() -> tuple[Any, Any]:
        if state["doc"] is None or state["part"] is None:
            raise RuntimeError("the delivered document is not open (the 'reopen' check did not succeed)")
        return state["doc"], state["part"]

    # 1 -- the original was not modified -----------------------------------
    def check_source_sha256() -> tuple[str, dict[str, Any]]:
        source = spec.get("source_catpart")
        wanted = spec.get("source_sha256")
        if not source or not wanted:
            return "skipped", {"reason": "spec has no source_catpart/source_sha256", "source_catpart": source}
        path = os.path.abspath(str(source))
        if not os.path.isfile(path):
            raise FileNotFoundError(f"source_catpart not found on disk: {path}")
        actual = _sha256(path)
        evidence = {
            "source_catpart": path,
            "expected_sha256": str(wanted).strip().lower(),
            "actual_sha256": actual,
            "match": actual == str(wanted).strip().lower(),
        }
        return ("pass" if evidence["match"] else "fail"), evidence

    # 2 -- the delivered file exists, opens, and is a Part document ---------
    def check_reopen() -> tuple[str, dict[str, Any]]:
        delivered = spec.get("catpart")
        if not delivered:
            raise ValueError("spec.catpart is required")
        path = os.path.abspath(str(delivered))
        if not os.path.isfile(path):
            raise FileNotFoundError(f"delivered .CATPart not found: {path}")
        evidence: dict[str, Any] = {
            "path": path,
            "size_bytes": os.path.getsize(path),
            "sha256": _sha256(path),
            "mtime_local": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))),
        }
        session.ensure_connected()
        doc, opened = session.open_document(path)
        evidence.update(opened)
        evidence["catia"] = {
            "connected_by": session.how,
            "caption": session.caption,
            "python": sys.executable,
        }
        evidence["document_name"] = _safe_name(doc)
        try:
            part = doc.Part
        except Exception as exc:
            raise RuntimeError(f"the file opened but is not a Part document: {_err(exc)}") from exc
        evidence["part_name"] = _safe_name(part)
        evidence["open_documents"] = session.open_paths()
        state["doc"] = doc
        state["part"] = part
        state["opened_here"] = not opened["already_open"]
        if opened["already_open"]:
            evidence["note"] = (
                "file was already open in CATIA: reading it in place proves nothing about the bytes on disk"
            )
            return "unavailable", evidence
        if opened.get("documents_after") != opened.get("documents_before", -1) + 1:
            evidence["note"] = "Documents.Count did not increase by exactly 1; the disk load was not proven"
            return "fail", evidence
        evidence["note"] = "loaded from disk (Documents.Count +1); deliberately left open"
        return "pass", evidence

    # 3 -- volume ----------------------------------------------------------
    def check_volume() -> tuple[str, dict[str, Any]]:
        wanted = expected.get("volume_mm3")
        if wanted is None:
            return "fail", {"reason": "spec has no expected.volume_mm3 (a verification spec must state every assertion it wants proven)"}
        doc, part = require_document()
        measured = measure_volume_mm3(session, doc, part, part.MainBody)
        want = float(wanted)
        allowed = max(tolerance["volume_abs_mm3"], abs(want) * tolerance["volume_rel"])
        delta = measured["volume_mm3"] - want
        evidence = {
            "expected_mm3": _num(want),
            "actual_mm3": _num(measured["volume_mm3"]),
            "raw_volume_m3": _num(measured["volume_m3"], 12),
            "unit_conversion": "Measurable.Volume is m3; multiplied by 1e9",
            "delta_mm3": _num(delta),
            "tolerance_mm3": _num(allowed),
            "tolerance_rule": "max(volume_abs_mm3, expected*volume_rel)",
            "within_tolerance": abs(delta) <= allowed,
            "measured_on": "Part.MainBody / SPAWorkbench.GetMeasurable",
        }
        return ("pass" if evidence["within_tolerance"] else "fail"), evidence

    # 4 -- bounding box from a vertex sweep --------------------------------
    def check_bbox() -> tuple[str, dict[str, Any]]:
        wanted = expected.get("bbox")
        if not isinstance(wanted, dict) or "min" not in wanted or "max" not in wanted:
            return "fail", {"reason": "spec has no expected.bbox (a verification spec must state every assertion it wants proven)"}
        doc, part = require_document()
        try:
            box = stl_bbox(session, doc)
        except Exception as exc:
            box = vertex_bbox(session, doc, part)
            box["stl_error"] = _err(exc)
        state["bbox"] = box
        rows = []
        ok = True
        for axis, index in (("x", 0), ("y", 1), ("z", 2)):
            expected_min = float(wanted["min"][index])
            expected_max = float(wanted["max"][index])
            actual_min = box["min"][index]
            actual_max = box["max"][index]
            delta_min = actual_min - expected_min
            delta_max = actual_max - expected_max
            within = abs(delta_min) <= tolerance["length_mm"] and abs(delta_max) <= tolerance["length_mm"]
            ok = ok and within
            rows.append(
                {
                    "axis": axis,
                    "expected_min": _num(expected_min),
                    "actual_min": _num(actual_min),
                    "delta_min": _num(delta_min),
                    "expected_max": _num(expected_max),
                    "actual_max": _num(actual_max),
                    "delta_max": _num(delta_max),
                    "within_tolerance": within,
                }
            )
        evidence = {
            "method": box["method"],
            "vertex_count": box["vertex_count"],
            "triangle_count": box.get("triangle_count"),
            "search_attempts": box["attempts"],
            "stl_error": box.get("stl_error"),
            "axes": rows,
            "dimensions_mm": {
                "x": _num(box["dimensions"][0]),
                "y": _num(box["dimensions"][1]),
                "z": _num(box["dimensions"][2]),
            },
            "tolerance_mm": _num(tolerance["length_mm"]),
            "within_tolerance": ok,
        }
        return ("pass" if ok else "fail"), evidence

    # 4b -- sketch frames (non-XY semantics) -------------------------------
    def check_frames() -> tuple[str, dict[str, Any]]:
        wanted = expected.get("frames")
        if wanted is None:
            return "skipped", {"reason": "spec declares no expected.frames"}
        if not isinstance(wanted, dict):
            return "fail", {"reason": "expected.frames must be an object of sketch tail -> plane"}
        doc, part = require_document()
        found = read_all_circles(session, part)
        by_tail: dict[str, dict[str, Any]] = {}
        for row in found["sketches"]:
            tail = str(row["sketch"]).rsplit("/", 1)[-1]
            by_tail[tail] = row
        rows = []
        ok = True
        for sketch_name, plane in wanted.items():
            row = by_tail.get(str(sketch_name))
            if row is None:
                rows.append({"sketch": sketch_name, "status": "missing"})
                ok = False
                continue
            actual = (row.get("frame") or {}).get("plane")
            within = actual == str(plane)
            ok = ok and within
            rows.append(
                {
                    "sketch": sketch_name,
                    "expected_plane": plane,
                    "actual_plane": actual,
                    "frame": row.get("frame"),
                    "frame_error": row.get("frame_error"),
                    "within_tolerance": within,
                }
            )
        return ("pass" if ok else "fail"), {"rows": rows, "within_tolerance": ok}

    # 5 -- hole circles read back from every sketch ------------------------
    def check_holes() -> tuple[str, dict[str, Any]]:
        wanted = expected.get("holes")
        if wanted is None:
            return "fail", {"reason": "spec has no expected.holes (use [] to assert a hole-free model)"}
        if not isinstance(wanted, list):
            return "fail", {"reason": "expected.holes must be a list", "value": str(wanted)[:120], "within_tolerance": False}
        if not wanted:
            return "pass", {"note": "the spec expects no holes"}
        doc, part = require_document()
        found = read_all_circles(session, part)
        space = (
            "world"
            if any(
                isinstance(item, dict) and (item.get("space") == "world" or "z" in item)
                for item in wanted
            )
            else "sketch"
        )
        rows, leftovers = match_holes(wanted, found["circles"], tolerance["length_mm"], space=space)
        ok = bool(rows) and all(row.get("within_tolerance") is True for row in rows) and not leftovers
        evidence = {
            "tolerance_mm": _num(tolerance["length_mm"]),
            "space": space,
            "coordinate_frame": (
                "model coordinates via Sketch.GetAbsoluteAxisData"
                if space == "world"
                else "sketch-local 2D compared to global x/y (circles read on PlaneXY sketches)"
            ),
            "expected_count": len(wanted),
            "measured_circle_count": len(found["circles"]),
            "measured_circles": [
                {
                    "x": _num(c["x"]),
                    "y": _num(c["y"]),
                    "r": _num(c["r"]),
                    "sketch": c["sketch"],
                    "plane": c.get("plane"),
                    "world": (
                        {axis: _num(c["world"][axis]) for axis in ("x", "y", "z")}
                        if isinstance(c.get("world"), dict)
                        else None
                    ),
                }
                for c in found["circles"]
            ],
            "rows": rows,
            "circles_not_in_spec": [
                {"x": _num(c["x"]), "y": _num(c["y"]), "r": _num(c["r"]), "sketch": c["sketch"]}
                for c in leftovers
            ],
            "sketches": found["sketches"],
            "sketch_read_errors": found["errors"],
            "within_tolerance": ok,
        }
        return ("pass" if ok else "fail"), evidence

    # 6 -- driver parameters ----------------------------------------------
    def check_drivers() -> tuple[str, dict[str, Any]]:
        wanted = expected.get("drivers")
        if not isinstance(wanted, dict):
            return "fail", {"reason": "spec has no expected.drivers (a verification spec must state every assertion it wants proven)"}
        if not wanted:
            return "pass", {"note": "the spec expects no driver values", "rows": []}
        doc, part = require_document()
        parameters = list_parameters(part)
        by_full = {row["name"]: row for row in parameters}
        tails: dict[str, list[str]] = defaultdict(list)
        for row in parameters:
            tails[row["name"].rsplit("\\", 1)[-1]].append(row["name"])
        rows = []
        ok = True
        for key, value in wanted.items():
            key = str(key)
            tail = key.rsplit("\\", 1)[-1]
            row: dict[str, Any] = {"spec_key": key, "expected": _num(value)}
            if key in by_full:
                name, matched_by = key, "full_name"
            elif len(tails.get(tail, [])) == 1:
                name, matched_by = tails[tail][0], "trailing_segment"
            elif tails.get(tail):
                row.update(
                    {
                        "status": "ambiguous",
                        "candidates": tails[tail][:10],
                        "within_tolerance": False,
                        "note": "several parameters share this trailing name; refusing to guess",
                    }
                )
                rows.append(row)
                ok = False
                continue
            else:
                row.update(
                    {
                        "status": "missing",
                        "searched_segment": tail,
                        "parameter_count": len(parameters),
                        "within_tolerance": False,
                    }
                )
                rows.append(row)
                ok = False
                continue
            parameter = by_full[name]
            row.update({"matched_full_name": name, "matched_by": matched_by, "unit": parameter.get("unit")})
            actual = parameter.get("value")
            if not isinstance(actual, float):
                row.update(
                    {
                        "status": "non_numeric",
                        "raw_value": parameter.get("raw_value", parameter.get("value_error")),
                        "within_tolerance": False,
                    }
                )
                rows.append(row)
                ok = False
                continue
            delta = actual - float(value)
            within = abs(delta) <= tolerance["length_mm"]
            ok = ok and within
            row.update(
                {
                    "actual": _num(actual),
                    "delta": _num(delta),
                    "status": "ok" if within else "value_mismatch",
                    "within_tolerance": within,
                }
            )
            rows.append(row)
        evidence = {
            "rows": rows,
            "parameter_count": len(parameters),
            "available_parameter_names": [row["name"] for row in parameters][:60],
            "tolerance_value": _num(tolerance["length_mm"]),
            "within_tolerance": ok,
        }
        return ("pass" if ok else "fail"), evidence

    # 7 -- MainBody feature count -----------------------------------------
    def check_feature_count() -> tuple[str, dict[str, Any]]:
        wanted = expected.get("feature_count")
        if wanted is None:
            return "fail", {"reason": "spec has no expected.feature_count (a verification spec must state every assertion it wants proven)"}
        doc, part = require_document()
        shapes = int(part.MainBody.Shapes.Count)
        names = [_safe_name(part.MainBody.Shapes.Item(index)) for index in range(1, min(shapes, 60) + 1)]
        try:
            bodies = [_safe_name(part.Bodies.Item(index)) for index in range(1, int(part.Bodies.Count) + 1)]
        except Exception:
            bodies = []
        ok = shapes == int(wanted)
        evidence = {
            "expected": int(wanted),
            "actual": shapes,
            "shape_names": names,
            "sketch_count": int(part.MainBody.Sketches.Count),
            "main_body": _safe_name(part.MainBody),
            "bodies": bodies,
            "within_tolerance": ok,
        }
        return ("pass" if ok else "fail"), evidence

    # 8 -- geometric second opinion ---------------------------------------
    def check_physical_hole_probe() -> tuple[str, dict[str, Any]]:
        require_document()
        return _physical_hole_probe(session, spec, tolerance, state.get("bbox"))

    # 9 -- the source file was left alone ---------------------------------
    def check_original_untouched() -> tuple[str, dict[str, Any]]:
        source = spec.get("source_catpart")
        if not source:
            return "skipped", {"reason": "spec has no source_catpart"}
        path = os.path.abspath(str(source))
        evidence: dict[str, Any] = {"source_catpart": path, "touched": False}
        if os.path.isfile(path):
            evidence["sha256"] = _sha256(path)
            evidence["size_bytes"] = os.path.getsize(path)
            evidence["mtime_local"] = time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(path))
            )
            if spec.get("source_sha256"):
                evidence["sha256_matches_spec"] = (
                    evidence["sha256"] == str(spec["source_sha256"]).strip().lower()
                )
        else:
            evidence["note_missing"] = "source_catpart is not on disk; nothing was touched either way"
        delivered = spec.get("catpart")
        evidence["same_file_as_delivered"] = bool(delivered) and session.norm(str(delivered)) == session.norm(path)
        try:
            session.ensure_connected()
            open_paths = session.open_paths()
            evidence["open_documents"] = open_paths
            evidence["open_in_catia"] = any(session.norm(item) == session.norm(path) for item in open_paths)
        except Exception as exc:
            evidence["open_in_catia"] = None
            evidence["open_check_error"] = _err(exc)
        evidence["note"] = "reported only: this script never opened, closed, saved or measured the source file"
        return "pass", evidence

    try:
        record("source_sha256", check_source_sha256)
        record("reopen", check_reopen)
        record("volume", check_volume)
        record("bbox", check_bbox)
        record("frames", check_frames)
        record("holes", check_holes)
        record("drivers", check_drivers)
        record("feature_count", check_feature_count)
        record("physical_hole_probe", check_physical_hole_probe)
        record("original_untouched", check_original_untouched)
    finally:
        # Close only the document this run opened itself, and only after every
        # measurement is done, so the next run can prove a fresh disk load.
        owned = state.get("opened_here")
        doc = state.get("doc")
        if owned and doc is not None:
            try:
                doc.Close()
                checks.append(
                    {"id": "cleanup", "status": "pass", "evidence": {"closed": "document opened by this run"}}
                )
            except Exception as exc:
                errors.append({"check": "cleanup", "error": _err(exc)})
        for note in session.shutdown():
            errors.append({"check": "cleanup", "error": note})

    passed = bool(checks) and all(check["status"] not in FAILING_STATUSES for check in checks)
    return {"passed": passed, "checks": checks, "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Independent acceptance verification for one delivered CATIA V5 .CATPart."
    )
    parser.add_argument("spec", help="path to spec.json")
    parser.add_argument("--json-out", dest="json_out", default=None, help="also write the verdict JSON here")
    arguments = parser.parse_args(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    verdict = run(arguments.spec)
    text = json.dumps(verdict, ensure_ascii=False, separators=(",", ":"), default=str)
    exit_code = 0 if verdict.get("passed") else 1
    if arguments.json_out:
        try:
            with open(arguments.json_out, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
        except Exception as exc:
            verdict.setdefault("errors", []).append({"check": "json_out", "error": _err(exc)})
            text = json.dumps(verdict, ensure_ascii=False, separators=(",", ":"), default=str)
            exit_code = 2
    try:
        print(text)
    except UnicodeEncodeError:
        print(json.dumps(verdict, ensure_ascii=True, separators=(",", ":"), default=str))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
