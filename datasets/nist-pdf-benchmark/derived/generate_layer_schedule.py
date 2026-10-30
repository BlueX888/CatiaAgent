"""Compact layer/profile derived drawing for 2.5D NIST parts.

Instead of listing every B-rep face, this schedule lists the unique profiles on
each planar layer normal to the main axis, plus side features:

* ``F<id> D <d> A <area> OUTER P<i> INNER P<i> ...`` — a layer face at signed
  offset ``d`` along the main normal.
* ``P<i> CIRCLE u v r ...`` / ``P<i> WIRE`` + ``L``/``A`` segment rows — the
  profile in the layer's (u,v) coordinates.
* A profile that appears on two layers is THROUGH between them; a circle that
  matches a cylinder gets its side and axial span.
* Cylinders/cones not parallel to the main axis are listed as side features.
* Wall faces implied by the profiles are omitted and counted in the header.

The PDF is the only generation input; the sidecar JSON keeps expectations.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
import sys
from pathlib import Path

import build123d as bd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import generate_schedule as base  # noqa: E402
import text_layer  # noqa: E402

ROOT = base.ROOT
REPO = base.REPO
BENCH = base.BENCH
CAD_PYTHON = base.CAD_PYTHON
ROWS_PER_SHEET = 60
r = base.r
CHAIN_TOL = 0.05


def _distance(a, b):
    return math.sqrt((a.X - b.X) ** 2 + (a.Y - b.Y) ** 2 + (a.Z - b.Z) ** 2)


def oriented_edges(wire):
    """Edges in traversal order, each paired with True when its stored direction is reversed.

    ``Wire.order_edges()`` orders but does not reorient edges, and OCCT may hand
    back an edge whose stored direction opposes the wire. Rows written from the
    stored start/end then break the chain (a reversed B-spline produced a
    near-collinear arc through the wrong endpoints and a 948k mm² face). Chain
    by shared endpoints so every row runs from the previous end to its own end.
    """
    edges = list(wire.edges())
    if not edges and float(getattr(wire, "length", 0) or 0) > 1e-6:
        # build123d's Wire.edges() can come back empty for stray wires that
        # still hold edges; enumerate them directly.
        from OCP.TopExp import TopExp_Explorer
        from OCP.TopAbs import TopAbs_EDGE
        from OCP.TopoDS import TopoDS
        explorer = TopExp_Explorer(wire.wrapped, TopAbs_EDGE)
        while explorer.More():
            edges.append(bd.Edge(TopoDS.Edge(explorer.Current())))
            explorer.Next()
    try:
        ordered = list(wire.order_edges())
        if len(ordered) == len(edges):
            edges = ordered
    except Exception:
        pass
    if not edges:
        return []
    chain = [(edges[0], False)]
    remaining = list(range(1, len(edges)))
    end = edges[0].position_at(1)
    while remaining:
        best = None
        for index in remaining:
            edge = edges[index]
            for reversed_flag, point in ((False, edge.position_at(0)), (True, edge.position_at(1))):
                distance = _distance(point, end)
                if distance <= CHAIN_TOL and (best is None or distance < best[0]):
                    best = (distance, index, reversed_flag)
        if best is None:
            # Section edges from adjacent tangent faces can meet with a small
            # seam; fall back to the nearest endpoint before giving up.
            for index in remaining:
                edge = edges[index]
                for reversed_flag, point in ((False, edge.position_at(0)), (True, edge.position_at(1))):
                    distance = _distance(point, end)
                    if best is None or distance < best[0]:
                        best = (distance, index, reversed_flag)
            if best is None:
                break
        _, index, reversed_flag = best
        chain.append((edges[index], reversed_flag))
        remaining.remove(index)
        end = edges[index].position_at(0) if reversed_flag else edges[index].position_at(1)
    return chain


def wire_is_closed(wire, tol=5e-3):
    """True when the chained edges return to their start.

    OCCT sectioning through a tangent face can leave an open stray edge stored
    as an inner wire of the face (one 27.5 mm line on FTC-10 layer 15.0-15.5).
    It bounds no area and must not become a hole.
    """
    chain = oriented_edges(wire)
    if not chain:
        return False
    first_edge, first_reverse = chain[0]
    start = first_edge.position_at(1) if first_reverse else first_edge.position_at(0)
    last_edge, last_reverse = chain[-1]
    end = last_edge.position_at(0) if last_reverse else last_edge.position_at(1)
    return _distance(start, end) <= tol


def edge_ends(edge, reversed_flag):
    """(start, mid, end) in traversal order."""
    start = edge.position_at(1) if reversed_flag else edge.position_at(0)
    end = edge.position_at(0) if reversed_flag else edge.position_at(1)
    return start, edge.position_at(0.5), end


def edge_polyline(edge, reversed_flag, u_index, v_index, max_chord=0.2, cap=96):
    """(u,v) samples from traversal start to end; the chord error of a blend
    section is ~c²/(8r), so a 0.5 mm chord keeps smooth curves within 0.05 mm."""
    length = float(edge.length)
    count = max(1, min(cap, int(math.ceil(length / max_chord))))
    samples = []
    for step in range(count + 1):
        t = step / count
        point = edge.position_at(1 - t) if reversed_flag else edge.position_at(t)
        samples.append((getattr(point, "XYZ"[u_index]), getattr(point, "XYZ"[v_index])))
    return samples


def axis_layout(normal):
    """Return (axis index, u index, v index, u name, v name, sketch plane).

    The (u,v) order matches the local H/V axes of the CATIA sketch plane whose
    normal is the positive main axis: xy = (X,Y), yz = (Y,Z), zx = (Z,X).
    """
    index = max(range(3), key=lambda i: abs(normal[i]))
    mapping = {2: (0, 1, "xy"), 0: (1, 2, "yz"), 1: (2, 0, "zx")}
    u_index, v_index, plane = mapping[index]
    return index, (u_index, v_index), "XYZ"[u_index], "XYZ"[v_index], plane


def plane_offset(normal, centre):
    return sum(normal[i] * getattr(centre, "XYZ"[i]) for i in range(3))


def uv_rows(wire, u_index, v_index):
    """Wire rows in the layer's 2D (u,v) coordinates, in traversal order.

    Every row carries both endpoints; ``A`` rows also carry centre, radius and a
    sampled midpoint so the reader can rebuild the exact arc without guessing
    which neighbour it joins.
    """
    uv = lambda point: (point.X, point.Y, point.Z)
    rows = []
    for edge, reversed_flag in oriented_edges(wire)[:400]:
        kind = str(edge.geom_type).split(".")[-1]
        start, middle, end = edge_ends(edge, reversed_flag)
        if kind == "LINE":
            a, b = uv(start), uv(end)
            rows.append(f"L {r(a[u_index])} {r(a[v_index])} {r(b[u_index])} {r(b[v_index])}")
        elif kind == "CIRCLE":
            centre = edge.arc_center
            radius = base.edge_radius(edge)
            if radius is None:
                samples = edge_polyline(edge, reversed_flag, u_index, v_index)
                for (su_, sv_), (eu_, ev_) in zip(samples, samples[1:]):
                    rows.append(f"L {r(su_)} {r(sv_)} {r(eu_)} {r(ev_)}")
                continue
            radius = abs(radius)
            c, a, m, e = uv(centre), uv(start), uv(middle), uv(end)
            su, sv = a[u_index], a[v_index]
            mu, mv = m[u_index], m[v_index]
            eu, ev = e[u_index], e[v_index]
            cross = (mu - su) * (ev - mv) - (mv - sv) * (eu - mu)
            turn = "CW" if cross < 0 else "CCW"
            rows.append(f"A {r(c[u_index])} {r(c[v_index])} {r(radius)} "
                        f"{r(su)} {r(sv)} {r(mu)} {r(mv)} {r(eu)} {r(ev)} {turn}")
        else:
            # Non-circular edge (blend/side-feature conic): the requirement
            # language has no spline primitive, so it is chained into short
            # line segments whose combined error stays below the print
            # resolution.
            samples = edge_polyline(edge, reversed_flag, u_index, v_index)
            for (su_, sv_), (eu_, ev_) in zip(samples, samples[1:]):
                rows.append(f"L {r(su_)} {r(sv_)} {r(eu_)} {r(ev_)}")
    return rows


def circle_of(wire):
    edges = list(wire.edges())
    if not edges:
        return None
    centres = []
    radii = []
    for edge in edges:
        if not str(edge.geom_type).endswith("CIRCLE"):
            return None
        radius = base.edge_radius(edge)
        if radius is None:
            return None
        centre = edge.arc_center
        centres.append((round(centre.X, 6), round(centre.Y, 6), round(centre.Z, 6)))
        radii.append(round(radius, 6))
    if len(set(centres)) != 1 or len(set(radii)) != 1:
        return None
    total = sum(float(edge.length) for edge in edges)
    if abs(total - 2 * math.pi * radii[0]) > 1e-3:
        return None
    return centres[0], radii[0]


def uv_path(wire, u_index, v_index):
    """Closed compact profile: START plus ["line"/"arc", ...] segments.

    Each segment's end is the next segment's start, so the copied array closes
    exactly. Arc direction and large-arc flag are chosen so the arc passes
    through the edge's sampled midpoint.
    """
    import math as _math
    edges = oriented_edges(wire)
    if not edges:
        return None, []
    starts = []
    ends = []
    mids = []
    for edge, reversed_flag in edges:
        start, middle, end = edge_ends(edge, reversed_flag)
        starts.append((getattr(start, "XYZ"[u_index]), getattr(start, "XYZ"[v_index])))
        ends.append((getattr(end, "XYZ"[u_index]), getattr(end, "XYZ"[v_index])))
        mids.append((getattr(middle, "XYZ"[u_index]), getattr(middle, "XYZ"[v_index])))
    start = [r(starts[0][0]), r(starts[0][1])]
    segments = []
    for index, (edge, reversed_flag) in enumerate(edges):
        kind = str(edge.geom_type).split(".")[-1]
        eu, ev = r(ends[index][0]), r(ends[index][1])
        if _math.dist(starts[index], ends[index]) < 5e-4:
            # A tick shorter than the printed resolution contributes nothing and
            # would be a zero-length edge for the modeler.
            continue
        if kind == "LINE":
            segments.append(["line", eu, ev])
            continue
        if kind != "CIRCLE" or base.edge_radius(edge) is None:
            # A conic (ellipse/hyperbola) or spline is a side feature's cut
            # through this layer or a blend; chain short chords so the loop
            # follows the curve instead of cutting across it.
            samples = edge_polyline(edge, reversed_flag, u_index, v_index)
            for (su_, sv_), (eu_, ev_) in zip(samples, samples[1:]):
                segments.append(["line", r(eu_), r(ev_)])
            continue
        radius = base.edge_radius(edge)
        if radius is None:
            return None, []
        radius = abs(radius)
        centre = edge.arc_center
        cx = getattr(centre, "XYZ"[u_index])
        cy = getattr(centre, "XYZ"[v_index])
        su, sv = r(starts[index][0]), r(starts[index][1])
        a0 = _math.atan2(sv - cy, su - cx)
        a1 = _math.atan2(ev - cy, eu - cx)
        ccw = (a1 - a0) % (2 * _math.pi)
        candidates = []
        for clockwise, sweep in ((False, ccw), (True, ccw - 2 * _math.pi)):
            mid_angle = a0 + sweep / 2
            mid = (cx + radius * _math.cos(mid_angle), cy + radius * _math.sin(mid_angle))
            distance = _math.dist(mid, mids[index])
            candidates.append((distance, clockwise, sweep))
        _, clockwise, sweep = min(candidates, key=lambda item: item[0])
        multiples = (cx, cy)
        if abs(abs(sweep) - _math.pi) <= 1e-6:
            large = False
        else:
            large = abs(sweep) > _math.pi
        segments.append(["arc", eu, ev, r(radius), clockwise, large])
    return start, segments


def cylinder_records(shape):
    records = []
    for face in shape.faces():
        kind = str(face.geom_type).split(".")[-1]
        if kind not in {"CYLINDER", "CONE", "SPHERE", "TORUS"}:
            continue
        record = {"kind": kind, "area": r(face.area, 3)}
        try:
            axis = face.axis_of_rotation
            point = axis.position
            direction = base.unit([axis.direction.X, axis.direction.Y, axis.direction.Z])
            record.update({"axisPoint": [r(point.X), r(point.Y), r(point.Z)],
                           "axisDirection": [r(v) for v in direction],
                           "span": base.axial_extent(face, axis)})
        except Exception:
            axis = None
        radius = None
        for edge in face.edges():
            if str(edge.geom_type).endswith("CIRCLE"):
                candidate = base.edge_radius(edge)
                if candidate is not None:
                    radius = candidate
                    break
        try:
            record["ends"] = base.circle_ends(face)
        except Exception:
            record["ends"] = []
        if radius is not None:
            record["radius"] = r(radius)
            if axis is not None:
                try:
                    record["side"] = base.face_side(shape, face, axis, radius)
                except Exception:
                    record["side"] = None
        if kind == "CONE":
            try:
                record["semiAngle"] = r(face.semi_angle)
            except Exception:
                record["semiAngle"] = None
        if kind == "SPHERE":
            sphere_radius = face.radius
            if sphere_radius is None:
                candidates = [base.edge_radius(edge) for edge in face.edges()
                              if str(edge.geom_type).endswith("CIRCLE")]
                candidates = [value for value in candidates if value is not None]
                sphere_radius = max(candidates) if candidates else None
            record["radius"] = None if sphere_radius is None else r(sphere_radius)
            centre = face.center()
            record["centre"] = [r(centre.X), r(centre.Y), r(centre.Z)]
        if kind == "TORUS":
            centre = face.center()
            record["centre"] = [r(centre.X), r(centre.Y), r(centre.Z)]
            try:
                record["radii"] = [r(v) for v in face.radii]
            except Exception:
                record["radii"] = None
        records.append(record)
    return records


def build_rows(shape):
    faces = list(shape.faces())
    planes = []
    for face in faces:
        if not str(face.geom_type).endswith("PLANE"):
            continue
        normal = face.normal_at()
        planes.append((face, [r(normal.X, 6), r(normal.Y, 6), r(normal.Z, 6)]))
    if not planes:
        raise SystemExit("No planar faces; layer schedule unsupported")
    main_normal = max(planes, key=lambda item: float(item[0].area))[1]
    axis_index, (u_index, v_index), u_name, v_name, sketch_plane = axis_layout(main_normal)
    # Always use the positive axis: layer offsets then match the positive
    # sketch-plane normal and are nonnegative for a part in +axis space.
    main_normal = [1.0 if i == axis_index else 0.0 for i in range(3)]

    layer_faces = []
    wall_count = 0
    angled = []
    for face, normal in planes:
        if abs(abs(normal[axis_index]) - 1) < 1e-6:
            # Both senses of the main axis are layers; the signed offset carries direction.
            layer_faces.append((face, normal))
        elif abs(normal[axis_index]) < 1e-6 and (abs(normal[u_index]) > 0.999 or abs(normal[v_index]) > 0.999):
            wall_count += 1
        else:
            angled.append((face, normal))

    profiles = {}
    profile_order = []
    layers = []
    for face, normal in layer_faces:
        d = r(plane_offset(main_normal, face.center()), 4)
        surface_normal = face.normal_at()
        normal = [r(surface_normal.X, 6), r(surface_normal.Y, 6), r(surface_normal.Z, 6)]
        outer_wire = face.outer_wire()
        inner_wires = list(face.inner_wires())
        layer = {"offset": d, "area": r(face.area, 3), "normal": normal,
                 "outer": None, "inner": []}
        for role, wire in [("outer", outer_wire)] + [("inner", item) for item in inner_wires]:
            circle = circle_of(wire)
            if circle is not None:
                centre, radius = circle
                u = r(centre[u_index], 4)
                v = r(centre[v_index], 4)
                key = ("circle", u, v, r(radius, 4))
            else:
                rows_uv = uv_rows(wire, u_index, v_index)
                key = ("wire", tuple(rows_uv))
            if key not in profiles:
                if circle is not None:
                    entry = {"kind": "CIRCLE", "u": u, "v": v, "radius": r(radius, 4),
                             "layers": [], "rows": []}
                else:
                    path = uv_path(wire, u_index, v_index)
                    entry = {"kind": "WIRE", "layers": [], "rows": rows_uv,
                             "path": path if path and path[0] is not None else None}
                profiles[key] = len(profile_order) + 1
                profile_order.append(entry)
            pid = profiles[key]
            profile_order[pid - 1]["layers"].append(d)
            if role == "outer":
                layer["outer"] = pid
            else:
                layer["inner"].append(pid)
        layers.append(layer)

    cylinders = cylinder_records(shape)
    # Attach the matching coaxial cylinder to each circle profile so a hole is
    # one row: position, diameter, side and depth.
    matched = set()
    for entry in profile_order:
        if entry["kind"] != "CIRCLE":
            continue
        for index, record in enumerate(cylinders):
            direction = record.get("axisDirection")
            if not direction or abs(direction[axis_index]) < 0.999:
                continue
            if record.get("radius") is None or abs(record["radius"] - entry["radius"]) > 1e-3:
                continue
            point = record.get("axisPoint") or [0, 0, 0]
            pu, pv = r(point[u_index], 4), r(point[v_index], 4)
            if abs(pu - entry["u"]) > 1e-3 or abs(pv - entry["v"]) > 1e-3:
                continue
            entry["side"] = record.get("side") or "hole"
            entry["span"] = record.get("span")
            entry["through"] = len(set(entry["layers"])) >= 2
            entry["matchedKind"] = record.get("kind")
            matched.add(index)
            break
        if entry.get("side"):
            continue
        # Countersinks/counterbores: the opening circle belongs to a CONE or a
        # second cylinder whose axis point may not equal the circle centre.
        for index, record in enumerate(cylinders):
            if index in matched:
                continue
            direction = record.get("axisDirection")
            if not direction or abs(direction[axis_index]) < 0.999:
                continue
            for end in record.get("ends") or []:
                if abs(end["radius"] - entry["radius"]) > 1e-3:
                    continue
                if abs(end["centre"][u_index] - entry["u"]) > 1e-3:
                    continue
                if abs(end["centre"][v_index] - entry["v"]) > 1e-3:
                    continue
                entry["side"] = record.get("side") or "hole"
                entry["span"] = record.get("span")
                entry["matchedKind"] = record.get("kind")
                if record.get("kind") == "CONE":
                    entry["halfAngle"] = record.get("semiAngle")
                entry["through"] = len(set(entry["layers"])) >= 2
                matched.add(index)
                break
            if entry.get("side"):
                break

    rows = []
    for index, layer in enumerate(layers, start=1):
        inner = " ".join(f"P{pid}" for pid in layer["inner"])
        normal = layer.get("normal") or [0, 0, 0]
        rows.append(f"F{index} D {layer['offset']} N {normal[0]} {normal[1]} {normal[2]} "
                    f"A {layer['area']} OUTER P{layer['outer']}"
                    + (f" INNER {inner}" if inner else ""))
    for pid, entry in enumerate(profile_order, start=1):
        layer_text = " ".join(f"{value:g}" for value in sorted(set(entry["layers"])))
        if entry["kind"] == "CIRCLE":
            detail = ""
            if entry.get("side"):
                detail = f" SIDE {entry['side']}"
            if entry.get("matchedKind") in {"CONE", "CYLINDER"}:
                detail += f" KIND {entry['matchedKind']}"
            if entry.get("halfAngle") is not None:
                detail += f" HALF {entry['halfAngle']}"
            if entry.get("span"):
                detail += f" SPAN {entry['span'][0]} {entry['span'][1]}"
            if entry.get("through"):
                detail += " THROUGH"
            rows.append(f"P{pid} CIRCLE {entry['u']} {entry['v']} {entry['radius']} "
                        f"LAYERS {layer_text}{detail}")
        else:
            rows.append(f"P{pid} WIRE LAYERS {layer_text}")
            rows.extend("  " + row for row in entry["rows"])
            start, segments = entry.get("path") or (None, [])
            if start is not None and all(segment[0] in {"line", "arc"} for segment in segments):
                rows.append("  START " + json.dumps(start, ensure_ascii=False, separators=(",", ":")))
                for index, segment in enumerate(segments):
                    rows.append(f"  SEG{index} " + json.dumps(
                        segment, ensure_ascii=False, separators=(",", ":")))

    chamfer_rows = []
    seen_chamfers = set()
    for record in cylinders:
        if record.get("kind") != "CONE" or record.get("semiAngle") is None:
            continue
        ends = record.get("ends") or []
        if len(ends) < 2:
            continue
        direction = record.get("axisDirection")
        point = record.get("axisPoint")
        if not direction or not point:
            continue
        values = []
        for end in ends:
            offset = sum((end["centre"][i] - point[i]) * direction[i] for i in range(3))
            values.append((end["radius"], offset, end["centre"]))
        small = min(values, key=lambda item: item[0])
        large = max(values, key=lambda item: item[0])
        size = large[0] - small[0]
        if size <= 1e-6:
            continue
        if abs(abs(record["semiAngle"]) - 45.0) > 1e-3 or abs(abs(large[1] - small[1]) - size) > 1e-2:
            chamfer_rows.append(
                f"K{len(chamfer_rows)+1} CONE_NOT_45 AX {point} DX {direction} "
                f"R1 {r(small[0])} R2 {r(large[0])} HALF {record['semiAngle']} "
                f"S1 {r(small[1])} S2 {r(large[1])}")
            continue
        # 45-degree countersink: a chamfer of `size` on the small-radius edge
        # located at the large-radius (outer) end.
        cu, cv = r(large[2][u_index], 4), r(large[2][v_index], 4)
        layer = r(sum(large[2][i] * main_normal[i] for i in range(3)), 4)
        key = (layer, r(small[0]), r(size), cu, cv)
        if key in seen_chamfers:
            continue
        seen_chamfers.add(key)
        chamfer_rows.append(
            f"K{len(chamfer_rows)+1} CHAMFER LAYER {layer} EDGE_R {r(small[0])} "
            f"SIZE {r(size)} ANGLE 45 CU {cu} CV {cv} AX {point} DX {direction}")
    rows.extend(chamfer_rows)

    side_rows = []
    for index, record in enumerate(cylinders):
        if index in matched:
            continue
        direction = record.get("axisDirection")
        if direction and abs(direction[axis_index]) >= 0.999:
            # A coaxial cylinder without a layer circle still matters (e.g. a
            # counterbore inside a pocket); keep it, marked unmatched.
            pass
        side_rows.append(f"S{len(side_rows)+1} {record['kind']} AX {record.get('axisPoint')} "
                         f"DX {direction} R {record.get('radius')} "
                         f"HALF {record.get('semiAngle')} SIDE {record.get('side')} "
                         f"SPAN {record.get('span')} C {record.get('centre')} "
                         f"RADII {record.get('radii')} AREA {record['area']}")
    rows.extend(side_rows)

    header = [
        "TEXT LAYER PRESENT - READ TEXT SPANS ONLY; DO NOT RUN OCR (MANY PAGES, OCR TIMES OUT)",
        f"UNITS MM   DATUM: MODEL ORIGIN   MAIN NORMAL (X,Y,Z) = "
        f"({main_normal[0]:g}, {main_normal[1]:g}, {main_normal[2]:g})   "
        f"LAYER SKETCH PLANE {sketch_plane}: local H = {u_name}, local V = {v_name}; "
        f"use plane \"{sketch_plane}\" and offset = layer D along +{ 'XYZ'[axis_index] }",
        f"OVERALL {r(shape.bounding_box().size.X,3)} X {r(shape.bounding_box().size.Y,3)} X "
        f"{r(shape.bounding_box().size.Z,3)}   LAYERS {len(layers)}   "
        f"PROFILES {len(profile_order)}   WALLS OMITTED {wall_count}",
        "F<id> D d N nx ny nz A area OUTER P<i> INNER P<i>...: the face normal points "
        "OUT of the material; a pocket whose bottom is this layer opens toward N.",
        "A profile on two layers is THROUGH. CIRCLES carry SIDE/SPAN/KIND when matched.",
        "EVERY LAYER'S INNER PROFILES ARE OPENINGS ON THAT LAYER: process all layers; "
        "a circle on one layer only is a blind/counterbored feature cut from that layer, "
        "and KIND CONE + HALF marks a countersink.",
        "L/A rows are human-readable; START + SEGn rows are the exact compact requirement arrays to copy.",
        "S rows are side features not parallel to the main normal.",
        "K rows give finishing edges: CHAMFER LAYER d EDGE_R r SIZE s ANGLE 45 at (CU,CV).",
    ]
    return rows, layers, profile_order, cylinders, main_normal, (u_name, v_name), wall_count, header


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    case, step_path = base.load_case(args.case)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = out_dir / f"{case['caseId']}.pdf"
    sidecar = out_dir / f"{case['caseId']}.layers.json"
    if output_pdf.exists() or sidecar.exists():
        raise SystemExit(f"Refusing to overwrite {output_pdf} or {sidecar}")

    shape = bd.import_step(str(step_path))
    box = (shape.solids()[0].bounding_box() if shape.solids() else shape.bounding_box())
    rows, layers, profiles, cylinders, main_normal, names, wall_count, header = build_rows(shape)
    chunks = [rows[i:i + ROWS_PER_SHEET] for i in range(0, len(rows), ROWS_PER_SHEET)]

    main_view = {2: "top", 1: "front", 0: "right"}[max(range(3), key=lambda i: abs(main_normal[i]))]
    annotations = [{"kind": "overall", "view": main_view}]
    # Grouped callouts for repeated through circles (count only; positions are
    # in the profile rows).
    groups = {}
    for entry in profiles:
        if entry["kind"] == "CIRCLE" and len(set(entry["layers"])) >= 2:
            groups.setdefault(entry["radius"], []).append(entry)
    for radius, items in sorted(groups.items(), key=lambda item: -len(item[1]))[:6]:
        if len(items) < 4:
            continue
        entry = items[0]
        centre = [0.0, 0.0, 0.0]
        # Callout position is only illustrative; the schedule carries exact u,v.
        centre[0] = float(entry["u"])
        centre[2 if names[1] == "Z" else 1] = float(entry["v"])
        annotations.append({"kind": "hole", "view": main_view,
                            "args": [centre, round(radius * 2, 3)],
                            "kwargs": {"thru": True, "count": len(items)}})

    sheets = [{
        "size": "A3", "scale": 0.2, "projection": "THIRD ANGLE",
        "notes": header[:4], "annotations": annotations,
    }]
    total = len(chunks)
    for index, chunk in enumerate(chunks):
        sheets.append({
            "size": "A2", "views": False, "projection": "THIRD ANGLE",
            "notes": [f"LAYER / PROFILE SCHEDULE {index + 1} OF {total} - {case['caseId']} "
                      f"- UNITS MM - MAIN NORMAL {main_normal} - TEXT LAYER, NO OCR",
                      "SEE SHEET 1 FOR THE LEGEND", *chunk],
            "annotations": [],
        })

    spec = {"sourceStep": str(step_path), "outputPdf": str(output_pdf),
            "caseId": case["caseId"], "title": f"{case['caseId']} DERIVED NOMINAL",
            "sheets": sheets}
    spec_path = out_dir / f"{case['caseId']}.spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    frozen = {
        "schema": "nist-layer-schedule/1",
        "caseId": case["caseId"],
        "sourceStep": str(step_path),
        "sourceSha256": hashlib.sha256(step_path.read_bytes()).hexdigest(),
        "volumeMm3": r(float(shape.volume), 6),
        "bbox": {"min": [r(v, 6) for v in box.min], "max": [r(v, 6) for v in box.max]},
        "mainNormal": main_normal,
        "layerCoordinates": list(names),
        "layers": layers,
        "profiles": profiles,
        "cylinders": cylinders,
        "rowCount": len(rows),
        "sheetCount": len(sheets),
        "tolerances": {"lengthMm": 0.01, "volumeMm3": 10.0},
    }
    sidecar.write_text(json.dumps(frozen, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"case": case["caseId"], "rows": len(rows), "sheets": len(sheets),
                      "layers": len(layers), "profiles": len(profiles),
                      "wallsOmitted": wall_count, "volume": frozen["volumeMm3"],
                      "spec": str(spec_path), "sidecar": str(sidecar)}, ensure_ascii=False))
    if args.dry_run:
        return 0

    def draw():
        return subprocess.run([str(CAD_PYTHON), "-m", "catia_mcp.cad_support", "draw", str(spec_path)],
                              cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                              errors="replace")

    completed = draw()
    view_fallback = False
    if completed.returncode:
        # OCCT HLR projection fails on a few vendor models (Standard_ConstructionError).
        # The schedule itself is complete; emit it without views rather than no
        # drawing at all, and record the limitation in the sidecar/provenance.
        print(completed.stdout[-1000:], file=sys.stderr)
        view_fallback = True
        sheets[0] = {"size": "A3", "views": False, "projection": "THIRD ANGLE",
                     "annotations": [],
                     "notes": ["VIEWS UNAVAILABLE FOR THIS MODEL (HLR FAILED); "
                               "THE SCHEDULE IS COMPLETE - READ IT INSTEAD", *header[:6]]}
        spec["sheets"] = sheets
        spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
        completed = draw()
    print(completed.stdout[-2000:])
    if completed.returncode:
        print(completed.stderr[-3000:], file=sys.stderr)
        return completed.returncode
    overlay = text_layer.apply(output_pdf, [sheet.get("notes", []) for sheet in sheets])
    if view_fallback:
        frozen["viewFallback"] = True
        sidecar.write_text(json.dumps(frozen, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"textLayer": overlay, "viewFallback": view_fallback}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
