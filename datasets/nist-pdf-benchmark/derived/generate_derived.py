"""Generate a candidate NIST measurement drawing from the reference STEP.

Verification-side data preparation only. The output PDF is the single input the
generation agent sees; it carries views, callouts and coordinate tables measured
from the reference model. No model scripts, feature plans or requirement JSON are
embedded. Every number in the tables is a measurement of the reference geometry.

Usage (cadgen/OCCT interpreter):
    python generate_derived.py --case FTC-09 --out-dir derived/ftc09 [--dry-run]

The candidate does not certify feature or dimension completeness. It must be
reviewed and supplemented before it can be used as an acceptance input.
The script refuses to overwrite an existing PDF/provenance pair.
"""
from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

import build123d as bd

ROOT = Path(__file__).resolve().parents[3]          # F:/CatiaAgent
REPO = ROOT / "catia-v5-mcp-server"
BENCH = ROOT / "datasets" / "nist-pdf-benchmark"
CAD_PYTHON = Path(r"F:\CatiaAgent\work\text-to-cad-env\Scripts\python.exe")
ROWS_PER_SHEET = 40

AXES = {"X": 0, "Y": 1, "Z": 2}


def load_case(case_id: str):
    manifest = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
    case = next((item for item in manifest["cases"] if item["caseId"] == case_id), None)
    if case is None:
        raise SystemExit(f"Unknown case {case_id}")
    paths = [ref["path"] for ref in case.get("referenceModels", [])
             if str(ref.get("path", "")).lower().endswith((".stp", ".step"))]
    equivalent = (case.get("geometryEquivalentReference") or {})
    if isinstance(equivalent, dict) and equivalent.get("path"):
        paths.append(equivalent["path"])
    for path in paths:
        resolved = ROOT / path
        if resolved.is_file():
            return case, resolved
    raise SystemExit(f"No STEP reference for {case_id}")


def rounded(value, digits=3):
    return round(float(value), digits)


def unit(vector):
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector] if norm else [0.0, 0.0, 0.0]


def face_radius(face):
    try:
        value = face.radius
        if value is not None:
            return float(value)
    except (AttributeError, TypeError, ValueError):
        pass
    for edge in face.edges():
        if str(edge.geom_type).endswith("CIRCLE"):
            try:
                radius = edge.radius
                if radius is not None:
                    return float(radius)
            except (AttributeError, TypeError, ValueError):
                continue
    return None


def cylinder_records(shape):
    records = []
    for face in shape.faces():
        if not str(face.geom_type).endswith("CYLINDER"):
            continue
        radius = face_radius(face)
        if radius is None:
            continue
        try:
            axis = face.axis_of_rotation
            point = axis.position
            direction = unit([axis.direction.X, axis.direction.Y, axis.direction.Z])
        except Exception:
            continue
        centre = face.center()
        # axial span from the face's own vertices
        values = []
        for vertex in face.vertices():
            point_value = (vertex.X, vertex.Y, vertex.Z)
            values.append(sum((point_value[i] - getattr(point, "XYZ"[i])) * direction[i]
                              for i in range(3)))
        if not values:
            continue
        span = max(values) - min(values)
        # material inside the radius => boss, outside => hole
        radial = [centre.X - point.X, centre.Y - point.Y, centre.Z - point.Z]
        dot = sum(radial[i] * direction[i] for i in range(3))
        radial = [radial[i] - dot * direction[i] for i in range(3)]
        length = math.sqrt(sum(value * value for value in radial))
        if length < 1e-9:
            continue
        radial = [value / length for value in radial]
        base = [getattr(point, "XYZ"[i]) + dot * direction[i] for i in range(3)]
        probe_out = [base[i] + (radius + 0.05) * radial[i] for i in range(3)]
        probe_in = [base[i] + max(radius - 0.05, 0.01) * radial[i] for i in range(3)]
        inside_out, inside_in = bool(shape.is_inside(probe_out)), bool(shape.is_inside(probe_in))
        side = "boss" if (inside_in and not inside_out) else ("hole" if (inside_out and not inside_in) else None)
        if side is None:
            continue
        records.append({
            "radius": radius, "diameter": 2 * radius, "axis": direction,
            # The face centre can lie on a curved surface, not on its axis.
            "centre": base, "axialSpan": span, "side": side,
        })
    return records


