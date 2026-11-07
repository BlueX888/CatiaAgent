"""Extract sketches and the feature tree from a reference CATPart (data prep).

NIST FTC/CTC reference CATParts keep their original sketches and features. This
tool dumps them so the data-preparation side can build a complete, dimensioned
derived drawing without reverse-engineering surfaces:

- every sketch element: point / line / circle with 2D coordinates;
- every constraint: name, type, dimensional value;
- the feature tree: feature name, type, owning sketch, simple limit values.

The output is verification-side material and must not be given to the
generation side.

    python scripts/extract_catpart_sketches.py <part.CATPart> <out.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catia_mcp.connection import CATIAConnection  # noqa: E402

_POINT_VBS = r"""
Public Function mcp_point2d(p)
  Dim a(1)
  p.GetCoordinates a
  mcp_point2d = a(0) & "," & a(1)
End Function
"""

_CENTER_VBS = r"""
Public Function mcp_center2d(c)
  Dim a(1)
  c.GetCenter a
  mcp_center2d = a(0) & "," & a(1)
End Function
"""


def _pair(app, vbs: str, name: str, target) -> list[float] | None:
    try:
        raw = str(app.SystemService.Evaluate(vbs, 0, name, [target]))
        return [round(float(v), 6) for v in raw.strip(",").split(",")]
    except Exception:
        return None


def _element(app, element) -> dict:
    kind = getattr(element, "GeometricType", None)
    construction = False
    try:
        construction = bool(element.Construction)
    except Exception:
        pass
    record = {"name": str(element.Name), "geometricType": kind,
              "construction": construction}
    if kind == 1:
        record["kind"] = "axis"
    elif kind == 2:  # Point2D
        record["kind"] = "point"
        record["xy"] = _pair(app, _POINT_VBS, "mcp_point2d", element)
    elif kind == 3:  # Line2D
        record["kind"] = "line"
        record["start"] = _pair(app, _POINT_VBS, "mcp_point2d", element.StartPoint)
        record["end"] = _pair(app, _POINT_VBS, "mcp_point2d", element.EndPoint)
    elif kind == 5:  # Circle2D
        record["kind"] = "circle"
        record["centre"] = _pair(app, _CENTER_VBS, "mcp_center2d", element)
        try:
            record["radius"] = round(float(element.Radius), 6)
        except Exception:
            record["radius"] = None
    else:
        record["kind"] = f"type{kind}"
    return record


def _constraint(constraint) -> dict:
    record = {"name": str(constraint.Name)}
    for attr in ("Type", "Value"):
        try:
            record[attr.lower()] = getattr(constraint, attr)
        except Exception:
            pass
    try:
        record["dimensionValue"] = round(float(constraint.Dimension.Value), 6)
    except Exception:
        pass
    return record


def _feature(shape) -> dict:
    record = {"name": str(shape.Name)}
    try:
        record["type"] = str(shape.Type)
    except Exception:
        pass
    for attr in ("Diameter", "Depth", "Height"):
        try:
            record[attr.lower()] = round(float(getattr(shape, attr)), 6)
        except Exception:
            pass
    try:
        sketch = shape.Sketch
        record["sketch"] = str(sketch.Name)
    except Exception:
        pass
    return record


def extract(part_path: str) -> dict:
    path = str(Path(part_path).resolve())
    conn = CATIAConnection()
    conn.connect()
    app = conn.app
    try:
        app.DisplayFileAlerts = False
    except Exception:
        pass
    doc = app.Documents.Open(path)
    if doc is None:
        doc = app.ActiveDocument
    doc.Activate()
    part = doc.Part
    part.Update()
    body = part.MainBody
    result = {"part": path, "document": str(doc.Name), "features": [], "sketches": []}
    for index in range(1, body.Shapes.Count + 1):
        result["features"].append(_feature(body.Shapes.Item(index)))
    for index in range(1, body.Sketches.Count + 1):
        sketch = body.Sketches.Item(index)
        entry = {"index": index, "name": str(sketch.Name), "elements": [], "constraints": []}
        elements = sketch.GeometricElements
        for element_index in range(1, elements.Count + 1):
            entry["elements"].append(_element(app, elements.Item(element_index)))
        constraints = sketch.Constraints
        for constraint_index in range(1, constraints.Count + 1):
            entry["constraints"].append(_constraint(constraints.Item(constraint_index)))
        result["sketches"].append(entry)
    doc.Close()
    return result


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    result = extract(sys.argv[1])
    out = Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{result['document']}: {len(result['features'])} features, "
          f"{len(result['sketches'])} sketches -> {out}")
    for sketch in result["sketches"]:
        kinds = {}
        for element in sketch["elements"]:
            kinds[element["kind"]] = kinds.get(element["kind"], 0) + 1
        print(f"  {sketch['name']}: {kinds}, {len(sketch['constraints'])} constraints")
    return 0


if __name__ == "__main__":
    sys.exit(main())
