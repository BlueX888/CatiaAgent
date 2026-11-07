"""Offline tests for the deterministic editing layer.

Covers only logic that needs no CATIA: plan validation, the SQLite journal and
the pure helpers under ``catia_mcp.editing``. Nothing here imports win32com,
starts CATIA or talks to the network; the contract under test is
``docs/editing-protocol-v1.md``.
"""

from __future__ import annotations

import sqlite3

import pytest

import catia_mcp.editing.journal as journal_module
from catia_mcp.editing.coordinator import model_value
from catia_mcp.editing.journal import Journal
from catia_mcp.editing.probe import approx_equal, param_kind
from catia_mcp.editing.validation import (
    ACCEPTANCE_TYPES,
    PLAN_SCHEMA,
    acceptance_tolerance,
    plan_hash,
    validate_plan,
)

# ---------------------------------------------------------------------------
# fixtures: a hand-built catia-model-snapshot/1 and a valid plan
# ---------------------------------------------------------------------------

SNAPSHOT_SCHEMA = "catia-model-snapshot/1"
SESSION_ID = "sess0000abcd"
MODEL_REVISION = 3

SOURCE_PATH = "C:\\work\\输入\\plate.CATPart"
WORKING_PATH = "C:\\work\\edits\\plate.CATPart"

DRIVER_LENGTH = "Part2\\L"
DRIVER_ANGLE = "Part2\\Angle"
DIMENSION_LENGTH = "Part2\\PartBody\\Sketch.1\\Length.1"
FORMULA_DIMENSION = "Part2\\PartBody\\Sketch.1\\Offset.13"
SYSTEM_PARAMETER = "Part2\\PartNumber"


def make_snapshot() -> dict:
    """A snapshot shaped like section 3.1 of the protocol."""
    return {
        "schema": SNAPSHOT_SCHEMA,
        "editSessionId": SESSION_ID,
        "modelRevision": MODEL_REVISION,
        "source": {
            "path": SOURCE_PATH,
            "sha256": "a" * 64,
            "sizeBytes": 196608,
            "mtimeUtc": "2026-10-23T05:59:00.000+00:00",
        },
        "workingCopy": {
            "path": WORKING_PATH,
            "sha256": "b" * 64,
            "createdUtc": "2026-10-23T06:00:00.000+00:00",
        },
        "document": {"name": "plate.CATPart", "fullName": WORKING_PATH, "saved": False},
        "part": {
            "name": "Part2",
            "mainBody": "PartBody",
            "featureCount": 2,
            "bodyNames": ["PartBody"],
        },
        "parameters": [
            {
                "handle": DRIVER_LENGTH,
                "name": DRIVER_LENGTH,
                "value": 160.0,
                "unit": "mm",
                "dimensionType": "LENGTH",
                "kind": "driver",
                "writable": True,
            },
            {
                "handle": DRIVER_ANGLE,
                "name": DRIVER_ANGLE,
                "value": 45.0,
                "unit": "deg",
                "dimensionType": "ANGLE",
                "kind": "driver",
                "writable": True,
            },
            {
                "handle": DIMENSION_LENGTH,
                "name": DIMENSION_LENGTH,
                "value": 12.0,
                "unit": "mm",
                "dimensionType": "LENGTH",
                "kind": "dimension",
                "writable": False,
            },
            {
                "handle": FORMULA_DIMENSION,
                "name": FORMULA_DIMENSION,
                "value": 55.0,
                "unit": "mm",
                "dimensionType": "LENGTH",
                "kind": "dimension",
                "writable": False,
                "drivenBy": {"relationName": "HoleX_L", "expression": "L/2-E"},
            },
            {
                "handle": SYSTEM_PARAMETER,
                "name": SYSTEM_PARAMETER,
                "value": 0.0,
                "unit": "none",
                "dimensionType": "REAL",
                "kind": "system",
                "writable": False,
            },
        ],
        "relations": [
            {"name": "HoleX_L", "expression": "L/2-E", "targetHandle": FORMULA_DIMENSION}
        ],
        "geometry": {
            "volumeMm3": 239692.63,
            "areaMm2": 25300.0,
            "bbox": {
                "min": {"x": -80.0, "y": -30.0, "z": 0.0},
                "max": {"x": 80.0, "y": 30.0, "z": 10.0},
                "dimensions": {"x": 160.0, "y": 60.0, "z": 10.0},
            },
            "method": "vertex sweep (Type=顶点,sel)",
            "exact": True,
        },
        "holes": [{"center": {"x": 65.0, "y": 10.0}, "radius": 6.0, "source": "Sketch.1"}],
        "drivers": [DRIVER_LENGTH, DRIVER_ANGLE],
        "impacts": {
            DRIVER_LENGTH: {
                "handle": DRIVER_LENGTH,
                "writeStuck": True,
                "geometryChanged": True,
                "holeCentersMoved": True,
                "edgeDistancesPreserved": True,
                "restored": True,
            },
            DRIVER_ANGLE: {
                "handle": DRIVER_ANGLE,
                "writeStuck": True,
                "geometryChanged": False,
                "holeCentersMoved": False,
                "edgeDistancesPreserved": True,
                "restored": True,
            },
        },
        "capabilities": {
            "writableDrivers": [DRIVER_LENGTH, DRIVER_ANGLE],
            "geometryDrivers": [DRIVER_LENGTH],
            "nonsemanticDrivers": [DRIVER_ANGLE],
            "invariants": {
                "holes_edge_distance": {
                    "supported": True,
                    "reason": "hole margins follow L through HoleX_L",
                }
            },
            "unsupported": [],
        },
        "limitations": [],
        "capturedUtc": "2026-10-23T06:00:01.000+00:00",
    }


