"""Offline tests for the stateful clarification gate.

A ``needs_clarification`` decision must stop the session from producing or
applying another plan until the *user* answers. The gate is what makes the P0
"rewrite intent.text and retry" bypass ineffective: the host-injected user
message is the only thing that can clear it, and a hand-written plan is blocked
at ``apply_edit_plan``.
"""

from __future__ import annotations

from typing import Any

import pytest

import catia_mcp.editing.coordinator as coordinator_module
import catia_mcp.editing.intent as intent_module
from catia_mcp.editing.coordinator import EditCoordinator
from catia_mcp.editing.journal import Journal
from _editing_fakes import (
    FakeApp,
    FakeConnection,
    FakeDocument,
    FakeParameter,
    FakePart,
    read_model_from_part,
)

DRIVER = "Part2\\L"
SESSION_ID = "sess-gate"


def _session(working_path: str) -> dict[str, Any]:
    return {
        "schema": "catia-edit-session/1",
        "editSessionId": SESSION_ID,
        "modelRevision": 0,
        "createdUtc": "2026-10-26T00:00:00.000+00:00",
        "status": "ready",
        "source": {
            "path": "C:\\work\\source\\gate.CATPart",
            "sha256": "a" * 64,
            "sizeBytes": 196608,
            "mtimeUtc": "2026-10-26T00:00:00.000+00:00",
        },
        "workingCopy": {
            "path": working_path,
            "sha256": "b" * 64,
            "createdUtc": "2026-10-26T00:00:00.000+00:00",
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


class Rig:
    def __init__(self, tmp_path, monkeypatch) -> None:
        self.journal = Journal(tmp_path / "gate.sqlite3")
        self.app = FakeApp()
        self.coordinator = EditCoordinator(FakeConnection(self.app), journal=self.journal)
        self.part = FakePart([FakeParameter(DRIVER, 160.0, "mm")])
        self.doc = FakeDocument("C:\\work\\sessions\\gate.CATPart", self.part)
        self.app.Documents.add(self.doc)
        self.journal.save_session(_session(self.doc.FullName))
        monkeypatch.setattr(coordinator_module, "read_model", read_model_from_part)
        monkeypatch.setattr(coordinator_module, "build_snapshot", lambda *a, **k: {"modelRevision": 0, "parameters": [], "holes": [], "geometry": {}, "capabilities": {}, "impacts": {}})

    def arm(self, user_text: str = "长度改成 20") -> None:
        session = self.journal.load_session(SESSION_ID)
        assert session is not None
        session["pendingClarification"] = {
            "userText": user_text,
            "textSource": "platform",
            "unresolved": ["operation.unit"],
            "message": "the text states a number without a unit",
            "modelRevision": 0,
            "createdUtc": "2026-10-26T00:00:00.000+00:00",
        }
        self.journal.save_session(session)

    def pending(self) -> dict[str, Any] | None:
        session = self.journal.load_session(SESSION_ID)
        assert session is not None
        return session.get("pendingClarification")

    def draft(self, text: str = "长度改成 20") -> dict[str, Any]:
        return {
            "schema": "cad-intent-draft/1",
            "text": text,
            "target": {"parameter": DRIVER, "attribute": "length"},
            "operation": {"type": "set", "amount": 20, "unit": "mm", "scope": "overall"},
            "reference": {"basis": "current"},
            "preserve": [],
        }


@pytest.fixture()
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def _planned(intent, snapshot, **kwargs):
    return {
        "schema": "cad-intent-compile/1",
        "decision": "planned",
        "plan": {
            "schema": "catia-edit-plan/1",
            "editSessionId": SESSION_ID,
            "operationId": "op-gate",
            "expectedRevision": 0,
            "changes": [{"parameter": DRIVER, "oldValue": 160.0, "newValue": 20.0, "unit": "mm"}],
            "preserve": [],
            "acceptance": [],
        },
        "operationId": "op-gate",
        "checks": [],
        "unresolved": [],
        "candidates": [],
        "notes": [],
        "message": "planned",
        "textSource": "platform",
        "binding": None,
        "intent": None,
    }


def _clarify(intent, snapshot, **kwargs):
    return {
        "schema": "cad-intent-compile/1",
        "decision": "needs_clarification",
        "plan": None,
        "operationId": None,
        "checks": [],
        "unresolved": ["operation.unit"],
        "candidates": [],
        "notes": [],
        "message": "the text states a number without a unit",
        "textSource": "platform",
        "binding": None,
        "intent": None,
    }


def test_apply_is_blocked_while_a_clarification_is_pending(rig: Rig) -> None:
    rig.arm()
    plan = _planned({}, {})["plan"]
    result = rig.coordinator.apply_edit_plan(SESSION_ID, plan)
    assert result["ok"] is False
    assert result["code"] == "CLARIFICATION_PENDING"
    assert result["effects"]["modified"] is False
    assert rig.part.update_calls == 0
    assert rig.pending() is not None


def test_compile_plan_is_withheld_until_the_user_answers(rig: Rig, monkeypatch) -> None:
    rig.arm()
    monkeypatch.setattr(intent_module, "compile_intent", _planned)
    result = rig.coordinator.compile_intent(
        SESSION_ID,
        rig.draft(),
        user_text="长度改成 20",
    )
    assert result["data"]["decision"] == "needs_clarification"
    assert result["data"]["plan"] is None
    assert result["data"]["pendingClarification"]["unresolved"] == ["operation.unit"]
    assert rig.pending() is not None


def test_repeating_the_message_with_the_confirm_flag_is_not_an_answer(rig: Rig, monkeypatch) -> None:
    rig.arm()
    monkeypatch.setattr(intent_module, "compile_intent", _planned)
    result = rig.coordinator.compile_intent(
        SESSION_ID,
        rig.draft(),
        user_text="长度改成 20",
        user_confirmed=True,
        confirmation_text="长度改成 20",
    )
    assert result["data"]["decision"] == "needs_clarification"
    assert rig.pending() is not None


def test_a_new_user_message_with_matching_confirmation_clears_the_gate(rig: Rig, monkeypatch) -> None:
    rig.arm()
    monkeypatch.setattr(intent_module, "compile_intent", _planned)
    result = rig.coordinator.compile_intent(
        SESSION_ID,
        rig.draft("单位是 mm，长度改成 20"),
        user_text="单位是 mm，长度改成 20",
        user_confirmed=True,
        confirmation_text="单位是 mm",
    )
    assert result["data"]["decision"] == "planned"
    assert result["data"]["clarificationCleared"]["confirmationMatched"] is True
    assert rig.pending() is None


def test_confirmation_must_be_part_of_the_current_message(rig: Rig, monkeypatch) -> None:
    rig.arm()
    monkeypatch.setattr(intent_module, "compile_intent", _planned)
    result = rig.coordinator.compile_intent(
        SESSION_ID,
        rig.draft("长度改成 30"),
        user_text="长度改成 30",
        user_confirmed=True,
        confirmation_text="单位是 mm",
    )
    assert result["data"]["decision"] == "needs_clarification"
    assert rig.pending() is not None


def test_a_clarification_updates_the_pending_text(rig: Rig, monkeypatch) -> None:
    rig.arm()
    monkeypatch.setattr(intent_module, "compile_intent", _clarify)
    result = rig.coordinator.compile_intent(
        SESSION_ID,
        rig.draft("长度改成 20，单位是什么"),
        user_text="长度改成 20，单位是什么",
    )
    assert result["data"]["decision"] == "needs_clarification"
    assert rig.pending()["userText"] == "长度改成 20，单位是什么"


def _ambiguous(intent, snapshot, **kwargs):
    return {
        "schema": "cad-intent-compile/1",
        "decision": "needs_clarification",
        "plan": None,
        "operationId": None,
        "checks": [],
        "unresolved": ["target.parameter.ambiguous"],
        "candidates": [{"handle": DRIVER}],
        "notes": [],
        "message": "two drivers match; bind one with a sketch or kind",
        "textSource": "platform",
        "clarificationSource": "compiler",
        "binding": None,
        "intent": None,
    }


def test_a_model_fixable_ambiguity_opens_but_does_not_block_compilation(rig: Rig, monkeypatch) -> None:
    monkeypatch.setattr(intent_module, "compile_intent", _ambiguous)
    first = rig.coordinator.compile_intent(SESSION_ID, rig.draft(), user_text="把侧面孔的直径改为 8 mm")
    assert first["data"]["decision"] == "needs_clarification"
    assert "pendingClarification" not in first["data"]
    assert first["data"]["openAmbiguity"]["unresolved"] == ["target.parameter.ambiguous"]
    session = rig.journal.load_session(SESSION_ID)
    assert session is not None
    assert session.get("pendingClarification") is None
    assert session.get("openAmbiguity") is not None

    # A hand-written plan is still blocked while the binding is unresolved.
    blocked = rig.coordinator.apply_edit_plan(SESSION_ID, _planned({}, {})["plan"])
    assert blocked["ok"] is False
    assert blocked["code"] == "AMBIGUITY_OPEN"
    assert rig.part.update_calls == 0


def test_a_planned_compile_clears_the_open_ambiguity(rig: Rig, monkeypatch) -> None:
    monkeypatch.setattr(intent_module, "compile_intent", _ambiguous)
    rig.coordinator.compile_intent(SESSION_ID, rig.draft(), user_text="把侧面孔的直径改为 8 mm")
    monkeypatch.setattr(intent_module, "compile_intent", _planned)
    second = rig.coordinator.compile_intent(
        SESSION_ID,
        rig.draft("把侧面孔的直径改为 8 mm"),
        user_text="把侧面孔的直径改为 8 mm",
    )
    assert second["data"]["decision"] == "planned"
    session = rig.journal.load_session(SESSION_ID)
    assert session is not None
    assert session.get("openAmbiguity") is None
    applied = rig.coordinator.apply_edit_plan(SESSION_ID, second["data"]["plan"])
    assert applied["ok"] is True
    assert applied["data"]["verdict"] == "PASS"
