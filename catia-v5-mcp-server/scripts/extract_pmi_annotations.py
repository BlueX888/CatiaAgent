"""Extract PMI annotations from a reference CATPart (verification-side tool).

The NIST FTC/CTC reference CATParts carry Functional Tolerancing & Annotation
sets. Their nominal dimensions are the authoritative values the 2D sheet refers
to but does not spell out. This tool reads them without modifying the file:

- dimension annotations: ``Dimension3D().Get2dAnnot().GetValue().Value``
- text annotations: ``Text().Text``
- every annotation also reports ``Type`` / ``SuperType``

It is a data-preparation tool. Its output is verification-side material and must
not be given to the generation side.

    python scripts/extract_pmi_annotations.py <part.CATPart> <out.json>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catia_mcp.connection import CATIAConnection  # noqa: E402


def _dimension_value(annotation):
    try:
        return round(float(annotation.Dimension3D().Get2dAnnot().GetValue().Value), 6)
    except Exception:
        return None


def _annotation_text(annotation):
    try:
        text = annotation.Text()
        value = getattr(text, "Text", None)
        if callable(value):
            value = value()
        return str(value) if value is not None else None
    except Exception:
        return None


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
    result = {
        "part": path,
        "document": str(doc.Name),
        "annotationSets": [],
    }
    try:
        sets = part.AnnotationSets
    except Exception as exc:
        result["error"] = f"no annotation sets: {exc}"
        sets = None
    if sets is not None:
        for set_index in range(1, sets.Count + 1):
            annotation_set = sets.Item(set_index)
            if annotation_set is None:
                result["annotationSets"].append({"index": set_index, "error": "None annotation set"})
                continue
            entry = {"name": str(annotation_set.Name), "annotations": []}
            annotations = annotation_set.Annotations
            for index in range(1, annotations.Count + 1):
                annotation = annotations.Item(index)
                if annotation is None:
                    entry["annotations"].append({"index": index, "error": "None annotation"})
                    continue
                record = {
                    "index": index,
                    "name": str(annotation.Name),
                    "type": str(annotation.Type),
                    "superType": str(annotation.SuperType),
                }
                if record["superType"] == "FTA_Dimension" or "Dimension" in record["type"]:
                    record["value"] = _dimension_value(annotation)
                if record["type"] == "FTA_Text":
                    record["text"] = _annotation_text(annotation)
                entry["annotations"].append(record)
            result["annotationSets"].append(entry)
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
    dims = [
        a for s in result.get("annotationSets", []) for a in s["annotations"]
        if a.get("value") is not None
    ]
    print(f"{result.get('document')}: {len(dims)} dimension annotations -> {out}")
    for item in dims:
        print(f"  {item['name']}: {item['value']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