def make_change(**overrides) -> dict:
    base = {
        "parameter": DRIVER_LENGTH,
        "oldValue": 160.0,
        "newValue": 200.0,
        "unit": "mm",
        "reason": "lengthen the plate",
    }
    base.update(overrides)
    return base


def make_plan(**overrides) -> dict:
    base = {
        "schema": PLAN_SCHEMA,
        "editSessionId": SESSION_ID,
        "operationId": "op-0001",
        "expectedRevision": MODEL_REVISION,
        "intent": {"text": "长度改为 200 mm", "resolved": "absolute length = 200 mm"},
        "changes": [make_change()],
        "preserve": [],
        "acceptance": [],
    }
    base.update(overrides)
    return base


def problems_for(plan=None, snapshot=None, session_id: str = SESSION_ID) -> list[dict]:
    return validate_plan(
        make_plan() if plan is None else plan,
        make_snapshot() if snapshot is None else snapshot,
        session_id=session_id,
    )


def codes(problems: list) -> list[str]:
    """Every problem must carry exactly code/message/path as strings."""
    assert isinstance(problems, list)
    for problem in problems:
        assert set(problem) == {"code", "message", "path"}
        assert isinstance(problem["code"], str) and problem["code"]
        assert isinstance(problem["message"], str) and problem["message"]
        assert isinstance(problem["path"], str)
    return [problem["code"] for problem in problems]


# ---------------------------------------------------------------------------
# A. validate_plan
# ---------------------------------------------------------------------------


def test_a_fully_valid_plan_has_no_problems():
    assert problems_for() == []


def test_non_object_plan_is_invalid_plan():
    for plan in ([], "catia-edit-plan/1", 7, None):
        problems = validate_plan(plan, make_snapshot(), session_id=SESSION_ID)
        assert codes(problems) == ["INVALID_PLAN"]


@pytest.mark.parametrize("schema", [None, "catia-edit-plan/2", "", 1])
def test_wrong_or_missing_schema_is_invalid_plan(schema):
    plan = make_plan()
    if schema is None:
        plan.pop("schema")
    else:
        plan["schema"] = schema
    problems = problems_for(plan)
    assert codes(problems) == ["INVALID_PLAN"]
    assert problems[0]["path"] == "schema"


def test_session_id_mismatch_is_invalid_plan():
    problems = problems_for(make_plan(editSessionId="someone-else"))
    assert codes(problems) == ["INVALID_PLAN"]
    assert problems[0]["path"] == "editSessionId"


@pytest.mark.parametrize("operation_id", ["", "   ", None, 42])
def test_missing_or_blank_operation_id_is_invalid_plan(operation_id):
    plan = make_plan()
    plan["operationId"] = operation_id
    if operation_id is None:
        plan.pop("operationId")
    problems = problems_for(plan)
    assert codes(problems) == ["INVALID_PLAN"]
    assert problems[0]["path"] == "operationId"


