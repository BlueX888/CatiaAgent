"""Build Fixture F: base plate + one additive boss + one side hole on YZ.

Purpose (P1, guide section 5.2): a model where a circular profile is *not*
necessarily a hole and where one sketch is not on XY.

  * base plate 160 x 100 x 12 with two Dia10 through holes (same as Fixture A)
  * an additive cylindrical boss, Dia24 x 20 from the base plane, centred at
    the sketch origin: its circle is a measured *additive* profile
  * a Dia10 side hole whose sketch lies on PlaneYZ (world YZ), so its local
    centre (0, 6) maps to world (0, 0, 6): a measured *hole* on a non-XY sketch

User parameters: L, W, T, D, E (as Fixture A), R (boss diameter), BH (boss
height from the base plane), S (side-hole diameter).

Run:
    .venv/Scripts/python.exe tests/fixtures/build_fixture_f.py
"""
from __future__ import annotations

import json
import math
import os
import sys
import traceback

import pythoncom
import win32com.client

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from build_fixtures import CST_DISTANCE, CST_RADIUS, Builder, ref, tail  # noqa: E402

OUT_DIR = os.environ.get("CATIA_FIXTURE_OUT") or os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(OUT_DIR, "FixtureF-boss-and-side-hole.CATPart")


def build() -> dict:
    pythoncom.CoInitialize()
    app = win32com.client.Dispatch("CATIA.Application")
    app.Visible = True
    app.DisplayFileAlerts = False
    for i in range(app.Documents.Count, 0, -1):
        doc = app.Documents.Item(i)
        try:
            if not doc.Saved:
                doc.Close()
                print(f"[cleanup] closed unsaved {doc.Name}")
        except Exception:
            pass

    b = Builder(app, "F")
    L, W, T, D, E = 160.0, 100.0, 12.0, 10.0, 15.0
    R, BH, S = 24.0, 20.0, 10.0
    pL = b.user_length("L", L)
    pW = b.user_length("W", W)
    pT = b.user_length("T", T)
    pD = b.user_length("D", D)
    pE = b.user_length("E", E)
    pR = b.user_length("R", R)
    pBH = b.user_length("BH", BH)
    pS = b.user_length("S", S)

    # ---- base plate (same construction as Fixture A) ------------------
    holes = [(-(L / 2 - E), W / 2 - E), (L / 2 - E, W / 2 - E)]
    _sketch, pad = b.rect_sketch(L, W, T, holes, True, E, D, edge_mode="edge")
    b.formula("f_L", b.find_param(b.rect_dims["names"]["L"]), "L")
    b.formula("f_W", b.find_param(b.rect_dims["names"]["W"]), "W")
    b.formula("f_Lx", b.find_param(b.rect_dims["names"]["x"]), "L/2")
    b.formula("f_Wy", b.find_param(b.rect_dims["names"]["y"]), "W/2")
    b.formula("f_T", pad.FirstLimit.Dimension, "T")
    for hole in b.hole_dims:
        b.formula(f"f_hole{hole['index']}_r", b.find_param(hole["names"]["r"]), "D/2")
        b.formula(f"f_hole{hole['index']}_x", b.find_param(hole["names"]["x"]), "E-D/2")
        b.formula(f"f_hole{hole['index']}_y", b.find_param(hole["names"]["y"]), "E-D/2")

    # ---- additive boss on XY -----------------------------------------
    plane_xy = b.part.CreateReferenceFromObject(b.part.OriginElements.PlaneXY)
    boss_sketch = b.body.Sketches.Add(plane_xy)
    boss_factory = boss_sketch.OpenEdition()
    boss_circle = boss_factory.CreateClosedCircle(0.0, 0.0, R / 2.0)
    boss_radius = boss_sketch.Constraints.AddMonoEltCst(CST_RADIUS, ref(b.part, boss_circle))
    boss_radius.Dimension.Value = R / 2.0
    # Only the radius is constrained. A distance-to-axis constraint measures the
    # circle's *minimum* distance to the line (踩坑 54), so value 0 would make the
    # circle tangent and move its centre whenever the radius changes. Leaving the
    # centre at the sketch origin keeps "diameter change" a pure diameter change.
    boss_sketch.CloseEdition()
    b.part.UpdateObject(boss_sketch)
    boss_pad = b.part.ShapeFactory.AddNewPad(boss_sketch, BH)
    b.part.Update()
    b.formula("f_boss_r", boss_radius.Dimension, "R/2")
    b.formula("f_boss_h", boss_pad.FirstLimit.Dimension, "BH")
    b.say(f"boss pad created: r dim={boss_radius.Dimension.Name}, h dim={boss_pad.FirstLimit.Dimension.Name}")

    # ---- side hole on PlaneYZ (non-XY sketch) ------------------------
    plane_yz = b.part.CreateReferenceFromObject(b.part.OriginElements.PlaneYZ)
    side_sketch = b.body.Sketches.Add(plane_yz)
    side_factory = side_sketch.OpenEdition()
    # Local (0, 6) on PlaneYZ = world (x=0, y=0, z=6).
    side_circle = side_factory.CreateClosedCircle(0.0, 6.0, S / 2.0)
    side_radius = side_sketch.Constraints.AddMonoEltCst(CST_RADIUS, ref(b.part, side_circle))
    side_radius.Dimension.Value = S / 2.0
    side_sketch.CloseEdition()
    b.part.UpdateObject(side_sketch)
    pocket = b.part.ShapeFactory.AddNewPocket(side_sketch, 200.0)
    symmetric = None
    for attribute, value in (("IsSymmetric", True),):
        try:
            setattr(pocket, attribute, value)
            symmetric = f"{attribute}={value}"
            break
        except Exception as exc:  # noqa: BLE001 - log the CATIA refusal
            b.say(f"pocket.{attribute} failed: {type(exc).__name__}: {exc}")
    b.part.Update()
    b.formula("f_side_r", side_radius.Dimension, "S/2")
    b.say(f"side pocket created: r dim={side_radius.Dimension.Name}, symmetric={symmetric}")

    b.part.Update()
    spa = b.doc.GetWorkbench("SPAWorkbench")
    meas = spa.GetMeasurable(b.part.CreateReferenceFromObject(b.body))
    volume = meas.Volume * 1e9
    # Closed form: plate + boss above the plate - side hole through the plate.
    expected = (
        (L * W - 2 * math.pi * (D / 2) ** 2) * T
        + math.pi * (R / 2) ** 2 * (BH - T)
        - math.pi * (S / 2) ** 2 * L
    )
    b.say(f"volume={volume:.4f} expected={expected:.4f} delta={volume - expected:.4f}")

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    b.doc.SaveAs(OUT_PATH)
    b.say(f"saved {OUT_PATH}")

    report = {
        "kind": "F",
        "path": OUT_PATH,
        "volume_mm3": volume,
        "expected_volume_mm3": expected,
        "symmetric": symmetric,
        "parameters": {"L": L, "W": W, "T": T, "D": D, "E": E, "R": R, "BH": BH, "S": S},
        "log": b.log,
    }
    with open(os.path.join(OUT_DIR, "build-report-F.json"), "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != "log"}, ensure_ascii=False))
    return report


if __name__ == "__main__":
    try:
        build()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
