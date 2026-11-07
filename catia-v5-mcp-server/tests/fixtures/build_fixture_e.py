"""Build Fixture E: a solid with no dimensional constraints / user parameters.

This is the capability-boundary fixture. It is a valid solid with sketch
dimensions omitted entirely, so ``driver_handles()`` is empty and
``catia_begin_edit`` must answer UNSUPPORTED_CAPABILITY instead of pretending
the model is editable.

Run: .venv/Scripts/python.exe tests/fixtures/build_fixture_e.py
Requires a running CATIA V5 instance (the script attaches to it).
"""
from __future__ import annotations

import json
import os
import sys
import traceback

import pythoncom
import win32com.client

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("CATIA_FIXTURE_OUT") or HERE
OUT = os.path.join(OUT_DIR, "FixtureE-noparam.CATPart")

CST_ON = 2
CST_HORIZONTALITY = 10
CST_VERTICALITY = 13


def ref(part, obj):
    return part.CreateReferenceFromObject(obj)


def build(app) -> dict:
    doc = app.Documents.Add("Part")
    part = doc.Part
    body = part.MainBody
    sketch = body.Sketches.Add(ref(part, part.OriginElements.PlaneXY))
    factory = sketch.OpenEdition()
    hw, hh = 80.0, 50.0
    lines = [
        factory.CreateLine(-hw, -hh, hw, -hh),
        factory.CreateLine(hw, -hh, hw, hh),
        factory.CreateLine(hw, hh, -hw, hh),
        factory.CreateLine(-hw, hh, -hw, -hh),
    ]
    cons = sketch.Constraints
    for line, code in zip(
        lines, (CST_HORIZONTALITY, CST_VERTICALITY, CST_HORIZONTALITY, CST_VERTICALITY)
    ):
        cons.AddMonoEltCst(code, ref(part, line))
    for a, a_pt, b, b_pt in (
        (lines[0], "EndPoint", lines[1], "StartPoint"),
        (lines[1], "EndPoint", lines[2], "StartPoint"),
        (lines[2], "EndPoint", lines[3], "StartPoint"),
        (lines[3], "EndPoint", lines[0], "StartPoint"),
    ):
        cons.AddBiEltCst(CST_ON, ref(part, getattr(a, a_pt)), ref(part, getattr(b, b_pt)))
    for cx, cy in ((-65.0, 35.0), (65.0, 35.0)):
        factory.CreateClosedCircle(cx, cy, 5.0)
    sketch.CloseEdition()
    part.UpdateObject(sketch)
    part.ShapeFactory.AddNewPad(sketch, 12.0)
    part.Update()
    parameters = []
    for i in range(1, part.Parameters.Count + 1):
        p = part.Parameters.Item(i)
        try:
            value = str(p.Value)
        except Exception as exc:  # non-numeric feature/system parameters
            value = f"<unreadable: {exc}>"
        parameters.append({"name": str(p.Name), "value": value})
    doc.SaveAs(OUT)
    doc.Close()
    result = {"path": OUT, "parameterCount": len(parameters), "parameters": parameters}
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
