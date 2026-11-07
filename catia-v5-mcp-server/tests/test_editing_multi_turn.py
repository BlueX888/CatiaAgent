"""Offline tests for multi-turn history and revert (P2, guide section 5.4)."""

from __future__ import annotations

from typing import Any

import pytest

import catia_mcp.editing.coordinator as coordinator_module
from catia_mcp.editing.coordinator import EditCoordinator
from catia_mcp.editing.journal import Journal
from catia_mcp.editing.validation import PLAN_SCHEMA
from _editing_fakes import (
    FakeApp,
    FakeConnection,
    FakeDocument,
    FakeParameter,
    FakePart,
    read_model_from_part,
)

DRIVER = "Part2\\L"
SESSION_ID = "sess-multi"


def make_session(working_path: str) -> dict[str, Any]:
    return {
        "schema": "catia-edit-session/1",
        "editSessionId": SESSION_ID,
        "modelRevision": 0,
        "createdUtc": "2026-10-26T00:00:00.000+00:00",
        "status": "ready",
        "source": {
            "path": "C:\\work\\source\\multi.CATPart",
            "sha256": "a" * 64,
            "sizeBytes": 196608,
            "mtimeUtc": "2026-10-26T00:00:00.000+00:00",
        },
        "workingCopy": {
            "path": working_path,
            "sha256": "b" * 64,
            "createdUtc": "2026-10-26T00:00:00.000+00:00",
        },
        "baselineParameters": {DRIVER: {"value": 160.0, "unit": "mm"}},
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


def make_plan(value: float, *, operation_id: str, revision: int) -> dict[str, Any]:
    return {
        "schema": PLAN_SCHEMA,
        "editSessionId": SESSION_ID,
        "operationId": operation_id,
        "expectedRevision": revision,
        "intent": {"text": f"把长度改成 {value} mm", "resolved": f"L={value}"},
        "changes": [
            {
                "parameter": DRIVER,
                "oldValue": 160.0 if revision == 0 else 200.0,
                "newValue": value,
                "unit": "mm",
                "reason": "multi-turn test",
            }
        ],
        "preserve": [],
        "acceptance": [],
    }


class Rig:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.journal = Journal(tmp_path / "multi.sqlite3")
        self.app = FakeApp()
        self.coordinator = EditCoordinator(FakeConnection(self.app), journal=self.journal)
        self.part = FakePart([FakeParameter(DRIVER, 160.0, "mm")])
        self.doc = FakeDocument("C:\\work\\sessions\\multi.CATPart", self.part)
        self.app.Documents.add(self.doc)
        self.journal.save_session(make_session(self.doc.FullName))
        monkeypatch.setattr(coordinator_module, "read_model", read_model_from_part)


@pytest.fixture()
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def test_history_records_before_after_and_baseline(rig: Rig) -> None:
    first = rig.coordinator.apply_edit_plan(SESSION_ID, make_plan(200.0, operation_id="op-1", revision=0))
    assert first["ok"] is True and first["data"]["verdict"] == "PASS"

    history = rig.coordinator.get_history(SESSION_ID)["data"]
    assert history["modelRevision"] == 1
    assert history["baselineParameters"][DRIVER]["value"] == 160.0
    assert history["currentParameters"][DRIVER] == 200.0
    assert history["canRevertLast"] is True
    assert history["operations"][-1]["operationId"] == "op-1"
    assert history["operations"][-1]["before"]["parameters"][DRIVER] == 160.0
    assert history["operations"][-1]["after"]["parameters"][DRIVER] == 200.0
    assert history["operations"][-1]["intentText"] == "把长度改成 200.0 mm"


def test_revert_requires_confirm(rig: Rig) -> None:
    rig.coordinator.apply_edit_plan(SESSION_ID, make_plan(200.0, operation_id="op-1", revision=0))
    result = rig.coordinator.revert_last_edit(SESSION_ID)
    assert result["ok"] is False
    assert result["code"] == "INVALID_PLAN"
    assert rig.part.Parameters.Item(DRIVER).Value == 200.0


def test_revert_restores_the_recorded_before_state(rig: Rig) -> None:
    rig.coordinator.apply_edit_plan(SESSION_ID, make_plan(200.0, operation_id="op-1", revision=0))
    result = rig.coordinator.revert_last_edit(SESSION_ID, confirm=True)
    assert result["ok"] is True, result
    assert result["data"]["verdict"] == "PASS"
    assert result["data"]["revertedOperationId"] == "op-1"
    assert rig.part.Parameters.Item(DRIVER).Value == 160.0
    assert result["data"]["modelRevision"] == 2
    types = {item["type"] for item in result["data"]["revertAcceptance"]}
    assert {"volume", "bbox_range"} <= types

    history = rig.coordinator.get_history(SESSION_ID)["data"]
    assert history["canRevertLast"] is False
    assert history["operations"][-1]["revertOf"] == "op-1"


def test_a_second_revert_is_refused(rig: Rig) -> None:
    rig.coordinator.apply_edit_plan(SESSION_ID, make_plan(200.0, operation_id="op-1", revision=0))
    rig.coordinator.revert_last_edit(SESSION_ID, confirm=True)
    second = rig.coordinator.revert_last_edit(SESSION_ID, confirm=True)
    assert second["ok"] is False
    assert second["code"] == "ALREADY_REVERTED"


def test_revert_after_two_edits_only_undoes_the_last(rig: Rig) -> None:
    rig.coordinator.apply_edit_plan(SESSION_ID, make_plan(200.0, operation_id="op-1", revision=0))
    second_plan = make_plan(220.0, operation_id="op-2", revision=1)
    second = rig.coordinator.apply_edit_plan(SESSION_ID, second_plan)
    assert second["ok"] is True, second
    result = rig.coordinator.revert_last_edit(SESSION_ID, confirm=True)
    assert result["ok"] is True, result
    assert rig.part.Parameters.Item(DRIVER).Value == 200.0
    assert result["data"]["revertedOperationId"] == "op-2"
