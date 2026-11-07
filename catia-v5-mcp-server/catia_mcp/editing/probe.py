"""Low-level CATIA reads used by the editing layer.

Everything here is read-only. Each reader degrades to an explicit error string
instead of inventing a value: callers must be able to tell "not available"
from "zero".
"""

from __future__ import annotations

import math
import os
import struct
import tempfile
from typing import Any

S = "\\"

_CIRCLES_VBS = r"""
Public Function mcp_editing_circles(sk)
    Dim g, i, c(1), s
    s = ""
    For i = 1 To sk.GeometricElements.Count
        Set g = sk.GeometricElements.Item(i)
        If g.GeometricType = 5 Then
            g.GetCenter c
            s = s & c(0) & "," & c(1) & "," & g.Radius & ";"
        End If
    Next
    mcp_editing_circles = s
End Function
"""

_POINT_VBS = r"""
Public Function mcp_editing_point(m)
    Dim a(2)
    m.GetPoint a
    mcp_editing_point = a(0) & "," & a(1) & "," & a(2)
End Function
"""

# Sketch.GetAbsoluteAxisData returns origin + horizontal + vertical through an
# out-array that pywin32 cannot marshal (踩坑 45), so it goes through VBScript.
_AXIS_VBS = r"""
Public Function mcp_editing_axis(sk)
    Dim a(8), i, s
    sk.GetAbsoluteAxisData a
    s = ""
    For i = 0 To 8
        s = s & a(i) & ","
    Next
    mcp_editing_axis = s
End Function
"""


def _err(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}"


def _vec(value: Any) -> list[float] | None:
    try:
        return [round(float(value[k]), 9) for k in ("x", "y", "z")]
    except Exception:
        return None


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def _plane_name(horizontal: list[float], vertical: list[float]) -> str:
    """Which model plane does this sketch's local XY span?"""
    canonical = {
        "XY": ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0]),
        "XZ": ([1.0, 0.0, 0.0], [0.0, 0.0, 1.0]),
        "YZ": ([0.0, 1.0, 0.0], [0.0, 0.0, 1.0]),
    }
    for name, (h, v) in canonical.items():
        if all(abs(a - b) <= 1e-6 for a, b in zip(horizontal, h)) and all(
            abs(a - b) <= 1e-6 for a, b in zip(vertical, v)
        ):
            return name
    return "oblique"


def _normal_axis(horizontal: list[float], vertical: list[float]) -> str | None:
    """Model axis whose unit vector is the sketch normal (right-handed)."""
    normal = [
        horizontal[1] * vertical[2] - horizontal[2] * vertical[1],
        horizontal[2] * vertical[0] - horizontal[0] * vertical[2],
        horizontal[0] * vertical[1] - horizontal[1] * vertical[0],
    ]
    for index, axis in enumerate(("x", "y", "z")):
        if abs(abs(normal[index]) - 1.0) <= 1e-6 and all(
            abs(normal[j]) <= 1e-6 for j in range(3) if j != index
        ):
            return axis
    return None


def _to_world(origin: list[float], horizontal: list[float], vertical: list[float], x: float, y: float) -> dict[str, float]:
    return {
        "x": round(origin[0] + x * horizontal[0] + y * vertical[0], 6),
        "y": round(origin[1] + x * horizontal[1] + y * vertical[1], 6),
        "z": round(origin[2] + x * horizontal[2] + y * vertical[2], 6),
    }


def param_kind(name: str) -> str:
    """Path shape is the only stable signal: top-level params are user params."""
    depth = name.count(S)
    if depth <= 1:
        return "driver"
    lowered = name.lower()
    for marker in ("length", "offset", "radius", "angle", "distance", "长度", "偏移", "半径", "角度", "距离"):
        if marker in lowered:
            return "dimension"
    return "feature"


