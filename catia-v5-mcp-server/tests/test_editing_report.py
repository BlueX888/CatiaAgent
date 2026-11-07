"""Offline tests for the delivery report (JSON structure + Markdown truthfulness).

The report is the audit artifact: it must show the request and its parsed
intent, the measured before/after state, the effective tolerances, what was not
verified, and the recovery history. The Markdown sheet must never turn a
missing ``sourceUnchanged`` measurement into an assumed "是".
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from catia_mcp.editing.journal import Journal
from catia_mcp.editing.report import build_report, write_reports
from catia_mcp.editing.validation import plan_hash

SESSION_ID = "sess-report"
DELIVERY_PATH = "C:\\work\\delivery\\plate.CATPart"
DELIVERY_SHA = "d" * 64
DELIVERED_BYTES = 196608
LENGTH = "Part2\\L"
WIDTH = "Part2\\W"


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def model_state(length: float, width: float, volume: float) -> dict[str, Any]:
    """The before/after shape stored in ``session["results"]``."""
    return {
        "parameters": {LENGTH: length, WIDTH: width},
        "geometry": {
            "volumeMm3": volume,
            "areaMm2": 25000.0,
            "bbox": {
                "min": {"x": -length / 2.0, "y": -width / 2.0, "z": 0.0},
                "max": {"x": length / 2.0, "y": width / 2.0, "z": 10.0},
                "dimensions": {"x": length, "y": width, "z": 10.0},
            },
        },
        "holes": [{"center": {"x": 0.0, "y": 0.0, "z": 0.0}, "radius": 6.0}],
    }


def make_plan() -> dict[str, Any]:
    return {
        "schema": "catia-edit-plan/1",
        "editSessionId": SESSION_ID,
        "operationId": "op-apply",
        "expectedRevision": 0,
        "intent": {
            "text": "把长度从 160 改成 200 mm",
            "resolved": {"parameter": LENGTH, "target": 200.0, "unit": "mm"},
        },
        "changes": [
            {
                "parameter": LENGTH,
                "oldValue": 160.0,
                "newValue": 200.0,
                "unit": "mm",
                "reason": "加长板料",
            }
        ],
        "preserve": [],
        "acceptance": [
            {"type": "bbox_dimension", "axis": "x", "expected": 200.0, "absTolerance": 0.02},
            {"type": "volume", "expected": 300000.0},
        ],
    }


def rejected_plan() -> dict[str, Any]:
    return {
        "schema": "catia-edit-plan/1",
        "editSessionId": SESSION_ID,
        "operationId": "op-rejected",
        "expectedRevision": 1,
        "intent": {"text": "把未知参数改成 1", "resolved": {"parameter": "Part2\\Missing"}},
        "changes": [
            {"parameter": "Part2\\Missing", "oldValue": 1.0, "newValue": 2.0, "unit": "mm"}
        ],
        "preserve": [],
        "acceptance": [],
    }


def make_session() -> dict[str, Any]:
    plan = make_plan()
    rejected = rejected_plan()
    return {
        "schema": "catia-edit-session/1",
        "editSessionId": SESSION_ID,
        "modelRevision": 1,
        "source": {
            "path": "C:\\work\\source\\plate.CATPart",
            "sha256": "a" * 64,
            "sizeBytes": 196608,
            "mtimeUtc": "2026-10-24T00:00:00.000+00:00",
        },
        "workingCopy": {
            "path": "C:\\work\\working\\plate.CATPart",
            "sha256": "b" * 64,
            "createdUtc": "2026-10-24T00:00:01.000+00:00",
        },
        "snapshot": {
            "limitations": ["hole centres are sketch-local; the bounding box is planar-assumed"],
            "capabilities": {"writableDrivers": [LENGTH, WIDTH]},
        },
        "results": [
            {
                "operationId": "op-apply",
                "verdict": "PASS",
                "businessState": "verified",
                "sessionState": "verified",
                "modelRevision": 1,
                "code": "VERIFIED",
                "plan": plan,
                "intent": plan["intent"],
                "before": model_state(160.0, 60.0, 240000.0),
                "after": model_state(200.0, 60.0, 300000.0),
                "steps": [
                    {
                        "index": 1,
                        "parameter": LENGTH,
                        "oldValue": 160.0,
                        "newValue": 200.0,
                        "unit": "mm",
                        "readback": 200.0,
                        "status": "applied",
                    }
                ],
                "recovery": {
                    "attempted": True,
                    "restored": True,
                    "paramsMatch": True,
                    "volumeMatch": True,
                    "bboxMatch": True,
                    "holesMatch": True,
                    "unavailable": [],
                },
                "checks": [
                    {"id": f"param-readback:{LENGTH}", "status": "pass", "evidence": {"actual": 200.0}}
                ],
                "utc": "2026-10-24T01:00:00.000+00:00",
            },
            {
                "operationId": "op-rejected",
                "verdict": "REJECTED",
                "businessState": "rejected",
                "sessionState": "verified",
                "modelRevision": 1,
                "code": "PARAM_UNKNOWN",
                "plan": rejected,
                "intent": rejected["intent"],
                "before": model_state(200.0, 60.0, 300000.0),
                "after": None,
                "steps": [],
                "recovery": {"attempted": False, "restored": None},
                "checks": [
                    {"id": "reject:PARAM_UNKNOWN", "status": "fail", "evidence": {"path": "changes[0].parameter"}}
                ],
                "utc": "2026-10-24T01:01:00.000+00:00",
            },
        ],
    }


def expected_model() -> dict[str, Any]:
    return {
        "part": "Part2",
        "features": {"mainBody": "PartBody", "shapes": ["Pad.1"], "sketches": [], "bodies": ["PartBody"]},
        "parameters": [
            {"handle": LENGTH, "value": 200.0, "kind": "driver"},
            {"handle": WIDTH, "value": 60.0, "kind": "driver"},
        ],
        "geometry": {
            "volumeMm3": 300000.0,
            "bbox": {"dimensions": {"x": 200.0, "y": 60.0, "z": 10.0}},
        },
        "holes": [{"center": {"x": 0.0, "y": 0.0, "z": 0.0}, "radius": 6.0}],
        "limitations": [],
    }


def verification() -> dict[str, Any]:
    return {
        "passed": False,
        "checks": [
            {"id": "reopen", "status": "pass", "evidence": {"documentCountDelta": True}},
            {"id": "volume", "status": "fail", "evidence": {"expected": 300000.0, "actual": 299999.0}},
            {"id": "holes", "status": "warn", "evidence": {"reason": "hole readback unavailable"}},
        ],
    }


@pytest.fixture()
def report() -> dict[str, Any]:
    return build_report(
        make_session(),
        DELIVERY_PATH,
        DELIVERY_SHA,
        DELIVERED_BYTES,
        expected_model(),
        verification(),
        journal=None,
    )


# ---------------------------------------------------------------------------
# JSON structure
# ---------------------------------------------------------------------------


def test_report_has_the_required_audit_sections(report: dict[str, Any]) -> None:
    for key in ("request", "tolerances", "unverified", "recoveryHistory", "operations"):
        assert key in report


def test_request_carries_the_intent_and_the_changes(report: dict[str, Any]) -> None:
    request = report["request"]
    # The later rejected attempt must not replace the applied request.
    assert request["operationId"] == "op-apply"
    assert request["intent"]["text"] == "把长度从 160 改成 200 mm"
    assert request["intent"]["resolved"] == {"parameter": LENGTH, "target": 200.0, "unit": "mm"}
    assert request["changes"] == [
        {
            "parameter": LENGTH,
            "oldValue": 160.0,
            "newValue": 200.0,
            "unit": "mm",
            "reason": "加长板料",
        }
    ]
    applied = next(op for op in report["operations"] if op["operationId"] == "op-apply")
    assert applied["intent"]["text"] == "把长度从 160 改成 200 mm"
    assert applied["plan"]["operationId"] == "op-apply"


def test_operations_carry_measured_before_and_after(report: dict[str, Any]) -> None:
    applied = next(op for op in report["operations"] if op["operationId"] == "op-apply")
    assert applied["before"]["parameters"] == {LENGTH: 160.0, WIDTH: 60.0}
    assert applied["after"]["parameters"] == {LENGTH: 200.0, WIDTH: 60.0}
    assert applied["before"]["volumeMm3"] == 240000.0
    assert applied["after"]["volumeMm3"] == 300000.0
    assert applied["before"]["bbox"]["dimensions"]["x"] == 160.0
    assert applied["after"]["bbox"]["dimensions"]["x"] == 200.0
    assert applied["before"]["holes"][0]["radius"] == 6.0
    assert applied["after"]["holes"][0]["radius"] == 6.0
    assert applied["steps"][0]["readback"] == 200.0
    assert applied["verdict"] == "PASS"


def test_tolerances_expose_explicit_and_default_rules(report: dict[str, Any]) -> None:
    tolerances = report["tolerances"]
    assert tolerances["operationId"] == "op-apply"
    assert tolerances["source"] == "mixed"
    assert tolerances["defaults"]["lengthMm"] == 0.01
    assert tolerances["defaults"]["volume"]["absMm3"] == 0.01
    assert tolerances["defaults"]["volume"]["rel"] == 1e-6

    assert len(tolerances["items"]) == 2
    bbox_item, volume_item = tolerances["items"]
    assert bbox_item["type"] == "bbox_dimension"
    assert bbox_item["absTolerance"] == 0.02
    assert bbox_item["explicit"] is True
    assert bbox_item["effective"] == {"abs": 0.02, "rel": None, "rule": "计划显式容差"}

    assert volume_item["type"] == "volume"
    assert volume_item["absTolerance"] is None
    assert volume_item["explicit"] is False
    # max(0.01 mm3, expected * 1e-6) with expected = 300000 -> 0.3
    assert volume_item["effective"]["abs"] == pytest.approx(0.3)
    assert volume_item["effective"]["rel"] == 1e-6
    assert "max(0.01" in volume_item["effective"]["rule"]

    assert tolerances["explicit"] == [
        {"operationId": "op-apply", "index": 0, "type": "bbox_dimension", "absTolerance": 0.02, "relTolerance": None}
    ]


def test_unverified_lists_every_non_pass_check_and_limitations(report: dict[str, Any]) -> None:
    unverified = report["unverified"]
    assert [check["id"] for check in unverified["checks"]] == ["volume", "holes"]
    assert unverified["checks"][0]["status"] == "fail"
    assert unverified["checks"][1]["status"] == "warn"
    assert unverified["limitations"] == [
        "hole centres are sketch-local; the bounding box is planar-assumed"
    ]


def test_recovery_history_records_the_rollback_evidence(report: dict[str, Any]) -> None:
    history = report["recoveryHistory"]
    assert len(history) == 1
    entry = history[0]
    assert entry["operationId"] == "op-apply"
    assert entry["attempted"] is True
    assert entry["restored"] is True
    assert entry["utc"] == "2026-10-24T01:00:00.000+00:00"
    assert entry["evidence"]["paramsMatch"] is True
    assert entry["evidence"]["volumeMatch"] is True
    assert entry["evidence"]["bboxMatch"] is True
    assert entry["evidence"]["holesMatch"] is True


def test_rejected_attempt_never_replaces_the_request_without_an_after_state(report: dict[str, Any]) -> None:
    rejected = next(op for op in report["operations"] if op["operationId"] == "op-rejected")
    assert rejected["after"] is None
    assert rejected["verdict"] == "REJECTED"
    assert report["request"]["operationId"] == "op-apply"
    assert report["lastVerdict"] == "REJECTED"


def test_report_merges_full_detail_from_the_journal(tmp_path) -> None:
    """A delivered session keeps summaries; the plan/baselines live in the journal."""
    plan = make_plan()
    journal = Journal(tmp_path / "report-journal.sqlite3")
    result_envelope = {
        "ok": True,
        "code": "VERIFIED",
        "message": "all checks passed",
        "tool": "catia_edit",
        "operation_id": "op-apply",
        "data": {
            "editSessionId": SESSION_ID,
            "operationId": "op-apply",
            "businessState": "verified",
            "sessionState": "verified",
            "modelRevision": 1,
            "verdict": "PASS",
            "planHash": "sha256:" + plan_hash(plan),
            "idempotentReplay": False,
            "before": model_state(160.0, 60.0, 240000.0),
            "after": model_state(200.0, 60.0, 300000.0),
            "steps": [{"index": 1, "parameter": LENGTH, "readback": 200.0}],
            "checks": [{"id": "param-readback", "status": "pass", "evidence": {}}],
            "recovery": {"attempted": True, "restored": True, "paramsMatch": True},
            "delivery": None,
        },
        "effects": {"modified": True},
    }
    journal.upsert_operation(
        "op-apply",
        SESSION_ID,
        plan_hash(plan),
        "verified",
        {"plan": plan, "intent": plan["intent"], "result": result_envelope},
    )
    session = make_session()
    session["results"] = [
        {
            "operationId": "op-apply",
            "verdict": "PASS",
            "businessState": "verified",
            "sessionState": "verified",
            "modelRevision": 1,
            "code": "VERIFIED",
            "utc": "2026-10-24T01:00:00.000+00:00",
        }
    ]

    report = build_report(
        session,
        DELIVERY_PATH,
        DELIVERY_SHA,
        DELIVERED_BYTES,
        expected_model(),
        verification(),
        journal=journal,
    )

    assert report["request"]["operationId"] == "op-apply"
    assert report["request"]["intent"]["text"] == "把长度从 160 改成 200 mm"
    applied = next(op for op in report["operations"] if op["operationId"] == "op-apply")
    assert applied["after"]["parameters"][LENGTH] == 200.0
    assert applied["before"]["parameters"][LENGTH] == 160.0
    assert applied["plan"]["changes"][0]["newValue"] == 200.0
    assert report["tolerances"]["items"][0]["type"] == "bbox_dimension"
    assert report["recoveryHistory"][0]["attempted"] is True
    assert report["recoveryHistory"][0]["restored"] is True
    assert report.get("journalNotes") in (None, [])


# ---------------------------------------------------------------------------
# Markdown truthfulness
# ---------------------------------------------------------------------------


def write_and_read_markdown(
    tmp_path,
    session: dict[str, Any],
    verification_payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    delivery = tmp_path / "plate.CATPart"
    paths = write_reports(
        session,
        str(delivery),
        DELIVERY_SHA,
        DELIVERED_BYTES,
        expected_model(),
        verification_payload,
        journal=None,
    )
    with open(paths["markdown"], encoding="utf-8") as handle:
        markdown = handle.read()
    with open(paths["json"], encoding="utf-8") as handle:
        report_json = json.load(handle)
    return markdown, report_json


@pytest.mark.parametrize(
    ("source_unchanged", "expected_label"),
    [(None, "未验证"), (True, "是"), (False, "否")],
    ids=["missing", "measured_true", "measured_false"],
)
def test_markdown_source_unchanged_uses_only_the_measured_value(
    tmp_path, source_unchanged, expected_label
) -> None:
    verification_payload = verification()
    if source_unchanged is not None:
        verification_payload["sourceUnchanged"] = source_unchanged

    markdown, _ = write_and_read_markdown(tmp_path, make_session(), verification_payload)

    assert f"（会话结束时未变：{expected_label}）" in markdown
    if source_unchanged is None:
        # The specific regression: a missing measurement is never "是".
        assert "（会话结束时未变：是）" not in markdown
        assert "未验证" in markdown


def test_markdown_renders_all_audit_sections(tmp_path) -> None:
    markdown, report_json = write_and_read_markdown(
        tmp_path, make_session(), verification()
    )

    for heading in (
        "## 原始需求与解析语义",
        "## 参数前后值",
        "## 验收项与容差",
        "## 操作历史",
        "## 重开验证项",
        "## 未判定/未验证项",
        "## 恢复历史",
    ):
        assert heading in markdown
    assert "把长度从 160 改成 200 mm" in markdown
    assert "加长板料" in markdown
    assert "`Part2\\L` | 160.0 | 200.0" in markdown
    assert "240000.0 mm³ → 300000.0 mm³" in markdown
    assert "attempted=True，restored=True" in markdown
    assert report_json["request"]["operationId"] == "op-apply"


def test_more_than_twenty_operations_keeps_the_latest_request(tmp_path) -> None:
    """session['results'] caps at 20; journal-only older rows must not become 'latest'."""
    journal = Journal(tmp_path / "long-journal.sqlite3")
    session = make_session()
    session["results"] = []
    for index in range(1, 22):
        plan = make_plan()
        plan["operationId"] = f"op-{index:02d}"
        envelope = {
            "ok": True,
            "code": "VERIFIED",
            "message": "all checks passed",
            "tool": "catia_edit",
            "operation_id": plan["operationId"],
            "data": {
                "editSessionId": SESSION_ID,
                "operationId": plan["operationId"],
                "businessState": "verified",
                "sessionState": "verified",
                "modelRevision": index,
                "verdict": "PASS",
                "before": model_state(150.0 + index, 60.0, 200000.0 + index),
                "after": model_state(151.0 + index, 60.0, 200001.0 + index),
                "steps": [],
                "checks": [],
            },
        }
        journal.upsert_operation(
            plan["operationId"],
            SESSION_ID,
            plan_hash(plan),
            "verified",
            {"plan": plan, "intent": plan["intent"], "result": envelope},
        )
        # Force a strictly increasing journal timestamp order.
        with journal._connect() as conn:
            conn.execute(
                "UPDATE operations SET updated_utc = ? WHERE session_id = ? AND operation_id = ?",
                (f"2026-10-24T00:{index:02d}:00.000+00:00", SESSION_ID, plan["operationId"]),
            )

    report = build_report(
        session,
        DELIVERY_PATH,
        DELIVERY_SHA,
        DELIVERED_BYTES,
        expected_model(),
        verification(),
        journal=journal,
    )

    assert len(report["operations"]) == 21
    assert report["request"]["operationId"] == "op-21"
    timestamps = [operation["utc"] for operation in report["operations"]]
    assert timestamps == sorted(timestamps)
