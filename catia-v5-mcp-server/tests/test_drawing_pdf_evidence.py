"""Offline tests for PDF evidence extraction (T1).

A synthetic PDF is generated in the test so the suite has no binary fixture and
no dependency on the dataset directory. No CATIA, no win32com.
"""

from __future__ import annotations

import json
import os

import pytest

from catia_mcp.drawing.pdf_evidence import crop_pdf_region, inspect_pdf


def test_ocr_subprocess_receives_private_png_snapshot(monkeypatch):
    import subprocess
    from types import SimpleNamespace
    import catia_mcp.drawing.pdf_evidence as evidence

    class Pixmap:
        def tobytes(self, kind):
            assert kind == "png"
            return b"\x89PNG\r\n\x1a\nverified-pixels"
    class Page:
        def get_pixmap(self, dpi):
            assert dpi == 200
            return Pixmap()
    def run(command, **kwargs):
        assert command[-2] == "-"
        assert kwargs["input"] == b"\x89PNG\r\n\x1a\nverified-pixels"
        assert "stdin" not in kwargs
        return SimpleNamespace(returncode=0, stdout=b'[{"text":"42","confidence":0.9,"bboxPx":[0,0,10,10]}]', stderr=b"")
    monkeypatch.setattr(subprocess, "run", run)
    assert evidence._ocr_spans(Page(), 0, dpi=200)[0]["text"] == "42"

def _require_pymupdf():
    try:
        import pymupdf  # noqa: F401
    except ImportError:  # pragma: no cover - install issue
        try:
            import fitz  # noqa: F401
        except ImportError:
            pytest.skip("PyMuPDF is not installed")


@pytest.fixture()
def drawing(tmp_path):
    _require_pymupdf()
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)  # A4 landscape in points
    page.insert_text((60, 80), "UNITS: MILLIMETRES", fontsize=12)
    page.insert_text((60, 120), "120", fontsize=12)
    page.insert_text((160, 120), "80", fontsize=12)
    page.insert_text((60, 160), "10", fontsize=12)
    page.insert_text((60, 200), "4x Ø10 THRU", fontsize=12)
    page.insert_text((60, 240), "20", fontsize=12)
    path = tmp_path / "drawing.pdf"
    doc.save(str(path))
    doc.close()
    return path


def test_inspect_returns_stable_span_ids_and_millimetre_boxes(drawing, tmp_path):
    manifest = inspect_pdf(str(drawing), evidence_dir=str(tmp_path / "evidence"))
    assert manifest["schema"] == "catia-pdf-evidence/1"
    assert manifest["source"]["pageCount"] == 1
    spans = manifest["pages"][0]["spans"]
    assert [s["id"] for s in spans] == [f"p1:s{i}" for i in range(1, len(spans) + 1)]
    assert any(s["text"] == "120" for s in spans)
    for span in spans:
        x0, y0, x1, y1 = span["bboxMm"]
        assert 0 <= x0 < x1 <= 297
        assert 0 <= y0 < y1 <= 210
    assert os.path.isfile(manifest["evidencePath"])
    saved = json.loads(open(manifest["evidencePath"], encoding="utf-8").read())
    assert saved["source"]["sha256"] == manifest["source"]["sha256"]


def test_candidates_keep_callout_semantics(drawing, tmp_path):
    manifest = inspect_pdf(str(drawing), evidence_dir=str(tmp_path / "evidence"))
    candidates = manifest["pages"][0]["dimensionCandidates"]
    callout = next(c for c in candidates if "THRU" in c["text"])
    assert callout["kindHint"] == "diameter"
    assert callout["countHint"] == 4
    assert callout["throughHint"] is True
    assert "Ø" in callout["text"]


def test_unit_hints_are_reported(drawing, tmp_path):
    manifest = inspect_pdf(str(drawing), evidence_dir=str(tmp_path / "evidence"))
    hints = manifest["pages"][0]["unitHints"]
    assert hints and hints[0]["unit"] == "mm"


