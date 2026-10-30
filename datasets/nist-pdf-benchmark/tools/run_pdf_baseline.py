"""Run the current PDF reader (text + OCR) over every original NIST PDF.

This is the T1 baseline: it records what the shipping reader can see, not what
the drawing contains. It writes one evidence manifest per case plus a compact
summary. It never modifies the source PDFs.

    catia-v5-mcp-server/.venv/Scripts/python.exe datasets/nist-pdf-benchmark/tools/run_pdf_baseline.py
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from catia_mcp.drawing.pdf_evidence import inspect_pdf

HERE = Path(__file__).resolve().parent
BENCH = HERE.parent
ROOT = BENCH.parents[1]
OUT = ROOT / "runs" / "nist-pdf-benchmark" / "baseline-20261028"

_DIMENSION_RE = re.compile(r"(?:Ø|DIA|R\s?)\s*\d|\d+\s*[xX×]\s*Ø|\d+\.?\d*\s*±")


def main() -> None:
    manifest = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))
    OUT.mkdir(parents=True, exist_ok=True)
    cases = []
    for case in manifest["cases"]:
        case_id = case["caseId"]
        pdf = ROOT / case["originalPdf"]["path"]
        print(f"== {case_id}: {pdf.name}", flush=True)
        row = {
            "caseId": case_id,
            "pdf": case["originalPdf"]["path"],
            "pages": case["originalPdf"]["pages"],
            "textChars": case["originalPdf"]["textChars"],
        }
        try:
            evidence = inspect_pdf(
                str(pdf),
                evidence_dir=str(OUT / case_id),
                include_ocr=True,
                ocr_dpi=400,
            )
        except Exception as exc:
            row["error"] = f"{type(exc).__name__}: {exc}"
            cases.append(row)
            print("   ERROR", row["error"], flush=True)
            continue
        text_spans = sum(page["textSpanCount"] for page in evidence["pages"])
        ocr_spans = sum(page["ocrSpanCount"] for page in evidence["pages"])
        candidates = [
            candidate
            for page in evidence["pages"]
            for candidate in page["dimensionCandidates"]
            if any(_DIMENSION_RE.search(candidate["text"]) for _ in (candidate,))
        ]
        # Distinct numbers seen in OCR spans, useful to judge coverage without
        # pretending a number is the right dimension.
        numbers = sorted({
            float(value)
            for page in evidence["pages"]
            for span in page["spans"]
            if span.get("source") == "ocr"
            for value in re.findall(r"\d+\.\d+|\d+", span["text"].replace("Ø", " "))
            if value not in ("", ".")
        })
        row.update({
            "textSpanCount": text_spans,
            "ocrSpanCount": ocr_spans,
            "dimensionLikeCandidates": len(candidates),
            "sampleCandidates": [c["text"] for c in candidates[:20]],
            "distinctNumbers": numbers[:80],
            "warnings": evidence["warnings"],
            "evidencePath": evidence["evidencePath"],
        })
        cases.append(row)
        print(f"   text={text_spans} ocr={ocr_spans} dims={len(candidates)}", flush=True)

    baseline = {
        "schema": "nist-pdf-baseline/1",
        "runAt": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        "reader": "catia_inspect_drawing text spans + RapidOCR 400 dpi",
        "note": (
            "Coverage counts what the reader can cite, not whether the drawing is "
            "complete. A number appearing in OCR is not proof that it is the right "
            "dimension."
        ),
        "cases": cases,
    }
    (OUT / "baseline.json").write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n",
                                      encoding="utf-8")
    lines = [
        "# NIST 原始 PDF 读取基线（T1）",
        "",
        f"运行时间：{baseline['runAt']}",
        "",
        "| 案例 | 页数 | 文本 span | OCR span | 尺寸样候选 | 结论 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in cases:
        if "error" in row:
            lines.append(f"| {row['caseId']} | {row['pages']} | - | - | - | 读取失败：{row['error']} |")
        else:
            lines.append(
                f"| {row['caseId']} | {row['pages']} | {row['textSpanCount']} | "
                f"{row['ocrSpanCount']} | {row['dimensionLikeCandidates']} | 可读，需逐项判定是否为正确尺寸 |"
            )
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT / 'baseline.json'}")


if __name__ == "__main__":
    main()
