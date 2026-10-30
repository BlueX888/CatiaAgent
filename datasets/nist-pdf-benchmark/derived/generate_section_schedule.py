"""Cross-section stack schedule for prismatic NIST parts.

The solid is sampled at every interval between distinct axial levels. For each
interval the true cross-section is emitted as region rows:

    L<k> Z0 a Z1 b AREA s OUTER P<i> INNER P<i>...

The generation agent builds the part as the union of pads: pad each OUTER
profile from Z0 to Z1 on the plane at Z0, with the INNER profiles as holes in
the same sketch. This captures disconnected regions, through and blind cuts
exactly, without relying on the agent to infer pocket directions.

Levels come from every planar face normal to the main axis plus the axial
extent of non-planar faces (tori/cones/spheres are sampled inside their span so
blends are approximated by short steps). Side features (cylinders/cones not
parallel to the main axis) are listed separately as S rows.
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
import generate_layer_schedule as layer  # noqa: E402
import text_layer  # noqa: E402

ROOT = base.ROOT
REPO = base.REPO
CAD_PYTHON = base.CAD_PYTHON
ROWS_PER_SHEET = 60
r = base.r

BLEND_SAMPLES = 1


def axis_layout(normal):
    index = max(range(3), key=lambda i: abs(normal[i]))
    mapping = {2: (0, 1, "xy"), 0: (1, 2, "yz"), 1: (2, 0, "zx")}
    u_index, v_index, plane = mapping[index]
    return index, (u_index, v_index), "XYZ"[u_index], "XYZ"[v_index], plane


def axial(value, axis_point, direction):
    return sum((value[i] - axis_point[i]) * direction[i] for i in range(3))


def build_rows(shape, main_axis=None, blend_samples=BLEND_SAMPLES):
    faces = list(shape.faces())
    planes = [(f, f.normal_at()) for f in faces if str(f.geom_type).endswith("PLANE")]
    main_normal = max(planes, key=lambda item: float(item[0].area))[1]
    axis_index, (u_index, v_index), u_name, v_name, sketch_plane = axis_layout(
        [main_normal.X, main_normal.Y, main_normal.Z])
    if main_axis is not None:
        if main_axis not in (0, 1, 2):
            raise ValueError("main axis must be 0, 1 or 2")
        axis_index = main_axis
        _, (u_index, v_index), u_name, v_name, sketch_plane = axis_layout(
            [1.0 if i == axis_index else 0.0 for i in range(3)])
    direction = [1.0 if i == axis_index else 0.0 for i in range(3)]

    levels = set()
    side_features = []
    for face in faces:
        kind = str(face.geom_type).split(".")[-1]
        if kind == "PLANE":
            normal = face.normal_at()
            if abs(abs([normal.X, normal.Y, normal.Z][axis_index]) - 1) < 1e-6:
                centre = face.center()
                levels.add(r(sum(getattr(centre, "XYZ"[i]) * direction[i] for i in range(3)), 4))
            continue
        try:
            axis = face.axis_of_rotation
            point = axis.position
            face_direction = base.unit([axis.direction.X, axis.direction.Y, axis.direction.Z])
            span = base.axial_extent(face, axis)
        except Exception:
            continue
        if span is None:
            continue
        if abs(face_direction[axis_index]) < 0.999:
            radius = None
            for edge in face.edges():
                if str(edge.geom_type).endswith("CIRCLE"):
                    radius = base.edge_radius(edge)
                    if radius is not None:
                        break
            semi_angle = None
            if kind == "CONE":
                try:
                    semi_angle = r(face.semi_angle)
                except Exception:
                    semi_angle = None
            side_features.append({
                "kind": kind, "axisPoint": [r(point.X), r(point.Y), r(point.Z)],
                "axisDirection": [r(v) for v in face_direction],
                "radius": None if radius is None else r(radius),
                "span": [r(span[0]), r(span[1])],
                "semiAngle": None,
                "area": r(face.area, 3),
            })
            continue
        # Non-planar face parallel to the axis: use its axial extent as levels,
        # adding interior samples so a blend is represented by short steps.
        point_offset = sum(getattr(point, "XYZ"[i]) * direction[i] for i in range(3))
        low = r(point_offset + span[0], 4)
        high = r(point_offset + span[1], 4)
        levels.add(low)
        levels.add(high)
        if high > low:
            for sample in range(1, blend_samples):
                levels.add(r(low + (high - low) * sample / blend_samples, 4))

    box = (shape.solids()[0].bounding_box() if shape.solids() else shape.bounding_box())
    low_global = r([box.min.X, box.min.Y, box.min.Z][axis_index], 4)
    high_global = r([box.max.X, box.max.Y, box.max.Z][axis_index], 4)
    levels.add(low_global)
    levels.add(high_global)
    ordered = sorted(level for level in levels if low_global - 1e-6 <= level <= high_global + 1e-6)

    profiles = {}
    profile_order = []
    layers = []
    rows = []
    for a, b in zip(ordered, ordered[1:]):
        if b - a <= 1e-4:
            continue
        mid = (a + b) / 2.0
        plane = bd.Plane(origin=tuple(mid if i == axis_index else 0.0 for i in range(3)),
                         z_dir=tuple(direction))
        try:
            section = bd.section(shape, section_by=plane)
        except Exception:
            continue
        section_faces = list(section.faces())
        if not section_faces:
            continue
        regions = []
        face_area = 0.0
        for face in section_faces:
            wires = [("OUTER", face.outer_wire())]
            wires += [(f"INNER{i}", wire) for i, wire in enumerate(face.inner_wires(), start=1)]
            ids = []
            open_outer = False
            for role, wire in wires:
                circle = layer.circle_of(wire)
                if circle is None and not layer.wire_is_closed(wire):
                    # A tangential section leaves an open stray wire stored as
                    # an inner boundary; it bounds no area.
                    if role == "OUTER":
                        open_outer = True
                        break
                    continue
                if circle is not None:
                    centre, radius = circle
                    u = r(centre[u_index], 4)
                    v = r(centre[v_index], 4)
                    key = ("circle", u, v, r(radius, 4))
                else:
                    rows_uv = layer.uv_rows(wire, u_index, v_index)
                    key = ("wire", tuple(rows_uv))
                if key not in profiles:
                    if circle is not None:
                        entry = {"kind": "CIRCLE", "u": u, "v": v, "radius": r(radius, 4), "rows": []}
                    else:
                        path = layer.uv_path(wire, u_index, v_index)
                        entry = {"kind": "WIRE", "rows": rows_uv,
                                 "path": path if path and path[0] is not None else None}
                    profiles[key] = len(profile_order) + 1
                    profile_order.append(entry)
                ids.append((role, profiles[key]))
            if open_outer or not ids:
                continue
            regions.append(ids)
            face_area += face.area
        if regions:
            layers.append({"z0": a, "z1": b, "area": r(face_area, 3),
                           "regions": regions})

    # Merge adjacent intervals whose cross-section profiles are identical: the
    # union of pads is unchanged, but the model needs far fewer features/pages.
    merged = []
    for layer_row in layers:
        signature = tuple(tuple(tuple(region) for region in layer_row["regions"]))
        if (merged and merged[-1]["signature"] == signature
                and abs(merged[-1]["z1"] - layer_row["z0"]) < 1e-6):
            merged[-1]["z1"] = layer_row["z1"]
        else:
            merged.append({**layer_row, "signature": signature})
    layers = merged

    line_id = 0
    cuts = {}
    for layer_row in layers:
        for region in layer_row["regions"]:
            outer = next((pid for role, pid in region if role == "OUTER"), None)
            inner = [pid for role, pid in region if role != "OUTER"]
            if outer is None:
                continue
            line_id += 1
            rows.append(f"L{line_id} Z0 {layer_row['z0']} Z1 {layer_row['z1']} "
                        f"A {layer_row['area']} OUTER P{outer}")
            for pid in inner:
                cuts.setdefault(pid, []).append((layer_row["z0"], layer_row["z1"]))
    # Merge contiguous cut intervals per profile: one cut row per span keeps the
    # requirement small and mirrors how a machinist reads a through/blind hole.
    cut_rows = []
    for pid in sorted(cuts):
        spans = sorted(cuts[pid])
        merged_spans = []
        for z0, z1 in spans:
            if merged_spans and z0 <= merged_spans[-1][1] + 1e-4:
                merged_spans[-1] = (merged_spans[-1][0], max(merged_spans[-1][1], z1))
            else:
                merged_spans.append((z0, z1))
        for z0, z1 in merged_spans:
            cut_rows.append((pid, z0, z1))
    for index, (pid, z0, z1) in enumerate(cut_rows, start=1):
        rows.append(f"C{index} CUT P{pid} Z0 {z0} Z1 {z1}")

    for pid, entry in enumerate(profile_order, start=1):
        if entry["kind"] == "CIRCLE":
            rows.append(f"P{pid} CIRCLE {entry['u']} {entry['v']} {entry['radius']}")
        else:
            rows.append(f"P{pid} WIRE")
            rows.extend("  " + row for row in entry["rows"])
            start, segments = entry.get("path") or (None, [])
            if start is not None and all(segment[0] in {"line", "arc"} for segment in segments):
                rows.append("  START " + json.dumps(start, ensure_ascii=False, separators=(",", ":")))
                for index, segment in enumerate(segments):
                    rows.append(f"  SEG{index} " + json.dumps(segment, ensure_ascii=False, separators=(",", ":")))

    for index, feature in enumerate(side_features, start=1):
        rows.append(f"S{index} {feature['kind']} AX {feature['axisPoint']} "
                    f"DX {feature['axisDirection']} R {feature['radius']} "
                    f"SPAN {feature['span']} HALF {feature['semiAngle']}")

    header = [
        "TEXT LAYER PRESENT - READ TEXT SPANS ONLY; DO NOT RUN OCR (MANY PAGES, OCR TIMES OUT)",
        f"UNITS MM   MAIN NORMAL (X,Y,Z) = ({direction[0]:g}, {direction[1]:g}, {direction[2]:g})   "
        f"SKETCH PLANE {sketch_plane}: local H = {u_name}, local V = {v_name}",
        f"OVERALL {r(box.size.X,3)} X {r(box.size.Y,3)} X {r(box.size.Z,3)}   "
        f"LAYERS {len(layers)}   PROFILES {len(profile_order)}",
        "SOLID = UNION OF PADS THEN CUTS. Pad each L row's OUTER P<i> from Z0 to Z1; "
        "then cut each C row's profile P<i> from Z0 to Z1 (pocket, direction normal).",
        "L<k> Z0 a Z1 b A area OUTER P<i>   C<k> CUT P<i> Z0 a Z1 b   P<i> CIRCLE u v r | P<i> WIRE + START/SEGn",
        "WIRE START/SEGn are the exact compact requirement arrays; copy them in order.",
        "S rows are side features not parallel to the main axis (holes/bosses on side faces).",
    ]
    return (rows, layers, profile_order, side_features, direction, (u_name, v_name),
            sketch_plane, cut_rows, header)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--source-step", default=None)
    parser.add_argument("--main-axis", type=int, default=None, choices=[0, 1, 2])
    parser.add_argument("--blend-samples", type=int, default=BLEND_SAMPLES)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    case, step_path = base.load_case(args.case)
    if args.source_step:
        step_path = Path(args.source_step).resolve()
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = out_dir / f"{case['caseId']}.pdf"
    sidecar = out_dir / f"{case['caseId']}.sections.json"
    if output_pdf.exists() or sidecar.exists():
        raise SystemExit(f"Refusing to overwrite {output_pdf} or {sidecar}")

    shape = bd.import_step(str(step_path))
    (rows, layers, profiles, side_features, direction, names, plane,
     cut_rows, header) = build_rows(shape, args.main_axis, args.blend_samples)
    chunks = [rows[i:i + ROWS_PER_SHEET] for i in range(0, len(rows), ROWS_PER_SHEET)]
    sheets = [{
        "size": "A3", "views": False, "projection": "THIRD ANGLE",
        "notes": header[:6], "annotations": [],
    }]
    total = len(chunks)
    for index, chunk in enumerate(chunks):
        sheets.append({
            "size": "A2", "views": False, "projection": "THIRD ANGLE",
            "notes": [f"CROSS-SECTION STACK {index + 1} OF {total} - {case['caseId']} - "
                      f"UNITS MM - TEXT LAYER, NO OCR",
                      "SEE SHEET 1 FOR THE LEGEND", *chunk],
            "annotations": [],
        })
    spec = {"sourceStep": str(step_path), "outputPdf": str(output_pdf),
            "caseId": case["caseId"], "title": f"{case['caseId']} DERIVED NOMINAL",
            "sheets": sheets}
    spec_path = out_dir / f"{case['caseId']}.spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    box = (shape.solids()[0].bounding_box() if shape.solids() else shape.bounding_box())
    frozen = {
        "schema": "nist-section-schedule/1", "caseId": case["caseId"],
        "sourceStep": str(step_path),
        "sourceSha256": hashlib.sha256(step_path.read_bytes()).hexdigest(),
        "volumeMm3": r(float(shape.volume), 6),
        "bbox": {"min": [r(v, 6) for v in box.min], "max": [r(v, 6) for v in box.max]},
        "mainDirection": direction, "sketchPlane": plane, "layerCoordinates": list(names),
        "layers": layers,
        "cuts": [{"profile": pid, "z0": z0, "z1": z1} for pid, z0, z1 in cut_rows],
        "profiles": profiles,
        "sideFeatures": side_features,
        "rowCount": len(rows), "sheetCount": len(sheets),
    }
    sidecar.write_text(json.dumps(frozen, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"case": case["caseId"], "rows": len(rows), "sheets": len(sheets),
                      "layers": len(layers), "profiles": len(profiles),
                      "side": len(side_features), "volume": frozen["volumeMm3"]}, ensure_ascii=False))
    if args.dry_run:
        return 0
    completed = subprocess.run([str(CAD_PYTHON), "-m", "catia_mcp.cad_support", "draw", str(spec_path)],
                               cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
    print(completed.stdout[-1500:])
    if completed.returncode:
        print(completed.stderr[-2000:], file=sys.stderr)
        return completed.returncode
    overlay = text_layer.apply(output_pdf, [sheet.get("notes", []) for sheet in sheets])
    print(json.dumps({"textLayer": overlay}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
