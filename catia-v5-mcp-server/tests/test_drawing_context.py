"""Context evidence is separate from PDF spans and tied to the staged PDF."""
import hashlib
import json
import asyncio
import math
import copy

import fitz
import pytest

from catia_mcp.drawing.pdf_evidence import inspect_pdf, _geometry_bounds_2d
from catia_mcp.drawing.circle_evidence import derive_circle_measurement
from catia_mcp.drawing.feature_model import validate_features
from catia_mcp.drawing.tools import DrawingTools
from catia_mcp.generation_scope import GenerationScope
from catia_mcp.server import CATIAMCPServer


@pytest.fixture
def drawing_and_context(tmp_path):
    pdf = tmp_path / "drawing.pdf"
    doc = fitz.open()
    doc.new_page(width=200, height=200)
    doc.save(str(pdf))
    doc.close()
    context = tmp_path / "drawing-context.json"
    context.write_text(json.dumps({
        "schema": "catia-drawing-context/1", "pdfSha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "assetId": "drawing_abc", "sources": [
            {"kind": "dimensions", "sha256": "a" * 64},
            {"kind": "texts", "sha256": "b" * 64},
            {"kind": "titleBlock", "sha256": "c" * 64}],
        "dimensions": [{"entity_id": "drawing_abc:1", "display_text": "40",
                        "raw_measurement": 39.5, "spatial_frame_id": "block:1"}],
        "texts": [{"entity_id": "drawing_abc:2", "plain_text": "UNITS mm"}],
        "titleBlock": [],
    }), encoding="utf-8")
    return pdf, context


def test_context_keeps_source_identity_and_does_not_invent_pdf_spans(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    result = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))
    assert result["pages"][0]["spans"] == []
    context_spans = result["contextEvidence"]
    dim = next(span for span in context_spans if span["id"] == "ctx:dimensions:drawing_abc:1")
    assert dim["text"] == "40"
    assert dim["source"] == "drawing_context"
    assert dim["sourceSha256"] == "a" * 64
    assert "39.5" not in dim["text"]
    stored = json.loads((tmp_path / "evidence/evidence.json").read_text(encoding="utf-8"))
    assert stored["contextEvidence"] == context_spans
    repeated = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"))
    assert repeated["contextEvidence"] == context_spans


def test_context_for_another_pdf_is_rejected(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["pdfSha256"] = "0" * 64
    context.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="PDF hash"):
        inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))