@pytest.mark.parametrize("revision", [MODEL_REVISION - 1, MODEL_REVISION + 1, None, 0])
def test_expected_revision_mismatch_is_stale_revision(revision):
    problems = problems_for(make_plan(expectedRevision=revision))
    assert codes(problems) == ["STALE_REVISION"]
    assert problems[0]["path"] == "expectedRevision"


def test_unknown_parameter_handle_is_param_unknown():
    problems = problems_for(make_plan(changes=[make_change(parameter="Part2\\Missing")]))
    assert codes(problems) == ["PARAM_UNKNOWN"]
    assert problems[0]["path"] == "changes[0].parameter"


def test_formula_driven_dimension_is_not_writable():
    problems = problems_for(
        make_plan(changes=[make_change(parameter=FORMULA_DIMENSION, oldValue=55.0, newValue=60.0)])
    )
    assert codes(problems) == ["PARAM_NOT_WRITABLE"]
    assert problems[0]["path"] == "changes[0].parameter"


def test_present_but_not_writable_parameter_is_not_writable():
    snapshot = make_snapshot()
    snapshot["parameters"][0].update({"writable": False, "notes": ["probe failed"]})
    problems = problems_for(make_plan(), snapshot)
    assert codes(problems) == ["PARAM_NOT_WRITABLE"]
    assert problems[0]["path"] == "changes[0].parameter"
    assert "writable=False" in problems[0]["message"]


@pytest.mark.parametrize(
    ("handle", "old_value", "new_value", "unit"),
    [(DIMENSION_LENGTH, 12.0, 15.0, "mm"), (SYSTEM_PARAMETER, 0.0, 1.0, "none")],
    ids=["dimension", "system"],
)
def test_non_driver_kinds_are_not_writable(handle, old_value, new_value, unit):
    problems = problems_for(
        make_plan(
            changes=[
                make_change(parameter=handle, oldValue=old_value, newValue=new_value, unit=unit)
            ]
        )
    )
    assert codes(problems) == ["PARAM_NOT_WRITABLE"]


def test_unit_mismatch_is_unit_incompatible():
    problems = problems_for(make_plan(changes=[make_change(unit="deg")]))
    assert codes(problems) == ["UNIT_INCOMPATIBLE"]
    assert problems[0]["path"] == "changes[0].unit"


def test_angle_parameter_rejects_millimetre_change():
    problems = problems_for(
        make_plan(
            changes=[
                make_change(parameter=DRIVER_ANGLE, oldValue=45.0, newValue=90.0, unit="mm")
            ]
        )
    )
    assert codes(problems) == ["UNIT_INCOMPATIBLE"]


@pytest.mark.parametrize("new_value", [-5.0, 0.0, 2_000_000.0], ids=["negative", "zero", "huge"])
def test_length_out_of_range_is_out_of_range(new_value):
    problems = problems_for(make_plan(changes=[make_change(newValue=new_value)]))
    assert codes(problems) == ["OUT_OF_RANGE"]
    assert problems[0]["path"] == "changes[0].newValue"


def test_angle_magnitude_limit_is_out_of_range():
    problems = problems_for(
        make_plan(
            changes=[
                make_change(
                    parameter=DRIVER_ANGLE, oldValue=45.0, newValue=-2_000_000.0, unit="deg"
                )
            ]
        )
    )
    assert codes(problems) == ["OUT_OF_RANGE"]


@pytest.mark.parametrize("old_value", [155.0, 160.0000011], ids=["wrong", "just_outside"])
def test_stale_old_value_is_stale_plan(old_value):
    problems = problems_for(make_plan(changes=[make_change(oldValue=old_value)]))
    assert codes(problems) == ["STALE_PLAN"]
    assert problems[0]["path"] == "changes[0].oldValue"


def test_old_value_equal_within_one_micrometre_is_accepted():
    assert problems_for(make_plan(changes=[make_change(oldValue=160.0000004)])) == []


def test_duplicate_parameter_in_one_plan_is_invalid_plan():
    problems = problems_for(
        make_plan(changes=[make_change(), make_change(newValue=210.0)])
    )
    assert codes(problems) == ["INVALID_PLAN"]
    assert problems[0]["path"] == "changes[1].parameter"


