"""Optional preview generation; it never changes the engineering verdict."""
import os
from pathlib import Path
import tempfile

from catia_mcp.cad_support_client import run_support
from catia_mcp.drawing.verify import run_independent_verifier


def create_part_preview(part_path, expectation, *, step_path=None, tolerances=None):
    if not os.environ.get("CATIA_CAD_SUPPORT_PYTHON"):
        return {"status": "unavailable", "message": "CAD preview interpreter is not configured"}
    try:
        with tempfile.TemporaryDirectory(prefix="catia-preview-") as temporary:
            if step_path is None:
                step_path = str(Path(temporary) / "generated.step")
                report = run_independent_verifier(part_path, expectation, tolerances=tolerances, export_step=step_path)
                if report.get("verdict") != "PASS":
                    return {"status": "failed", "message": "Preview export verification failed"}
            result = run_support("preview", {"sourceStep": str(step_path),
                "outputMesh": str(Path(part_path).with_suffix(".preview.json"))})
            return result["data"] if result["ok"] else {"status": "failed", "message": str(result.get("error", result.get("code")))}
    except Exception as exc:
        return {"status": "failed", "message": str(exc)}