def test_decimal_comma_annotation_supports_a_dimension(drawing_and_context, tmp_path):
    from catia_mcp.drawing.requirements import _contains_number
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["dimensions"][0].update(display_text="R22,5", dimension_type=4)
    context.write_text(json.dumps(payload), encoding="utf-8")
    manifest = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "unitEvidence": ["ctx:texts:drawing_abc:2"],
           "parameters": [{"name": "R", "value": 22.5, "evidence": ["ctx:dimensions:drawing_abc:1"]}],
           "features": [{"id": "base", "operation": "pad", "length": "R",
                         "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "R"}]}}]}
    assert validate_features(req, manifest)["decision"] == "ready"
    assert _contains_number("R22,5", 22.5) and _contains_number("R22.5", 22.5)
    assert not _contains_number("1,000", 1) and not _contains_number("1,000", 1000)


def test_dxf_unicode_escape_does_not_merge_into_dimension_number(drawing_and_context, tmp_path):
    from catia_mcp.drawing.requirements import _contains_number
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["dimensions"][0].update(display_text=r"\U+2205110", dimension_type=3)
    context.write_text(json.dumps(payload), encoding="utf-8")
    manifest = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))
    dim = next(span for span in manifest["contextEvidence"] if span["sourceKind"] == "dimensions")
    assert dim["text"] == "∅110"
    assert _contains_number(dim["text"], 110) and not _contains_number(dim["text"], 2205110)
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "unitEvidence": ["ctx:texts:drawing_abc:2"],
           "parameters": [{"name": "D", "value": 110, "evidence": [dim["id"]]}],
           "features": [{"id": "base", "operation": "pad", "length": "D",
                         "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "D / 2"}]}}]}
    assert validate_features(req, manifest)["decision"] == "ready"
    req["parameters"][0]["value"] = 2205110
    assert validate_features(req, manifest)["decision"] == "invalid"


def test_block_definition_text_cannot_alone_prove_a_dimension(drawing_and_context, tmp_path):
    from catia_mcp.drawing.requirements import _evidence_number_matches
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["texts"].append({"entity_id": "drawing_abc:local", "plain_text": "80.01",
                             "spatial_frame_id": "block:2", "coordinate_system": "block_local"})
    context.write_text(json.dumps(payload), encoding="utf-8")
    manifest = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))
    compact = DrawingTools(None).execute("catia_inspect_drawing", {
        "path": str(pdf), "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "compact"),
        "compactSpans": True})
    local_compact = next(span for span in compact["data"]["contextEvidence"] if span["id"].endswith(":local"))
    assert local_compact["coordinateSystem"] == "block_local"
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "unitEvidence": ["ctx:texts:drawing_abc:2"],
           "parameters": [{"name": "L", "value": 80.01, "evidence": ["ctx:texts:drawing_abc:local"]}],
           "features": [{"id": "base", "operation": "pad", "length": "L",
                         "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "L"}]}}]}
    assert validate_features(req, manifest)["decision"] == "invalid"
    local = next(span for span in manifest["contextEvidence"] if span["id"].endswith(":local"))
    index = {local["id"]: local}
    assert not _evidence_number_matches(index, [local["id"]], "80.01", 80.01, allow_raw=True)
    assert not _evidence_number_matches(index, [local["id"]], "80.01", 80.01, allow_raw=False)
    manifest["pages"][0]["spans"].append({"id": "p1:visible", "text": "80.01"})
    req["parameters"][0]["evidence"].append("p1:visible")
    assert validate_features(req, manifest)["decision"] == "ready"
    req["parameters"][0].update(value=40, evidence=["ctx:dimensions:drawing_abc:1"])
    assert validate_features(req, manifest)["decision"] == "ready"


def test_scaled_radial_raw_value_requires_matching_endpoints(drawing_and_context, tmp_path):
    from catia_mcp.drawing.feature_model import _dimension_evidence_matches
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["dimensions"][0].update(
        display_text="R3", dimension_type=4, coordinate_system="layout_wcs",
        raw_measurement=2.5, raw_measurement_unit="drawing_unit",
        measurement_basis="declared_drawing_unit", length_scale_factor=.5,
        text_override="", definition_points=[[0, 0, 0], [5, 0, 0]])
    payload["sources"].append({"kind": "geometry", "sha256": "d" * 64})
    payload["geometry"] = [
        {"entity_id": f"drawing_abc:{name}", "spatial_frame_id": "block:1",
         "coordinate_system": "ocs", "geometry_type": "CIRCLE",
         "geometry": {"center_ocs": center, "radius": radius, "extrusion": [0, 0, 1]}}
        for name, center, radius in [("ref", [0, 0, 0], 5), ("target", [20, 0, 0], 6.5)]]
    context.write_text(json.dumps(payload), encoding="utf-8")
    span = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))["contextEvidence"][0]
    assert _dimension_evidence_matches(span, 2.5, "length")
    assert not _dimension_evidence_matches(span, 1.25, "length")
    selection = {"targetCircleId": "drawing_abc:target", "referenceCircleId": "drawing_abc:ref",
                 "referenceDimensionId": "drawing_abc:1"}
    assert derive_circle_measurement(str(context), hashlib.sha256(pdf.read_bytes()).hexdigest(), selection)["text"] == "6.5"
    span["rawMeasurement"]["definitionPoints"][1][0] = 4
    assert not _dimension_evidence_matches(span, 2.5, "length")
    payload["dimensions"][0]["definition_points"][1][0] = 4
    context.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not describe"):
        derive_circle_measurement(str(context), hashlib.sha256(pdf.read_bytes()).hexdigest(), selection)


