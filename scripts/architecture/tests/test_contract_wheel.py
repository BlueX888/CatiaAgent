"""T13 wheel check: packaged contracts load and compile outside the repo."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "check_contract_wheel.py"
REPO_ROOT = Path(__file__).resolve().parents[3]
SERVER_ROOT = REPO_ROOT / "catia-v5-mcp-server"
REQUIRED = {"wheel_built", "contracts_packaged", "dependency_metadata",
            "isolated_import", "schema_compilation"}


def run(server_root: Path, out: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--server-root", str(server_root), "--out", str(out)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=900,
    )


def test_wheel_check_passes_and_records_every_check(tmp_path):
    out = tmp_path / "check.json"
    result = run(SERVER_ROOT, out)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "contract-wheel-check/1"
    assert {item["id"] for item in payload["checks"]} == REQUIRED
    assert all(item["passed"] is True for item in payload["checks"])
    for item in payload["checks"]:
        assert Path(item["evidence_path"]).is_file(), item


def test_missing_server_root_fails_closed(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "check.json"
    result = run(empty, out)
    assert result.returncode != 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    by_id = {item["id"]: item for item in payload["checks"]}
    assert set(by_id) == REQUIRED
    assert by_id["wheel_built"]["passed"] is False
    assert by_id["contracts_packaged"]["passed"] is False
