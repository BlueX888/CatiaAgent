"""Overlay an invisible text layer matching cadgen's note rows.

cadgen renders notes as stroke outlines, so the PDF has no extractable text.
This preparation step inserts the exact drawn strings at the same positions
(invisible render mode) so a reader gets the schedule without OCR. It is
verification-side tooling; the PDF remains the only generation input.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf

MARGIN_MM = 10.0
TITLE_H_MM = 40.0
NOTE_INSET_MM = 3.0
NOTE_HEIGHT_MM = 2.5
NOTE_ROW_MM = 5.0
PREFIX = "DERIVED DRAWING - NOMINAL GEOMETRY"


def note_lines(page_notes):
    drawn = [PREFIX, *page_notes]
    return [f"{'NOTES:  ' if index == 0 else '        '}{index + 1}. {note}"
            for index, note in enumerate(drawn)]


def apply(pdf_path: Path, pages_notes: list[list[str]]) -> dict:
    doc = pymupdf.open(str(pdf_path))
    if doc.page_count != len(pages_notes):
        raise ValueError(f"page count {doc.page_count} != notes pages {len(pages_notes)}")
    inserted = 0
    for index, notes in enumerate(pages_notes):
        page = doc[index]
        height_pt = page.rect.height
        mm_to_pt = 72.0 / 25.4
        lines = note_lines(notes)
        for row, line in enumerate(lines):
            y_mm = MARGIN_MM + TITLE_H_MM + 4.0 + (len(lines) - 1 - row) * NOTE_ROW_MM
            x_pt = (MARGIN_MM + NOTE_INSET_MM) * mm_to_pt
            y_pt = height_pt - (y_mm + 0.35 * NOTE_HEIGHT_MM) * mm_to_pt
            page.insert_text((x_pt, y_pt), line, fontsize=NOTE_HEIGHT_MM * mm_to_pt,
                             fontname="helv", render_mode=3, color=(0, 0, 0))
            inserted += 1
    temporary = pdf_path.with_suffix(".withtext.pdf")
    doc.save(str(temporary), garbage=3, deflate=True)
    doc.close()
    temporary.replace(pdf_path)
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    provenance_path = pdf_path.with_suffix(".provenance.json")
    if provenance_path.exists():
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        provenance.setdefault("pdf", {})["sha256"] = digest
        provenance["textLayer"] = {
            "rows": inserted,
            "note": "Invisible text identical to the drawn note rows; no values added.",
        }
        provenance_path.write_text(json.dumps(provenance, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    return {"rows": inserted, "sha256": digest}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf")
    parser.add_argument("spec")
    args = parser.parse_args(argv)
    spec = json.loads(Path(args.spec).read_text(encoding="utf-8"))
    result = apply(Path(args.pdf), [sheet.get("notes", []) for sheet in spec["sheets"]])
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