def test_scaled_linear_raw_value_requires_recorded_dimension_direction(drawing_and_context, tmp_path):
    from catia_mcp.drawing.feature_model import _dimension_evidence_matches
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["dimensions"][0].update(
        display_text="14", dimension_type=0, coordinate_system="layout_wcs",
        raw_measurement=13.5, raw_measurement_unit="drawing_unit",
        measurement_basis="declared_drawing_unit", length_scale_factor=.5,
        dimension_rotation_degrees=0, dimension_extrusion=[0, 0, 1],
        text_override="", definition_point_names=["defpoint", "defpoint2", "defpoint3"],
        definition_points=[[27, 12, 0], [0, 0, 0], [27, 2, 0]])
    context.write_text(json.dumps(payload), encoding="utf-8")
    span = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))["contextEvidence"][0]
    assert _dimension_evidence_matches(span, 13.5, "length")
    span["rawMeasurement"]["dimensionRotationDegrees"] = 90
    assert not _dimension_evidence_matches(span, 13.5, "length")
    span["rawMeasurement"]["dimensionRotationDegrees"] = 45
    assert not _dimension_evidence_matches(span, 13.5, "length")
    span["rawMeasurement"]["definitionPoints"][2][:2] = [13.5 * 2 ** .5, 13.5 * 2 ** .5]
    assert _dimension_evidence_matches(span, 13.5, "length")
    span["rawMeasurement"]["definitionPoints"][2][:2] = [27, 2]
    span["rawMeasurement"]["dimensionRotationDegrees"] = 0
    span["rawMeasurement"]["dimensionExtrusion"] = [0, 1, 0]
    assert not _dimension_evidence_matches(span, 13.5, "length")
    span["rawMeasurement"]["dimensionExtrusion"] = [0, 0, 1]
    span["rawMeasurement"]["definitionPointNames"] = None
    assert not _dimension_evidence_matches(span, 13.5, "length")


