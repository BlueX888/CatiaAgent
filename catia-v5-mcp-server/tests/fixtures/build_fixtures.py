"""Build fixture A / B / C (and the renamed variant) on a real CATIA session.

Fixture A: 160x100x12 plate, two Dia10 through holes. Each hole is dimensioned
against its own nearest rectangle edges with a constant margin dimension, so a
length/width change moves the holes with the boundary (edge margin preserved).
Fixture B: same geometry, holes dimensioned against fixed construction axes
(positions frozen), plus one user parameter that drives nothing.
Fixture C: same plate without holes (measurement-unavailable probe).

Run:  .venv/Scripts/python.exe build_fixtures.py A|B|C|A-renamed
Output directory: $CATIA_FIXTURE_OUT or this file's directory.
Every step prints; on failure the part is left open unsaved.
"""
from __future__ import annotations

import json
import math
import os
import sys
import traceback

import pythoncom
import win32com.client

OUT_DIR = os.environ.get("CATIA_FIXTURE_OUT") or os.path.dirname(os.path.abspath(__file__))
REPORT = os.path.join(OUT_DIR, "build-report.json")

CST_DISTANCE = 1
CST_ON = 2
CST_LENGTH = 5
CST_HORIZONTALITY = 10
CST_VERTICALITY = 13
CST_RADIUS = 14


def ref(part, obj):
    return part.CreateReferenceFromObject(obj)


def tail(dimension) -> str:
    """Last two path segments of a dimension parameter, stable across reparenting."""
    return "\\".join(str(dimension.Name).split("\\")[-2:])


def vec(v):
    return v


