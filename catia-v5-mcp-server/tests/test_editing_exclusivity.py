"""Offline tests for write exclusivity while an edit session owns a document.

``active_session_ids`` decides which sessions still hold a working copy. A
failed or rolled-back session keeps the document (recovery/finalize may still
need it), a session whose document is closed is abandoned automatically, and a
plan-level rejection never owns anything.
"""

from __future__ import annotations

from typing import Any

import pytest

from catia_mcp.editing.journal import Journal
from catia_mcp.tools.editing import EditingTools
from _editing_fakes import FakeApp, FakeDocument, FakeDocuments, FakeConnection

SESSION_ID = "sess-exclusive"


def session_record(session_id: str, working_path: str, status: str) -> dict[str, Any]:
    return {
        "schema": "catia-edit-session/1",
        "editSessionId": session_id,
        "modelRevision": 0,
        "createdUtc": "2026-10-24T00:00:00.000+00:00",
        "status": status,
        "source": {
            "path": f"C:\\work\\source\\{session_id}.CATPart",
            "sha256": "a" * 64,
            "sizeBytes": 196608,
            "mtimeUtc": "2026-10-24T00:00:00.000+00:00",
        },
        "workingCopy": {
            "path": working_path,
            "sha256": "b" * 64,
            "createdUtc": "2026-10-24T00:00:01.000+00:00",
        },
        "impacts": {},
        "results": [],
    }


@pytest.fixture()
def tools(tmp_path) -> EditingTools:
    tool = EditingTools(FakeConnection(FakeApp(FakeDocuments())))
    tool.coordinator.journal = Journal(tmp_path / "exclusivity.sqlite3")
    return tool


def add_session(tool: EditingTools, session_id: str, status: str, *, document_open: bool) -> str:
    working_path = f"C:\\work\\edits\\{session_id}.CATPart"
    tool.coordinator.journal.save_session(session_record(session_id, working_path, status))
    if document_open:
        tool.coordinator.conn.app.Documents.add(FakeDocument(working_path))
    return working_path


def stored_status(tool: EditingTools, session_id: str) -> str:
    record = tool.coordinator.journal.load_session(session_id)
    assert record is not None
    return record["status"]


@pytest.mark.parametrize("status", ["failed", "rolled_back"])
def test_failed_and_rolled_back_sessions_still_own_an_open_working_copy(tools: EditingTools, status: str) -> None:
    add_session(tools, SESSION_ID, status, document_open=True)

    active = tools.active_session_ids()

    assert active == [SESSION_ID]
    assert stored_status(tools, SESSION_ID) == status


def test_failed_session_with_an_open_copy_blocks_generic_writers(tools: EditingTools) -> None:
    add_session(tools, SESSION_ID, "failed", document_open=True)

    blocked = tools.guard("catia_part_design_pad")

    assert blocked is not None
    assert blocked["ok"] is False
    assert blocked["code"] == "BUSY"
    assert blocked["data"]["activeSessions"] == [SESSION_ID]
    assert blocked["effects"]["modified"] is False
    # Read-only tools are never blocked.
    assert tools.guard("catia_get_parameters") is None


@pytest.mark.parametrize("status", ["failed", "rolled_back", "ready", "verified", "unknown"])
def test_closed_working_copy_is_abandoned_automatically(tools: EditingTools, status: str) -> None:
    add_session(tools, SESSION_ID, status, document_open=False)

    active = tools.active_session_ids()

    assert active == []
    assert stored_status(tools, SESSION_ID) == "abandoned"
    events = tools.coordinator.journal.events(SESSION_ID)
    assert [event["kind"] for event in events] == ["session_abandoned"]
    assert events[0]["payload"]["reason"] == "working copy is not open"


def test_rejected_session_is_not_active_and_is_not_abandoned(tools: EditingTools) -> None:
    add_session(tools, SESSION_ID, "rejected", document_open=False)

    active = tools.active_session_ids()

    assert active == []
    # A rejected plan is only a verdict: there is no owned document to abandon.
    assert stored_status(tools, SESSION_ID) == "rejected"
    assert tools.coordinator.journal.events(SESSION_ID) == []


def test_only_open_owning_sessions_are_returned_from_a_mixed_journal(tools: EditingTools) -> None:
    add_session(tools, "sess-failed-open", "failed", document_open=True)
    add_session(tools, "sess-rolled-back-closed", "rolled_back", document_open=False)
    add_session(tools, "sess-rejected-closed", "rejected", document_open=False)
    add_session(tools, "sess-ready-open", "ready", document_open=True)
    add_session(tools, "sess-completed-open", "completed", document_open=True)

    active = set(tools.active_session_ids())

    assert active == {"sess-failed-open", "sess-ready-open"}
    assert stored_status(tools, "sess-rolled-back-closed") == "abandoned"
    assert stored_status(tools, "sess-rejected-closed") == "rejected"
    assert stored_status(tools, "sess-completed-open") == "completed"
