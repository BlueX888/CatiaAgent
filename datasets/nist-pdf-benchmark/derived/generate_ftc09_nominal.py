"""Reference-side nominal dimension drawing, without a modeling plan.

Input measurements and provenance stay on the preparation side. Only the
resulting PDF is supplied to a generation session.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from io import BytesIO
from pathlib import Path

from reportlab.lib.colors import Color, black
from reportlab.lib.pagesizes import A3, landscape
from reportlab.pdfgen import canvas


PROFILES = ["outer", "opening", "slot1", "slot2", "slot3", "keyhole", "marking_left", "marking_right"]
TITLES = {"outer": "OUTER PERIMETER", "opening": "CENTRAL OPENING",
          "slot1": "SLOT 1", "slot2": "SLOT 2", "slot3": "SLOT 3", "keyhole": "KEYHOLE",
          "marking_left": "LEFT MARK RECESS", "marking_right": "RIGHT MARK RECESS"}
THICKNESS = 3.03784
FLOOR = 1.76784


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def generate(measurements, case, output):
    output = Path(output).resolve()
    provenance = output.with_suffix(".provenance.json")
    for target in (output, provenance):
        if target.exists() or target.is_symlink():
            raise FileExistsError(target)
    inputs = [measurements / name for name in ("detail-geometry-reference-only-v3.json",
              "cylindrical-surfaces.json", "countersink-measurements.json")]
    geometry, cylinders, sinks = [json.loads(p.read_text(encoding="utf-8")) for p in inputs]
    holes = [r for r in cylinders["groups"] if r["fullCylinder"] and r["materialSide"] == "concave"]
    if len(holes) != 29 or len(sinks["coneAxes"]) != 4:
        raise ValueError("Unexpected feature census; review source measurements")
    if abs(sinks["solidBbox"]["max"][1] - THICKNESS) > 1e-7:
        raise ValueError("Source thickness changed")
    W, H = landscape(A3)
    buffer = BytesIO()
    c = canvas.Canvas(buffer, pagesize=(W, H), pageCompression=1, invariant=1)
    c.setTitle(f"{case} derived nominal geometry G1")
    page_count = 11

    def frame(page, title):
        c.setFillColor(black)
        c.setStrokeColor(black)
        c.setLineWidth(0.7)
        c.rect(18, 18, W - 36, H - 36)
        c.setFont("Helvetica-Bold", 16)
        c.drawString(35, H - 43, f"{case}  |  {title}")
        c.setFont("Helvetica", 9)
        c.drawString(35, H - 60, "DERIVED NOMINAL GEOMETRY G1 - dimensions in mm - geometric reconstruction drawing")
        c.drawString(35, H - 75, "X right, Z up when looking from +Y; Y=0 back face, Y=3.03784 front face. Coordinates share origin O.")
        c.setFont("Helvetica", 8)
        c.drawRightString(W - 35, 31, f"G1  |  SHEET {page} OF {page_count}  |  NOT A MANUFACTURING RELEASE")

    def text(x, y, value, size=9, bold=False):
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        if c.stringWidth(value, c._fontname, size) > W - 35 - x:
            raise ValueError(f"Text does not fit: {value}")
        c.drawString(x, y, value)

    def profile_view(item, box, label=False):
        x, y, width, height = box
        xmin, zmin, xmax, zmax = item["bounds"]
        scale = min((width - 50) / (xmax - xmin), (height - 50) / (zmax - zmin))
        ox = x + (width - (xmax - xmin) * scale) / 2
        oy = y + (height - (zmax - zmin) * scale) / 2
        tx = lambda px: ox + (px - xmin) * scale
        tz = lambda pz: oy + (pz - zmin) * scale
        for edge in item["edges"]:
            points = edge["points"]
            path = c.beginPath()
            path.moveTo(tx(points[0][0]), tz(points[0][1]))
            for px, pz in points[1:]:
                path.lineTo(tx(px), tz(pz))
            c.drawPath(path)
            if label:
                c.setFillColor(Color(.05, .25, .65))
                c.setFont("Helvetica-Bold", 7)
                c.drawString(tx(edge["mid"][0]) + 3, tz(edge["mid"][1]) + 3, str(edge["id"]))
                c.setFillColor(black)
        return tx, tz, scale

    frame(1, "GENERAL VIEW AND DIMENSION BASIS")
    tx, tz, scale = profile_view(geometry["outer"], (35, 112, 485, 605))
    # All material boundaries in this view derive from the same measured solid;
    # no unattached STEP construction edges are projected.
    for name in PROFILES[1:]:
        for edge in geometry[name]["edges"]:
            points = edge["points"]
            path = c.beginPath()
            path.moveTo(tx(points[0][0]), tz(points[0][1]))
            for px, pz in points[1:]:
                path.lineTo(tx(px), tz(pz))
            c.drawPath(path)
    for hole in holes:
        c.circle(tx(hole["xMm"]), tz(hole["zMm"]), hole["radiusMm"] * scale)
    for i, sink in enumerate(sinks["coneAxes"], 1):
        c.circle(tx(sink["centerX"]), tz(sink["centerZ"]), sink["rMax"] * scale)
        c.setFont("Helvetica-Bold", 7)
        c.drawString(tx(sink["centerX"]) + 6, tz(sink["centerZ"]) + 6, f"C{i}")
    c.saveState()
    c.setDash(3, 2)
    c.line(tx(-102), tz(0), tx(102), tz(0))
    c.line(tx(0), tz(-145), tx(0), tz(145))
    c.restoreState()
    text(tx(0) + 4, tz(0) + 4, "O", 8)
    text(50, 93, "OVERALL X 190.5000   Z 279.4000   GEOMETRIC THICKNESS Y 3.03784", 9)
    for i, name in enumerate(PROFILES, 2):
        text(560, 700 - (i - 2) * 22, f"SHEET {i:02d}: {TITLES[name]}", 10, True)
    notes = ["SHEET 10: complete circular apertures, coordinate table.",
             "SHEET 11: section C-C through each corner aperture C1-C4.",
             "Profiles on sheets 3-7 and circular holes marked THRU penetrate both faces.",
             "Mark recesses on sheets 8-9 open on +Y; flat floor at Y=1.76784.",
             "Nominal geometry source: NIST FTC-09 Rev D STEP; geometric datum O",
             "is the reference coordinate origin, not a claimed manufacturing PMI datum.",
             "For this reconstruction, geometric thickness is 3.03784 mm.",
             "Original material note: nominal 0.1195 in stock = 3.03530 mm.",
             "The stock designation does not override the geometric thickness here.",
             "Numerical reconstruction checks: coordinates 0.005 mm, material 10 mm3.",
             "These are verification limits, not asserted manufacturing tolerances.",
             "Use tabulated dimensions; views are individually scaled, not to print scale."]
    for i, note in enumerate(notes):
        text(560, 505 - i * 20, note, 9)
    text(560, 217, "PART COMPLETENESS", 11, True)
    text(560, 195, "8 planar boundaries: perimeter, 5 through openings, 2 blind mark recesses.")
    text(560, 177, "29 circular apertures: 25 plain through holes + 4 countersunk through holes.")
    text(560, 159, "All nominal material boundaries are dimensioned in the following sheets.")
    c.showPage()

    for page, name in enumerate(PROFILES, 2):
        item = geometry[name]
        frame(page, TITLES[name])
        kind = "PERIMETER, Y=0 TO 3.03784" if name == "outer" else (
            "BLIND RECESS FROM +Y; FLOOR Y=1.76784; DEPTH 1.27000" if name.startswith("marking")
            else "THROUGH BOTH FACES, Y=0 TO 3.03784")
        text(40, 698, f"PROFILE {name.upper()}  |  {kind}", 10, True)
        profile_view(item, (35, 120, 510, 555), label=True)
        text(45, 100, "Edge numbers link this view to geometric coordinate dimensions.", 9)
        text(570, 680, "EDGE  TYPE       START X,Z                END X,Z", 9, True)
        text(570, 665, "ARC: R = radius; C = center; M = a point on the shown arc", 8)
        y = 642
        for edge in item["edges"]:
            a, b = edge["start"], edge["end"]
            text(570, y, f"E{edge['id']:02d} {edge['type']:<6} {a[0]:.4f} {a[1]:.4f} {b[0]:.4f} {b[1]:.4f}", 9)
            y -= 15
            if edge["type"] == "CIRCLE":
                p, m = edge["center"], edge["mid"]
                text(595, y, f"R {edge['radiusMm']:.4f} C {p[0]:.4f} {p[1]:.4f} M {m[0]:.4f} {m[1]:.4f}", 8)
                y -= 15
        if y < 65:
            raise ValueError(f"Profile table too long: {name}")
        c.showPage()

    frame(10, "CIRCULAR APERTURES - COMMON COORDINATE DATUM O")
    text(45, 698, "ALL HOLES AXIAL ALONG Y; diameter and center coordinates in mm.", 10, True)
    text(45, 680, "THRU = continuous opening Y=0 to Y=3.03784; CSK = the section on sheet 11.", 10)
    text(70, 653, "HOLE       X               Z              DIAMETER        TYPE", 11, True)
    c.line(65, 643, 865, 643)
    for i, hole in enumerate(holes, 1):
        sink_id = next((j for j, s in enumerate(sinks["coneAxes"], 1)
            if abs(s["centerX"] - hole["xMm"]) < 1e-4 and abs(s["centerZ"] - hole["zMm"]) < 1e-4), None)
        kind = f"CSK C{sink_id} - SEE SHEET 11" if sink_id else "THRU"
        text(70, 624 - (i - 1) * 18, f"H{i:02d} {hole['xMm']:12.4f} {hole['zMm']:12.4f} {hole['diameterMm']:12.4f}    {kind}", 10)
    text(900, 605, "25 PLAIN THROUGH", 10, True)
    text(900, 585, "4 COUNTERSUNK THROUGH", 10, True)
    text(900, 560, "Each row is one axis.")
    text(900, 540, "No inferred count from faces.")
    c.showPage()

    frame(11, "SECTION C-C - FOUR CORNER APERTURES")
    s = sinks["coneAxes"][0]
    cx, oy, k = 300, 395, 32
    p = lambda x, y: (cx + x * k, oy + y * k)
    for side in (-1, 1):
        points = [(side * 7, 0), (side * s["rMin"], 0), (side * s["rMin"], s["yMin"]),
                  (side * s["rMax"], THICKNESS), (side * 7, THICKNESS)]
        path = c.beginPath()
        path.moveTo(*p(*points[0]))
        for v in points[1:]:
            path.lineTo(*p(*v))
        path.close()
        c.setFillColor(Color(.88, .9, .92))
        c.drawPath(path, fill=1)
    c.setFillColor(black)
    text(120, 560, "SECTION THROUGH EACH C1-C4 AXIS; LOCAL R HORIZONTAL, Y VERTICAL", 10, True)
    for i, line in enumerate([
        f"THROUGH DIAMETER {2*s['rMin']:.4f}", f"FRONT MOUTH DIAMETER {2*s['rMax']:.4f}",
        "INCLUDED ANGLE 90 DEG", f"CYLINDRICAL RANGE Y=0 TO {s['yMin']:.5f}",
        f"CONICAL RANGE Y={s['yMin']:.5f} TO {THICKNESS:.5f}", "CONICAL DEPTH 0.76200",
        "LARGER MOUTH ON THE +Y FRONT FACE; SMALLER BORE OPENS ON Y=0."]):
        text(90, 330 - i * 23, line, 11)
    text(765, 565, "AXIS LOCATION FROM DATUM O", 11, True)
    for i, sink in enumerate(sinks["coneAxes"], 1):
        text(765, 530 - (i - 1) * 28, f"C{i} X {sink['centerX']:.4f}  Z {sink['centerZ']:.4f}", 11)
    text(765, 350, "Section applies to all four locations.", 10)
    text(765, 330, "C-C is a radial section through a Y axis.", 10)
    c.showPage()
    c.save()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as f:
        f.write(buffer.getvalue())
    provenance.write_text(json.dumps({"schema": "nist-nominal-drawing/1", "caseId": case,
        "inputMeasurements": [{"path": str(p), "sha256": digest(p)} for p in inputs],
        "pdf": {"path": str(output), "sha256": digest(output), "pages": page_count},
        "reviewStatus": "pending_pdf_readback_and_geometry_review",
        "scope": "Nominal solid geometry; PMI and manufacturing release are excluded."}, indent=2), encoding="utf-8")
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--measurements", type=Path, required=True)
    parser.add_argument("--case", choices=["FTC-09", "STC-09"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    print(generate(args.measurements, args.case, args.out))
