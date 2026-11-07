"""Block instances remain bound, bounded 2D data, not manufactured drawing evidence."""
import copy
import hashlib
import io
import json
from pathlib import Path

import fitz
import pytest

from catia_mcp.drawing.block_context import query_context_blocks
from catia_mcp.drawing import pdf_evidence
from catia_mcp.drawing.pdf_evidence import inspect_pdf, query_context_geometry
from catia_mcp.drawing.tools import DrawingTools


@pytest.fixture
def block_input(tmp_path):
    pdf = tmp_path / "drawing.pdf"
    with fitz.open() as doc:
        doc.new_page(width=200, height=200).insert_text((20, 20), "UNITS mm")
        doc.save(str(pdf))
    asset = "drawing_abc"
    matrix = [[1, 0, 0, 10], [0, 1, 0, 20], [0, 0, 1, 0], [0, 0, 0, 1]]
    def definition(block_id, members):
        return {"record_type": "block_definition", "block_id": block_id, "name": block_id,
                "base_point": [0, 0, 0], "entity_ids": members, "has_external_reference": False}
    def instance(entity, parent, child, coordinates):
        return {"record_type": "block_instance", "entity_id": asset + ":" + entity,
                "spatial_frame_id": parent, "coordinate_system": coordinates, "container_id": parent,
                "definition_id": child, "transform_column_vectors": copy.deepcopy(matrix),
                "rows": 1, "columns": 1, "row_spacing": 0, "column_spacing": 0,
                "layer_id": "layer:1", "unit": "drawing_unit", "display": {"invisible": 0}}
    payload = {
        "schema": "catia-drawing-context/1", "pdfSha256": hashlib.sha256(pdf.read_bytes()).hexdigest(),
        "assetId": asset, "sources": [{"kind": kind, "sha256": char * 64}
            for kind, char in (("dimensions", "a"), ("texts", "b"), ("titleBlock", "c"), ("blocks", "d"))],
        "dimensions": [], "texts": [], "titleBlock": [],
        "blocks": {"definitions": [definition("block:1", [asset + ":P"]),
                                   definition("block:2", [asset + ":A", asset + ":B"]),
                                   definition("block:3", [asset + ":L1", asset + ":L2"])],
                   "instances": [instance("P", "block:1", "block:2", "layout_wcs"),
                                 instance("A", "block:2", "block:3", "block_local"),
                                 instance("B", "block:2", "block:3", "block_local")]},
    }
    context = tmp_path / "drawing-context.json"
    context.write_text(json.dumps(payload), encoding="utf-8")
    return pdf, context, payload


def test_nested_blocks_are_paged_in_raw_frames_and_never_become_evidence(block_input, tmp_path):
    pdf, context, payload = block_input
    args = {"path": str(pdf), "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "ev"),
            "geometryOnly": True}
    tools = DrawingTools(None)
    summary = tools.execute("catia_inspect_drawing", {**args, "blockQuery": {"summaryOnly": True}})
    assert summary["ok"] is True, summary
    assert summary["data"]["contextBlocks"]["groups"] == [
        {"frame": "block:1", "coordinateSystem": "layout_wcs", "count": 1},
        {"frame": "block:2", "coordinateSystem": "block_local", "count": 2}]
    page = tools.execute("catia_inspect_drawing", {**args, "blockQuery": {
        "frame": "block:2", "coordinateSystem": "block_local", "definitionId": "block:3", "limit": 1}})
    assert page["ok"] is True, page
    result = page["data"]["contextBlocks"]
    assert result["sourceSha256"] == "d" * 64 and result["matchedCount"] == 2 and result["nextOffset"] == 1
    assert result["records"][0]["entity_id"] == "drawing_abc:A"
    assert result["records"][0]["transform_column_vectors"] == payload["blocks"]["instances"][1]["transform_column_vectors"]
    assert "not PDF or dimension evidence" in result["note"]
    next_page = query_context_blocks(str(context), payload["pdfSha256"], {
        "frame": "block:2", "coordinateSystem": "block_local", "definitionId": "block:3", "offset": 1, "limit": 1})
    assert next_page["records"][0]["entity_id"] == "drawing_abc:B" and next_page["nextOffset"] is None
    manifest = inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "ev2"), context_path=str(context))
    assert manifest["contextEvidence"] == []
    assert not tools.execute("catia_inspect_drawing", {**args, "blockQuery": {}})["ok"]