def test_problems_aggregate_instead_of_stopping_at_the_first_one():
    plan = make_plan(
        schema="catia-edit-plan/2",
        editSessionId="someone-else",
        changes=[make_change(parameter="Part2\\Missing")],
    )
    assert codes(problems_for(plan)) == ["INVALID_PLAN", "INVALID_PLAN", "PARAM_UNKNOWN"]


def test_preserve_is_rejected_when_the_invariant_is_unsupported():
    snapshot = make_snapshot()
    snapshot["capabilities"]["invariants"]["holes_edge_distance"] = {
        "supported": False,
        "reason": "hole positions are held by geometric constraints only",
    }
    plan = make_plan(
        preserve=[{"type": "holes_edge_distance", "parameter": DRIVER_LENGTH, "targetValue": 15.0}]
    )
    problems = problems_for(plan, snapshot)
    assert codes(problems) == ["INVARIANT_UNSUPPORTED"]
    assert problems[0]["path"] == "preserve[0]"
    assert "geometric constraints" in problems[0]["message"]


def test_preserve_is_rejected_when_the_driver_moves_a_boundary_but_not_the_holes():
    snapshot = make_snapshot()
    snapshot["impacts"][DRIVER_LENGTH].update(
        {"geometryChanged": True, "edgeDistancesPreserved": False}
    )
    plan = make_plan(
        preserve=[{"type": "holes_edge_distance", "parameter": DRIVER_LENGTH, "targetValue": 15.0}]
    )
    problems = problems_for(plan, snapshot)
    assert codes(problems) == ["INVARIANT_UNSUPPORTED"]
    assert problems[0]["path"] == "preserve[0]"


@pytest.mark.parametrize(
    "item",
    [
        {"type": "surface_area", "expected": 100.0},
        {"type": "VOLUME", "expected": 1.0},
        {"expected": 1.0},
        "volume",
    ],
    ids=["unknown_type", "wrong_case", "missing_type", "not_an_object"],
)
def test_unknown_acceptance_type_is_invalid_plan(item):
    problems = problems_for(make_plan(acceptance=[item]))
    assert codes(problems) == ["INVALID_PLAN"]
    assert problems[0]["path"] == "acceptance[0]"


ACCEPTANCE_PAYLOADS = [
    {"type": "bbox_range", "axis": "x", "min": -100.0, "max": 100.0},
    {"type": "bbox_dimension", "axis": "y", "expected": 60.0, "absTolerance": 0.01},
    {"type": "volume", "expected": 239692.63, "absTolerance": 0.01},
    {"type": "hole_positions", "holes": [{"x": 65.0, "y": 10.0}]},
    {"type": "hole_world_positions", "holes": [{"x": 65.0, "y": 10.0, "z": 0.0}]},
    {"type": "hole_radius", "expected": 6.0},
    {"type": "unchanged_dimension", "parameter": DRIVER_ANGLE, "expected": 45.0},
    {"type": "feature_count", "expected": 2},
]


@pytest.mark.parametrize("item", ACCEPTANCE_PAYLOADS, ids=[p["type"] for p in ACCEPTANCE_PAYLOADS])
def test_whitelisted_acceptance_types_are_accepted(item):
    assert problems_for(make_plan(acceptance=[item])) == []


def test_documented_acceptance_whitelist_matches_the_module():
    documented = {
        "bbox_range",
        "bbox_dimension",
        "volume",
        "hole_positions",
        "hole_world_positions",
        "hole_radius",
        "unchanged_dimension",
        "feature_count",
    }
    assert ACCEPTANCE_TYPES == documented


def test_plan_hash_ignores_key_order():
    plan = make_plan()
    reordered = {
        "acceptance": [],
        "changes": [
            {
                "unit": "mm",
                "newValue": 200.0,
                "parameter": DRIVER_LENGTH,
                "oldValue": 160.0,
                "reason": "lengthen the plate",
            }
        ],
        "preserve": [],
        "intent": {"resolved": "absolute length = 200 mm", "text": "长度改为 200 mm"},
        "operationId": "op-0001",
        "expectedRevision": MODEL_REVISION,
        "editSessionId": SESSION_ID,
        "schema": PLAN_SCHEMA,
    }
    assert plan_hash(plan) == plan_hash(reordered)