def group_cylinders(records):
    groups = {}
    for record in records:
        key = (round(record["diameter"], 3), round(record["axialSpan"], 3),
               tuple(round(value, 3) for value in record["axis"]), record["side"])
        groups.setdefault(key, []).append(record)
    result = []
    for (diameter, span, axis, side), items in groups.items():
        result.append({
            "diameter": diameter, "span": span, "axis": axis,
            "side": side, "centres": [item["centre"] for item in items],
            "count": len(items),
        })
    result.sort(key=lambda item: (item["side"], item["diameter"], item["count"]), reverse=True)
    return result


def view_for_axis(axis):
    """Named view whose viewing direction is parallel to the axis.

    ``three_views`` creates only top/front/right/iso, so both senses of an axis
    map to the same view; the projected circle or face is identical.
    """
    index = max(range(3), key=lambda i: abs(axis[i]))
    return {2: "top", 1: "front", 0: "right"}[index]


def profile_rows(shape, normal, limit_faces=2, limit_rows=90):
    """Ordered outer-wire rows for the largest planar faces normal to `normal`."""
    candidates = []
    for face in shape.faces():
        if not str(face.geom_type).endswith("PLANE"):
            continue
        actual = face.normal_at()
        if abs(abs(actual.dot(bd.Vector(*normal))) - 1) > 1e-4:
            continue
        candidates.append((face.area, face))
    candidates.sort(key=lambda item: item[0], reverse=True)
    rows = []
    for face_index, (_, face) in enumerate(candidates[:limit_faces]):
        wire = face.outer_wire()
        edges = list(wire.edges())
        try:
            edges = list(wire.order_edges())
        except Exception:
            pass
        for edge in edges[:limit_rows]:
            kind = str(edge.geom_type).split(".")[-1]
            start, end = edge.position_at(0), edge.position_at(1)
            if kind == "LINE":
                rows.append(f"F{face_index + 1}L {start.X:.3f} {start.Y:.3f} {start.Z:.3f} "
                            f"{end.X:.3f} {end.Y:.3f} {end.Z:.3f}")
            elif kind == "CIRCLE":
                centre = edge.arc_center
                radius = float(edge.radius)
                middle = edge.position_at(0.5)
                rows.append(f"F{face_index + 1}A {centre.X:.3f} {centre.Y:.3f} {centre.Z:.3f} "
                            f"{radius:.3f} {start.X:.3f} {start.Y:.3f} {start.Z:.3f} "
                            f"{middle.X:.3f} {middle.Y:.3f} {middle.Z:.3f}")
            else:
                rows.append(f"F{face_index + 1}U {edge.length:.3f}")
    return rows