def test_definition_members_are_explicitly_requested_and_paged(block_input):
    _, context, payload = block_input
    base = {"kind": "definitions", "definitionId": "block:3"}
    result = query_context_blocks(str(context), payload["pdfSha256"], base)
    assert result["records"][0]["entityCount"] == 2 and "entity_ids" not in result["records"][0]
    first = query_context_blocks(str(context), payload["pdfSha256"], {**base, "includeMembers": True, "memberLimit": 1})
    assert first["records"][0]["entity_ids"] == ["drawing_abc:L1"]
    assert first["records"][0]["memberNextOffset"] == 1
    second = query_context_blocks(str(context), payload["pdfSha256"], {**base, "includeMembers": True, "memberOffset": 1})
    assert second["records"][0]["entity_ids"] == ["drawing_abc:L2"]
    assert second["records"][0]["memberNextOffset"] is None
    with pytest.raises(ValueError, match="definitionId"):
        query_context_blocks(str(context), payload["pdfSha256"], {"kind": "definitions", "includeMembers": True})


@pytest.mark.parametrize("kind", ["blocks", "geometry"])
def test_query_uses_one_validated_snapshot(block_input, monkeypatch, kind):
    _, context, payload = block_input
    payload["sources"].append({"kind": "geometry", "sha256": "e" * 64})
    payload["geometry"] = [{"entity_id": "drawing_abc:L1", "spatial_frame_id": "block:3",
                            "coordinate_system": "block_local", "geometry_type": "LINE",
                            "geometry": {"start": [0, 0, 0], "end": [1, 1, 0]}}]
    context.write_text(json.dumps(payload), encoding="utf-8")
    original = pdf_evidence._read_context_bytes
    reads = 0
    def changed(path):
        nonlocal reads
        data = original(path)
        if path == context:
            reads += 1
            if reads == 2:
                return data.replace(b'"block:3"', b'"block:4"')
        return data
    monkeypatch.setattr(pdf_evidence, "_read_context_bytes", changed)
    if kind == "blocks":
        query_context_blocks(str(context), payload["pdfSha256"], {"summaryOnly": True})
    else:
        assert query_context_geometry(str(context), payload["pdfSha256"], {})["records"][0]["geometry"]["end"] == [1, 1, 0]
    assert reads == 1


@pytest.mark.parametrize("kind,change_on", [("geometry", 2), ("blocks", 2), ("circle", 2), ("both", 3)])
def test_inspection_rejects_context_changed_between_steps(block_input, tmp_path, monkeypatch, kind, change_on):
    pdf, context, payload = block_input
    payload["sources"].append({"kind": "geometry", "sha256": "e" * 64})
    payload["geometry"] = [{"entity_id": "drawing_abc:L1", "spatial_frame_id": "block:3",
                            "coordinate_system": "block_local", "geometry_type": "LINE",
                            "geometry": {"start": [0, 0, 0], "end": [1, 1, 0]}}]
    context.write_text(json.dumps(payload), encoding="utf-8")
    original = pdf_evidence._read_context_bytes
    reads = 0
    def changed(path):
        nonlocal reads
        data = original(path)
        if path == context:
            reads += 1
            if reads == change_on:
                # The alternate payload remains valid and keeps the same PDF and source hashes.
                altered = json.loads(data)
                altered["geometry"][0]["geometry"]["end"][0] = 999
                return json.dumps(altered).encode()
        return data
    monkeypatch.setattr(pdf_evidence, "_read_context_bytes", changed)
    args = {"path": str(pdf), "drawingContextPath": str(context), "evidenceDir": str(tmp_path / "ev"),
            "geometryOnly": True}
    if kind in ("geometry", "both"):
        args["geometryQuery"] = {}
    if kind in ("blocks", "both"):
        args["blockQuery"] = {"summaryOnly": True}
    if kind == "circle":
        args["circleMeasurement"] = {"targetCircleId": "drawing_abc:L1", "referenceCircleId": "drawing_abc:L2",
                                     "referenceDimensionId": "drawing_abc:D1"}
    response = DrawingTools(None).execute("catia_inspect_drawing", args)
    assert response["ok"] is False and "changed since PDF inspection" in response["message"]
    assert reads == change_on


