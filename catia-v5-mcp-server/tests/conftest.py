"""Test isolation: every test gets its own edit journal."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_edit_journal(tmp_path, monkeypatch):
    monkeypatch.setenv("CATIA_MCP_EDIT_DB", str(tmp_path / "editing.sqlite3"))