def build_spec(case, step_path, output_pdf):
    imported = bd.import_step(str(step_path))
    solids = list(imported.solids())
    if len(solids) != 1:
        raise ValueError("Measurement drawing requires exactly one solid")
    # Views are rendered from the complete source STEP. Reject loose geometry
    # rather than measuring the solid but drawing construction edges as well.
    shape = solids[0]
    for member_type in ("faces", "edges", "vertices"):
        solid_members = list(getattr(shape, member_type)())
        if any(not any(member.wrapped.IsSame(solid_member.wrapped) for solid_member in solid_members)
               for member in getattr(imported, member_type)()):
            raise ValueError("Reference STEP contains non-solid geometry; use a verified pure-solid rendering STEP")
    face_counts = {kind: sum(1 for face in shape.faces() if str(face.geom_type).endswith(kind))
                   for kind in ("CYLINDER", "CONE")}
    box = shape.bounding_box()
    size = [box.size.X, box.size.Y, box.size.Z]
    centre = [(box.min.X + box.max.X) / 2, (box.min.Y + box.max.Y) / 2,
              (box.min.Z + box.max.Z) / 2]
    records = cylinder_records(shape)
    groups = group_cylinders(records)

    dominant = None
    best = -1.0
    for face in shape.faces():
        if not str(face.geom_type).endswith("PLANE"):
            continue
        normal = face.normal_at()
        area = float(face.area)
        if area > best and max(abs(normal.X), abs(normal.Y), abs(normal.Z)) > 0.999:
            best, dominant = area, normal
    normal = [round(dominant.X), round(dominant.Y), round(dominant.Z)]
    main_view = view_for_axis(normal)

    # A cylindrical face is not necessarily one complete hole or boss. Do not
    # infer feature counts, through status, or depth from its face span.
    annotations = [{"kind": "overall", "view": main_view}]

    rows = []
    rows.append(f"UNITS MM   DATUM: MODEL ORIGIN   OVERALL {size[0]:.3f} X {size[1]:.3f} X {size[2]:.3f}")
    hole_groups = [g for g in groups if g["side"] == "hole"]
    boss_groups = [g for g in groups if g["side"] == "boss"]
    rows.append(f"CYLINDRICAL FACES CLASSIFIED {len(records)} OF {face_counts['CYLINDER']}; "
                "FACES MAY BE PARTIAL OR SHARE A FEATURE")
    rows.append(f"CONICAL FACES {face_counts['CONE']}; CONICAL SURFACE DIMENSIONS NOT INCLUDED")
    hole_id = 0
    for group in hole_groups:
        for centre in group["centres"]:
            hole_id += 1
            direction = group["axis"]
            rows.append(f"F{hole_id:03d} CONCAVE AXIS POINT X{centre[0]:.3f} Y{centre[1]:.3f} Z{centre[2]:.3f} "
                        f"D{group['diameter']:.3f} AXIAL SPAN {group['span']:.3f} "
                        f"DIRECTION ({direction[0]:.3f},{direction[1]:.3f},{direction[2]:.3f})")
    boss_id = 0
    for group in boss_groups:
        for centre in group["centres"]:
            boss_id += 1
            direction = group["axis"]
            rows.append(f"F{hole_id + boss_id:03d} CONVEX AXIS POINT X{centre[0]:.3f} Y{centre[1]:.3f} Z{centre[2]:.3f} "
                        f"D{group['diameter']:.3f} AXIAL SPAN {group['span']:.3f} "
                        f"DIRECTION ({direction[0]:.3f},{direction[1]:.3f},{direction[2]:.3f})")
    profile = profile_rows(shape, normal)
    rows.append("PARTIAL PROFILE ROWS - ONLY TWO PLANAR FACE OUTER WIRES; INCOMPLETE")
    rows.append("PROFILE ROWS: L X1 Y1 Z1 X2 Y2 Z2 | A CX CY CZ R SX SY SZ MX MY MZ")
    rows.extend(profile)

    header = [f"UNITS MM   DATUM: MODEL ORIGIN   OVERALL {size[0]:.3f} X {size[1]:.3f} X {size[2]:.3f}",
              "CANDIDATE MEASUREMENTS ONLY - NOT A COMPLETE ENGINEERING DRAWING",
              "CYLINDRICAL FACE MEASUREMENTS DO NOT ESTABLISH FEATURE COUNTS OR DEPTHS"]
    chunks = [rows[index:index + ROWS_PER_SHEET] for index in range(0, len(rows), ROWS_PER_SHEET)]
    sheets = [{
        "size": "A3", "scale": 0.2, "projection": "THIRD ANGLE",
        "notes": header, "annotations": annotations,
    }]
    total = len(chunks)
    for index, chunk in enumerate(chunks):
        sheets.append({
            "size": "A3", "views": False, "projection": "THIRD ANGLE",
            "notes": [f"FACE MEASUREMENTS {index + 1} OF {total} - NOT A COMPLETE DRAWING",
                      f"UNITS MM   DATUM: MODEL ORIGIN   OVERALL "
                      f"{size[0]:.3f} X {size[1]:.3f} X {size[2]:.3f}", *chunk],
            "annotations": [],
        })
    spec = {
        "sourceStep": str(step_path),
        "outputPdf": str(output_pdf),
        "caseId": case["caseId"],
        "title": f"{case['caseId']} DERIVED NOMINAL",
        "sheets": sheets,
        "_summary": {"rows": len(rows), "sheets": len(sheets), "concaveFaces": hole_id,
                     "convexFaces": boss_id, "profileRows": len(profile), "dominantNormal": normal},
    }
    return spec


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--source-step", help="Optional reference-side STEP for rendering")
    args = parser.parse_args(argv)
    case, step_path = load_case(args.case)
    if args.source_step:
        step_path = Path(args.source_step).resolve(strict=True)
        if step_path.suffix.lower() not in {".step", ".stp"}:
            raise ValueError("source-step must be a STEP file")
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = out_dir / f"{case['caseId']}.pdf"
    spec_path = out_dir / f"{case['caseId']}.spec.json"
    if output_pdf.exists() or spec_path.exists() or output_pdf.with_suffix(".provenance.json").exists():
        raise SystemExit(f"Refusing to overwrite artifacts in {out_dir}")
    spec = build_spec(case, step_path, output_pdf)
    summary = spec.pop("_summary")
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"spec": str(spec_path), **summary}, ensure_ascii=False))
    if args.dry_run:
        return 0
    completed = subprocess.run([str(CAD_PYTHON), "-m", "catia_mcp.cad_support", "draw", str(spec_path)],
                               cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(completed.stdout[-2000:])
    if completed.returncode:
        print(completed.stderr[-3000:], file=sys.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
