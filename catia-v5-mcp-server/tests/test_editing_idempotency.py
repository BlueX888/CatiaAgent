"""Offline tests for edit-operation idempotency.

``catia_apply_edit_plan`` must answer an exact retry from the journal instead
of writing a second time, must not let one session's operation id overwrite
(or replay) another session's result, and must never re-execute an operation
that recovery already reconciled or validation already rejected.
"""

from __future__ import annotations

from typing import Any

import pytest

import catia_mcp.editing.coordinator as coordinator_module
from catia_mcp.editing.coordinator import EditCoordinator
from catia_mcp.editing.journal import Journal
from catia_mcp.editing.validation import PLAN_SCHEMA, plan_hash
from _editing_fakes import (
    FakeApp,
    FakeConnection,
    FakeDocument,
    FakeParameter,
    FakePart,
    read_model_from_part,
)

DRIVER = "Part2\\L"
OPERATION_ID = "op-1"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def make_plan(
    session_id: str,
    *,
    operation_id: str = OPERATION_ID,
    old_value: float = 160.0,
    new_value: float = 200.0,
    expected_revision: int = 0,
) -> dict[str, Any]:
    return {
        "schema": PLAN_SCHEMA,
        "editSessionId": session_id,
        "operationId": operation_id,
        "expectedRevision": expected_revision,
        "intent": {"text": f"把长度改成 {new_value} mm", "resolved": {"length": new_value}},
        "changes": [
            {
                "parameter": DRIVER,
                "oldValue": old_value,
                "newValue": new_value,
                "unit": "mm",
                "reason": "idempotency test",
            }
        ],
        "preserve": [],
        "acceptance": [],
    }


def make_session(session_id: str, working_path: str, *, status: str = "ready", revision: int = 0) -> dict[str, Any]:
    return {
        "schema": "catia-edit-session/1",
        "editSessionId": session_id,
        "modelRevision": revision,
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
            "createdUtc": "2026-10-24T00:00:00.000+00:00",
        },
        "impacts": {
            DRIVER: {
                "handle": DRIVER,
                "writeStuck": True,
                "geometryChanged": True,
                "holeCentersMoved": False,
                "edgeDistancesPreserved": True,
                "affectedParameters": [],
                "restored": True,
            }
        },
        "results": [],
    }


