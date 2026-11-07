"""Analyse planar faces of an AP203 STEP file (data-preparation tool).

For every PLANE-backed ADVANCED_FACE this reports the face normal, area from
the boundary loops, and how many loops are all-circular (holes) versus
mixed line/arc (cut-outs). It is used to choose which NIST part can be turned
into a dimensioned derived drawing and built as an extruded profile.

    python scripts/step_planar_analysis.py <part.stp> [out.json]

The output is verification-side material.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

_ENTITY_RE = re.compile(r"#(\d+)\s*=\s*(.*?);", re.S)


def _load(path: Path) -> dict[int, str]:
    text = path.read_text(encoding="latin-1")
    if "DATA;" not in text:
        raise RuntimeError(f"{path} is not a STEP DATA section")
    data = text.split("DATA;", 1)[1]
    return {int(m.group(1)): " ".join(m.group(2).split()) for m in _ENTITY_RE.finditer(data)}


def _refs(value: str) -> list[int]:
    return [int(x) for x in re.findall(r"#(\d+)", value)]


def _head(entity: str) -> str:
    return entity.split("(", 1)[0].strip().upper()


def _numbers(entity: str) -> list[float] | None:
    match = re.search(r"\(([^()]*)\)\)\s*$", entity)
    if not match:
        return None
    try:
        return [float(v) for v in match.group(1).split(",")]
    except ValueError:
        return None


def _cartesian(ents: dict[int, str], pid: int) -> list[float] | None:
    entity = ents[pid]
    if _head(entity) != "CARTESIAN_POINT":
        refs = _refs(entity)
        if not refs:
            return None
        entity = ents[refs[0]]
    return _numbers(entity)


def _direction(ents: dict[int, str], did: int) -> list[float] | None:
    entity = ents[did]
    if _head(entity) != "DIRECTION":
        refs = _refs(entity)
        if not refs:
            return None
        entity = ents[refs[0]]
    return _numbers(entity)


def _axis2(ents: dict[int, str], aid: int) -> dict:
    refs = _refs(ents[aid])
    return {
        "origin": _cartesian(ents, refs[0]),
        "axis": _direction(ents, refs[1]),
        "ref": _direction(ents, refs[2]),
    }


def _loop_segments(ents: dict[int, str], loop_id: int) -> list[dict]:
    out = []
    for oriented_id in _refs(ents[loop_id]):
        oriented = ents[oriented_id]
        edge_id = _refs(oriented)[-1]
        edge = ents[edge_id]
        v1 = _cartesian(ents, _refs(edge)[0])
        v2 = _cartesian(ents, _refs(edge)[1])
        curve = ents[_refs(edge)[-1]]
        kind = _head(curve)
        if kind == "LINE":
            out.append({"kind": "line", "start": v1, "end": v2})
        elif kind == "CIRCLE":
            axis = _axis2(ents, _refs(curve)[0])
            radius = float(re.search(r",\s*([\d.Ee+-]+)\s*\)$", curve).group(1))
            out.append({
                "kind": "circle", "start": v1, "end": v2,
                "centre": axis["origin"], "axis": axis["axis"], "radius": radius,
                "full": v1 == v2,
            })
        else:
            out.append({"kind": kind, "start": v1, "end": v2})
    return out


def _basis(normal: list[float]) -> tuple[list[float], list[float]]:
    n = normal
    helper = [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0]
    u = [n[1] * helper[2] - n[2] * helper[1],
         n[2] * helper[0] - n[0] * helper[2],
         n[0] * helper[1] - n[1] * helper[0]]
    length = math.sqrt(sum(v * v for v in u)) or 1.0
    u = [v / length for v in u]
    v = [n[1] * u[2] - n[2] * u[1],
         n[2] * u[0] - n[0] * u[2],
         n[0] * u[1] - n[1] * u[0]]
    return u, v


def _project(point: list[float], origin: list[float], u: list[float], v: list[float]) -> tuple[float, float]:
    d = [point[i] - origin[i] for i in range(3)]
    return (sum(d[i] * u[i] for i in range(3)), sum(d[i] * v[i] for i in range(3)))


def _loop_area(segments: list[dict], origin: list[float], u: list[float], v: list[float]) -> float:
    total = 0.0
    for segment in segments:
        if segment["kind"] == "line":
            x1, y1 = _project(segment["start"], origin, u, v)
            x2, y2 = _project(segment["end"], origin, u, v)
            total += x1 * y2 - x2 * y1
        elif segment["kind"] == "circle":
            cx, cy = _project(segment["centre"], origin, u, v)
            radius = segment["radius"]
            if segment["full"]:
                total += 2.0 * math.pi * radius * radius
                continue
            a = math.atan2(segment["start"][1] - segment["centre"][1],
                           segment["start"][0] - segment["centre"][0])
            b = math.atan2(segment["end"][1] - segment["centre"][1],
                           segment["end"][0] - segment["centre"][0])
            while b <= a:
                b += 2.0 * math.pi
            total += (radius * (cx * (math.sin(b) - math.sin(a)) - cy * (math.cos(b) - math.cos(a)))
                      + radius * radius * (b - a))
    return abs(total) / 2.0


def analyse(path: Path) -> dict:
    ents = _load(path)
    faces = []
    for eid, entity in ents.items():
        if _head(entity) != "ADVANCED_FACE":
            continue
        refs = _refs(entity)
        surface = ents[refs[-1]]
        if _head(surface) != "PLANE":
            continue
        axis = _axis2(ents, _refs(surface)[0])
        normal = axis["axis"]
        origin = axis["origin"]
        if not normal or not origin:
            continue
        u, v = _basis(normal)
        outer_area = 0.0
        inner_area = 0.0
        holes = 0
        cutouts = 0
        segment_total = 0
        bound_count = 0
        for bound_id in refs[:-1]:
            bound = ents[bound_id]
            loop = _loop_segments(ents, _refs(bound)[0])
            segment_total += len(loop)
            bound_count += 1
            area = _loop_area(loop, origin, u, v)
            if _head(bound) == "FACE_OUTER_BOUND":
                outer_area += area
            else:
                inner_area += area
                if loop and all(s["kind"] == "circle" and s["full"] for s in loop):
                    holes += 1
                else:
                    cutouts += 1
        faces.append({
            "faceId": eid,
            "normal": [round(x, 6) for x in normal],
            "origin": [round(x, 6) for x in origin],
            "bounds": bound_count,
            "segments": segment_total,
            "outerAreaMm2": round(outer_area, 6),
            "innerAreaMm2": round(inner_area, 6),
            "netAreaMm2": round(outer_area - inner_area, 6),
            "circularHoles": holes,
            "nonCircularCutouts": cutouts,
        })
    faces.sort(key=lambda item: item["netAreaMm2"], reverse=True)
    return {"file": str(path), "planarFaces": faces, "faceCount": len(faces)}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    result = analyse(Path(sys.argv[1]))
    text = json.dumps(result, indent=2, ensure_ascii=False)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text + "\n", encoding="utf-8")
    print(text[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
