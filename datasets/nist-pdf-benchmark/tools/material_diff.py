"""Locate missing/excess material between a delivery and a reference STEP.

Runs the two boolean differences (reference - actual, actual - reference) and
reports each connected difference volume with its bounding box, so a mismatch
can be traced to a feature instead of only a scalar volume.

    python material_diff.py --actual delivery.step --reference reference.step --out diff.json
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import build123d as bd


def shapes_of(shape):
    return list(shape.solids())


def describe(shape):
    if shape is None:
        return []
    result = []
    for solid in shapes_of(shape):
        volume = float(solid.volume)
        if not math.isfinite(volume) or volume <= 1e-9:
            continue
        box = solid.bounding_box()
        low = [box.min.X, box.min.Y, box.min.Z]
        high = [box.max.X, box.max.Y, box.max.Z]
        result.append({
            "volumeMm3": round(volume, 6),
            "bbox": {"min": [round(v, 4) for v in low],
                     "max": [round(v, 4) for v in high]},
            "centre": [round((low[i] + high[i]) / 2, 4) for i in range(3)],
        })
    result.sort(key=lambda item: item["volumeMm3"], reverse=True)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--actual", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args(argv)
    actual = bd.import_step(str(Path(args.actual).resolve()))
    reference = bd.import_step(str(Path(args.reference).resolve()))
    a, b = actual.solids()[0], reference.solids()[0]
    missing = b - a
    excess = a - b
    report = {
        "schema": "nist-material-diff/1",
        "actual": str(Path(args.actual).resolve()),
        "reference": str(Path(args.reference).resolve()),
        "actualVolumeMm3": round(float(a.volume), 6),
        "referenceVolumeMm3": round(float(b.volume), 6),
        "missing": describe(missing),
        "excess": describe(excess),
        "missingTotalMm3": round(sum(item["volumeMm3"] for item in describe(missing)), 6),
        "excessTotalMm3": round(sum(item["volumeMm3"] for item in describe(excess)), 6),
    }
    Path(args.out).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({key: report[key] for key in
                      ("actualVolumeMm3", "referenceVolumeMm3", "missingTotalMm3", "excessTotalMm3")},
                     ensure_ascii=False))
    for label in ("missing", "excess"):
        for item in report[label][:8]:
            print(label, item["volumeMm3"], item["centre"], item["bbox"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
