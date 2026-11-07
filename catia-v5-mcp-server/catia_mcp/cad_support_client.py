"""Private preparation/verification subprocess boundary; never an agent tool."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import subprocess
import tempfile


def run_support(operation, spec, *, python=None, timeout=120):
    """Run the optional CAD kernel outside the COM interpreter, without retries."""
    if operation not in {"draw", "inspect", "compare", "evaluate", "preview", "edges", "reference"} or not isinstance(spec, dict):
        raise ValueError("Expected a supported operation and a JSON object.")
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive.")
    interpreter = python or os.environ.get("CATIA_CAD_SUPPORT_PYTHON")
    if not interpreter or not Path(interpreter).is_file():
        raise ValueError("Set CATIA_CAD_SUPPORT_PYTHON to the isolated Python executable.")
    with tempfile.TemporaryDirectory(prefix="cad-support-") as directory:
        path = Path(directory) / "spec.json"
        path.write_text(json.dumps(spec, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        try:
            result = subprocess.run(
                [str(interpreter), "-m", "catia_mcp.cad_support", operation, str(path)],
                cwd=Path(__file__).resolve().parents[1], stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": "CAD_SUPPORT_TIMEOUT", "operation": operation,
                    "message": "Helper timed out; inspect output paths before retrying."}
        except OSError as exc:
            return {"ok": False, "code": "CAD_SUPPORT_START_FAILED", "operation": operation,
                    "message": str(exc)}
    diagnostics = result.stderr[-4000:]
    try:
        payload = json.loads(result.stdout)
        if (not isinstance(payload, dict) or type(payload.get("ok")) is not bool
                or payload.get("operation") != operation
                or (payload["ok"] and (result.returncode != 0 or not isinstance(payload.get("data"), dict)))
                or (not payload["ok"] and result.returncode == 0)):
            raise ValueError("Inconsistent helper result")
    except (ValueError, TypeError):
        return {"ok": False, "code": "CAD_SUPPORT_PROTOCOL_ERROR", "operation": operation,
                "exitCode": result.returncode, "diagnostics": diagnostics}
    return {**payload, "exitCode": result.returncode, "diagnostics": diagnostics}
