"""Reference geometry is separate, canonical and hash-bound context evidence."""
from __future__ import annotations

import copy
import hashlib
import json

import fitz
import pytest

from catia_mcp.drawing.feature_model import validate_features
from catia_mcp.drawing.pdf_evidence import (
    _load_drawing_context,
    inspect_pdf,
    query_context_reference_geometry,
)
from catia_mcp.drawing.tools import DrawingTools


REFERENCE_ITEMS = [
    {
        "id": "rg:cyl:1", "kind": "cylinder", "role": "hole",
        "radiusMm": 6.3, "axisPoint": [0.0, 0.0, 0.0], "axisDir": [0.0, 0.0, 1.0],
        "axialRangeMm": [0.0, 20.0], "faceIds": ["step-face:1"], "through": True,
    },
    {
        "id": "rg:plane:1", "kind": "plane", "normal": [0.0, 0.0, 1.0],
        "offsetMm": 20.0, "areaMm2": 100.0, "faceIds": ["step-face:2"],
    },
]


@pytest.fixture
def reference_context(tmp_path):
    pdf = tmp_path / "drawing.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=200)
    doc.save(str(pdf))
    doc.close()
    payload = {
        "schema": "catia-drawing-context/1",
        "pdfSha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "assetId": "drawing_abc",
        "sources": [
            {"kind": "dimensions", "sha256": "a" * 64},
            {"kind": "texts", "sha256": "b" * 64},
            {"kind": "titleBlock", "sha256": "c" * 64},
            {"kind": "referenceGeometry", "sha256": "d" * 64},
        ],
        "dimensions": [{"entity_id": "drawing_abc:1", "display_text": "12.6",
                        "spatial_frame_id": "block:1"}],
        "texts": [{"entity_id": "drawing_abc:u", "plain_text": "UNITS: mm"}],
        "titleBlock": [],
        "partId": "part_abc",
        "referenceGeometry": REFERENCE_ITEMS,
    }
    context = tmp_path / "drawing-context.json"
    context.write_text(json.dumps(payload), encoding="utf-8")
    return pdf, context, payload


def _manifest(reference_context, tmp_path):
    pdf, context, _ = reference_context
    return inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"),
                       context_path=str(context))


def _requirements(span_id: str, diameter: float = 12.6, length: float = 20.0):
    return {
        "schema": "catia-feature-requirements/1",
        "unit": "mm",
        "unitEvidence": ["ctx:texts:drawing_abc:u"],
        "parameters": [
            {"name": "D", "value": diameter, "evidence": [span_id]},
            {"name": "L", "value": length, "evidence": [span_id]},
        ],
        "features": [{
            "id": "base", "operation": "pad", "length": "L",
            "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "D / 2"}]},
        }],
    }


def test_reference_geometry_spans_are_canonical_and_hash_bound(reference_context, tmp_path):
    manifest = _manifest(reference_context, tmp_path)
    spans = [span for span in manifest["contextEvidence"]
             if span["sourceKind"] == "referenceGeometry"]
    assert [span["id"] for span in spans] == [
        "ctx:referenceGeometry:rg:cyl:1", "ctx:referenceGeometry:rg:plane:1"
    ]
    cylinder = spans[0]
    assert cylinder["source"] == "drawing_context"
    assert cylinder["assetId"] == "drawing_abc"
    assert cylinder["partId"] == "part_abc"
    assert cylinder["coordinateSystem"] == "canonical"
    assert cylinder["text"] == "Ø12.6 R6.3"
    assert cylinder["values"]["diameterMm"] == pytest.approx(12.6)
    assert cylinder["values"]["through"] is True
    assert cylinder["sourceSha256"] == "d" * 64
    assert manifest["contextSource"]["sources"]["referenceGeometry"] == "d" * 64


def test_reference_geometry_query_pages_and_filters(reference_context):
    pdf, context, _ = reference_context
    pdf_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    result = query_context_reference_geometry(
        str(context), pdf_sha, {"kind": "cylinder", "offset": 0, "limit": 1}
    )
    assert result["summary"]["count"] == 2
    assert result["matchedCount"] == 1
    assert result["records"][0]["spanId"] == "ctx:referenceGeometry:rg:cyl:1"
    assert result["records"][0]["text"] == "Ø12.6 R6.3"
    assert result["records"][0]["values"]["diameterMm"] == pytest.approx(12.6)
    assert result["nextOffset"] is None
    summary = query_context_reference_geometry(
        str(context), pdf_sha, {"summaryOnly": True}
    )
    assert summary["records"] == []
    with pytest.raises(ValueError, match="Invalid reference geometry query"):
        query_context_reference_geometry(str(context), pdf_sha, {"kind": "bogus"})
    with pytest.raises(ValueError, match="Invalid reference geometry query parameters"):
        query_context_reference_geometry(str(context), pdf_sha, {"limit": 0})