class Builder:
    def __init__(self, app, label: str):
        self.app = app
        self.label = label
        self.log: list[str] = []
        self.doc = app.Documents.Add("Part")
        self.part = self.doc.Part
        self.body = self.part.MainBody
        self.params = self.part.Parameters
        self.sentinel: dict[str, str] = {}

    def say(self, msg: str) -> None:
        print(f"[{self.label}] {msg}", flush=True)
        self.log.append(msg)

    # ---- parameters -------------------------------------------------
    def user_length(self, name: str, value: float):
        p = self.params.CreateDimension(name, "LENGTH", 0)
        p.ValuateFromString(f"{value}mm")
        self.say(f"user param {name} = {value} mm")
        return p

    # ---- sketch -----------------------------------------------------
    def rect_sketch(self, L: float, W: float, T: float, holes, hole_formulas: bool, E: float, D: float, edge_mode: str = "edge"):
        origin = self.part.OriginElements
        plane = self.part.CreateReferenceFromObject(origin.PlaneXY)
        sketch = self.body.Sketches.Add(plane)
        self.sketch = sketch
        factory = sketch.OpenEdition()
        hw, hh = L / 2.0, W / 2.0

        b = factory.CreateLine(-hw, -hh, hw, -hh)
        r = factory.CreateLine(hw, -hh, hw, hh)
        t = factory.CreateLine(hw, hh, -hw, hh)
        le = factory.CreateLine(-hw, hh, -hw, -hh)
        self.lines = {"bottom": b, "right": r, "top": t, "left": le}

        cons = sketch.Constraints
        for line, code, name in (
            (b, CST_HORIZONTALITY, "h-bottom"),
            (t, CST_HORIZONTALITY, "h-top"),
            (le, CST_VERTICALITY, "v-left"),
            (r, CST_VERTICALITY, "v-right"),
        ):
            cons.AddMonoEltCst(code, ref(self.part, line))
            self.say(f"constraint {name}")

        for a, a_pt, b_, b_pt in (
            (b, "EndPoint", r, "StartPoint"),
            (r, "EndPoint", t, "StartPoint"),
            (t, "EndPoint", le, "StartPoint"),
            (le, "EndPoint", b, "StartPoint"),
        ):
            cons.AddBiEltCst(CST_ON, ref(self.part, getattr(a, a_pt)), ref(self.part, getattr(b_, b_pt)))
        self.say("constraint 4 corner coincidences")

        # --- centering: two fixed construction axes through the sketch origin.
        # sketch.AbsoluteAxis.Vertical is not exposed by this CATIA build, so the
        # reference lines are explicit fixed construction geometry.
        vline = factory.CreateLine(0.0, -hh - 50.0, 0.0, hh + 50.0)
        hline = factory.CreateLine(-hw - 50.0, 0.0, hw + 50.0, 0.0)
        for line in (vline, hline):
            line.Construction = True
            cons.AddMonoEltCst(0, ref(self.part, line))  # 0 = REFERENCE (fixed)
        self.say("fixed construction axes")

        d_x = cons.AddBiEltCst(CST_DISTANCE, ref(self.part, le), ref(self.part, vline))
        d_y = cons.AddBiEltCst(CST_DISTANCE, ref(self.part, b), ref(self.part, hline))
        d_x.Dimension.Value = hw
        d_y.Dimension.Value = hh
        d_l = cons.AddBiEltCst(CST_DISTANCE, ref(self.part, b.StartPoint), ref(self.part, b.EndPoint))
        d_l.Dimension.Value = L
        d_w = cons.AddBiEltCst(CST_DISTANCE, ref(self.part, r.StartPoint), ref(self.part, r.EndPoint))
        d_w.Dimension.Value = W
        self.say(f"rect dims: x={hw} y={hh} L={L} W={W}")

        # --- holes
        # A Circle2D's CenterPoint is not exposed by this CATIA build, so a hole
        # is positioned by its own min-distance to a construction line.
        # Fixture A references the rectangle's own edges (the margin survives a
        # resize because the edge moves); fixture B references the fixed axes
        # (positions are frozen, so an edge margin cannot be preserved).
        hole_dims = []
        for idx, (cx, cy) in enumerate(holes, start=1):
            circle = factory.CreateClosedCircle(cx, cy, D / 2.0)
            dim_r = cons.AddMonoEltCst(CST_RADIUS, ref(self.part, circle))
            dim_r.Dimension.Value = D / 2.0
            if edge_mode == "edge":
                x_edge = le if cx < 0 else r
                dim_x = cons.AddBiEltCst(CST_DISTANCE, ref(self.part, circle), ref(self.part, x_edge))
                dim_y = cons.AddBiEltCst(CST_DISTANCE, ref(self.part, circle), ref(self.part, t))
                dim_x.Dimension.Value = E - D / 2.0
                dim_y.Dimension.Value = E - D / 2.0
            else:
                dim_x = cons.AddBiEltCst(CST_DISTANCE, ref(self.part, circle), ref(self.part, vline))
                dim_y = cons.AddBiEltCst(CST_DISTANCE, ref(self.part, circle), ref(self.part, hline))
                dim_x.Dimension.Value = abs(cx) - D / 2.0
                dim_y.Dimension.Value = abs(cy) - D / 2.0
            hole_dims.append(
                {
                    "index": idx,
                    "center": [cx, cy],
                    "x": dim_x.Dimension,
                    "y": dim_y.Dimension,
                    "r": dim_r.Dimension,
                    "names": {
                        "x": tail(dim_x.Dimension),
                        "y": tail(dim_y.Dimension),
                        "r": tail(dim_r.Dimension),
                    },
                }
            )
            self.say(f"hole {idx} at ({cx},{cy}) r={D/2} dims={hole_dims[-1]['names']}")

        self.rect_dims = {
            "x": d_x.Dimension,
            "y": d_y.Dimension,
            "L": d_l.Dimension,
            "W": d_w.Dimension,
            "names": {
                "x": tail(d_x.Dimension),
                "y": tail(d_y.Dimension),
                "L": tail(d_l.Dimension),
                "W": tail(d_w.Dimension),
            },
        }
        sketch.CloseEdition()
        self.part.UpdateObject(sketch)
        self.say("sketch closed + updated")
        self.hole_dims = hole_dims

        pad = self.part.ShapeFactory.AddNewPad(sketch, T)
        self.pad = pad
        self.part.Update()
        self.say(f"pad created, length param = {pad.FirstLimit.Dimension.Name}")
        return sketch, pad

    # ---- formulas ---------------------------------------------------
    def find_param(self, name: str):
        matches = []
        for i in range(1, self.params.Count + 1):
            p = self.params.Item(i)
            n = str(p.Name)
            if n == name or n.endswith("\\" + name):
                matches.append(p)
        if len(matches) != 1:
            raise RuntimeError(f"cannot resolve parameter tail {name!r}: {len(matches)} matches")
        return matches[0]

    def formula(self, name: str, target, expression: str):
        rels = self.part.Relations
        try:
            rels.Remove(name)
        except Exception:
            pass
        f = rels.CreateFormula(name, "", target, expression)
        self.say(f"formula {name} on {target.Name}: {expression}")
        return f


