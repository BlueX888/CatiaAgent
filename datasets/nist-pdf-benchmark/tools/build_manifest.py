"""Build ``datasets/nist-pdf-benchmark/manifest.json`` for the NIST 17-case run.

This is the T0 registry: it records, for every case, the original PDF, its hash,
the available reference CAD files and their hashes, and what the PDF review says
about input sufficiency. It never writes to the NIST source tree.

Run from the repository root with the MCP venv::

    catia-v5-mcp-server/.venv/Scripts/python.exe datasets/nist-pdf-benchmark/tools/build_manifest.py
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pymupdf

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
ROOT = BENCH.parents[1]
NIST = ROOT / "datasets" / "NIST" / "models"

STC = NIST / "NIST_STC_v4" / "NIST-STC-PMI-v4"
HTC = NIST / "HTC" / "NIST-HTC-PMI-v2"
FTC_DEF = NIST / "NIST_FTC_definitions" / "NIST_MBE_PMI_FTC_Definitions"
CTC_DEF = NIST / "NIST_CTC_definitions" / "NIST_MBE_PMI_CTC_Definitions"
FTC_CAD = NIST / "NIST_FTC_CTC" / "NIST-FTC-CTC-PMI-CAD-models"

# caseId, family, pdf, units, revision, primary reference, other references
CASES = [
    ("STC-06", "STC", STC / "STC_06.pdf", "inch", "v4",
     [STC / "CATIA V5-6R2023" / "nist_stc_06_asme1_V56R2023.CATPart",
      STC / "NX 2027" / "nist_stc_06_asme1_NX2027.prt",
      STC / "Creo 10" / "nist_stc_06_asme1_cr10030.prt.3"]),
    ("STC-07", "STC", STC / "STC_07.pdf", "inch", "v4",
     [STC / "CATIA V5-6R2023" / "nist_stc_07_asme1_V56R2023.CATPart",
      STC / "NX 2027" / "nist_stc_07_asme1_NX2027.prt",
      STC / "Creo 10" / "nist_stc_07_asme1_cr10030.prt.1"]),
    ("STC-08", "STC", STC / "STC_08.pdf", "inch", "v4",
     [STC / "CATIA V5-6R2023" / "nist_stc_08_asme1_V56R2023.CATPart",
      STC / "NX 2027" / "nist_stc_08_asme1_NX2027.prt",
      STC / "Creo 10" / "nist_stc_08_asme1_cr10030.prt.1"]),
    ("STC-09", "STC", STC / "STC_09.pdf", "inch", "v4",
     [STC / "CATIA V5-6R2023" / "nist_stc_09_asme1_V5R2023.CATPart",
      STC / "NX 2027" / "nist_stc_09_asme1_NX2027.prt",
      STC / "Creo 10" / "nist_stc_09_asme1_cr10030.prt.1"]),
    ("STC-10", "STC", STC / "STC_10.pdf", "mm", "v4",
     [STC / "CATIA V5-6R2023" / "nist_stc_10_asme1_V56R2023.CATPart",
      STC / "NX 2027" / "nist_stc_10_asme1_NX2027.prt",
      STC / "Creo 10" / "nist_stc_10_asme1_cr10030.prt.1"]),
    ("HTC", "HTC", HTC / "NIST_HTC_20240531.pdf", "mm", "2024-05-31",
     [HTC / "nist_htc_V56R2023_20240530.CATPart",
      HTC / "NIST_HTC_NX2027_20240531.prt",
      HTC / "nist_htc_creo_10020_20240530.prt.1"]),
    ("FTC-06", "FTC", FTC_DEF / "nist_ftc_06_asme1_rd.pdf", "inch", "D",
     [FTC_DEF / "nist_ftc_06_asme1_rd.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ftc_06_asme1_rd_ct5290_ra.CATPart",
      FTC_CAD / "CATIA V5-6R2019" / "NIST_PMI_CATIA_V5-6R2021" / "nist_ftc_06_asme1_ct5315_rd.CATPart"]),
    ("FTC-07", "FTC", FTC_DEF / "nist_ftc_07_asme1_rd.pdf", "inch", "D",
     [FTC_DEF / "nist_ftc_07_asme1_rd.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ftc_07_asme1_rd_ct5290_ra.CATPart"]),
    ("FTC-08", "FTC", FTC_DEF / "nist_ftc_08_asme1_rc.pdf", "inch", "C",
     [FTC_DEF / "nist_ftc_08_asme1_rc.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ftc_08_asme1_rc_ct5290_ra.CATPart"]),
    ("FTC-09", "FTC", FTC_DEF / "nist_ftc_09_asme1_rd.pdf", "inch", "D",
     [FTC_DEF / "nist_ftc_09_asme1_rd.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ftc_09_asme1_rd_ct5290_ra.CATPart"]),
    ("FTC-10", "FTC", FTC_DEF / "nist_ftc_10_asme1_rb.pdf", "mm", "B",
     [FTC_DEF / "nist_ftc_10_asme1_rb.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ftc_10_asme1_rb_ct5290_ra.CATPart",
      FTC_CAD / "CATIA V5-6R2019" / "NIST_PMI_CATIA_V5-6R2021" / "nist_ftc_10_asme1_ct5315_rb.CATPart"]),
    ("FTC-11", "FTC", FTC_DEF / "nist_ftc_11_asme1_rb.pdf", "mm", "B",
     [FTC_DEF / "nist_ftc_11_asme1_rb.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ftc_11_asme1_rb_ct5290_ra.CATPart",
      FTC_CAD / "CATIA V5-6R2019" / "NIST_PMI_CATIA_V5-6R2021" / "nist_ftc_11_asme1_ct5315_rb.CATPart"]),
    ("CTC-01", "CTC", CTC_DEF / "nist_ctc_01_asme1_rd.pdf", "mm", "D",
     [CTC_DEF / "nist_ctc_01_asme1_rd.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ctc_01_asme1_rd_ct5290_ra.CATPart"]),
    ("CTC-02", "CTC", CTC_DEF / "nist_ctc_02_asme1_rc.pdf", "mm", "C",
     [CTC_DEF / "nist_ctc_02_asme1_rc.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ctc_02_asme1_rc_ct5290_ra.CATPart"]),
    ("CTC-03", "CTC", CTC_DEF / "nist_ctc_03_asme1_rc.pdf", "mm", "C",
     [CTC_DEF / "nist_ctc_03_asme1_rc.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ctc_03_asme1_rc_ct5290_ra.CATPart"]),
    ("CTC-04", "CTC", CTC_DEF / "nist_ctc_04_asme1_rd.pdf", "mm", "D",
     [CTC_DEF / "nist_ctc_04_asme1_rd.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ctc_04_asme1_rd_ct5290_ra.CATPart"]),
    ("CTC-05", "CTC", CTC_DEF / "nist_ctc_05_asme1_rd.pdf", "mm", "D",
     [CTC_DEF / "nist_ctc_05_asme1_rd.stp",
      FTC_CAD / "CATIA V5-6R2019" / "nist_ctc_05_asme1_rd_ct5290_ra.CATPart"]),
]

# T0 review result per case. ``sufficiency`` is deliberate: it records what the
# original PDF alone can define, not what the reference model contains.
REVIEW = {
    "STC-06": ("image_only", "3D PDF: no dimension text layer, but the page render shows the PMI callouts as image content and the saved views cover several orientations; usable only through the image-reading path. Reference CATPart is V5-6R2023 and does not open in B31 SP7."),
    "STC-07": ("image_only", "3D PDF: no dimension text layer, but the page render shows the PMI callouts as image content and the saved views cover several orientations; usable only through the image-reading path. Reference CATPart is V5-6R2023 and does not open in B31 SP7."),
    "STC-08": ("image_only", "3D PDF: no dimension text layer, but the page render shows the PMI callouts as image content and the saved views cover several orientations; usable only through the image-reading path. Reference CATPart is V5-6R2023 and does not open in B31 SP7."),
    "STC-09": ("image_only", "3D PDF: no dimension text layer, but the page render shows the PMI callouts as image content and the saved views cover several orientations; usable only through the image-reading path. Reference CATPart is V5-6R2023 and does not open in B31 SP7."),
    "STC-10": ("image_only", "3D PDF: no dimension text layer, but the page render shows the PMI callouts as image content and the saved views cover several orientations; usable only through the image-reading path. Reference CATPart is V5-6R2023 and does not open in B31 SP7."),
    "HTC": ("image_only", "3D PDF: 11 saved 3D views; no dimension text layer, callouts are image content. Reference CATPart is V5-6R2023 and does not open in B31 SP7."),
    "FTC-06": ("insufficient", "Drawing note 1-2 require the CAD model; page text contains notes and position callouts only, no size dimensions."),
    "FTC-07": ("insufficient", "Drawing note 1-2 require the CAD model; a few hole callouts exist but overall and position dimensions are model-defined."),
    "FTC-08": ("insufficient", "Drawing note 1-2 require the CAD model; text carries tolerance frames, not a complete dimension set."),
    "FTC-09": ("insufficient", "Drawing note 1-2 require the CAD model; only a subset of hole diameters and the sheet thickness are on the drawing."),
    "FTC-10": ("insufficient", "Drawing note 1-2 require the CAD model; callouts cover holes/tolerances but the body, pocket and positions are model-defined."),
    "FTC-11": ("candidate", "OD 63, ID 32, thickness 1.93, edge radius R1.5 and units are on the drawing; the raised-rim section is shown in View B. Candidate for direct modelling; needs a frozen review before acceptance."),
    "CTC-01": ("insufficient", "Drawing note 1 requires undimensioned features from the model; page text has only a few callouts."),
    "CTC-02": ("insufficient", "Drawing note 1 requires undimensioned features from the model; page text is notes only."),
    "CTC-03": ("insufficient", "Drawing note 1 requires undimensioned features from the model; page text has only a few callouts."),
    "CTC-04": ("insufficient", "Drawing note 1 requires undimensioned features from the model; page text has only a few callouts."),
    "CTC-05": ("insufficient", "Drawing note 1 requires undimensioned features from the model; page text is notes only."),
}


# NIST README-240318: "The STC CAD models are simplified versions of the NIST
# FTC models"; the numbering maps 1:1 and the geometry is identical. The STC
# CATPart itself is V5-6R2023 and cannot be opened here, but the FTC STEP is
# the same geometry and is measurable.
GEOMETRY_EQUIVALENT = {
    f"STC-{n:02d}": FTC_DEF / f"nist_ftc_{n:02d}_asme1_{'rc' if n == 8 else 'rd' if n != 10 else 'rb'}.stp"
    for n in range(6, 11)
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def pdf_stats(path: Path) -> dict:
    doc = pymupdf.open(path)
    chars = sum(len(page.get_text()) for page in doc)
    images = sum(len(page.get_images(full=True)) for page in doc)
    return {
        "pages": doc.page_count,
        "textChars": chars,
        "images": images,
        "hasUsableTextLayer": chars >= 400,
    }


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def main() -> None:
    cases = []
    for case_id, family, pdf, units, revision, refs in CASES:
        missing = [rel(p) for p in [pdf, *refs] if not p.is_file()]
        if missing:
            raise SystemExit(f"{case_id}: missing files: {missing}")
        sufficiency, reason = REVIEW[case_id]
        cases.append({
            "caseId": case_id,
            "family": family,
            "originalPdf": {
                "path": rel(pdf),
                "sha256": sha256(pdf),
                "bytes": pdf.stat().st_size,
                **pdf_stats(pdf),
            },
            "units": units,
            "revision": revision,
            "geometryEquivalentReference": (
                {"path": rel(GEOMETRY_EQUIVALENT[case_id]),
                 "sha256": sha256(GEOMETRY_EQUIVALENT[case_id]),
                 "basis": "NIST README-240318: STC models are simplified versions of the FTC models; geometry is identical."}
                if case_id in GEOMETRY_EQUIVALENT else None
            ),
            "referenceModels": [
                {"role": "primary" if index == 0 else f"alternate{index}", "path": rel(path),
                 "sha256": sha256(path), "bytes": path.stat().st_size}
                for index, path in enumerate(refs)
            ],
            "inputReview": {
                "sufficiency": sufficiency,
                "reason": reason,
                "derivedPdfRequired": sufficiency != "sufficient",
                "derivedPdfStatus": "not_required" if sufficiency == "sufficient" else "planned",
            },
            "acceptance": {
                "featureListFrozen": False,
                "expectedValuesFrozen": False,
                "completeModelVerified": False,
            },
        })
    manifest = {
        "schema": "nist-pdf-benchmark-manifest/1",
        "generated": date.today().isoformat(),
        "sourceRoot": "datasets/NIST/models",
        "caseCount": len(cases),
        "notes": [
            "The original PDF is always run as-is; derived drawings never replace that baseline.",
            "Reference models and frozen expectations are verification-side inputs; the generation side must not read them.",
            "Primary reference for FTC/CTC is the AP203 STEP, which this CATIA release opens; STC/HTC only ship CATPart/NX/Creo and the CATPart version must be checked in CATIA B31 SP7.",
        ],
        "cases": cases,
    }
    out = BENCH / "manifest.json"
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {out} ({len(cases)} cases)")


if __name__ == "__main__":
    main()