class EditingRig:
    """A journal + one or more fake CATIA documents, readable end to end."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        self.journal = Journal(tmp_path / "idempotency.sqlite3")
        self.app = FakeApp()
        self.coordinator = EditCoordinator(FakeConnection(self.app), journal=self.journal)
        self.read_calls = 0
        monkeypatch.setattr(coordinator_module, "read_model", self._read_model)

    def _read_model(self, app, doc, part):
        self.read_calls += 1
        return read_model_from_part(app, doc, part)

    def add_session(self, session_id: str, length: float = 160.0) -> FakeDocument:
        part = FakePart([FakeParameter(DRIVER, length, "mm")])
        doc = FakeDocument(f"C:\\work\\sessions\\{session_id}.CATPart", part)
        self.app.Documents.add(doc)
        self.journal.save_session(make_session(session_id, doc.FullName))
        return doc

    def set_status(self, session_id: str, status: str, revision: int = 0) -> None:
        session = self.journal.load_session(session_id)
        assert session is not None
        session["status"] = status
        session["modelRevision"] = revision
        self.journal.save_session(session)


@pytest.fixture()
def rig(tmp_path, monkeypatch) -> EditingRig:
    return EditingRig(tmp_path, monkeypatch)


# ---------------------------------------------------------------------------
# same session, same operation id, same plan
# ---------------------------------------------------------------------------


def test_same_session_same_plan_replays_the_stored_result(rig: EditingRig) -> None:
    doc = rig.add_session("sess-replay")
    plan = make_plan("sess-replay")

    first = rig.coordinator.apply_edit_plan("sess-replay", plan)

    assert first["ok"] is True
    assert first["code"] == "VERIFIED"
    assert first["data"]["verdict"] == "PASS"
    assert first["data"]["idempotentReplay"] is False
    assert first["data"]["after"]["parameters"][DRIVER] == 200.0
    assert doc.Part.update_calls == 1
    updates_after_first = doc.Part.update_calls
    reads_after_first = rig.read_calls
    stored = rig.journal.get_operation(OPERATION_ID, session_id="sess-replay")
    assert stored is not None
    assert stored["state"] == "verified"
    assert stored["plan_hash"] == plan_hash(plan)

    session_after_first = rig.journal.load_session("sess-replay")
    assert session_after_first is not None
    revision_after_first = session_after_first["modelRevision"]
    results_after_first = len(session_after_first["results"])

    second = rig.coordinator.apply_edit_plan("sess-replay", plan)

    assert second["ok"] is True
    assert second["code"] == "VERIFIED"
    assert second["data"]["verdict"] == "PASS"
    assert second["data"]["idempotentReplay"] is True
    assert second["data"]["after"]["parameters"][DRIVER] == 200.0
    assert second["data"]["operationId"] == OPERATION_ID
    assert second["data"]["planHash"] == "sha256:" + plan_hash(plan)
    # Nothing was written or even measured a second time.
    assert doc.Part.update_calls == updates_after_first
    assert rig.read_calls == reads_after_first
    assert doc.Part.Parameters.Item(DRIVER).Value == 200.0
    assert rig.journal.get_operation(OPERATION_ID, session_id="sess-replay")["state"] == "verified"
    session_after_replay = rig.journal.load_session("sess-replay")
    assert session_after_replay is not None
    assert session_after_replay["modelRevision"] == revision_after_first
    assert len(session_after_replay["results"]) == results_after_first


def test_same_session_reusing_an_operation_id_for_a_different_plan_conflicts(rig: EditingRig) -> None:
    doc = rig.add_session("sess-conflict")
    first = rig.coordinator.apply_edit_plan("sess-conflict", make_plan("sess-conflict"))
    assert first["ok"] is True
    updates_after_first = doc.Part.update_calls
    reads_after_first = rig.read_calls

    conflicting = make_plan("sess-conflict", new_value=180.0)
    result = rig.coordinator.apply_edit_plan("sess-conflict", conflicting)

    assert result["ok"] is False
    assert result["code"] == "OPERATION_CONFLICT"
    assert result["data"]["operationId"] == OPERATION_ID
    assert result["effects"]["modified"] is None
    assert doc.Part.update_calls == updates_after_first
    assert rig.read_calls == reads_after_first
    assert doc.Part.Parameters.Item(DRIVER).Value == 200.0


# ---------------------------------------------------------------------------
# different sessions, same operation id
# ---------------------------------------------------------------------------


def test_same_operation_id_in_two_sessions_is_not_shared(rig: EditingRig) -> None:
    """Reusing a client operation id in another session must stay isolated.

    This currently fails because ``journal.py`` declares ``operation_id`` as the
    global PRIMARY KEY and its upsert does not update ``session_id``/``plan_hash``:
    session B overwrites session A's row, then session A replays B's result.
    """
    doc_a = rig.add_session("sess-A")
    doc_b = rig.add_session("sess-B")
    plan_a = make_plan("sess-A", new_value=200.0)
    plan_b = make_plan("sess-B", new_value=180.0)

    result_a = rig.coordinator.apply_edit_plan("sess-A", plan_a)
    assert result_a["ok"] is True
    assert result_a["data"]["after"]["parameters"][DRIVER] == 200.0

    # Session B reuses the same client operation id. It must execute its own
    # plan, not replay or conflict with session A's operation.
    result_b = rig.coordinator.apply_edit_plan("sess-B", plan_b)
    assert result_b["ok"] is True
    assert result_b["code"] == "VERIFIED"
    assert result_b["data"]["idempotentReplay"] is False
    assert result_b["data"]["after"]["parameters"][DRIVER] == 180.0
    assert doc_a.Part.Parameters.Item(DRIVER).Value == 200.0
    assert doc_b.Part.Parameters.Item(DRIVER).Value == 180.0

    # Each session owns its journal row: no overwrite, no cross-result.
    row_a = rig.journal.get_operation(OPERATION_ID, session_id="sess-A")
    assert row_a is not None, "session A's operation row disappeared"
    assert row_a["session_id"] == "sess-A", "session A's row now belongs to another session"
    assert row_a["plan_hash"] == plan_hash(plan_a)
    assert row_a["payload"]["result"]["data"]["after"]["parameters"][DRIVER] == 200.0, (
        "session B's result overwrote session A's journaled payload"
    )
    row_b = rig.journal.get_operation(OPERATION_ID, session_id="sess-B")
    assert row_b is not None, (
        "session B's operation row was not created: the journal keys operations "
        "globally by operation_id, so session B overwrote session A's row"
    )
    assert row_b["session_id"] == "sess-B"
    assert row_b["plan_hash"] == plan_hash(plan_b)
    assert row_b["payload"]["result"]["data"]["after"]["parameters"][DRIVER] == 180.0

    # Replaying session A returns A's result, never B's.
    replay_a = rig.coordinator.apply_edit_plan("sess-A", plan_a)
    assert replay_a["data"]["idempotentReplay"] is True
    assert replay_a["data"]["after"]["parameters"][DRIVER] == 200.0
    assert doc_b.Part.Parameters.Item(DRIVER).Value == 180.0


# ---------------------------------------------------------------------------
# recovered state
# ---------------------------------------------------------------------------


def test_recovered_operation_without_a_result_is_not_executed_again(rig: EditingRig) -> None:
    doc = rig.add_session("sess-recovered", length=200.0)  # the applied target is already in place
    rig.set_status("sess-recovered", "failed", revision=1)
    plan = make_plan("sess-recovered")
    digest = plan_hash(plan)
    rig.journal.upsert_operation(
        OPERATION_ID,
        "sess-recovered",
        digest,
        "recovered",
        {"plan": plan, "intent": plan["intent"], "before": {"parameters": {DRIVER: 160.0}}},
    )

    result = rig.coordinator.apply_edit_plan("sess-recovered", plan)

    assert result["ok"] is True
    assert result["code"] == "RECOVERED"
    assert result["data"]["idempotentReplay"] is True
    assert result["data"]["businessState"] == "failed"
    assert result["effects"]["modified"] is False
    assert doc.Part.update_calls == 0
    assert rig.read_calls == 0
    assert doc.Part.Parameters.Item(DRIVER).Value == 200.0
    assert rig.journal.get_operation(OPERATION_ID, session_id="sess-recovered")["state"] == "recovered"
    assert rig.journal.events("sess-recovered") == []


def test_recovered_operation_with_a_stored_result_replays_it(rig: EditingRig) -> None:
    doc = rig.add_session("sess-recovered-result", length=200.0)
    rig.set_status("sess-recovered-result", "rolled_back", revision=1)
    plan = make_plan("sess-recovered-result")
    stored_result = {
        "ok": True,
        "code": "VERIFIED",
        "message": "the original application result",
        "data": {
            "operationId": OPERATION_ID,
            "verdict": "PASS",
            "idempotentReplay": False,
            "after": {"parameters": {DRIVER: 200.0}},
        },
        "effects": {"modified": True},
    }
    rig.journal.upsert_operation(
        OPERATION_ID,
        "sess-recovered-result",
        plan_hash(plan),
        "recovered",
        {"plan": plan, "result": stored_result},
    )

    result = rig.coordinator.apply_edit_plan("sess-recovered-result", plan)

    assert result["ok"] is True
    assert result["code"] == "VERIFIED"
    assert result["message"] == "the original application result"
    assert result["data"]["idempotentReplay"] is True
    assert result["data"]["after"]["parameters"][DRIVER] == 200.0
    assert doc.Part.update_calls == 0
    assert rig.read_calls == 0


# ---------------------------------------------------------------------------
# rejected state
# ---------------------------------------------------------------------------


def test_rejected_plan_replay_returns_the_rejection_without_reexecution(rig: EditingRig) -> None:
    doc = rig.add_session("sess-rejected")
    plan = make_plan("sess-rejected", operation_id="op-rej")
    plan["changes"] = [
        {"parameter": "Part2\\Missing", "oldValue": 1.0, "newValue": 2.0, "unit": "mm", "reason": "bad"}
    ]

    first = rig.coordinator.apply_edit_plan("sess-rejected", plan)

    assert first["ok"] is False
    assert first["code"] == "PARAM_UNKNOWN"
    assert first["data"]["verdict"] == "REJECTED"
    assert first["data"]["idempotentReplay"] is False
    # A rejected plan never produces an after-state.
    assert first["data"]["after"] is None
    assert first["data"]["problems"][0]["code"] == "PARAM_UNKNOWN"
    assert doc.Part.update_calls == 0
    reads_after_first = rig.read_calls
    stored = rig.journal.get_operation("op-rej", session_id="sess-rejected")
    assert stored is not None
    assert stored["state"] == "rejected"

    second = rig.coordinator.apply_edit_plan("sess-rejected", plan)

    assert second["ok"] is False
    assert second["code"] == "PARAM_UNKNOWN"
    assert second["data"]["verdict"] == "REJECTED"
    assert second["data"]["idempotentReplay"] is True
    assert second["data"]["after"] is None
    assert second["data"]["problems"][0]["code"] == "PARAM_UNKNOWN"
    assert doc.Part.update_calls == 0
    assert rig.read_calls == reads_after_first