def test_same_page_without_ocr_keeps_prior_ocr_citations(drawing, tmp_path, monkeypatch):
    import json
    import catia_mcp.drawing.pdf_evidence as evidence
    span = {"id": "p1:o1", "text": "38", "source": "ocr", "confidence": 0.9,
            "bboxMm": [1, 2, 3, 4]}
    monkeypatch.setattr(evidence, "_ocr_spans", lambda *args, **kwargs: [span])
    directory = tmp_path / "evidence"
    inspect_pdf(str(drawing), pages=[1], evidence_dir=str(directory), include_ocr=True)
    repeated = inspect_pdf(str(drawing), pages=[1], evidence_dir=str(directory))
    assert span in repeated["pages"][0]["spans"]
    assert repeated["pages"][0]["ocrRetained"] is True
    stored = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
    assert span in stored["pages"][0]["spans"]
    assert stored["pages"][0]["ocrSpanCount"] == 1


def test_changed_ocr_does_not_reassign_existing_citation(drawing, tmp_path, monkeypatch):
    import json
    import catia_mcp.drawing.pdf_evidence as evidence
    state = {"text": "38"}
    monkeypatch.setattr(evidence, "_ocr_spans", lambda *args, **kwargs: [
        {"id": "p1:o1", "text": state["text"], "source": "ocr", "confidence": 0.9, "bboxMm": [1, 2, 3, 4]}])
    directory = tmp_path / "evidence"
    inspect_pdf(str(drawing), evidence_dir=str(directory), include_ocr=True)
    state["text"] = "83"
    with pytest.raises(RuntimeError, match="OCR evidence changed"):
        inspect_pdf(str(drawing), evidence_dir=str(directory), include_ocr=True)
    stored = json.loads((directory / "evidence.json").read_text(encoding="utf-8"))
    assert next(span for span in stored["pages"][0]["spans"] if span["id"] == "p1:o1")["text"] == "38"


@pytest.mark.parametrize("text,units", [
    ("ALL DIMENSIONS ARE IN MILLIMITERS", ["mm"]),
    ("ALL DIMENSIONS ARE IN", []),
    ("UNITS: IN", ["inch"]),
    ("UNITS mm AND inches", ["mm", "inch"]),
])
def test_unit_hints_ignore_english_prepositions(text, units):
    from catia_mcp.drawing.pdf_evidence import _unit_hints
    assert [h["unit"] for h in _unit_hints([{"id": "p1:o1", "text": text}])] == units


def test_page_selection_and_out_of_range(drawing, tmp_path):
    manifest = inspect_pdf(str(drawing), pages=[1], evidence_dir=str(tmp_path / "ev"))
    assert manifest["source"]["inspectedPages"] == [1]
    with pytest.raises(RuntimeError, match="outside 1..1"):
        inspect_pdf(str(drawing), pages=[2], evidence_dir=str(tmp_path / "ev"))


def test_truncated_pdf_is_an_explicit_error(drawing, tmp_path):
    broken = tmp_path / "broken.pdf"
    data = drawing.read_bytes()
    broken.write_bytes(data[: int(len(data) * 0.6)])
    with pytest.raises(RuntimeError, match="damaged|truncated|cannot be opened"):
        inspect_pdf(str(broken), evidence_dir=str(tmp_path / "ev"))


def test_missing_and_non_pdf_inputs(drawing, tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        inspect_pdf(str(tmp_path / "nope.pdf"))
    txt = tmp_path / "drawing.txt"
    txt.write_text("120")
    with pytest.raises(RuntimeError, match="Only PDF"):
        inspect_pdf(str(txt))


def test_pdf_without_text_layer_is_rejected(tmp_path):
    _require_pymupdf()
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=842, height=595)
    page.draw_rect(pymupdf.Rect(60, 60, 400, 300), color=(0, 0, 0), width=1)
    path = tmp_path / "scan.pdf"
    doc.save(str(path))
    doc.close()
    with pytest.raises(RuntimeError, match="no text layer"):
        inspect_pdf(str(path), evidence_dir=str(tmp_path / "ev"))


