"""Cross-process CATIA automation lock."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from catia_mcp.locking import AutomationBusy, automation_lock

REPO = Path(__file__).resolve().parents[1]


def test_lock_can_be_acquired_released_and_reacquired(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(tmp_path / "lock"))
    with automation_lock(timeout=1):
        assert (tmp_path / "lock").exists()
    with automation_lock(timeout=1):
        pass


def test_second_process_times_out_and_reports_busy(tmp_path):
    lock_file = tmp_path / "lock"
    holder = (
        "import sys, time\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from catia_mcp.locking import automation_lock\n"
        "with automation_lock(timeout=5):\n"
        "    print('held', flush=True)\n"
        "    time.sleep(5)\n"
    )
    env = {**os.environ, "CATIA_MCP_LOCK_FILE": str(lock_file)}
    process = subprocess.Popen(
        [sys.executable, "-c", holder],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == "held"
        monkey = pytest.MonkeyPatch()
        monkey.setenv("CATIA_MCP_LOCK_FILE", str(lock_file))
        try:
            with pytest.raises(AutomationBusy):
                with automation_lock(timeout=0.5):
                    pass
        finally:
            monkey.undo()
    finally:
        process.wait(timeout=20)


@pytest.mark.skipif(os.name != "nt", reason="Assisted lock protection uses Windows sharing semantics")
def test_assisted_lock_preserves_file_and_blocks_other_processes(tmp_path, monkeypatch):
    lock_dir = tmp_path / "work" / "catia-agent-lock"
    lock_dir.mkdir(parents=True)
    lock_file = lock_dir / "catia-automation.lock"
    original = b"existing user data"
    lock_file.write_bytes(original)
    monkeypatch.setenv("CATIA_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", "0" * 64)
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(lock_file))
    probe = ("from catia_mcp.locking import automation_lock, AutomationBusy\n"
             "try:\n with automation_lock(timeout=0.2, poll=0.05): print('acquired')\n"
             "except AutomationBusy: print('busy')\n")
    with automation_lock(timeout=1):
        result = subprocess.run([sys.executable, "-c", probe], cwd=REPO, text=True,
                                capture_output=True, check=True, timeout=10)
        assert result.stdout.strip() == "busy"
        with pytest.raises(PermissionError):
            lock_file.unlink()
        with pytest.raises(PermissionError):
            lock_dir.rename(lock_dir.with_name("renamed"))
    assert lock_file.read_bytes() == original
    with automation_lock(timeout=1):
        pass
    assert lock_file.read_bytes() == original


@pytest.mark.skipif(os.name != "nt", reason="Assisted lock protection uses Windows sharing semantics")
def test_assisted_lock_refuses_hard_link_without_modifying_target(tmp_path, monkeypatch):
    lock_file = tmp_path / "catia-automation.lock"
    original = tmp_path / "user-file"
    original.write_bytes(b"preserve")
    os.link(original, lock_file)
    monkeypatch.setenv("CATIA_ARTIFACT_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", "0" * 64)
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(lock_file))
    with pytest.raises(ValueError, match="unlinked"):
        with automation_lock(timeout=1):
            pytest.fail("hard link accepted")
    assert original.read_bytes() == b"preserve"
