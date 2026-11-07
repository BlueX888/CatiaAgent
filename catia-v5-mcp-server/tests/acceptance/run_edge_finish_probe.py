"""Live evidence for exact fresh-edge selection and native finishing formulas.

This is an API probe, not a public feature-language capability or NIST result.
"""
import argparse
import json
import math
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pythoncom
from catia_mcp.connection import CATIAConnection
from catia_mcp.drawing.feature_executor import FeatureExecutor
from catia_mcp.drawing.feature_model import compile_features
from catia_mcp.locking import automation_lock
from catia_mcp.topology import search_topology
from catia_mcp.tools.measurement import read_measurable_array
from catia_mcp.tools.part_design import PartDesignTools


def run(operation):
    directory = ROOT.parent / "work" / f"edge-finish-{operation}-{time.time_ns()}"
    directory.mkdir()
    requirements = {"schema": "catia-feature-requirements/1", "unit": "mm",
        "parameters": [{"name": n, "value": v} for n, v in [("L", 40), ("W", 30), ("T", 8)]],
        "features": [{"id": "base", "operation": "pad", "length": "T", "sketch": {
            "plane": "xy", "profiles": [{"type": "rectangle", "width": "L", "height": "W"}]}}]}
    executor = FeatureExecutor(CATIAConnection())
    report = {"operation": operation, "passed": False, "directory": str(directory)}
    try:
        plan = compile_features(requirements, None, require_evidence=False)
        executor.execute(plan)
        selection = executor.doc.Selection
        spa = executor.doc.GetWorkbench("SPAWorkbench")
        last = executor.body.Shapes.Item(executor.body.Shapes.Count)
        candidates = []
        count = search_topology(selection, last, "Edge").count
        selected = []
        try:
            for index in range(1, count + 1):
                ref = selection.Item(index).Reference
                measurable = spa.GetMeasurable(ref)
                points = read_measurable_array(executor.conn.app, measurable, "GetPointsOnCurve", 9)
                row = {"index": index, "points": points, "referenceName": str(ref.DisplayName)}
                candidates.append(row)
                if math.dist(points[3:6], [20, 0, 8]) < 1e-6:
                    selected.append(ref)
                    selected_index = index
            if len(selected) != 1:
                raise RuntimeError(f"Expected exactly one edge at [20,0,8], got {len(selected)}")
        finally:
            selection.Clear()
        report["edgeSnapshot"] = candidates
        part = executor.part
        parameter = part.Parameters.CreateDimension("FINISH_R", "LENGTH", 0)
        parameter.ValuateFromString("2mm")
        if operation == "fillet":
            feature = part.ShapeFactory.AddNewSolidEdgeFilletWithConstantRadius(selected[0], 0, 2)
            dimension = feature.Radius
        else:
            # Enum values verified from this installation's PartTypeLib.tlb.
            report["toolResult"] = PartDesignTools(executor.conn).execute("catia_chamfer", {
                "edge_name": f"Edge.{selected_index}", "length": 2, "angle": 45})
            feature = executor.body.Shapes.Item(executor.body.Shapes.Count)
            dimension = feature.Length1
        feature.Name = "finish"
        part.Relations.CreateFormula("FinishSize", "", dimension, "FINISH_R")
        rows = []
        for radius in (2, 2.2):
            parameter.ValuateFromString(f"{radius}mm")
            part.Update()
            volume = executor._volume()
            expected = 9600 - 40 * radius ** 2 * ((1 - math.pi / 4) if operation == "fillet" else .5)
            row = {"radius": radius, "volumeMm3": volume, "expectedMm3": expected,
                   "passed": abs(volume - expected) < .01}
            rows.append(row)
            assert row["passed"], row
            executor.doc.ExportData(str(directory / f"size-{radius}.step"), "stp")
        report["variants"] = rows
        parameter.ValuateFromString("2mm")
        part.Update()
        report["delivery"] = executor.save(str(directory / "probe.CATPart"))
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
        raise
    finally:
        executor.close()
        (directory / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps({k: v for k, v in report.items() if k in {"operation", "passed", "directory", "variants", "error"}}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=["fillet", "chamfer"])
    args = parser.parse_args()
    pythoncom.CoInitialize()
    try:
        with automation_lock(timeout=0):
            run(args.operation)
    finally:
        pythoncom.CoUninitialize()
