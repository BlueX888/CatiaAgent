"""Build Fixture D: the plate length is driven by P1 + P2.

Two writable parameters jointly control the X dimension, so a request such as
"change the length" has no unique parameter solution. The acceptance case uses
it to prove that an invented/ambiguous handle is refused before any write.

Run: .venv/Scripts/python.exe tests/fixtures/build_fixture_d.py
Requires a running CATIA V5 instance (the script attaches to it).
"""
from __future__ import annotations

import json
import math
import os
import sys
import traceback

import pythoncom
import win32com.client

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from build_fixtures import Builder  # noqa: E402

OUT_DIR = os.environ.get("CATIA_FIXTURE_OUT") or HERE
OUT = os.path.join(OUT_DIR, "FixtureD-ambiguous.CATPart")


def build(app) -> dict:
    b = Builder(app, "D")
    L, W, T, D, E = 160.0, 100.0, 12.0, 10.0, 15.0
    p1 = b.user_length("P1", L / 2)
    b.user_length("P2", L / 2)
    b.user_length("W", W)
    b.user_length("T", T)
    b.user_length("D", D)
    b.user_length("E", E)
    holes = [(-(L / 2 - E), W / 2 - E), (L / 2 - E, W / 2 - E)]
    b.rect_sketch(L, W, T, holes, True, E, D, edge_mode="edge")
    b.formula("f_L", b.find_param(b.rect_dims["names"]["L"]), "P1 + P2")
    b.formula("f_Lx", b.find_param(b.rect_dims["names"]["x"]), "(P1 + P2)/2")
    b.formula("f_W", b.find_param(b.rect_dims["names"]["W"]), "W")
    b.formula("f_Wy", b.find_param(b.rect_dims["names"]["y"]), "W/2")
    b.formula("f_T", b.pad.FirstLimit.Dimension, "T")
    for h in b.hole_dims:
        b.formula(f"f_hole{h['index']}_r", b.find_param(h["names"]["r"]), "D/2")
        b.formula(f"f_hole{h['index']}_x", b.find_param(h["names"]["x"]), "E-D/2")
        b.formula(f"f_hole{h['index']}_y", b.find_param(h["names"]["y"]), "E-D/2")
    b.part.Update()

    spa = b.doc.GetWorkbench("SPAWorkbench")
    meas = spa.GetMeasurable(b.part.CreateReferenceFromObject(b.body))
    vol160 = meas.Volume * 1e9
    p1.ValuateFromString("90mm")
    b.part.Update()
    vol170 = meas.Volume * 1e9
    p1.ValuateFromString("80mm")
    b.part.Update()
    b.doc.SaveAs(OUT)
    result = {
        "path": OUT,
        "params": ["P1", "P2", "W", "T", "D", "E"],
        "volume_l160": vol160,
        "volume_l170": vol170,
        "expected_l160": (160 * 100 - 2 * math.pi * 25) * 12,
        "expected_l170": (170 * 100 - 2 * math.pi * 25) * 12,
    }
    print(json.dumps(result, ensure_ascii=False))
    return result


def main() -> None:
    pythoncom.CoInitialize()
    app = win32com.client.GetActiveObject("CATIA.Application")
    app.Visible = True
    app.DisplayFileAlerts = False
    for i in range(app.Documents.Count, 0, -1):
        doc = app.Documents.Item(i)
        try:
            if not doc.Saved:
                doc.Close()
        except Exception:
            pass
    build(app)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