def test_inspect_drawing_summarizes_reference_geometry_by_default(reference_context, tmp_path):
    pdf, context, _ = reference_context
    tools = DrawingTools(None)
    default = tools.execute("catia_inspect_drawing", {
        "path": str(pdf), "drawingContextPath": str(context),
        "evidenceDir": str(tmp_path / "default"),
    })
    assert default["ok"] is True
    assert default["data"]["referenceGeometrySummary"]["count"] == 2
    assert all(span["sourceKind"] != "referenceGeometry"
               for span in default["data"]["contextEvidence"])
    queried = tools.execute("catia_inspect_drawing", {
        "path": str(pdf), "drawingContextPath": str(context),
        "evidenceDir": str(tmp_path / "queried"),
        "referenceGeometryQuery": {"kind": "cylinder"},
    })
    assert queried["data"]["contextReferenceGeometry"]["records"][0]["spanId"] == (
        "ctx:referenceGeometry:rg:cyl:1"
    )


def test_reference_geometry_can_prove_a_dimension(reference_context, tmp_path):
    manifest = _manifest(reference_context, tmp_path)
    span_id = "ctx:referenceGeometry:rg:cyl:1"
    assert validate_features(_requirements(span_id), manifest)["decision"] == "ready"
    bad = _requirements(span_id, diameter=12.7)
    assert validate_features(bad, manifest)["decision"] == "invalid"
    fake = _requirements("ctx:referenceGeometry:rg:cyl:9")
    result = validate_features(fake, manifest)
    assert result["decision"] != "ready"
    assert any(problem["code"] == "EVIDENCE_MISSING" for problem in result["problems"])


def test_tampered_reference_hash_is_rejected(reference_context, tmp_path):
    manifest = _manifest(reference_context, tmp_path)
    span_id = "ctx:referenceGeometry:rg:cyl:1"
    tampered = copy.deepcopy(manifest)
    for span in tampered["contextEvidence"]:
        if span["id"] == span_id:
            span["sourceSha256"] = "0" * 64
    result = validate_features(_requirements(span_id), tampered)
    assert result["decision"] == "invalid"
    assert any("hash" in problem["message"].lower() for problem in result["problems"])


def test_reference_geometry_and_source_must_occur_together(reference_context, tmp_path):
    pdf, context, payload = reference_context
    without_array = copy.deepcopy(payload)
    without_array.pop("referenceGeometry")
    context.write_text(json.dumps(without_array), encoding="utf-8")
    with pytest.raises(RuntimeError, match="must occur together"):
        _load_drawing_context(str(context), payload["pdfSha256"],
                              data=context.read_bytes())
    without_source = copy.deepcopy(payload)
    without_source["sources"] = [
        item for item in without_source["sources"]
        if item["kind"] != "referenceGeometry"
    ]
    context.write_text(json.dumps(without_source), encoding="utf-8")
    with pytest.raises(RuntimeError, match="must occur together"):
        _load_drawing_context(str(context), payload["pdfSha256"],
                              data=context.read_bytes())


def test_invalid_reference_geometry_records_are_rejected(reference_context):
    pdf, context, payload = reference_context
    bad_id = copy.deepcopy(payload)
    bad_id["referenceGeometry"][0]["id"] = "rg:cyl:not-a-number"
    context.write_text(json.dumps(bad_id), encoding="utf-8")
    with pytest.raises(RuntimeError, match="reference geometry id"):
        _load_drawing_context(str(context), payload["pdfSha256"],
                              data=context.read_bytes())
    bad_number = copy.deepcopy(payload)
    bad_number["referenceGeometry"][0]["radiusMm"] = "6.3"
    context.write_text(json.dumps(bad_number), encoding="utf-8")
    with pytest.raises(RuntimeError, match="must be finite"):
        _load_drawing_context(str(context), payload["pdfSha256"],
                              data=context.read_bytes())


def test_old_context_without_reference_geometry_is_unchanged(tmp_path):
    pdf = tmp_path / "drawing.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=200)
    doc.save(str(pdf))
    doc.close()
    payload = {
        "schema": "catia-drawing-context/1",
        "pdfSha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "assetId": "drawing_abc",
        "sources": [
            {"kind": "dimensions", "sha256": "a" * 64},
            {"kind": "texts", "sha256": "b" * 64},
            {"kind": "titleBlock", "sha256": "c" * 64},
        ],
        "dimensions": [{"entity_id": "drawing_abc:1", "display_text": "40"}],
        "texts": [],
        "titleBlock": [],
    }
    context = tmp_path / "drawing-context.json"
    context.write_text(json.dumps(payload), encoding="utf-8")
    spans, source, _ = _load_drawing_context(
        str(context), payload["pdfSha256"], data=context.read_bytes()
    )
    assert [span["id"] for span in spans] == ["ctx:dimensions:drawing_abc:1"]
    assert "referenceGeometry" not in source["sources"]