def read_parameters(part: Any) -> list[dict[str, Any]]:
    params = part.Parameters
    rows: list[dict[str, Any]] = []
    for i in range(1, params.Count + 1):
        p = params.Item(i)
        row: dict[str, Any] = {"handle": str(p.Name), "name": str(p.Name)}
        try:
            value = p.Value
            row["value"] = float(value) if isinstance(value, (int, float)) else value
        except Exception as exc:
            row["value"] = None
            row["valueError"] = _err(exc)
        try:
            row["unit"] = str(p.Unit.Symbol)
        except Exception:
            row["unit"] = "unknown"
        row["dimensionType"] = {
            "mm": "LENGTH",
            "deg": "ANGLE",
        }.get(row["unit"], "UNKNOWN")
        # V5's Relation object exposes the expression but not its destination
        # parameter, so a driven parameter is identified by not being a writable
        # driver instead of by a fabricated link.
        row["drivenBy"] = None
        row["kind"] = param_kind(row["handle"])
        if row["kind"] == "driver" and not isinstance(row.get("value"), (int, float)):
            row["kind"] = "system"
        rows.append(row)
    return rows


def read_relations(part: Any) -> list[dict[str, Any]]:
    rels = part.Relations
    rows = []
    for i in range(1, rels.Count + 1):
        rel = rels.Item(i)
        row: dict[str, Any] = {"name": str(rel.Name), "targetHandle": None}
        try:
            row["expression"] = str(rel.Value)
        except Exception as exc:
            row["expression"] = ""
            row["expressionError"] = _err(exc)
        try:
            comment = str(rel.Comment)
            if comment:
                row["comment"] = comment
        except Exception:
            pass
        rows.append(row)
    return rows


def read_body_features(part: Any) -> dict[str, Any]:
    body = part.MainBody
    shapes = []
    for i in range(1, body.Shapes.Count + 1):
        shapes.append(str(body.Shapes.Item(i).Name))
    sketches = []
    for i in range(1, body.Sketches.Count + 1):
        sketches.append(str(body.Sketches.Item(i).Name))
    bodies = []
    try:
        for i in range(1, part.Bodies.Count + 1):
            bodies.append(str(part.Bodies.Item(i).Name))
    except Exception:
        pass
    return {
        "mainBody": str(body.Name),
        "shapes": shapes,
        "sketches": sketches,
        "bodies": bodies,
    }


def read_sketch_frames(app: Any, part: Any) -> tuple[list[dict[str, Any]], list[str]]:
    """Sketch local-to-model frames from ``Sketch.GetAbsoluteAxisData``.

    The frame is what makes a hole centre comparable with the solid's global
    bounding box for non-XY sketches. If the read fails the frame is reported
    as unavailable; callers must not guess one.
    """
    frames: list[dict[str, Any]] = []
    limitations: list[str] = []
    body = part.MainBody
    for i in range(1, body.Sketches.Count + 1):
        sketch = body.Sketches.Item(i)
        name = str(sketch.Name)
        row: dict[str, Any] = {"name": name, "frame": None}
        try:
            raw = str(app.SystemService.Evaluate(_AXIS_VBS, 0, "mcp_editing_axis", [sketch]))
            values = [float(part) for part in raw.split(",") if part.strip()]
            if len(values) != 9:
                raise ValueError(f"expected 9 values, got {len(values)}")
            origin = [round(v, 6) for v in values[0:3]]
            horizontal = [round(v, 9) for v in values[3:6]]
            vertical = [round(v, 9) for v in values[6:9]]
            plane = _plane_name(horizontal, vertical)
            row["frame"] = {
                "origin": {"x": origin[0], "y": origin[1], "z": origin[2]},
                "horizontal": {"x": horizontal[0], "y": horizontal[1], "z": horizontal[2]},
                "vertical": {"x": vertical[0], "y": vertical[1], "z": vertical[2]},
                "plane": plane,
                "normalAxis": _normal_axis(horizontal, vertical),
                "coordinateSpace": "sketch-local",
                "source": "Sketch.GetAbsoluteAxisData",
            }
            if plane == "oblique":
                limitations.append(
                    f"sketch {name}: frame is oblique (H={horizontal}, V={vertical}); "
                    f"left/right/up/down must be resolved against this frame, not the view"
                )
        except Exception as exc:  # noqa: BLE001 - report, never invent a frame
            row["error"] = _err(exc)
            limitations.append(f"sketch {name}: absolute axis readback failed ({_err(exc)})")
        frames.append(row)
    return frames, limitations