def test_plan_hash_changes_when_a_value_changes():
    base = plan_hash(make_plan())
    assert plan_hash(make_plan(changes=[make_change(newValue=200.5)])) != base
    assert plan_hash(make_plan(changes=[make_change(oldValue=159.0)])) != base
    assert plan_hash(make_plan(operationId="op-0002")) != base


@pytest.mark.xfail(
    strict=False,
    reason="contract mismatch: editing-protocol-v1.md 3.4 publishes planHash as 'sha256:<hex>' "
    "but validation.plan_hash() returns a bare hex digest",
)
def test_plan_hash_carries_the_documented_sha256_prefix():
    assert plan_hash(make_plan()).startswith("sha256:")


# ---------------------------------------------------------------------------
# B. Journal (SQLite, tmp_path)
# ---------------------------------------------------------------------------


def session_payload(
    session_id: str,
    *,
    status: str = "ready",
    revision: int = 0,
    handle: str = DRIVER_LENGTH,
) -> dict:
    return {
        "schema": "catia-edit-session/1",
        "editSessionId": session_id,
        "modelRevision": revision,
        "createdUtc": "2026-10-23T06:00:00.000+00:00",
        "status": status,
        "source": {
            "path": SOURCE_PATH,
            "sha256": "c" * 64,
            "sizeBytes": 196608,
            "mtimeUtc": "2026-10-23T05:59:00.000+00:00",
        },
        "workingCopy": {
            "path": WORKING_PATH,
            "sha256": "d" * 64,
            "createdUtc": "2026-10-23T06:00:00.000+00:00",
        },
        "impacts": {},
        "parameters": [{"handle": handle, "value": 160.0}],
        "results": [],
    }


def row_count(db_path, table: str) -> int:
    connection = sqlite3.connect(db_path)
    try:
        return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        connection.close()


def test_session_roundtrip_and_unknown_session(tmp_path):
    journal = Journal(tmp_path / "editing.sqlite3")
    payload = session_payload("sess-roundtrip")
    journal.save_session(payload)
    assert journal.load_session("sess-roundtrip") == payload
    assert journal.load_session("sess-missing") is None


def test_list_sessions_orders_by_updated_time(tmp_path, monkeypatch):
    stamps = iter(
        [
            "2026-10-23T06:00:00.000+00:00",
            "2026-10-23T06:00:01.000+00:00",
            "2026-10-23T06:00:02.000+00:00",
        ]
    )
    monkeypatch.setattr(journal_module, "_now", lambda: next(stamps))
    journal = Journal(tmp_path / "editing.sqlite3")
    journal.save_session(session_payload("sess-old"))
    journal.save_session(session_payload("sess-new"))

    listed = journal.list_sessions()
    assert [row["session_id"] for row in listed] == ["sess-new", "sess-old"]
    assert [row["updated_utc"] for row in listed] == [
        "2026-10-23T06:00:01.000+00:00",
        "2026-10-23T06:00:00.000+00:00",
    ]
    assert set(listed[0]) == {"session_id", "status", "revision", "working_path", "updated_utc"}

    # Re-saving the older session bumps its updated time, so it leads the list.
    journal.save_session(session_payload("sess-old", status="verified", revision=1))
    listed = journal.list_sessions()
    assert [row["session_id"] for row in listed] == ["sess-old", "sess-new"]
    assert listed[0]["status"] == "verified" and listed[0]["revision"] == 1


def test_upsert_operation_returns_payload_and_overwrites_state(tmp_path):
    db_path = tmp_path / "editing.sqlite3"
    journal = Journal(db_path)
    journal.upsert_operation(
        "op-1", "sess-1", "hash-a", "intent", {"plan": {"operationId": "op-1"}, "step": 1}
    )
    stored = journal.get_operation("op-1")
    assert stored["operation_id"] == "op-1"
    assert stored["session_id"] == "sess-1"
    assert stored["plan_hash"] == "hash-a"
    assert stored["state"] == "intent"
    assert stored["payload"] == {"plan": {"operationId": "op-1"}, "step": 1}

    journal.upsert_operation(
        "op-1", "sess-1", "hash-a", "verified", {"result": {"verdict": "PASS"}}
    )
    stored = journal.get_operation("op-1")
    assert stored["state"] == "verified"
    assert stored["payload"] == {"result": {"verdict": "PASS"}}
    assert row_count(db_path, "operations") == 1
    assert journal.get_operation("op-missing") is None


