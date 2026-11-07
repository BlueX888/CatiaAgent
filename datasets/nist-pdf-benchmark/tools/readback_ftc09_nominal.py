"""Verification-side reconstruction using only numeric text in the final PDF.

This does not supply a plan or reference data to the generation agent.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re

import build123d as bd
import pymupdf


N = r"[-+]?\d+(?:\.\d+)?"
PROFILE_NAMES = {"outer", "opening", "slot1", "slot2", "slot3", "keyhole", "marking_left", "marking_right"}


def number(pattern, text):
    found = re.findall(pattern, text)
    if len(found) != 1:
        raise ValueError(f"Expected exactly one dimension: {pattern}")
    return float(found[0])


def read_profiles(pages):
    result = {}
    for page in pages:
        header = re.search(r"^PROFILE (\w+)\s*\|\s*(.+)$", page, re.M)
        if not header:
            continue
        name, depth_text = header.group(1).lower(), header.group(2)
        if name in result or name not in PROFILE_NAMES:
            raise ValueError("Unexpected or duplicated profile")
        if name.startswith("marking"):
            floor = number(r"FLOOR Y=(" + N + r")", depth_text)
            depth = number(r"DEPTH (" + N + r")", depth_text)
            y0, y1 = floor, floor + depth
        else:
            span = re.search(r"Y=(" + N + r") TO (" + N + r")", depth_text)
            if not span or (name != "outer" and "THROUGH BOTH FACES" not in depth_text):
                raise ValueError("Unspecified profile depth")
            y0, y1 = map(float, span.groups())
        rows = []
        for line in page.splitlines():
            edge = re.fullmatch(r"E(\d+) (LINE|CIRCLE)\s+(" + N + r") (" + N + r") (" + N + r") (" + N + r")", line.strip())
            if edge:
                i, kind, x0, z0, x1, z1 = edge.groups()
                rows.append({"id": int(i), "type": kind, "start": [float(x0), float(z0)], "end": [float(x1), float(z1)]})
            elif re.fullmatch(r"R (" + N + r") C (" + N + r") (" + N + r") M (" + N + r") (" + N + r")", line.strip()):
                values = list(map(float, re.findall(N, line)))
                if not rows or rows[-1]["type"] != "CIRCLE" or "mid" in rows[-1]:
                    raise ValueError("Arc dimensions are not associated with one edge")
                radius, cx, cz, mx, mz = values
                rows[-1].update(radius=radius, center=[cx, cz], mid=[mx, mz])
        if not rows or [r["id"] for r in rows] != list(range(1, len(rows) + 1)):
            raise ValueError("Missing or duplicated geometric edge row")
        for row, following in zip(rows, rows[1:] + rows[:1]):
            if math.dist(row["end"], following["start"]) > 1e-5:
                raise ValueError("Profile endpoints do not close")
            if row["type"] == "CIRCLE":
                if "mid" not in row or any(abs(math.dist(row[k], row["center"]) - row["radius"]) > 0.0002 for k in ("start", "end", "mid")):
                    raise ValueError("Arc radius and coordinates disagree")
        if y1 <= y0:
            raise ValueError("Nonpositive profile depth")
        result[name] = {"y0": y0, "y1": y1, "edges": rows}
    if set(result) != PROFILE_NAMES:
        raise ValueError("One or more dimensioned profiles are missing")
    return result


def read_pdf(pdf):
    with pymupdf.open(pdf) as doc:
        pages = [page.get_text() for page in doc]
    if len(pages) != 11:
        raise ValueError("Expected the complete eleven-sheet drawing")
    profiles = read_profiles(pages)
    holes = []
    for line in pages[9].splitlines():
        match = re.fullmatch(r"H(\d+)\s+(" + N + r")\s+(" + N + r")\s+(" + N + r")\s+(THRU|CSK C\d - SEE SHEET 11)", line.strip())
        if match:
            i, x, z, diameter, kind = match.groups()
            holes.append({"id": int(i), "x": float(x), "z": float(z), "diameter": float(diameter), "kind": kind})
    if [r["id"] for r in holes] != list(range(1, 30)) or sum(r["kind"] == "THRU" for r in holes) != 25:
        raise ValueError("Circular aperture table is incomplete")
    section = pages[10]
    sinks = [(int(i), float(x), float(z)) for i, x, z in re.findall(r"^C(\d) X (" + N + r")\s+Z (" + N + r")$", section, re.M)]
    if [s[0] for s in sinks] != list(range(1, 5)):
        raise ValueError("Countersink axis locations are incomplete")
    bore = number(r"THROUGH DIAMETER (" + N + r")", section)
    mouth = number(r"FRONT MOUTH DIAMETER (" + N + r")", section)
    angle = number(r"INCLUDED ANGLE (" + N + r") DEG", section)
    cone_depth = number(r"CONICAL DEPTH (" + N + r")", section)
    top = profiles["outer"]["y1"]
    start = number(r"CYLINDRICAL RANGE Y=0 TO (" + N + r")", section)
    if not math.isclose(mouth - bore, 2 * cone_depth * math.tan(math.radians(angle / 2)), abs_tol=1e-5):
        raise ValueError("Countersink dimensions conflict")
    if not math.isclose(top - start, cone_depth, abs_tol=1e-5):
        raise ValueError("Section depth conflicts with geometric thickness")
    for i, x, z in sinks:
        rows = [r for r in holes if r["kind"].startswith(f"CSK C{i} ")]
        if len(rows) != 1 or not math.isclose(rows[0]["x"], x, abs_tol=1e-5) or not math.isclose(rows[0]["z"], z, abs_tol=1e-5) or not math.isclose(rows[0]["diameter"], bore, abs_tol=1e-5):
            raise ValueError("Section and hole table do not correspond")
    for name, profile in profiles.items():
        if name.startswith("marking"):
            if not math.isclose(profile["y1"], top, abs_tol=1e-5):
                raise ValueError("Recess does not open on the front face")
        elif not math.isclose(profile["y0"], profiles["outer"]["y0"], abs_tol=1e-5) or not math.isclose(profile["y1"], top, abs_tol=1e-5):
            raise ValueError("Through profile and thickness conflict")
    return {"profiles": profiles, "holes": holes, "countersinks": {"axes": sinks, "bore": bore, "mouth": mouth, "start": start, "depth": cone_depth}}


def reconstruct(spec):
    def prism(profile):
        y = profile["y0"]
        point = lambda xz: (xz[0], y, xz[1])
        edges = [bd.Edge.make_line(point(e["start"]), point(e["end"])) if e["type"] == "LINE" else
                 bd.Edge.make_three_point_arc(point(e["start"]), point(e["mid"]), point(e["end"])) for e in profile["edges"]]
        face = bd.Face(bd.Wire(edges))
        return bd.extrude(face, amount=profile["y1"] - y, dir=(0, 1, 0))
    outer = spec["profiles"]["outer"]
    body = prism(outer)
    cuts = [prism(profile) for name, profile in spec["profiles"].items() if name != "outer"]
    for hole in spec["holes"]:
        plane = bd.Plane(origin=(hole["x"], outer["y0"], hole["z"]), z_dir=(0, 1, 0))
        cuts.append(bd.Solid.make_cylinder(hole["diameter"] / 2, outer["y1"] - outer["y0"], plane=plane))
    sink = spec["countersinks"]
    for _, x, z in sink["axes"]:
        plane = bd.Plane(origin=(x, sink["start"], z), z_dir=(0, 1, 0))
        cuts.append(bd.Solid.make_cone(sink["bore"] / 2, sink["mouth"] / 2, sink["depth"], plane=plane))
    result = body.cut(*cuts)
    if len(result.solids()) != 1 or not result.solids()[0].is_valid:
        raise ValueError("PDF dimensions do not reconstruct one valid solid")
    return result.solids()[0]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()
    args.out_dir.mkdir(exist_ok=False, parents=True)
    spec = read_pdf(args.pdf)
    part = reconstruct(spec)
    step = args.out_dir / "pdf-readback.step"
    bd.export_step(part, step)
    from catia_mcp.cad_support import compare_steps
    result = compare_steps({"actual": str(step), "reference": str(args.reference),
                            "lengthToleranceMm": 0.005, "volumeToleranceMm3": 10.0})
    report = {"pdf": str(args.pdf.resolve()), "pdfSha256": hashlib.sha256(args.pdf.read_bytes()).hexdigest(),
              "readback": spec, "comparison": result,
              "scope": "PDF numeric text readback and nominal solid comparison; no Agent or PMI/manufacturing validation."}
    (args.out_dir / "review.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"verdict": result["verdict"], "checks": result["checks"], "out": str(args.out_dir)}), flush=True)
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
