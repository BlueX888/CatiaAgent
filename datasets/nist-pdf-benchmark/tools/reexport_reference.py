"""Re-export a vendor CATPart reference to STEP through CATIA.

Some NIST STEP files are valid single solids that fail OCCT's shell-soundness
check, which breaks boolean material comparison. The same revision's CATPart
opens in the installed CATIA; exporting it produces a CATIA-written STEP. This
tool records the conversion but does not establish equivalence to the original
STEP; that requires a separate independent comparison.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
REPO = ROOT / "catia-v5-mcp-server"
sys.path.insert(0, str(REPO))

from catia_mcp.connection import CATIAConnection  # noqa: E402
from catia_mcp.locking import automation_lock  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catpart", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--original-step", default=None)
    parser.add_argument("--report", required=True)
    args = parser.parse_args(argv)
    catpart = Path(args.catpart).resolve()
    out = Path(args.out).resolve()
    if out.exists():
        raise SystemExit(f"Refusing to overwrite {out}")
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = CATIAConnection()
    with automation_lock(timeout=600):
        conn.connect()
        doc = conn.documents.Open(str(catpart))
        try:
            doc.Activate()
            doc.ExportData(str(out), "stp")
            volume = None
            try:
                spa = doc.GetWorkbench("SPAWorkbench")
                measurable = spa.GetMeasurable(doc.Part.CreateReferenceFromObject(doc.Part.MainBody))
                volume = round(float(measurable.Volume) * 1e9, 6)
            except Exception:
                volume = None
            body = doc.Part.MainBody
            feature_count = int(body.Shapes.Count)
        finally:
            doc.Close()
    report = {
        "schema": "nist-reference-reexport/1",
        "catpart": str(catpart),
        "step": str(out),
        "catiaVolumeMm3": volume,
        "catiaFeatureCount": feature_count,
        "originalStep": str(Path(args.original_step).resolve()) if args.original_step else None,
    }
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