def test_operations_in_state_filters_by_session_and_state(tmp_path):
    journal = Journal(tmp_path / "editing.sqlite3")
    journal.upsert_operation("op-a", "sess-1", "h1", "intent", {"n": 1})
    journal.upsert_operation("op-b", "sess-1", "h2", "verified", {"n": 2})
    journal.upsert_operation("op-c", "sess-2", "h3", "intent", {"n": 3})

    assert [op["operation_id"] for op in journal.operations_in_state("sess-1", ("intent",))] == [
        "op-a"
    ]
    both = journal.operations_in_state("sess-1", ("intent", "verified"))
    assert {op["operation_id"] for op in both} == {"op-a", "op-b"}
    assert [op["operation_id"] for op in journal.operations_in_state("sess-2", ("intent",))] == [
        "op-c"
    ]
    assert journal.operations_in_state("sess-1", ("failed",)) == []
    assert journal.operations_in_state("sess-unknown", ("intent",)) == []
    payloads = {op["operation_id"]: op["payload"] for op in both}
    assert payloads == {"op-a": {"n": 1}, "op-b": {"n": 2}}


def test_events_are_oldest_first_with_limit(tmp_path):
    journal = Journal(tmp_path / "editing.sqlite3")
    journal.append_event("session_begin", {"source": SOURCE_PATH}, session_id="sess-1")
    journal.append_event("intent", {"changes": [{"parameter": DRIVER_LENGTH}]}, session_id="sess-1",
                         operation_id="op-1")
    journal.append_event("finalize", {"passed": True, "备注": "交付"}, session_id="sess-1")
    journal.append_event("intent", {"other": True}, session_id="sess-2")

    events = journal.events("sess-1")
    assert [event["kind"] for event in events] == ["session_begin", "intent", "finalize"]
    assert events[1]["payload"] == {"changes": [{"parameter": DRIVER_LENGTH}]}
    assert events[2]["payload"] == {"passed": True, "备注": "交付"}
    assert all(isinstance(event["createdUtc"], str) for event in events)

    # The limit keeps the newest N events and still returns them oldest-first.
    assert [event["kind"] for event in journal.events("sess-1", limit=2)] == ["intent", "finalize"]
    assert [event["kind"] for event in journal.events("sess-2")] == ["intent"]


def test_two_journal_instances_on_one_file_see_each_other(tmp_path):
    db_path = tmp_path / "editing.sqlite3"
    before_restart = Journal(db_path)
    before_restart.save_session(session_payload("sess-shared", status="executing"))
    before_restart.upsert_operation("op-shared", "sess-shared", "hash", "intent", {"before": 160.0})
    before_restart.append_event("intent", {"operationId": "op-shared"}, session_id="sess-shared")

    after_restart = Journal(db_path)
    assert after_restart.load_session("sess-shared")["status"] == "executing"
    assert [row["session_id"] for row in after_restart.list_sessions()] == ["sess-shared"]
    assert after_restart.get_operation("op-shared")["state"] == "intent"
    assert [event["kind"] for event in after_restart.events("sess-shared")] == ["intent"]

    after_restart.upsert_operation(
        "op-shared", "sess-shared", "hash", "recovered", {"after": 160.0}
    )
    assert before_restart.get_operation("op-shared")["state"] == "recovered"


def test_non_ascii_handle_survives_a_roundtrip(tmp_path):
    handle = "Part1\\零件几何体\\草图.1\\偏移.13\\Offset"
    journal = Journal(tmp_path / "editing.sqlite3")
    journal.save_session(session_payload("sess-cjk", handle=handle))
    loaded = journal.load_session("sess-cjk")
    assert loaded["parameters"][0]["handle"] == handle
    assert loaded["parameters"][0]["handle"].split("\\") == [
        "Part1",
        "零件几何体",
        "草图.1",
        "偏移.13",
        "Offset",
    ]
    assert loaded["parameters"][0]["value"] == 160.0


