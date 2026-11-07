"""Run the independent verifier as a separate process.

The builder's own readbacks are useful for stopping a bad step early, but they
share the process, the connection and the code with the code that created the
geometry. The delivery decision therefore comes from
``scripts/independent_verify_part.py``: late-bound COM in another process opens
the saved file from disk. It shares only the optional CATIA instance selector.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("catia_mcp.drawing.verify")


def _verifier_path() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts" / "independent_verify_part.py"


def _work_root() -> Path:
    configured = os.environ.get("CATIA_MCP_WORK_DIR")
    if configured:
        return Path(configured)
    return Path(tempfile.gettempdir()) / "catia-mcp" / "pdf-modeling"


def run_independent_verifier(
    part_path: str,
    expectation: dict[str, Any],
    *,
    tolerances: dict[str, Any] | None = None,
    python_executable: str | None = None,
    timeout: int = 120,
    export_step: str | None = None,
    parameter_changes: dict | None = None,
    collect_cylindrical_faces: bool = True,
    edit_copy_of: str | None = None,
) -> dict[str, Any]:
    """Verify ``part_path`` against ``expectation`` in a separate process."""
    if not collect_cylindrical_faces:
        cylinder_keys = {"holes", "holeCentres", "cylinders", "fullCylinders"}
        if not parameter_changes or cylinder_keys.intersection(expectation):
            return {
                "verdict": "FAIL",
                "checks": [{"name": "cylindrical_faces_collection", "passed": False,
                            "detail": "Skipping is restricted to parameter-copy verification without cylinder expectations."}],
                "error": "invalid_cylindrical_faces_skip",
            }
    verifier = _verifier_path()
    if not verifier.is_file():
        return {
            "verdict": "FAIL",
            "checks": [{"name": "verifier_available", "passed": False,
                        "detail": f"missing verifier script: {verifier}"}],
            "error": "verifier_missing",
        }
    if not Path(part_path).is_file():
        return {
            "verdict": "FAIL",
            "checks": [{"name": "file_exists", "passed": False, "detail": part_path}],
            "error": "part_missing",
        }

    work = _work_root()
    work.mkdir(parents=True, exist_ok=True)
    spec_path = work / f"verify-spec-{os.getpid()}.json"
    spec = {
        "schema": "catia-build-verification/1",
        "partPath": str(Path(part_path).resolve()),
        "expected": expectation,
        "tolerances": tolerances or {"lengthMm": 0.01, "coordinateMm": 0.01, "volumeMm3": 0.01},
    }
    if export_step is not None:
        spec["exportStep"] = str(Path(export_step).resolve())
    if parameter_changes:
        spec["parameterChanges"] = parameter_changes
    if edit_copy_of is not None:
        spec["editCopyOf"] = str(Path(edit_copy_of).resolve())
    if not collect_cylindrical_faces:
        spec["collectCylindricalFaces"] = False
    spec_path.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")
    progress = Path(str(spec_path) + ".progress")
    try:
        progress.unlink()
    except OSError:
        pass
    command = [python_executable or sys.executable, str(verifier), str(spec_path)]
    logger.info("verifier start: %s (exe=%s)", command, sys.executable)
    started = time.monotonic()
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(verifier.parents[1]),
            creationflags=creationflags,
        )
    except Exception as exc:
        return {"verdict": "FAIL", "sessionHealth": "unknown", "error": "verifier_start_failed",
                "checks": [{"name": "verifier_start", "passed": False, "detail": type(exc).__name__}]}
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        returncode = process.returncode
    except subprocess.TimeoutExpired:
        # Best effort only: even a terminated client does not prove COM completed.
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10)
            else:
                process.kill()
        except (OSError, subprocess.TimeoutExpired):
            logger.warning("verifier process termination could not be confirmed")
        try:
            process.communicate(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
        logger.error("verifier timeout after %.2fs", time.monotonic() - started)
        return {
            "verdict": "FAIL",
            "sessionHealth": "unknown",
            "checks": [{"name": "verifier_timeout", "passed": False,
                        "detail": f"verifier exceeded {timeout}s"}],
            "error": "verifier_timeout",
        }
    except Exception as exc:
        return {"verdict": "FAIL", "sessionHealth": "unknown", "error": "verifier_communication_failed",
                "checks": [{"name": "verifier_communication", "passed": False,
                            "detail": type(exc).__name__}]}
    logger.info("verifier done: rc=%s elapsed=%.2fs", returncode,
                time.monotonic() - started)
    stdout = stdout.strip()
    report: dict[str, Any] | None = None
    if stdout:
        try:
            report = json.loads(stdout)
        except json.JSONDecodeError:
            # The verifier prints exactly one JSON document; if anything else
            # was printed, keep it as evidence instead of pretending success.
            report = None
    if (not isinstance(report, dict) or report.get("verdict") not in {"PASS", "FAIL"}
            or not isinstance(report.get("checks"), list) or not report["checks"]
            or any(not isinstance(c, dict) or type(c.get("passed")) is not bool for c in report["checks"])):
        return {
            "verdict": "FAIL",
            "sessionHealth": "unknown",
            "checks": [{"name": "verifier_output", "passed": False,
                        "detail": "verifier did not print a JSON report"}],
            "error": "verifier_output_invalid",
            "stdout": stdout[-4000:],
            "stderr": stderr[-4000:],
            "returncode": returncode,
        }
    # A valid failure report from the verifier exits 1; any other mismatch
    # means its COM cleanup cannot be inferred from the output.
    if ((report["verdict"] == "PASS" and returncode != 0)
            or (report["verdict"] == "FAIL" and returncode != 1)
            or (report["verdict"] == "PASS" and not all(c["passed"] for c in report["checks"]))
            or (report["verdict"] == "FAIL" and all(c["passed"] for c in report["checks"]))
            or report.get("sessionHealth") not in (None, "healthy")):
        report["sessionHealth"] = "unknown"
    if (returncode != 0 or report.get("sessionHealth") == "unknown"
            or not all(c["passed"] for c in report["checks"])):
        report["verdict"] = "FAIL"
        if returncode != 0:
            report["checks"].append({"name": "verifier_exit", "passed": False,
                                     "actual": returncode})
    report["specPath"] = str(spec_path)
    report["command"] = command
    report["returncode"] = returncode
    if stderr.strip():
        report["stderr"] = stderr[-4000:]
    return report