@pytest.mark.parametrize("extra", [0, 1])
def test_context_read_size_limit(block_input, extra):
    _, context, payload = block_input
    limit = 4 * 1024 * 1024
    data = context.read_bytes()
    data += b" " * (limit + extra - len(data))
    context.write_bytes(data)
    if extra:
        with pytest.raises(RuntimeError, match="exceeds 4 MiB"):
            pdf_evidence._drawing_context(str(context), payload["pdfSha256"])
    else:
        _, source = pdf_evidence._drawing_context(str(context), payload["pdfSha256"])
        assert source["sha256"] == hashlib.sha256(data).hexdigest()
        assert query_context_blocks(str(context), payload["pdfSha256"], {"summaryOnly": True}, source["sha256"])["matchedCount"] == 3


def test_context_read_rejects_growth_during_read(block_input, monkeypatch):
    _, context, payload = block_input
    limit = 4 * 1024 * 1024
    data = context.read_bytes()
    data += b" " * (limit - len(data))
    requested = []

    class GrowingFile(io.BytesIO):
        def read(self, size=-1):
            requested.append(size)
            self.seek(0, io.SEEK_END)
            self.write(b" ")  # another writer grows the file after open
            self.seek(0)
            return super().read(size)

    original = Path.open
    def open_growing(path, *args, **kwargs):
        if path == context and args and args[0] == "rb":
            return GrowingFile(data)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_growing)
    with pytest.raises(RuntimeError, match="exceeds 4 MiB"):
        pdf_evidence._drawing_context(str(context), payload["pdfSha256"])
    assert requested == [limit + 1]


@pytest.mark.parametrize("mutation", ["missing_hash", "invalid_hash", "missing_blocks", "wrong_pdf", "mixed_asset", "unbound_instance",
                                       "wrong_container", "duplicate_instance", "bad_matrix", "nan_matrix", "huge_matrix", "bad_array",
                                       "unknown_field", "unknown_display", "duplicate_definition"])
def test_blocks_reject_bad_binding_and_malformed_records(block_input, tmp_path, mutation):
    pdf, context, payload = block_input
    if mutation == "missing_hash":
        payload["sources"].pop()
    elif mutation == "invalid_hash":
        payload["sources"][-1]["sha256"] = "invalid"
    elif mutation == "missing_blocks":
        del payload["blocks"]
    elif mutation == "wrong_pdf":
        payload["pdfSha256"] = "0" * 64
    elif mutation == "mixed_asset":
        payload["blocks"]["instances"][0]["entity_id"] = "drawing_other:P"
    elif mutation == "unbound_instance":
        payload["blocks"]["instances"][0]["definition_id"] = "block:999"
    elif mutation == "wrong_container":
        payload["blocks"]["instances"][0]["container_id"] = "block:2"
    elif mutation == "duplicate_instance":
        payload["blocks"]["instances"].append(copy.deepcopy(payload["blocks"]["instances"][0]))
    elif mutation == "bad_matrix":
        payload["blocks"]["instances"][0]["transform_column_vectors"] = [[1, 2], [3, 4]]
    elif mutation == "nan_matrix":
        payload["blocks"]["instances"][0]["transform_column_vectors"][0][0] = float("nan")
    elif mutation == "huge_matrix":
        payload["blocks"]["instances"][0]["transform_column_vectors"][0][0] = 10 ** 400
    elif mutation == "bad_array":
        payload["blocks"]["instances"][0]["rows"] = 0
    elif mutation == "unknown_field":
        payload["blocks"]["instances"][0]["external_path"] = "outside"
    elif mutation == "unknown_display":
        payload["blocks"]["instances"][0]["display"]["external_path"] = "outside"
    else:
        payload["blocks"]["definitions"].append(copy.deepcopy(payload["blocks"]["definitions"][0]))
    context.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(RuntimeError):
        inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "ev"), context_path=str(context))


def test_legacy_context_remains_supported_and_blocks_are_optional(block_input, tmp_path):
    pdf, context, payload = block_input
    payload["sources"].pop()
    del payload["blocks"]
    context.write_text(json.dumps(payload), encoding="utf-8")
    assert inspect_pdf(str(pdf), evidence_dir=str(tmp_path / "ev"), context_path=str(context))["contextEvidence"] == []
    response = DrawingTools(None).execute("catia_inspect_drawing", {"path": str(pdf), "drawingContextPath": str(context),
        "evidenceDir": str(tmp_path / "ev2"), "geometryOnly": True, "blockQuery": {}})
    assert response["ok"] is False and "no block records" in response["message"]
