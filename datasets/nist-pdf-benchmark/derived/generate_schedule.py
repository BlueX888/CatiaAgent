"""Generate a complete derived drawing + feature schedule from a reference STEP.

Verification-side data preparation. The PDF is the only generation input; it
carries orthographic views on the first sheet and an ordinate/feature schedule
on the following sheets. Every face of the reference solid is represented:

* PLANE faces: normal, offset ``n.p = d``, area, every outer/inner wire with
  ordered line and arc segments.
* CYLINDER faces: axis point/direction, radius, axial span, material side
  (hole/boss), boundary circles.
* CONE faces: axis, half angle, boundary circle radii/positions, side.
* SPHERE faces: centre, radius.
* TORUS faces: centre, axis, major/minor radius.
* Other surfaces: boundary wires plus a marker; the schedule is explicit about
  what it cannot parameterise.

The sidecar JSON keeps the same data plus frozen expectations (volume, bbox,
face/cylinder inventory) for the independent verifier. It never enters the
generation workspace.

Usage:
    python generate_schedule.py --case FTC-09 --out-dir derived/FTC-09
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

ROOT = Path(__file__).resolve().parents[3]
REPO = ROOT / "catia-v5-mcp-server"
BENCH = ROOT / "datasets" / "nist-pdf-benchmark"
CAD_PYTHON = Path(r"F:\CatiaAgent\work\text-to-cad-env\Scripts\python.exe")
ROWS_PER_SHEET = 34
SHOW_VIEW_SHEET = True


def r(value, digits=4):
    return round(float(value), digits)


def unit(vector):
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else [0.0, 0.0, 0.0]


def load_case(case_id: str):
    manifest = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
    case = next((item for item in manifest["cases"] if item["caseId"] == case_id), None)
    if case is None:
        raise SystemExit(f"Unknown case {case_id}")
    candidates = [ref["path"] for ref in case.get("referenceModels", [])
                  if str(ref.get("path", "")).lower().endswith((".stp", ".step"))]
    equivalent = case.get("geometryEquivalentReference") or {}
    if isinstance(equivalent, dict) and equivalent.get("path"):
        candidates.append(equivalent["path"])
    for path in candidates:
        resolved = ROOT / path
        if resolved.is_file():
            return case, resolved
    raise SystemExit(f"No STEP reference for {case_id}")


def edge_radius(edge):
    try:
        value = edge.radius
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def wire_rows(wire, limit=400):
    edges = list(wire.edges())
    try:
        ordered = list(wire.order_edges())
        if len(ordered) == len(edges):
            edges = ordered
    except Exception:
        pass
    rows = []
    for edge in edges[:limit]:
        kind = str(edge.geom_type).split(".")[-1]
        start, end = edge.position_at(0), edge.position_at(1)
        if kind == "LINE":
            rows.append(f"L {r(start.X)} {r(start.Y)} {r(start.Z)} "
                        f"{r(end.X)} {r(end.Y)} {r(end.Z)}")
        elif kind == "CIRCLE":
            centre = edge.arc_center
            radius = edge_radius(edge)
            middle = edge.position_at(0.5)
            rows.append(f"A {r(centre.X)} {r(centre.Y)} {r(centre.Z)} {r(radius)} "
                        f"{r(start.X)} {r(start.Y)} {r(start.Z)} "
                        f"{r(middle.X)} {r(middle.Y)} {r(middle.Z)}")
        else:
            rows.append(f"U {kind} {r(start.X)} {r(start.Y)} {r(start.Z)} "
                        f"{r(end.X)} {r(end.Y)} {r(end.Z)} L {r(edge.length)}")
    if len(edges) > limit:
        rows.append(f"X TRUNCATED {len(edges) - limit} MORE EDGES")
    return rows


def face_side(shape, face, axis, radius):
    """Material inside the surface radius => boss, outside => hole."""
    if radius is None:
        return None
    centre = face.center()
    point = axis.position
    direction = unit([axis.direction.X, axis.direction.Y, axis.direction.Z])
    radial = [centre.X - point.X, centre.Y - point.Y, centre.Z - point.Z]
    dot = sum(radial[i] * direction[i] for i in range(3))
    radial = [radial[i] - dot * direction[i] for i in range(3)]
    length = math.sqrt(sum(value * value for value in radial))
    if length < 1e-9:
        return None
    radial = [value / length for value in radial]
    base = [getattr(point, "XYZ"[i]) + dot * direction[i] for i in range(3)]
    probe_out = [base[i] + (radius + 0.05) * radial[i] for i in range(3)]
    probe_in = [base[i] + max(radius - 0.05, 0.01) * radial[i] for i in range(3)]
    inside_out = bool(shape.is_inside(probe_out))
    inside_in = bool(shape.is_inside(probe_in))
    if inside_in and not inside_out:
        return "boss"
    if inside_out and not inside_in:
        return "hole"
    return None


def axial_extent(face, axis):
    point = axis.position
    direction = unit([axis.direction.X, axis.direction.Y, axis.direction.Z])
    values = []
    for vertex in face.vertices():
        values.append((vertex.X - point.X) * direction[0]
                      + (vertex.Y - point.Y) * direction[1]
                      + (vertex.Z - point.Z) * direction[2])
    return ([r(min(values)), r(max(values))] if values else None)


def circle_ends(face):
    circles = []
    for edge in face.edges():
        if not str(edge.geom_type).endswith("CIRCLE"):
            continue
        radius = edge_radius(edge)
        if radius is None:
            continue
        centre = edge.arc_center
        circles.append({"centre": [r(centre.X), r(centre.Y), r(centre.Z)],
                        "radius": r(radius)})
    return circles


def build_schedule(shape):
    faces = list(shape.faces())
    rows = []
    records = []
    counts = {}
    for index, face in enumerate(faces, start=1):
        kind = str(face.geom_type).split(".")[-1]
        counts[kind] = counts.get(kind, 0) + 1
        area = r(face.area, 3)
        record = {"id": index, "kind": kind, "area": area}
        rows.append(f"F{index} {kind} A {area}")
        if kind == "PLANE":
            normal = face.normal_at()
            centre = face.center()
            n = [r(normal.X), r(normal.Y), r(normal.Z)]
            d = r(n[0] * centre.X + n[1] * centre.Y + n[2] * centre.Z)
            record.update({"normal": n, "offset": d})
            rows.append(f"  N {n[0]} {n[1]} {n[2]} D {d}")
            wires = [("OUTER", face.outer_wire())]
            wires += [(f"INNER{k}", wire) for k, wire in enumerate(face.inner_wires(), start=1)]
            record["wires"] = []
            for name, wire in wires:
                rows.append(f"  {name}")
                wire_rows_data = wire_rows(wire)
                rows.extend("  " + row for row in wire_rows_data)
                record["wires"].append({"name": name, "rows": wire_rows_data})
        elif kind in {"CYLINDER", "CONE"}:
            axis = face.axis_of_rotation
            point = axis.position
            direction = unit([axis.direction.X, axis.direction.Y, axis.direction.Z])
            radius = None
            for edge in face.edges():
                if str(edge.geom_type).endswith("CIRCLE"):
                    candidate = edge_radius(edge)
                    if candidate is not None:
                        radius = candidate
                        break
            side = face_side(shape, face, axis, radius)
            span = axial_extent(face, axis)
            ends = circle_ends(face)
            record.update({"axisPoint": [r(point.X), r(point.Y), r(point.Z)],
                           "axisDirection": [r(v) for v in direction],
                           "side": side, "axialSpan": span, "ends": ends})
            if kind == "CONE":
                try:
                    semi_angle = r(face.semi_angle)
                except Exception:
                    semi_angle = None
                record["semiAngle"] = semi_angle
                rows.append(f"  AX {r(point.X)} {r(point.Y)} {r(point.Z)} "
                            f"DX {r(direction[0])} {r(direction[1])} {r(direction[2])} "
                            f"HALF {semi_angle} SIDE {side} SPAN {span}")
            else:
                record["radius"] = r(radius) if radius is not None else None
                rows.append(f"  AX {r(point.X)} {r(point.Y)} {r(point.Z)} "
                            f"DX {r(direction[0])} {r(direction[1])} {r(direction[2])} "
                            f"R {r(radius) if radius is not None else 'NONE'} "
                            f"SIDE {side} SPAN {span}")
            for end in ends:
                dee = r(sum(end["centre"][i] * direction[i] for i in range(3)))
                rows.append(f"  END D {dee} R {end['radius']} C "
                            f"{end['centre'][0]} {end['centre'][1]} {end['centre'][2]}")
        elif kind == "SPHERE":
            centre = face.center()
            record.update({"centre": [r(centre.X), r(centre.Y), r(centre.Z)],
                           "radius": r(face.radius)})
            rows.append(f"  C {r(centre.X)} {r(centre.Y)} {r(centre.Z)} R {r(face.radius)}")
        elif kind == "TORUS":
            centre = face.center()
            axis = face.axis_of_rotation
            direction = unit([axis.direction.X, axis.direction.Y, axis.direction.Z])
            try:
                major, minor = [r(value) for value in face.radii]
            except Exception:
                major, minor = None, None
            record.update({"centre": [r(centre.X), r(centre.Y), r(centre.Z)],
                           "axisDirection": [r(v) for v in direction],
                           "major": major, "minor": minor})
            rows.append(f"  C {r(centre.X)} {r(centre.Y)} {r(centre.Z)} "
                        f"DX {r(direction[0])} {r(direction[1])} {r(direction[2])} "
                        f"MAJ {major if major is not None else 'NONE'} "
                        f"MIN {minor if minor is not None else 'NONE'}")
        else:
            record["unsupported"] = kind
        if kind not in {"PLANE", "CYLINDER", "CONE"}:
            # Boundaries are listed for every non-plane face type so the
            # schedule still locates the surface when parameters are absent.
            for k, wire in enumerate(face.wires(), start=1):
                rows.append(f"  WIRE{k}")
                wire_rows_data = wire_rows(wire)
                rows.extend("  " + row for row in wire_rows_data)
                record.setdefault("wires", []).append({"name": f"WIRE{k}", "rows": wire_rows_data})
        records.append(record)
    return rows, records, counts


def choose_view(records):
    best = None
    for record in records:
        if record["kind"] != "PLANE" or "normal" not in record:
            continue
        normal = record["normal"]
        if max(abs(v) for v in normal) < 0.999:
            continue
        if best is None or record["area"] > best["area"]:
            best = record
    if best is None:
        return "front", [0, -1, 0]
    normal = best["normal"]
    index = max(range(3), key=lambda i: abs(normal[i]))
    return {2: "top", 1: "front", 0: "right"}[index], normal


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True)
    parser.add_argument("--out-dir", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    case, step_path = load_case(args.case)
    out_dir = Path(args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    output_pdf = out_dir / f"{case['caseId']}.pdf"
    sidecar = out_dir / f"{case['caseId']}.schedule.json"
    if output_pdf.exists() or sidecar.exists():
        raise SystemExit(f"Refusing to overwrite {output_pdf} or {sidecar}")

    shape = bd.import_step(str(step_path))
    box = (shape.solids()[0].bounding_box() if shape.solids() else shape.bounding_box())
    size = [r(box.size.X, 3), r(box.size.Y, 3), r(box.size.Z, 3)]
    rows, records, counts = build_schedule(shape)
    main_view, normal = choose_view(records)

    header = [
        "TEXT LAYER PRESENT - READ TEXT SPANS ONLY; DO NOT RUN OCR (MANY PAGES, OCR TIMES OUT)",
        "UNITS MM   DATUM: MODEL ORIGIN   MODEL COORDINATES, NOT VIEW COORDINATES",
        f"OVERALL {size[0]} X {size[1]} X {size[2]}   FACES {len(records)}   "
        f"SURFACES {', '.join(f'{k}:{v}' for k, v in sorted(counts.items()))}",
        "PLANE: N nx ny nz D d means n.p=d; wires are ordered around +N.",
        "ARC: A cx cy cz R r sx sy sz mx my mz = start and mid points on the arc.",
        "CYL AX x y z DX dx dy dz R r SIDE hole|boss SPAN a b; END D d R r C cx cy cz.",
        "CONE AX ... HALF deg ...; SPHERE C x y z R r; TORUS C ... MAJ m MIN n.",
        "ROWS BEGIN",
    ]
    chunks = [rows[i:i + ROWS_PER_SHEET] for i in range(0, len(rows), ROWS_PER_SHEET)]

    annotations = [{"kind": "overall", "view": main_view}]
    # Grouped callouts: repeated cylindrical holes only, to keep the view clean.
    groups = {}
    for record in records:
        if record["kind"] == "CYLINDER" and record.get("side") == "hole" and record.get("radius"):
            key = (round(record["radius"], 3), tuple(record.get("axisDirection") or []))
            groups.setdefault(key, []).append(record)
    for (radius, direction), items in sorted(groups.items(), key=lambda item: -len(item[1]))[:8]:
        if len(items) < 3 or len(direction) != 3:
            continue
        axis_index = max(range(3), key=lambda i: abs(direction[i]))
        view = {2: "top", 1: "front", 0: "right"}[axis_index]
        centre = items[0]["axisPoint"]
        annotations.append({"kind": "hole", "view": view, "args": [centre, round(radius * 2, 3)],
                            "kwargs": {"thru": True, "count": len(items)}})

    sheets = [{
        "size": "A3", "scale": 0.2, "projection": "THIRD ANGLE",
        "notes": header[:4], "annotations": annotations,
    }]
    total = len(chunks)
    for index, chunk in enumerate(chunks):
        sheets.append({
            "size": "A3", "views": False, "projection": "THIRD ANGLE",
            "notes": [f"ORDINATE / FEATURE SCHEDULE {index + 1} OF {total} "
                      f"- {case['caseId']} - UNITS MM - DATUM: MODEL ORIGIN - TEXT LAYER, NO OCR",
                      "SEE SHEET 1 FOR THE LEGEND AND VIEWS", *chunk],
            "annotations": [],
        })

    spec = {
        "sourceStep": str(step_path),
        "outputPdf": str(output_pdf),
        "caseId": case["caseId"],
        "title": f"{case['caseId']} DERIVED NOMINAL",
        "sheets": sheets,
    }
    spec_path = out_dir / f"{case['caseId']}.spec.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")

    volume = float(shape.volume)
    frozen = {
        "schema": "nist-derived-schedule/1",
        "caseId": case["caseId"],
        "sourceStep": str(step_path),
        "sourceSha256": hashlib.sha256(step_path.read_bytes()).hexdigest(),
        "volumeMm3": r(volume, 6),
        "bbox": {"min": [r(v, 6) for v in box.min], "max": [r(v, 6) for v in box.max]},
        "surfaceCounts": counts,
        "faceCount": len(records),
        "rowCount": len(rows),
        "sheetCount": len(sheets),
        "records": records,
        "tolerances": {"lengthMm": 0.01, "volumeMm3": 0.01},
    }
    sidecar.write_text(json.dumps(frozen, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"case": case["caseId"], "rows": len(rows), "sheets": len(sheets),
                      "faces": len(records), "counts": counts, "volume": frozen["volumeMm3"],
                      "spec": str(spec_path), "sidecar": str(sidecar)}, ensure_ascii=False))
    if args.dry_run:
        return 0
    completed = subprocess.run([str(CAD_PYTHON), "-m", "catia_mcp.cad_support", "draw", str(spec_path)],
                               cwd=REPO, capture_output=True, text=True, encoding="utf-8",
                               errors="replace")
    print(completed.stdout[-2000:])
    if completed.returncode:
        print(completed.stderr[-3000:], file=sys.stderr)
        return completed.returncode
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import text_layer
    overlay = text_layer.apply(output_pdf, [sheet.get("notes", []) for sheet in sheets])
    print(json.dumps({"textLayer": overlay}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