def test_circle_measurement_requires_matched_radial_anchor_and_frame(drawing_and_context, tmp_path, monkeypatch):
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["sources"].append({"kind": "geometry", "sha256": "d" * 64})
    payload["dimensions"] = [{
        "entity_id": "drawing_abc:dim", "display_text": "R75", "dimension_type": 4,
        "spatial_frame_id": "block:1", "coordinate_system": "layout_wcs",
        "raw_measurement": 37.5, "raw_measurement_unit": "drawing_unit",
        "measurement_basis": "declared_drawing_unit", "length_scale_factor": 2,
        "text_override": "", "definition_points": [[0, 0, 0], [37.5, 0, 0]],
    }]
    payload["geometry"] = [
        {"entity_id": f"drawing_abc:{name}", "spatial_frame_id": "block:1",
         "coordinate_system": "ocs", "geometry_type": "CIRCLE",
         "geometry": {"center_ocs": center, "radius": radius, "extrusion": [0, 0, 1]}}
        for name, center, radius in [("reference", [0, 0, 0], 37.5), ("hole", [20, 30, 0], 2)]
    ]
    selection = {"targetCircleId": "drawing_abc:hole", "referenceCircleId": "drawing_abc:reference",
                 "referenceDimensionId": "drawing_abc:dim"}
    def derive():
        context.write_text(json.dumps(payload), encoding="utf-8")
        return derive_circle_measurement(str(context), hashlib.sha256(pdf.read_bytes()).hexdigest(), selection)
    evidence = derive()
    assert evidence["text"] == "8"
    assert evidence["sourceKind"] == "derived_geometry" and evidence["approximate"] is True
    assert evidence["scaleFactor"] == 2
    payload["dimensions"][0]["text_override"] = None
    assert derive()["text"] == "8"
    del payload["dimensions"][0]["text_override"]
    with pytest.raises(ValueError, match="radial dimension"): derive()
    payload["dimensions"][0]["text_override"] = ""
    evidence = derive()
    assert evidence["text"] == "8"
    payload["geometry"][1]["geometry"]["center_ocs"] = [-20, 0, 0]
    selection["quantity"] = "offsetX"
    assert derive()["text"] == "-40"
    selection["quantity"] = "offsetY"
    assert derive()["text"] == "0"
    del selection["quantity"]
    payload["geometry"][1]["geometry"]["center_ocs"] = [20, 30, 0]
    assert derive() == evidence
    arguments = {"path": str(pdf), "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "derived"),
                 "geometryOnly": True, "circleMeasurement": selection}
    result = DrawingTools(None).execute("catia_inspect_drawing", arguments)
    assert result["ok"] is True
    assert result["data"]["derivedMeasurement"] == evidence
    result = DrawingTools(None).execute("catia_inspect_drawing", {
        "path": str(pdf), "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "derived")})
    assert result["ok"] is True
    assert any(s["id"] == evidence["id"] for s in result["data"]["contextEvidence"])
    saved = json.loads((tmp_path / "derived/evidence.json").read_text(encoding="utf-8"))
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "unitEvidence": ["ctx:texts:drawing_abc:2"],
           "parameters": [{"name": "D", "value": 8, "evidence": [evidence["id"]]}],
           "features": [{"id": "base", "operation": "pad", "length": "D",
                         "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "D / 2"}]}}]}
    assert validate_features(req, saved)["decision"] == "ready"
    tampered = copy.deepcopy(saved)
    next(s for s in tampered["contextEvidence"] if s["id"] == evidence["id"])["scaleFactor"] = 3
    assert validate_features(req, tampered)["decision"] == "invalid"
    assert DrawingTools(None).execute("catia_inspect_drawing", arguments)["ok"] is True
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    async def run_through_server():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        try:
            inspected = await server.call_tool("catia_inspect_drawing", arguments)
            assert not inspected.isError
            assert inspected.structuredContent["data"]["derivedMeasurement"]["id"] == evidence["id"]
            validated = await server.call_tool("catia_validate_requirements", {
                "requirements": req, "evidencePath": inspected.structuredContent["data"]["evidencePath"]})
            assert not validated.isError
            assert validated.structuredContent["code"] == "REQUIREMENTS_READY"
            calls = [{**arguments, "circleMeasurement": {**selection, "quantity": q}}
                     for q in ("diameter", "offsetX", "offsetY")]
            results = await asyncio.gather(*(server.call_tool("catia_inspect_drawing", args) for args in calls))
            assert all(not result.isError for result in results)
            ids = {result.structuredContent["data"]["derivedMeasurement"]["id"] for result in results}
            stored = json.loads((tmp_path / "derived/evidence.json").read_text(encoding="utf-8"))
            assert ids <= {span["id"] for span in stored["contextEvidence"]}
        finally:
            await server.close()
    asyncio.run(run_through_server())
    offset_args = {**arguments, "circleMeasurement": {**selection, "quantity": "offsetX"}}
    offset_result = DrawingTools(None).execute("catia_inspect_drawing", offset_args)
    assert offset_result["ok"] is True
    offset = offset_result["data"]["derivedMeasurement"]
    assert offset["text"] == "40" and offset["id"] != evidence["id"]
    saved_with_offset = json.loads((tmp_path / "derived/evidence.json").read_text(encoding="utf-8"))
    offset_req = copy.deepcopy(req)
    offset_req["parameters"].append({"name": "X", "value": 40, "evidence": [offset["id"]]})
    offset_req["features"][0]["sketch"]["profiles"][0]["x"] = "X"
    assert validate_features(offset_req, saved_with_offset)["decision"] == "ready"
    offset_args["circleMeasurement"]["quantity"] = "offsetY"
    assert DrawingTools(None).execute("catia_inspect_drawing", offset_args)["data"]["derivedMeasurement"]["text"] == "60"
    payload["texts"][0]["plain_text"] = "UNITS mm (updated)"
    context.write_text(json.dumps(payload), encoding="utf-8")
    changed = DrawingTools(None).execute("catia_inspect_drawing", {
        "path": str(pdf), "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "derived")})
    assert changed["ok"] is True
    assert not any(s.get("sourceKind") == "derived_geometry" for s in changed["data"]["contextEvidence"])
    payload["texts"][0]["plain_text"] = "UNITS mm"
    payload["geometry"][1]["spatial_frame_id"] = "block:other"
    with pytest.raises(ValueError, match="frame"): derive()
    payload["geometry"][1]["spatial_frame_id"] = "block:1"
    payload["dimensions"][0]["definition_points"][1][0] = 30
    with pytest.raises(ValueError, match="reference circle"): derive()
    payload["dimensions"][0]["definition_points"][1][0] = 37.5
    payload["geometry"][1]["geometry"]["extrusion"] = [0, 1, 0]
    with pytest.raises(ValueError, match="XY-plane"): derive()
    payload["geometry"][1]["geometry"]["extrusion"] = [0, 0, 1]
    payload["dimensions"][0]["display_text"] = "R70"
    with pytest.raises(ValueError, match="displayed"): derive()


def test_geometry_bounds_do_not_emit_nonfinite_numbers():
    assert _geometry_bounds_2d({"geometry_type": "CIRCLE", "geometry":
        {"center": [1e308, 0], "radius": 1e308}}) is None


@pytest.mark.parametrize("bulge,ymin,ymax", [(1, -1, 0), (-1, 0, 1)])
def test_polyline_bounds_include_bulge_arc_extrema(bulge, ymin, ymax):
    record = {"geometry_type": "LWPOLYLINE", "geometry": {"closed": False, "constant_width": 0,
        "vertices_xyseb": [[0, 0, 0, 0, bulge], [2, 0, 0, 0, 0]]}}
    low, high, conservative = _geometry_bounds_2d(record)
    assert low == pytest.approx([0, ymin]) and high == pytest.approx([2, ymax])
    assert conservative is False
    record["geometry"]["vertices_xyseb"][0][2] = 1
    assert _geometry_bounds_2d(record) is None


@pytest.mark.parametrize("bulge,low,high", [
    (math.tan(math.pi / 8), [0, 0], [1, 1]),
    (math.tan(3 * math.pi / 8), [0, 0], [2, 2]),
])
def test_polyline_bounds_distinguish_minor_and_major_arcs(bulge, low, high):
    record = {"geometry_type": "LWPOLYLINE", "geometry": {"closed": False, "constant_width": 0,
        "vertices_xyseb": [[1, 0, 0, 0, bulge], [0, 1, 0, 0, 0]]}}
    minimum, maximum, conservative = _geometry_bounds_2d(record)
    assert minimum == pytest.approx(low) and maximum == pytest.approx(high)
    assert conservative is False


def test_shallow_long_polyline_arc_gets_conservative_bounds():
    record = {"geometry_type": "LWPOLYLINE", "geometry": {"closed": False, "constant_width": 0,
        "vertices_xyseb": [[0, 0, 0, 0, 1e-12], [1e9, 0, 0, 0, 0]]}}
    minimum, maximum, conservative = _geometry_bounds_2d(record)
    assert minimum[1] <= -0.0005 and maximum[0] >= 1e9
    assert conservative is True


def test_context_entity_from_another_drawing_is_rejected(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["dimensions"][0]["entity_id"] = "drawing_other:1"
    context.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError, match="another drawing"):
        inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))


def test_context_evidence_is_accepted_by_feature_validator(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    manifest = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "evidence"), context_path=str(context))
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "unitEvidence": ["ctx:texts:drawing_abc:2"],
           "parameters": [{"name": "L", "value": 40, "evidence": ["ctx:dimensions:drawing_abc:1"]}],
           "features": [{"id": "base", "operation": "pad", "length": "L",
                         "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "L"}]}}]}
    assert validate_features(req, manifest)["decision"] == "ready"
    req["parameters"][0]["value"] = 39.5
    assert validate_features(req, manifest)["decision"] == "invalid"
    req["parameters"][0]["value"] = 40
    del manifest["contextEvidence"][0]["sourceSha256"]
    result = validate_features(req, manifest)
    assert result["decision"] == "invalid"
    assert result["problems"][0]["code"] == "EVIDENCE_INVALID"


def test_context_path_cannot_escape_generation_scope(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    root = tmp_path / "generation"
    root.mkdir()
    staged = root / "drawing.pdf"
    staged.write_bytes(pdf.read_bytes())
    scope = GenerationScope(str(root))
    with pytest.raises(ValueError, match="outside"):
        scope.prepare("catia_inspect_drawing", {"path": str(staged), "drawingContextPath": str(context)})


def test_inspect_tool_returns_separate_context_and_no_pdf_text_warning(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    result = DrawingTools(None).execute("catia_inspect_drawing", {
        "path": str(pdf), "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence")})
    assert result["ok"] is True
    assert result["data"]["contextEvidence"][0]["source"] == "drawing_context"
    assert any("no text spans" in warning for warning in result["data"]["warnings"])
    compact = DrawingTools(None).execute("catia_inspect_drawing", {
        "path": str(pdf), "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence"),
        "compactSpans": True})
    assert set(compact["data"]["contextEvidence"][0]) == {"id", "text", "rawMeasurement"}
    saved = json.loads((tmp_path / "evidence/evidence.json").read_text(encoding="utf-8"))
    assert saved["contextEvidence"][0]["sourceSha256"] == "a" * 64


def test_declared_raw_dimension_requires_rounding_and_units_provenance(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    record = payload["dimensions"][0]
    record.update(display_text="6", raw_measurement=6.299999999999983,
                  raw_measurement_unit="drawing_unit", measurement_basis="declared_drawing_unit",
                  dimension_type=0, text_override="", length_scale_factor=-1)
    context.write_text(json.dumps(payload), encoding="utf-8")
    manifest = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "raw-evidence"), context_path=str(context))
    span = manifest["contextEvidence"][0]
    assert span["text"] == "6"
    assert span["rawMeasurement"]["value"] == pytest.approx(6.3)
    req = {"schema": "catia-feature-requirements/1", "unit": "mm",
           "unitEvidence": ["ctx:texts:drawing_abc:2"],
           "parameters": [{"name": "L", "value": 6.3, "evidence": [span["id"]]}],
           "features": [{"id": "base", "operation": "pad", "length": "L",
                         "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "L"}]}}]}
    assert validate_features(req, manifest)["decision"] == "ready"
    span["rawMeasurement"]["textOverride"] = None
    assert validate_features(req, manifest)["decision"] == "ready"
    span["rawMeasurement"]["textOverridePresent"] = False
    assert validate_features(req, manifest)["decision"] == "invalid"
    span["rawMeasurement"]["textOverridePresent"] = True
    span["rawMeasurement"]["textOverride"] = "<>"
    assert validate_features(req, manifest)["decision"] == "ready"
    span["rawMeasurement"]["textOverride"] = ""
    for key, invalid in (("basis", None), ("unit", "paper_unit"), ("textOverride", "6"),
                         ("lengthScaleFactor", 0), ("dimensionType", 2)):
        old = span["rawMeasurement"][key]
        span["rawMeasurement"][key] = invalid
        assert validate_features(req, manifest)["decision"] == "invalid"
        span["rawMeasurement"][key] = old
    span["text"] = "7"
    assert validate_features(req, manifest)["decision"] == "invalid"
    span["text"] = "20.57"
    span["rawMeasurement"].update(value=51.43500000000002, lengthScaleFactor=0.4)
    req["parameters"][0]["value"] = 20.574
    assert validate_features(req, manifest)["decision"] == "ready"
    span["rawMeasurement"]["lengthScaleFactor"] = -0.4
    assert validate_features(req, manifest)["decision"] == "invalid"
    span["text"] = "320"
    span["rawMeasurement"].update(value=319.9999999999, lengthScaleFactor=5)
    req["parameters"][0]["value"] = 1600
    assert validate_features(req, manifest)["decision"] == "invalid"
    req["parameters"][0]["value"] = 320
    assert validate_features(req, manifest)["decision"] == "ready"
    for dimension_type, display in ((3, "Ø6"), (4, "R6")):
        span["text"] = display
        span["rawMeasurement"].update(value=6.3, lengthScaleFactor=1, dimensionType=dimension_type)
        req["parameters"][0]["value"] = 6.3
        assert validate_features(req, manifest)["decision"] == "ready"
        span["rawMeasurement"]["dimensionType"] = 4 if dimension_type == 3 else 3
        assert validate_features(req, manifest)["decision"] == "invalid"
    span["text"] = "Ø6"
    span["rawMeasurement"].update(value=6.3, lengthScaleFactor=1, dimensionType=0, textOverride="%%C<>")
    assert validate_features(req, manifest)["decision"] == "ready"
    span["rawMeasurement"]["textOverride"] = "%%C6"
    assert validate_features(req, manifest)["decision"] == "invalid"
    span["rawMeasurement"]["textOverride"] = "%%C<>"
    span["rawMeasurement"]["lengthScaleFactor"] = 5
    assert validate_features(req, manifest)["decision"] == "invalid"
    span["text"] = "(8x) ∅6"
    span["rawMeasurement"].update(value=6.3, lengthScaleFactor=1, dimensionType=3, textOverride="(8x) <>")
    assert validate_features(req, manifest)["decision"] == "ready"
    req["parameters"][0]["value"] = 8
    assert validate_features(req, manifest)["decision"] == "invalid"
    req["parameters"][0]["value"] = 6.3
    span["text"] = "(6x) ∅6"
    assert validate_features(req, manifest)["decision"] == "invalid"
    span["text"] = "(8x) ∅6"
    span["rawMeasurement"]["textOverride"] = "(8x) 6"
    assert validate_features(req, manifest)["decision"] == "invalid"


def test_context_geometry_is_paged_without_mixing_coordinate_frames(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["sources"].append({"kind": "geometry", "sha256": "d" * 64})
    payload["geometry"] = [
        {"entity_id": f"drawing_abc:{i}", "spatial_frame_id": frame, "coordinate_system": system,
         "geometry_type": "LINE", "geometry": {"start": [i, 0, 0], "end": [i, 2, 0]}}
        for i, frame, system in [(1, "block:1", "block_local"), (2, "block:1", "block_local"),
                                 (3, "block:1", "layout_wcs")]]
    payload["geometry"].extend([
        {"entity_id": "drawing_abc:4", "spatial_frame_id": "block:1", "coordinate_system": "block_local",
         "geometry_type": "ARC", "geometry": {"center_ocs": [4, 5, 0], "radius": 2}},
        {"entity_id": "drawing_abc:5", "spatial_frame_id": "block:1", "coordinate_system": "block_local",
         "geometry_type": "LWPOLYLINE", "geometry": {"vertices_xyseb": [[0, 0, 0, 0, 1]]}},
    ])
    context.write_text(json.dumps(payload), encoding="utf-8")
    result = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf),
        "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence"),
        "compactSpans": True, "geometryQuery": {"frame": "block:1", "coordinateSystem": "block_local", "kind": "LINE", "limit": 1}})
    assert result["ok"] is True
    geo = result["data"]["contextGeometry"]
    assert geo["sourceSha256"] == "d" * 64
    assert geo["matchedCount"] == 2 and geo["nextOffset"] == 1
    assert geo["records"][0]["coordinate_system"] == "block_local"
    compact_geometry = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf),
        "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence"),
        "geometryOnly": True, "geometryQuery": {"frame": "block:1", "coordinateSystem": "block_local", "kind": "LINE", "limit": 1}})
    assert compact_geometry["ok"] is True
    assert compact_geometry["data"]["contextGeometry"] == geo
    assert "contextEvidence" not in compact_geometry["data"] and "pages" not in compact_geometry["data"]
    persisted = json.loads((tmp_path / "evidence/evidence.json").read_text(encoding="utf-8"))
    assert persisted["contextEvidence"] and persisted["pages"]
    missing_query = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf), "geometryOnly": True})
    assert missing_query["ok"] is False and "requires geometryQuery" in missing_query["message"]
    with_ocr = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf), "geometryOnly": True,
        "geometryQuery": {}, "includeOcr": True})
    assert with_ocr["ok"] is False and "cannot be combined" in with_ocr["message"]
    assert {item["coordinateSystem"] for item in geo["groups"]} == {"block_local", "layout_wcs"}
    local = next(item for item in geo["groups"] if item["coordinateSystem"] == "block_local" and item["kind"] == "LINE")
    assert local["bounds2D"] == {"min": [1, 0], "max": [2, 2]}
    assert local["boundsIncomplete"] is False
    assert local["boundsConservative"] is False
    arc = next(item for item in geo["groups"] if item["kind"] == "ARC")
    assert arc["bounds2D"] == {"min": [2, 3], "max": [6, 7]}
    assert arc["boundsConservative"] is True
    poly = next(item for item in geo["groups"] if item["kind"] == "LWPOLYLINE")
    assert poly["bounds2D"] is None and poly["boundsIncomplete"] is True
    region = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf),
        "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence"), "compactSpans": True,
        "geometryQuery": {"frame": "block:1", "coordinateSystem": "block_local", "bbox": [0.5, -1, 1.5, 3]}})
    assert region["ok"] is True
    assert {item["entity_id"] for item in region["data"]["contextGeometry"]["records"]} == {"drawing_abc:1", "drawing_abc:5"}
    assert region["data"]["contextGeometry"]["boundsUnknownCount"] == 1
    invalid_region = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf),
        "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence"),
        "geometryQuery": {"bbox": [0, 0, 2, 2]}})
    assert invalid_region["ok"] is False and "frame" in invalid_region["message"]
    assert "Not PDF evidence" in geo["note"]
    mixed = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf),
        "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence"),
        "geometryQuery": {"frame": "block:1"}})
    assert mixed["ok"] is False and "coordinateSystem" in mixed["message"]
    payload["geometry"][0]["entity_id"] = "drawing_other:1"
    context.write_text(json.dumps(payload), encoding="utf-8")
    bad = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf),
        "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence"), "geometryQuery": {}})
    assert bad["ok"] is False and "mismatched" in bad["message"]


