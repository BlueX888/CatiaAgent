"""`begin_edit` must name a missing source as NOT_FOUND, not as a plan error (踩坑 70)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from catia_mcp.editing.coordinator import EditCoordinator
from catia_mcp.editing.journal import Journal


@pytest.fixture()
def coordinator(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> EditCoordinator:
    monkeypatch.setenv("CATIA_MCP_EDIT_DB", str(tmp_path / "journal.db"))
    return EditCoordinator(None, journal=Journal())


def test_missing_source_is_not_found(coordinator: EditCoordinator, tmp_path: Path) -> None:
    missing = str(tmp_path / "moved-away.CATPart")
    result = coordinator.begin_edit(missing)

    assert result["ok"] is False
    assert result["code"] == "NOT_FOUND"
    assert "does not exist" in result["message"]
    assert missing in result["message"]
    assert result["effects"]["modified"] is False
    assert result["recovery"]["automatic_retry_allowed"] is False


def test_missing_source_does_not_touch_catia(coordinator: EditCoordinator, tmp_path: Path) -> None:
    """A failed precondition must not reach the connection object."""
    result = coordinator.begin_edit(str(tmp_path / "absent.CATPart"))
    assert result["code"] == "NOT_FOUND"


def test_relative_source_stays_invalid_plan(coordinator: EditCoordinator) -> None:
    result = coordinator.begin_edit("relative\\part.CATPart")
    assert result["code"] == "INVALID_PLAN"


def test_non_catpart_stays_unsupported(coordinator: EditCoordinator, tmp_path: Path) -> None:
    other = tmp_path / "part.CATProduct"
    other.write_bytes(b"x")
    result = coordinator.begin_edit(str(other))
    assert result["code"] == "UNSUPPORTED_CAPABILITY"