def build(kind: str, out_path: str, param_names: dict[str, str], hole_formulas: bool):
    pythoncom.CoInitialize()
    app = win32com.client.Dispatch("CATIA.Application")
    app.Visible = True
    app.DisplayFileAlerts = False
    # Drop unsaved leftovers from earlier failed builds (this CATIA session is
    # one we launched; saved documents are never touched).
    docs = app.Documents
    for i in range(docs.Count, 0, -1):
        d = docs.Item(i)
        try:
            if not d.Saved:
                d.Close()
                print(f"[cleanup] closed unsaved {d.Name}")
        except Exception:
            pass
    b = Builder(app, kind)
    L, W, T, D, E = 160.0, 100.0, 12.0, 10.0, 15.0
    pL = b.user_length(param_names["L"], L)
    pW = b.user_length(param_names["W"], W)
    pT = b.user_length(param_names["T"], T)
    pD = b.user_length(param_names["D"], D)
    pE = b.user_length(param_names["E"], E)
    if kind == "B":
        b.user_length("UnusedParam", 42.0)

    holes = [] if kind == "C" else [(-(L / 2 - E), W / 2 - E), (L / 2 - E, W / 2 - E)]
    sketch, pad = b.rect_sketch(L, W, T, holes, hole_formulas, E, D, edge_mode="axis" if kind == "B" else "edge")

    # formulas on the rectangle dimensions
    b.formula("f_L", b.find_param(b.rect_dims["names"]["L"]), param_names["L"])
    b.formula("f_W", b.find_param(b.rect_dims["names"]["W"]), param_names["W"])
    b.formula("f_Lx", b.find_param(b.rect_dims["names"]["x"]), f"{param_names['L']}/2")
    b.formula("f_Wy", b.find_param(b.rect_dims["names"]["y"]), f"{param_names['W']}/2")

    # formulas on the pad length
    pad_len_param = b.pad.FirstLimit.Dimension
    b.formula("f_T", pad_len_param, param_names["T"])

    # holes: formula-driven positions (A) or literal (B)
    for h in b.hole_dims:
        sign = "-" if h["center"][0] < 0 else ""
        b.formula(
            f"f_hole{h['index']}_r",
            b.find_param(h["names"]["r"]),
            f"{param_names['D']}/2",
        )
        if hole_formulas:
            # The margin itself is the driven quantity; the edge reference makes
            # the position follow L/W without naming them.
            margin_expr = f"{param_names['E']}-{param_names['D']}/2"
            b.formula(f"f_hole{h['index']}_x", b.find_param(h["names"]["x"]), margin_expr)
            b.formula(f"f_hole{h['index']}_y", b.find_param(h["names"]["y"]), margin_expr)

    b.part.Update()
    b.say("part updated with formulas")

    # --- parametric proof: change L to 200 and read volume
    pL.ValuateFromString("200mm")
    b.part.Update()
    spa = b.doc.GetWorkbench("SPAWorkbench")
    meas = spa.GetMeasurable(b.part.CreateReferenceFromObject(b.body))
    vol200 = meas.Volume * 1e9
    expected = (200.0 * 100.0 - 2 * math.pi * 25.0) * 12.0
    b.say(f"parametric check: volume(L=200)={vol200:.4f} expected={expected:.4f}")
    pL.ValuateFromString("160mm")
    b.part.Update()
    vol160 = meas.Volume * 1e9
    b.say(f"restored: volume(L=160)={vol160:.4f}")

    # --- persist
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    b.doc.SaveAs(out_path)
    b.say(f"saved {out_path}")

    report = {
        "kind": kind,
        "path": out_path,
        "source_sha_placeholder": True,
        "volume_mm3_L160": vol160,
        "volume_mm3_L200": vol200,
        "expected_volume_L160": (160.0 * 100.0 - 2 * math.pi * 25.0) * 12.0,
        "expected_volume_L200": expected,
        "log": b.log,
    }
    with open(REPORT if kind == "A" else os.path.join(OUT_DIR, f"build-report-{kind}.json"), "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != "log"}, ensure_ascii=False))
    return report


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "A"
    names_ab = {"L": "L", "W": "W", "T": "T", "D": "D", "E": "E"}
    if kind == "A":
        build("A", os.path.join(OUT_DIR, "FixtureA-mounting-plate.CATPart"), names_ab, True)
    elif kind == "B":
        build("B", os.path.join(OUT_DIR, "FixtureB-fixed-holes.CATPart"), names_ab, False)
    elif kind == "A-renamed":
        build(
            "A-renamed",
            os.path.join(OUT_DIR, "FixtureA-renamed.CATPart"),
            {"L": "PlateLength", "W": "PlateWidth", "T": "PlateThickness", "D": "HoleDiameter", "E": "EdgeDistance"},
            True,
        )
    elif kind == "C":
        build("C", os.path.join(OUT_DIR, "FixtureC-no-holes.CATPart"), names_ab, False)
    else:
        raise SystemExit(f"unknown kind {kind}")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(1)
