import json
import subprocess
import sys

import pytest

from catia_mcp.cad_support_client import run_support


@pytest.mark.parametrize("output,code", [
    ("not JSON", 0),
    (json.dumps({"ok": True, "operation": "inspect", "data": {}}), 1),
    (json.dumps({"ok": False, "operation": "inspect"}), 0),
    (json.dumps({"ok": True, "operation": "compare", "data": {}}), 0),
])
def test_inconsistent_child_cannot_report_success(monkeypatch, output, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, code, output, "diagnostic"))
    result = run_support("inspect", {}, python=sys.executable)
    assert result["ok"] is False
    assert result["code"] == "CAD_SUPPORT_PROTOCOL_ERROR"


def test_timeout_is_not_retried(monkeypatch):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["stdin"] == subprocess.DEVNULL
        raise subprocess.TimeoutExpired(command, 1)
    monkeypatch.setattr(subprocess, "run", run)
    assert run_support("draw", {}, python=sys.executable)["code"] == "CAD_SUPPORT_TIMEOUT"
    assert len(calls) == 1


def test_real_child_failure_preserved():
    result = run_support("inspect", {"sourceStep": "missing.step"}, python=sys.executable)
    assert result["ok"] is False
    assert result["exitCode"] == 1
    assert result["error"]
