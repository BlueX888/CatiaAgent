"""Live MCP-handler composition probe with synthetic cited drawing evidence."""
import json
from pathlib import Path
import runpy
import sys
import time
import argparse
import os

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pythoncom
from catia_mcp.connection import CATIAConnection
from catia_mcp.drawing.tools import DrawingTools


def literal_rotation_trig_case():
    req = {"schema": "catia-feature-requirements/1", "unit": "mm", "unitEvidence": ["p1:u"],
           "parameters": [{"name": n, "value": v, "type": kind, "evidence": ["p1:" + n]}
                          for n, v, kind in [("L", 30, "length"), ("R", 10, "length"),
                                             ("T", 3, "length"), ("A", 30, "angle")]],
           "features": [{"id": "shaft", "operation": "shaft", "angle": 360,
               "sketch": {"plane": "xy", "profiles": [{"type": "polygon", "points": [
                   {"x": 0, "y": 0}, {"x": "R", "y": 0},
                   {"x": "R", "y": "L - T * tan(A) + T * sin(A) * cos(A)"}, {"x": "R - T", "y": "L"},
                   {"x": 0, "y": "L"}]}]}}]}
    manifest = {"source": {"sha256": "synthetic-literal-rotation-trig"}, "pages": [{"page": 1, "spans": [
        {"id": "p1:u", "text": "UNITS mm"}, {"id": "p1:L", "text": "30"},
        {"id": "p1:R", "text": "10"}, {"id": "p1:T", "text": "3"},
        {"id": "p1:A", "text": "30 degrees"}]}]}
    return req, manifest


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rotation", action="store_true")
    parser.add_argument("--literal-rotation-trig", action="store_true")
    parser.add_argument("--polygon", action="store_true")
    parser.add_argument("--wire", action="store_true")
    parser.add_argument("--arc-sector", choices=["ccw-small", "cw-small", "ccw-large", "cw-large"])
    parser.add_argument("--cad-python", help="isolated OCCT Python (or CATIA_CAD_SUPPORT_PYTHON)")
    args = parser.parse_args()
    if args.cad_python:
        os.environ["CATIA_CAD_SUPPORT_PYTHON"] = args.cad_python
    req, manifest = runpy.run_path(str(ROOT / "tests/test_feature_model.py"))["design"].__wrapped__()
    if args.polygon:
        req, manifest = runpy.run_path(str(ROOT / "tests/test_feature_model.py"))["polygon_design"].__wrapped__()
    if args.wire:
        req, manifest = runpy.run_path(str(ROOT / "tests/test_feature_model.py"))["wire_design"].__wrapped__((req, manifest))
    if args.arc_sector:
        req, manifest = runpy.run_path(str(ROOT / "tests/test_feature_model.py"))["sector_design"](
            args.arc_sector.startswith("cw-"), args.arc_sector.endswith("large"))
    if args.rotation:
        req = {"schema": "catia-feature-requirements/1", "unit": "mm", "unitEvidence": ["p1:u"],
               "parameters": [{"name": "R", "value": 5, "evidence": ["p1:R"]},
                              {"name": "T", "value": 10, "evidence": ["p1:T"]},
                              {"name": "A", "value": 360, "type": "angle", "evidence": ["p1:A"]}],
               "features": [
                   {"id": "shaft", "operation": "shaft", "angle": "A",
                    "sketch": {"plane": "yz", "profiles": [{"type": "rectangle", "width": "R * 2", "height": "T"}]}},
                   {"id": "groove", "operation": "groove", "angle": "A", "dependsOn": ["shaft"],
                    "sketch": {"plane": "yz", "profiles": [{"type": "rectangle", "x": "R", "y": "T / 4", "width": "R", "height": "T / 2"}]}}]}
        manifest = {"source": {"sha256": "synthetic-rotation"}, "pages": [{"page": 1, "spans": [
            {"id": "p1:u", "text": "UNITS mm"}, {"id": "p1:R", "text": "5"},
            {"id": "p1:T", "text": "10"}, {"id": "p1:A", "text": "360 degrees"}]}]}
    if args.literal_rotation_trig:
        req, manifest = literal_rotation_trig_case()
    directory = ROOT.parent / "work" / f"feature-tools-{time.time_ns()}"
    directory.mkdir()
    evidence = directory / "evidence.json"
    evidence.write_text(json.dumps(manifest), encoding="utf-8")
    pythoncom.CoInitialize()
    try:
        tools = DrawingTools(CATIAConnection())
        arguments = {"requirements": req, "evidencePath": str(evidence),
                     "deliveryPath": str(directory / "composition.CATPart")}
        for name in ("catia_validate_requirements", "catia_compile_build_plan", "catia_build_part"):
            result = tools.execute(name, arguments)
            (directory / f"{name}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
            print(json.dumps({"tool": name, "ok": result["ok"], "code": result["code"], "directory": str(directory)}), flush=True)
            assert result["ok"], result
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