def read_holes(
    app: Any, part: Any, frames: list[dict[str, Any]] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Circle centres/radii of every sketch, in sketch and model coordinates."""
    holes: list[dict[str, Any]] = []
    limitations: list[str] = []
    if frames is None:
        frames, frame_limitations = read_sketch_frames(app, part)
        limitations.extend(frame_limitations)
    frame_by_sketch = {row["name"]: row.get("frame") for row in frames}
    body = part.MainBody
    for i in range(1, body.Sketches.Count + 1):
        sketch = body.Sketches.Item(i)
        name = str(sketch.Name)
        try:
            raw = str(app.SystemService.Evaluate(_CIRCLES_VBS, 0, "mcp_editing_circles", [sketch]))
        except Exception as exc:
            limitations.append(f"sketch {name}: circle readback failed ({_err(exc)})")
            continue
        frame = frame_by_sketch.get(name)
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            try:
                x, y, r = (float(v) for v in chunk.split(","))
            except Exception:
                limitations.append(f"sketch {name}: unparsable circle '{chunk}'")
                continue
            row: dict[str, Any] = {
                "center": {"x": round(x, 6), "y": round(y, 6), "z": 0.0},
                "radius": round(r, 6),
                "source": name,
                "sketch": name,
                "coordinateSpace": "sketch-local",
            }
            if frame is not None:
                origin = _vec(frame["origin"])
                horizontal = _vec(frame["horizontal"])
                vertical = _vec(frame["vertical"])
                assert origin is not None and horizontal is not None and vertical is not None
                row["centerWorld"] = _to_world(origin, horizontal, vertical, x, y)
                row["worldFrame"] = {
                    "plane": frame["plane"],
                    "normalAxis": frame["normalAxis"],
                    "source": frame["source"],
                }
            holes.append(row)
    holes.sort(key=lambda h: (h["sketch"], h["center"]["x"], h["center"]["y"]))
    if not holes:
        limitations.append("no circle geometry found in any sketch; hole assertions are unavailable")
    else:
        missing = sorted({h["sketch"] for h in holes if "centerWorld" not in h})
        if missing:
            limitations.append(
                "world coordinates unavailable for circles in: " + ", ".join(missing)
                + "; they stay sketch-local and are not comparable with the global bbox"
            )
        else:
            limitations.append(
                "circular profiles are not holes by definition: role comes from the measured "
                "volume sign when a writable driver changes the radius (see roleEvidence)"
            )
    return holes, limitations


_FEATURE_MARKERS = (
    ("pad", ("凸台", "pad", "protrusion"), "additive"),
    ("pocket", ("凹槽", "pocket"), "subtractive"),
    ("hole", ("孔", "hole"), "subtractive"),
    ("shaft", ("旋转体", "旋转", "shaft"), "additive"),
    ("groove", ("旋转槽", "groove"), "subtractive"),
    ("rib", ("加强筋", "rib"), "additive"),
    ("slot", ("开槽", "slot"), "subtractive"),
    ("multipad", ("多凸台", "multipad"), "additive"),
)

# Only values observed on this CATIA build are named; anything else stays
# ``type_<n>`` so the report never presents a guessed enum as measured.
_CONSTRAINT_TYPES = {
    0: "fixed_or_unknown",
    1: "distance",
    2: "coincidence",
    5: "length",
    8: "parallel",
    10: "horizontality",
    13: "verticality",
    14: "radius",
}


def _feature_kind(name: str) -> tuple[str, str | None, str | None]:
    lowered = str(name).lower()
    for kind, markers, role in _FEATURE_MARKERS:
        for marker in markers:
            if marker in lowered:
                return kind, role, marker
        if kind == "multipad":
            continue
    return "unknown", None, None


def _constraint_rows(sketch: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        count = int(sketch.Constraints.Count)
    except Exception:
        return rows
    for i in range(1, count + 1):
        try:
            constraint = sketch.Constraints.Item(i)
        except Exception:
            continue
        row: dict[str, Any] = {"name": str(constraint.Name), "kind": None, "type": None}
        try:
            kind = int(constraint.Type)
            row["type"] = kind
            row["kind"] = _CONSTRAINT_TYPES.get(kind, f"type_{kind}")
        except Exception as exc:  # noqa: BLE001
            row["typeError"] = _err(exc)
        try:
            dimension = constraint.Dimension
            row["parameter"] = str(dimension.Name)
            row["value"] = float(dimension.Value)
            try:
                row["unit"] = str(dimension.Unit.Symbol)
            except Exception:
                row["unit"] = "unknown"
        except Exception:
            # Non-dimensional constraints (coincidence, parallel, fixed) have
            # no Dimension; that is expected, not an error.
            pass
        rows.append(row)
    return rows


def read_structure(
    app: Any, part: Any, holes: list[dict[str, Any]], frames: list[dict[str, Any]]
) -> dict[str, Any]:
    """Feature/sketch structure with the evidence behind every classification.

    ``Shape.Type`` does not exist on this CATIA build (踩坑 5), so a feature's
    kind is a *name-marker* classification, never claimed as measured. What is
    measured is the circle role (volume sign under a radius driver) and the
    sketch frame. Callers must keep those evidence strengths distinct.
    """
    body = part.MainBody
    limitations: list[str] = []
    shapes: list[dict[str, Any]] = []
    sketch_to_shapes: dict[str, list[str]] = {}
    for i in range(1, body.Shapes.Count + 1):
        shape = body.Shapes.Item(i)
        row: dict[str, Any] = {"name": str(shape.Name), "kind": "unknown", "materialRole": None}
        kind, role, marker = _feature_kind(row["name"])
        row["kind"] = kind
        row["materialRole"] = role
        row["kindEvidence"] = {
            "method": "name_marker" if marker else "none",
            "marker": marker,
            "strength": "name_only" if marker else "unknown",
            "note": "Shape.Type is unavailable on this CATIA build (踩坑 5)",
        }
        try:
            sketch_name = str(shape.Sketch.Name)
            row["sketch"] = sketch_name
            sketch_to_shapes.setdefault(sketch_name, []).append(row["name"])
        except Exception:
            row["sketch"] = None
        shapes.append(row)
    sketches: list[dict[str, Any]] = []
    for frame_row in frames:
        name = frame_row["name"]
        sketch_holes = [hole for hole in holes if hole.get("sketch") == name]
        constraints: list[dict[str, Any]] = []
        sketch = None
        try:
            sketch = body.Sketches.Item(name)
        except Exception:
            for index in range(1, body.Sketches.Count + 1):
                candidate = body.Sketches.Item(index)
                if str(candidate.Name) == name:
                    sketch = candidate
                    break
        if sketch is not None:
            try:
                constraints = _constraint_rows(sketch)
            except Exception:
                limitations.append(f"sketch {name}: constraints could not be read")
        else:
            limitations.append(f"sketch {name}: constraint readback could not bind the sketch")
        sketches.append(
            {
                "name": name,
                "frame": frame_row.get("frame"),
                "frameError": frame_row.get("error"),
                "usedByFeatures": sketch_to_shapes.get(name, []),
                "circles": [
                    {
                        "radius": hole["radius"],
                        "center": hole["center"],
                        "centerWorld": hole.get("centerWorld"),
                    }
                    for hole in sketch_holes
                ],
                "constraints": constraints,
            }
        )
    if shapes:
        limitations.append(
            "feature kind/materialRole is classified from the feature name only; it is a "
            "candidate, not a measured fact. Circle role is measured separately."
        )
    return {"features": shapes, "sketches": sketches, "bodies": _body_names(part), "limitations": limitations}


def _body_names(part: Any) -> list[str]:
    try:
        return [str(part.Bodies.Item(i).Name) for i in range(1, part.Bodies.Count + 1)]
    except Exception:
        return []


def read_volume_area(doc: Any, part: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    try:
        spa = doc.GetWorkbench("SPAWorkbench")
        measurable = spa.GetMeasurable(part.CreateReferenceFromObject(part.MainBody))
        out["volumeMm3"] = round(float(measurable.Volume) * 1e9, 6)
        out["areaMm2"] = round(float(measurable.Area) * 1e6, 6)
    except Exception as exc:
        out["volumeError"] = _err(exc)
    return out


_BBOX_SEARCH_CANDIDATES = (
    ("Type=顶点,sel", True),
    ("Topology.Vertex,sel", True),
    ("Type=Vertex,sel", True),
)


def _stl_points(path: str) -> list[list[float]]:
    data = open(path, "rb").read()
    if data[:5] == b"solid" and b"facet" in data[:4096]:
        points = []
        for line in data.decode("ascii", "replace").splitlines():
            line = line.strip()
            if line.startswith("vertex "):
                try:
                    points.append([float(value) for value in line.split()[1:4]])
                except Exception:
                    continue
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
        points.extend([list(values[3:6]), list(values[6:9]), list(values[9:12])])
    return points


def _lump_count(points: list[list[float]]) -> int:
    """Connected components of an STL triangle soup, by shared vertices.

    A parameter edit must not split one solid into several lumps. Pushing the
    hole edge margin E past the plate edge leaves the two circles outside the
    rectangle, and CATIA pads them as two extra cylinders (volume grows by
    2*pi*r^2*T) instead of failing the update. The bounding box then grows to
    cover them, so the holes-inside check still passes; counting lumps sees it.
    """
    if not points:
        return 0
    index: dict[tuple[float, float, float], int] = {}
    parent: list[int] = []

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        root_a, root_b = find(a), find(b)
        if root_a != root_b:
            parent[root_b] = root_a

    for triangle in range(0, len(points) - 2, 3):
        ids = []
        for offset in range(3):
            key = tuple(round(value, 4) for value in points[triangle + offset])
            node = index.get(key)
            if node is None:
                node = len(parent)
                index[key] = node
                parent.append(node)
            ids.append(node)
        union(ids[0], ids[1])
        union(ids[1], ids[2])
    return len({find(node) for node in range(len(parent))})


def _bbox_from_points(points: list[list[float]], method: str, **extra: Any) -> dict[str, Any]:
    mins = [min(point[k] for point in points) for k in range(3)]
    maxs = [max(point[k] for point in points) for k in range(3)]
    row = {
        "min": {"x": round(mins[0], 6), "y": round(mins[1], 6), "z": round(mins[2], 6)},
        "max": {"x": round(maxs[0], 6), "y": round(maxs[1], 6), "z": round(maxs[2], 6)},
        "dimensions": {
            "x": round(maxs[0] - mins[0], 6),
            "y": round(maxs[1] - mins[1], 6),
            "z": round(maxs[2] - mins[2], 6),
        },
        "method": method,
        # No API here proves planarity or exactness, so the box is never
        # advertised as exact.
        "exact": False,
        "planarAssumed": True,
    }
    row.update(extra)
    return row


def _read_bbox_stl(doc: Any) -> dict[str, Any] | None:
    """Bounding box from an STL tessellation of the document.

    A vertex sweep misses a cylinder's top (a circle has no vertex), so a
    Pad + boss model reported z=12 instead of 20 even when the whole body was
    selected. STL export measures the tessellated BRep, takes ~10 ms on these
    fixtures, and covers curved extremes. The temp file is deleted in a
    ``finally``; the document is not saved or modified.
    """
    path = None
    try:
        # CATIA must be able to write the export itself. A redirected TEMP made
        # ExportData block silently (the same authority problem as 踩坑 66), so
        # prefer the document's own directory and fall back to TEMP only when
        # the document has no real path yet.
        full_name = str(getattr(doc, "FullName", "") or "")
        base = os.path.dirname(os.path.abspath(full_name)) if os.path.isabs(full_name) else tempfile.gettempdir()
        path = os.path.join(base, f".catia-mcp-bbox-{os.getpid()}-{id(doc)}.stl")
        if os.path.exists(path):
            os.remove(path)
        doc.ExportData(path, "stl")
        points = _stl_points(path)
        if not points:
            return None
        return _bbox_from_points(
            points,
            "STL tessellation bbox (Document.ExportData)",
            triangleCount=len(points) // 3,
            lumpCount=_lump_count(points),
        )
    except Exception:
        return None
    finally:
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def read_bbox(app: Any, doc: Any, part: Any) -> dict[str, Any]:
    """Bounding box of the whole main body, curved features included.

    Selecting the last shape only measured that feature, so on a model with a
    Pad + Pad + Pocket the box was the pocket's box, not the part's (踩坑 17:
    a single feature has no vertices or an incomplete set). The STL pass is
    tried first because a vertex sweep cannot see a cylinder's top; the
    vertex sweep over the body remains as a fallback and as a cheap
    cross-check (踩坑 4 keeps the localized type-name candidates).
    """
    body = part.MainBody
    if body.Shapes.Count == 0:
        return {"error": "main body has no solid shape"}
    stl = _read_bbox_stl(doc)
    if stl is not None:
        return stl
    sel = doc.Selection
    errors = []
    for candidate, needs_selection in _BBOX_SEARCH_CANDIDATES:
        try:
            sel.Clear()
            if needs_selection:
                sel.Add(body)
            sel.Search(candidate)
            count = sel.Count
            if count == 0:
                errors.append(f"{candidate}: 0 vertices")
                continue
            refs = [sel.Item(i).Reference for i in range(1, count + 1)]
            points = []
            spa = doc.GetWorkbench("SPAWorkbench")
            for ref in refs:
                measurable = spa.GetMeasurable(ref)
                raw = str(app.SystemService.Evaluate(_POINT_VBS, 0, "mcp_editing_point", [measurable]))
                points.append([float(v) for v in raw.split(",")])
            sel.Clear()
            mins = [min(p[k] for p in points) for k in range(3)]
            maxs = [max(p[k] for p in points) for k in range(3)]
            return {
                "min": {"x": round(mins[0], 6), "y": round(mins[1], 6), "z": round(mins[2], 6)},
                "max": {"x": round(maxs[0], 6), "y": round(maxs[1], 6), "z": round(maxs[2], 6)},
                "dimensions": {
                    "x": round(maxs[0] - mins[0], 6),
                    "y": round(maxs[1] - mins[1], 6),
                    "z": round(maxs[2] - mins[2], 6),
                },
                "vertexCount": count,
                "method": f"vertex sweep over main body ({candidate})",
                "selection": "MainBody",
                # A vertex sweep is exact only for planar-faced solids and this API
                # has no planarity proof, so the box is never advertised as exact.
                "exact": False,
                "planarAssumed": True,
            }
        except Exception as exc:
            errors.append(f"{candidate}: {_err(exc)}")
    return {"error": "no vertex search candidate worked", "candidates": errors}


def read_model(app: Any, doc: Any, part: Any) -> dict[str, Any]:
    limitations: list[str] = []
    geometry = read_volume_area(doc, part)
    bbox = read_bbox(app, doc, part)
    geometry["bbox"] = bbox
    if "volumeMm3" not in geometry:
        limitations.append("volume unavailable")
    if "error" in bbox:
        limitations.append("bounding box unavailable")
    frames, frame_limitations = read_sketch_frames(app, part)
    limitations.extend(frame_limitations)
    holes, hole_limitations = read_holes(app, part, frames=frames)
    limitations.extend(hole_limitations)
    structure = read_structure(app, part, holes, frames)
    limitations.extend(structure.get("limitations") or [])
    return {
        "part": part.Name,
        "features": read_body_features(part),
        "structure": structure,
        "parameters": read_parameters(part),
        "relations": read_relations(part),
        "geometry": geometry,
        "holes": holes,
        "limitations": limitations,
        "measurementPrecision": {
            "lengthToleranceMm": 0.01,
            "volumeToleranceMm3": 0.01,
            "volumeRelativeTolerance": 1e-6,
        },
    }


def approx_equal(a: float, b: float, rel: float = 1e-6, abs_tol: float = 0.01) -> bool:
    if a is None or b is None:
        return False
    return abs(a - b) <= max(abs_tol, abs(b) * rel)


def same_geometry(before: dict[str, Any], after: dict[str, Any]) -> bool:
    vb = before.get("volumeMm3")
    va = after.get("volumeMm3")
    if vb is None or va is None:
        return False
    return math.isclose(vb, va, rel_tol=1e-9, abs_tol=1e-6)