# ---------------------------------------------------------------------------
# C. pure helpers: approx_equal, param_kind, acceptance_tolerance
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("a", "b"),
    [(None, 1.0), (1.0, None), (None, None)],
    ids=["a_none", "b_none", "both_none"],
)
def test_approx_equal_never_matches_a_missing_value(a, b):
    assert approx_equal(a, b) is False


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (1.0, 1.0, True),
        (1.0, 1.005, True),
        (1.0, 1.02, False),
        (0.0, 0.009, True),
        (0.0, 0.02, False),
    ],
)
def test_approx_equal_default_tolerance_is_0_01(a, b, expected):
    assert approx_equal(a, b) is expected


def test_approx_equal_with_zero_tolerance_requires_exact_equality():
    assert approx_equal(1.0, 1.0, rel=0.0, abs_tol=0.0) is True
    assert approx_equal(1.0, 1.0 + 1e-12, rel=0.0, abs_tol=0.0) is False


@pytest.mark.parametrize(
    ("a", "b", "kwargs", "expected"),
    [
        (1_000_000.0, 1_000_000.5, {}, True),
        (1_000_000.0, 1_000_004.0, {}, False),
        (1_000.0, 1_000.5, {"rel": 1e-3, "abs_tol": 0.0}, True),
        (1_000.0, 1_001.5, {"rel": 1e-3, "abs_tol": 0.0}, False),
        (-2.0, -2.004, {}, True),
        (-2.0, -2.02, {}, False),
    ],
    ids=[
        "large_within_relative",
        "large_outside_relative",
        "rel_override_in",
        "rel_override_out",
        "negative_in",
        "negative_out",
    ],
)
def test_approx_equal_scales_with_magnitude(a, b, kwargs, expected):
    assert approx_equal(a, b, **kwargs) is expected


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("L", "driver"),
        ("Part2\\L", "driver"),
        ("Part2\\Length.1", "driver"),
        ("Part2\\零件几何体\\草图.1\\偏移.13\\Offset", "dimension"),
        ("Part2\\PartBody\\Sketch.1\\OFFSET.5", "dimension"),
        ("Part2\\PartBody\\Sketch.1\\Radius.2", "dimension"),
        ("Part2\\PartBody\\Sketch.1\\Angle.1", "dimension"),
        ("Part2\\PartBody\\Sketch.1\\Distance.7", "dimension"),
        ("Part2\\PartBody\\草图.1\\半径.2", "dimension"),
        ("Part2\\PartBody\\Pad.1\\Thickness", "feature"),
    ],
    ids=[
        "bare_name",
        "top_level",
        "top_level_name_wins_over_marker",
        "chinese_marker",
        "case_insensitive",
        "radius",
        "angle",
        "distance",
        "chinese_radius",
        "feature",
    ],
)
def test_param_kind_uses_path_shape(name, expected):
    assert param_kind(name) == expected


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"type": "volume"}, 0.01),
        ({"type": "volume", "absTolerance": 0.02}, 0.02),
        ({"type": "bbox_dimension", "absTolerance": 0}, 0.0),
        ({"type": "hole_radius", "absTolerance": "0.05"}, 0.05),
        ({"absTolerance": 2}, 2.0),
    ],
    ids=["default", "explicit", "explicit_zero", "string_value", "integer_value"],
)
def test_acceptance_tolerance_default_and_explicit(item, expected):
    tolerance = acceptance_tolerance(item)
    assert isinstance(tolerance, float)
    assert tolerance == expected


# ---------------------------------------------------------------------------
# D. coordinator.model_value (pure lookup, no COM)
# ---------------------------------------------------------------------------


def test_model_value_returns_stored_value_or_none_for_missing_handles():
    model = {
        "parameters": [
            {"handle": DRIVER_LENGTH, "value": 160.0, "kind": "driver"},
            {"handle": FORMULA_DIMENSION, "value": 55.0, "kind": "dimension"},
        ]
    }
    assert model_value(model, DRIVER_LENGTH) == 160.0
    assert model_value(model, FORMULA_DIMENSION) == 55.0
    assert model_value(model, "Part2\\Missing") is None
    assert model_value(model, "part2\\l") is None  # handles are case sensitive
    assert model_value(model, None) is None
    assert model_value(model, "") is None
    assert model_value({}, DRIVER_LENGTH) is None
    assert model_value({"parameters": []}, DRIVER_LENGTH) is None