def test_polyline_object_vertices_are_returned_even_when_bounds_are_unknown(drawing_and_context, tmp_path):
    pdf, context = drawing_and_context
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["sources"].append({"kind": "geometry", "sha256": "d" * 64})
    vertices = [{"location": {"x": 1, "y": 2, "z": 0}, "bulge": 0.5}]
    payload["geometry"] = [{"entity_id": "drawing_abc:9", "spatial_frame_id": "block:1",
        "coordinate_system": "block_local", "geometry_type": "POLYLINE", "geometry": {"vertices": vertices}}]
    context.write_text(json.dumps(payload), encoding="utf-8")
    result = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf),
        "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "evidence"), "geometryOnly": True,
        "geometryQuery": {"frame": "block:1", "coordinateSystem": "block_local", "bbox": [20, 20, 30, 30]}})
    assert result["ok"] is True
    geometry = result["data"]["contextGeometry"]
    assert geometry["boundsUnknownCount"] == 1
    assert geometry["records"][0]["geometry"]["vertices"] == vertices


def test_context_evidence_flows_through_scoped_mcp_server(drawing_and_context, tmp_path, monkeypatch):
    pdf, context = drawing_and_context
    root = tmp_path / "generation"
    root.mkdir()
    staged_pdf, staged_context = root / "drawing.pdf", root / "drawing-context.json"
    staged_pdf.write_bytes(pdf.read_bytes())
    payload = json.loads(context.read_text(encoding="utf-8"))
    payload["sources"].append({"kind": "geometry", "sha256": "d" * 64})
    payload["geometry"] = [{"entity_id": "drawing_abc:3", "spatial_frame_id": "block:1",
        "coordinate_system": "block_local", "geometry_type": "LINE",
        "geometry": {"start": [0, 0, 0], "end": [5, 0, 0]}}]
    staged_context.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))

    async def run():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        try:
            inspected = await server.call_tool("catia_inspect_drawing", {
                "path": str(staged_pdf), "drawingContextPath": str(staged_context), "compactSpans": True,
                "geometryQuery": {"frame": "block:1", "limit": 1}})
            assert not inspected.isError
            data = inspected.structuredContent["data"]
            assert data["contextEvidence"][0]["id"] == "ctx:dimensions:drawing_abc:1"
            assert data["contextGeometry"]["records"][0]["entity_id"] == "drawing_abc:3"
            compact = await server.call_tool("catia_inspect_drawing", {
                "path": str(staged_pdf), "drawingContextPath": str(staged_context), "geometryOnly": True,
                "geometryQuery": {"frame": "block:1", "limit": 1}})
            assert not compact.isError
            assert "contextEvidence" not in compact.structuredContent["data"]
            assert compact.structuredContent["data"]["contextGeometry"] == data["contextGeometry"]
            req = {"schema": "catia-feature-requirements/1", "unit": "mm",
                   "unitEvidence": ["ctx:texts:drawing_abc:2"],
                   "parameters": [{"name": "L", "value": 40, "evidence": ["ctx:dimensions:drawing_abc:1"]}],
                   "features": [{"id": "base", "operation": "pad", "length": "L",
                                 "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "L"}]}}]}
            validated = await server.call_tool("catia_validate_requirements", {
                "requirements": req, "evidencePath": compact.structuredContent["data"]["evidencePath"]})
            assert not validated.isError
            assert validated.structuredContent["code"] == "REQUIREMENTS_READY"
        finally:
            await server.close()
    asyncio.run(run())
