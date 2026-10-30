"""Rebuild a solid from a cross-section stack sidecar and compare to the source.

This is a data-preparation completeness check: if the stack reconstructs the
reference volume, the drawing's layer rows are sufficient for a modeler.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import build123d as bd


def world(u, v, w, u_index, v_index, axis_index):
    point = [0.0, 0.0, 0.0]
    point[u_index] = u
    point[v_index] = v
    point[axis_index] = w
    return tuple(point)


def profile_face(profile, z, indices):
    u_index, v_index, axis_index = indices
    if profile["kind"] == "CIRCLE":
        u, v, radius = profile["u"], profile["v"], profile["radius"]
        u_vec = world(1, 0, 0, u_index, v_index, axis_index)
        w_vec = world(0, 0, 1, u_index, v_index, axis_index)
        plane = bd.Plane(origin=world(0, 0, z, u_index, v_index, axis_index),
                         x_dir=u_vec, z_dir=w_vec)
        return plane * bd.Pos(u, v) * bd.Circle(radius)
    rows = profile.get("rows") or []
    parsed = []
    for row in rows:
        tokens = row.split()
        if not tokens:
            continue
        if tokens[0] == "L":
            parsed.append(("L", (float(tokens[1]), float(tokens[2])),
                           (float(tokens[3]), float(tokens[4]))))
        elif tokens[0] == "U":
            # U <kind> x1 y1 x2 y2 L <length>: a non-circular edge (conic or
            # bspline from a blend or a side-feature cut). Its chord closes the
            # loop, matching uv_path.
            parsed.append(("L", (float(tokens[2]), float(tokens[3])),
                           (float(tokens[4]), float(tokens[5]))))
        elif tokens[0] == "A":
            numbers = [float(token) for token in tokens[1:-1]]
            start = (numbers[3], numbers[4])
            middle = (numbers[5], numbers[6])
            end = (numbers[7], numbers[8]) if len(numbers) >= 9 else None
            parsed.append(("A", start, middle, end))
    if not parsed:
        raise ValueError("Wire profile has no rows")
    def point(sample):
        return world(sample[0], sample[1], z, u_index, v_index, axis_index)

    # Rows are rounded to 1e-4, which is below OCCT's edge-connectivity
    # tolerance: anchor every segment at the previous end and close any
    # residual gap with a short line instead of trusting the printed start.
    first = parsed[0][1]
    previous = first
    edges = []
    for index, item in enumerate(parsed):
        if item[0] == "L":
            a, b = item[1], item[2]
        else:
            a = item[1]
            b = item[3] if item[3] is not None else parsed[(index + 1) % len(parsed)][1]
        if math.dist(previous, b) < math.dist(previous, a):
            a, b = b, a
        if math.dist(previous, b) < 5e-4:
            continue
        if item[0] == "L":
            edges.append(bd.Edge.make_line(point(previous), point(b)))
        else:
            edges.append(bd.ThreePointArc(point(previous), point(item[2]), point(b)))
        previous = b
    if math.dist(previous, first) > 1e-9:
        edges.append(bd.Edge.make_line(point(previous), point(first)))
    wire = bd.Wire(edges)
    return bd.Face(wire)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--sections", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    data = json.loads(Path(args.sections).read_text(encoding="utf-8"))
    direction = data["mainDirection"]
    axis_index = max(range(3), key=lambda i: abs(direction[i]))
    u_index, v_index = {2: (0, 1), 0: (1, 2), 1: (2, 0)}[axis_index]
    indices = (u_index, v_index, axis_index)
    profiles = data["profiles"]
    parts = []
    built = 0
    skipped = 0
    for layer in data["layers"]:
        z0, z1 = layer["z0"], layer["z1"]
        for region in layer["regions"]:
            outer_id = next((pid for role, pid in region if role == "OUTER"), None)
            inner_ids = [pid for role, pid in region if role != "OUTER"]
            used = ([outer_id] if outer_id is not None else []) + inner_ids
            if outer_id is None or any(
                    profiles[pid - 1]["kind"] == "WIRE" and not (profiles[pid - 1].get("path") or [None])[0]
                    for pid in used):
                skipped += 1
                continue
            face = profile_face(profiles[outer_id - 1], z0, indices)
            for pid in inner_ids:
                hole = profile_face(profiles[pid - 1], z0, indices)
                face = face - hole
            parts.append(bd.extrude(face, amount=z1 - z0, dir=tuple(direction)))
            built += 1
    if not parts:
        raise SystemExit("Schedule produced no solids")
    # Adjacent layers share a face but do not overlap; a compound sums their
    # volumes exactly, while a 250-solid boolean fuse silently dropped most of
    # the body (FTC-10 with 5 blend samples reconstructs 31468 of 188318 mm3).
    body = bd.Compound(children=parts)
    report = {
        "parts": built,
        "skippedRegions": skipped,
        "volumeMm3": round(float(body.volume), 6),
        "bbox": {"min": [round(v, 4) for v in body.bounding_box().min],
                 "max": [round(v, 4) for v in body.bounding_box().max]},
        "referenceVolumeMm3": data.get("volumeMm3"),
    }
    Path(args.out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps(report))


if __name__ == "__main__":
    main()
