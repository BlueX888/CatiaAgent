"""Live synthetic composition probe; creates/closes only its own new document."""
import json
import math
from pathlib import Path
import runpy
import sys
import time
import argparse
import copy

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import pythoncom
from catia_mcp.drawing.feature_model import compile_features
from catia_mcp.drawing.feature_executor import FeatureExecutor
from catia_mcp.drawing.builder import BuildError
from catia_mcp.drawing.verify import run_independent_verifier
from catia_mcp.cad_support_client import run_support
from catia_mcp.locking import automation_lock


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--plane", choices=["xy", "yz", "zx"], default="xy")
    parser.add_argument("--cad-python", help="Optional isolated OCCT interpreter for full STEP comparison")
    args = parser.parse_args()
    axes = {"xy": "xyz", "yz": "yzx", "zx": "zxy"}[args.plane]
    def world(values):
        mapped = dict(zip(axes, values))
        return [mapped[a] for a in "xyz"]
    req, manifest = runpy.run_path(str(ROOT / "tests/test_feature_model.py"))["design"].__wrapped__()
    for feature in req["features"]:
        feature["sketch"]["plane"] = args.plane
    plan = compile_features(req, manifest)
    pythoncom.CoInitialize()
    executor = FeatureExecutor()
    try:
        with automation_lock(timeout=1):
            try:
                result = executor.execute(plan)
                expected = 40 * 30 * 8 + math.pi * 6**2 * 4 - math.pi * 3**2 * 12
                actual = result["final"]["volumeMm3"]
                assert abs(actual - expected) < 0.01, (actual, expected)
                box = result["final"]["bbox"]
                for axis, length in zip(axes, [40, 30, 12]):
                    assert abs(box["min"][axis]) < 0.01, box
                    assert abs(box["max"][axis] - length) < 0.01, box
                changes = []
                current = {"L": 40, "W": 30, "T": 8, "R": 3}
                for name, value in [("L", 50), ("T", 10), ("R", 4)]:
                    executor.part.Parameters.Item(name).ValuateFromString(f"{value}mm")
                    executor.part.Update()
                    current[name] = value
                    volume = executor._volume()
                    wanted = current["L"] * current["W"] * current["T"] + math.pi * current["R"]**2 * current["T"] / 2
                    assert abs(volume - wanted) < 0.01, (name, volume, wanted)
                    box = executor._bbox()
                    assert abs(box["max"][axes[0]] - current["L"]) < 0.01
                    assert abs(box["max"][axes[2]] - 1.5 * current["T"]) < 0.01
                    changes.append({"parameter": name, "value": value, "volumeMm3": volume, "expectedMm3": wanted})
                part_path = ROOT.parent / "work" / f"feature-composition-{time.time_ns()}.CATPart"
                delivery = executor.save(str(part_path))
                expectation = {"volumeMm3": wanted,
                    "bbox": {"min": {"x": 0, "y": 0, "z": 0}, "max": dict(zip("xyz", world([50, 30, 15])))},
                    "featureCount": 3, "parameters": current,
                    "fullCylinders": [{"radius": 8, "height": 5, "centre": world([25, 15, 12.5]), "axis": world([0, 0, 1])},
                                      {"radius": 4, "height": 15, "centre": world([25, 15, 7.5]), "axis": world([0, 0, 1])}],
                    "formulas": {"base_Extent": "T", "boss_Extent": "(T / 2.0)",
                                 "bore_Extent": "(T * 2.0)", "bore_Offset": "(T * 1.5)"}}
                step_path = part_path.with_suffix(".step") if args.cad_python else None
                independent = run_independent_verifier(delivery["path"], expectation,
                                                       export_step=str(step_path) if step_path else None)
                assert independent["verdict"] == "PASS", independent
                geometry = None
                if args.cad_python:
                    final_req = copy.deepcopy(req)
                    for parameter in final_req["parameters"]:
                        parameter["value"] = current[parameter["name"]]
                    geometry = run_support("evaluate", {"requirements": final_req, "actual": str(step_path)},
                                           python=args.cad_python)
                    assert geometry["ok"] and geometry["data"]["verdict"] == "PASS", geometry
                # A reverse cut starting at the bottom points out of the solid.
                # Keep this negative case rather than silently flipping it.
                req["features"][-1]["sketch"]["offset"] = 0
                executor = FeatureExecutor()
                try:
                    executor.execute(compile_features(req, manifest))
                except BuildError as exc:
                    assert exc.stage == "bore" and "FEATURE_NO_EFFECT" in str(exc), str(exc)
                    rejected = {"stage": exc.stage, "error": str(exc), "scene": exc.scene}
                else:
                    raise AssertionError("Wrong-direction pocket was accepted")
                report = {"ok": True, "plane": args.plane, "expectedMm3": expected, "result": result,
                          "parameterChanges": changes, "noEffect": rejected,
                          "delivery": delivery, "independentVerification": independent}
                report["independentGeometry"] = geometry
                destination = ROOT.parent / "work" / f"feature-probe-{time.time_ns()}.json"
                with destination.open("x", encoding="utf-8") as output:
                    json.dump(report, output, ensure_ascii=False, indent=2)
                print(json.dumps({"ok": True, "reportPath": str(destination), "parameterChanges": changes}))
            except Exception as exc:
                print(json.dumps({"ok": False, "stage": getattr(exc, "stage", None),
                                  "scene": getattr(exc, "scene", {}), "error": str(exc)}))
                raise
            finally:
                executor.close()
    finally:
        pythoncom.CoUninitialize()


if __name__ == "__main__":
    main()