def test_extensionless_upload_is_detected_by_header(drawing, tmp_path):
    # AgentCPM stores uploads under an opaque id without a .pdf suffix.
    upload = tmp_path / "4b53b4d5-e51f-4fd3-82da-1eac92272621"
    upload.write_bytes(drawing.read_bytes())
    manifest = inspect_pdf(str(upload), evidence_dir=str(tmp_path / "ev"))
    assert manifest["source"]["pageCount"] == 1
    assert any(span["text"] == "120" for span in manifest["pages"][0]["spans"])


def test_crop_writes_a_png(drawing, tmp_path):
    result = crop_pdf_region(
        str(drawing), 1, [10, 10, 120, 80], dpi=120, output_path=str(tmp_path / "crop.png")
    )
    assert result["ok"] is True
    assert os.path.getsize(result["path"]) > 0
    with pytest.raises(RuntimeError, match="bboxMm"):
        crop_pdf_region(str(drawing), 1, [10, 10, 5, 80])


def _require_rapidocr():
    try:
        import rapidocr_onnxruntime  # noqa: F401
    except ImportError:  # pragma: no cover - optional dependency
        pytest.skip("rapidocr-onnxruntime is not installed")


def test_ocr_adds_citable_spans_for_an_image_only_page(tmp_path):
    """A page with no text layer still yields evidence ids when OCR is on.

    NIST FTC/CTC drawings store dimensions as vector outlines, so the text
    layer is notes-only. This builds the same situation with a raster image.
    """
    _require_pymupdf()
    _require_rapidocr()
    import pymupdf

    source = pymupdf.open()
    src_page = source.new_page(width=842, height=595)
    src_page.insert_text((100, 200), "120", fontsize=48)
    pixmap = src_page.get_pixmap(dpi=200)
    image_path = tmp_path / "text.png"
    pixmap.save(str(image_path))

    image_doc = pymupdf.open()
    page = image_doc.new_page(width=842, height=595)
    page.insert_image(page.rect, filename=str(image_path))
    pdf_path = tmp_path / "image-only.pdf"
    image_doc.save(str(pdf_path))
    image_doc.close()
    source.close()

    with pytest.raises(RuntimeError):
        inspect_pdf(str(pdf_path), evidence_dir=str(tmp_path / "no-ocr"))

    manifest = inspect_pdf(
        str(pdf_path), evidence_dir=str(tmp_path / "ocr"), include_ocr=True
    )
    page_row = manifest["pages"][0]
    assert page_row["textSpanCount"] == 0
    assert page_row["ocrSpanCount"] >= 1
    ocr_spans = [s for s in page_row["spans"] if s.get("source") == "ocr"]
    number_spans = [span for span in ocr_spans if "120" in span["text"]]
    assert number_spans
    # Full-page and rotated tile readings must map back to the actual label,
    # at (100, 200) PDF points, rather than keep tile-local pixel coordinates.
    for span in number_spans:
        x0, y0, x1, y1 = span["bboxMm"]
        assert 33 < (x0 + x1) / 2 < 66
        assert 48 < (y0 + y1) / 2 < 73
    assert all(span["id"].startswith("p1:o") for span in ocr_spans)
    assert any(w["code"] == "PAGE_TEXT_LAYER_ABSENT_OCR_USED" for w in manifest["warnings"])


def test_page_subset_inspection_merges_evidence(tmp_path):
    from catia_mcp.drawing.pdf_evidence import inspect_pdf
    import json
    source = tmp_path / "two.pdf"
    import pymupdf
    document = pymupdf.open()
    for text in ("UNITS MM", "PAGE TWO"):
        page = document.new_page()
        page.insert_text((50, 50), text)
    document.save(str(source))
    document.close()
    first = inspect_pdf(str(source), [1], str(tmp_path / "evidence"))
    assert [p["page"] for p in first["pages"]] == [1]
    second = inspect_pdf(str(source), [2], str(tmp_path / "evidence"))
    assert [p["page"] for p in second["pages"]] == [2]
    stored = json.loads((tmp_path / "evidence" / "evidence.json").read_text(encoding="utf-8"))
    assert [p["page"] for p in stored["pages"]] == [1, 2]
    assert stored["source"]["inspectedPages"] == [1, 2]
