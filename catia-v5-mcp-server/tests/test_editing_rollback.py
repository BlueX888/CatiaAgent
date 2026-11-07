"""Offline tests for the normalized baseline and the bounded rollback.

The coordinator must never hand ``read_model()["parameters"]`` (a list) to a
``.get()`` call, and a rollback may only claim ``restored=True`` when every
piece of evidence it could read (parameters, volume, bbox, holes) matches the
baseline. These tests drive the real methods with fake CATIA objects; only the
COM read boundary (``read_model``) is replaced by a deterministic reader.
"""

from __future__ import annotations

from typing import Any

import pytest

import catia_mcp.editing.coordinator as coordinator_module
from catia_mcp.editing.coordinator import EditCoordinator
from catia_mcp.editing.journal import Journal
from _editing_fakes import (
    FakeApp,
    FakeConnection,
    FakeDocument,
    FakeParameter,
    FakePart,
)

DRIVER_LENGTH = "Part2\\L"
DRIVER_WIDTH = "Part2\\W"
SESSION_ID = "sess-rollback"
OPERATION_ID = "op-rollback"


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------


def model_shape(
    *,
    parameters: list[dict[str, Any]],
    volume: float = 240000.0,
    dimensions: dict[str, float] | None = None,
    holes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """A dict shaped exactly like ``probe.read_model()``."""
    dimensions = dimensions or {"x": 160.0, "y": 60.0, "z": 10.0}
    half_x = dimensions["x"] / 2.0
    half_y = dimensions["y"] / 2.0
    return {
        "part": "Part2",
        "features": {
            "mainBody": "PartBody",
            "shapes": ["Pad.1"],
            "sketches": [],
            "bodies": ["PartBody"],
        },
        "parameters": parameters,
        "relations": [],
        "geometry": {
            "volumeMm3": volume,
            "areaMm2": 25000.0,
            "bbox": {
                "min": {"x": -half_x, "y": -half_y, "z": 0.0},
                "max": {"x": half_x, "y": half_y, "z": 10.0},
                "dimensions": dict(dimensions),
            },
        },
        "holes": holes
        if holes is not None
        else [{"center": {"x": 65.0, "y": 10.0, "z": 0.0}, "radius": 6.0}],
        "limitations": [],
    }


def parameter_row(handle: str, value: float, unit: str = "mm", kind: str = "driver") -> dict[str, Any]:
    return {
        "handle": handle,
        "name": handle,
        "value": value,
        "unit": unit,
        "dimensionType": "LENGTH",
        "drivenBy": None,
        "kind": kind,
    }


def baseline_model() -> dict[str, Any]:
    return model_shape(
        parameters=[
            parameter_row(DRIVER_LENGTH, 160.0),
            parameter_row(DRIVER_WIDTH, 60.0),
        ]
    )


def reader_from_document(app: Any, doc: Any, part: Any) -> dict[str, Any]:
    """Deterministic stand-in for ``probe.read_model`` (COM boundary only)."""
    return model_shape(
        parameters=[
            {
                "handle": param.Name,
                "name": param.Name,
                "value": float(param.Value),
                "unit": param.Unit.Symbol,
                "dimensionType": "LENGTH",
                "drivenBy": None,
                "kind": "driver",
            }
            for param in part.Parameters._items
        ],
        volume=doc.geometry["volumeMm3"],
        dimensions=doc.geometry["bbox"]["dimensions"],
        holes=doc.holes,
    )


@pytest.fixture()
def coordinator(tmp_path, monkeypatch) -> EditCoordinator:
    monkeypatch.setattr(coordinator_module, "read_model", reader_from_document)
    journal = Journal(tmp_path / "rollback.sqlite3")
    return EditCoordinator(FakeConnection(FakeApp()), journal=journal)


def make_document(*, length: float = 200.0, width: float = 60.0) -> FakeDocument:
    part = FakePart(
        [
            FakeParameter(DRIVER_LENGTH, length, "mm"),
            FakeParameter(DRIVER_WIDTH, width, "mm"),
        ]
    )
    doc = FakeDocument("C:\\work\\rollback\\plate.CATPart", part)
    doc.geometry = baseline_model()["geometry"]
    doc.holes = baseline_model()["holes"]
    return doc


PLAN = {
    "schema": "catia-edit-plan/1",
    "editSessionId": SESSION_ID,
    "operationId": OPERATION_ID,
    "expectedRevision": 0,
    "changes": [{"parameter": DRIVER_LENGTH, "oldValue": 160.0, "newValue": 200.0, "unit": "mm"}],
}


# ---------------------------------------------------------------------------
# _baseline_from_model
# ---------------------------------------------------------------------------


def test_baseline_from_model_normalizes_list_parameters(coordinator: EditCoordinator) -> None:
    model = model_shape(
        parameters=[
            parameter_row(DRIVER_LENGTH, 160.0),
            parameter_row(DRIVER_WIDTH, 60.0, unit="none", kind="system"),
            {"value": 1.0},  # no handle: skipped
            "not-a-parameter",  # not a dict: skipped
            {"handle": "", "value": 2.0},  # blank handle: skipped
            {"handle": "Part2\\Formula", "value": "not numeric"},  # kept as-is
        ],
        holes=[{"center": {"x": 1.0, "y": 2.0, "z": 0.0}, "radius": 3.0}],
    )

    baseline = coordinator._baseline_from_model(model)

    assert isinstance(baseline["parameters"], dict)
    assert baseline["parameters"][DRIVER_LENGTH] == 160.0
    assert baseline["parameters"][DRIVER_WIDTH] == 60.0
    assert baseline["parameters"]["Part2\\Formula"] == "not numeric"
    assert "Part2\\Formula" in baseline["parameters"]
    assert baseline["volumeMm3"] == 240000.0
    assert baseline["bbox"]["dimensions"] == {"x": 160.0, "y": 60.0, "z": 10.0}
    assert baseline["holes"] == [{"center": {"x": 1.0, "y": 2.0, "z": 0.0}, "radius": 3.0}]
    # The regression this guards: calling .get on the list would raise here.
    assert baseline["parameters"].get(DRIVER_LENGTH) == 160.0


def test_baseline_from_model_reads_top_level_geometry_fallbacks(coordinator: EditCoordinator) -> None:
    model = {
        "parameters": [parameter_row(DRIVER_LENGTH, 160.0)],
        "volumeMm3": 1234.5,
        "bbox": {"dimensions": {"x": 9.0, "y": 8.0, "z": 7.0}},
        "holes": [],
    }

    baseline = coordinator._baseline_from_model(model)

    assert baseline["volumeMm3"] == 1234.5
    assert baseline["bbox"]["dimensions"]["x"] == 9.0
    assert baseline["holes"] == []


# ---------------------------------------------------------------------------
# _bounded_rollback: proven restore
# ---------------------------------------------------------------------------


def test_rollback_restores_all_dimensions(coordinator: EditCoordinator) -> None:
    baseline = coordinator._baseline_from_model(baseline_model())
    doc = make_document(length=200.0)  # the failed apply left L at its target
    part = doc.Part

    result = coordinator._bounded_rollback(
        doc, PLAN, baseline, OPERATION_ID, SESSION_ID
    )

    assert result["attempted"] is True
    assert result["error"] is None
    assert result["paramsMatch"] is True
    assert result["volumeMatch"] is True
    assert result["bboxMatch"] is True
    assert result["holesMatch"] is True
    assert result["unavailable"] == []
    assert result["restored"] is True
    assert part.Parameters.Item(DRIVER_LENGTH).Value == 160.0
    assert result["steps"] == [
        {
            "parameter": DRIVER_LENGTH,
            "restoredValue": 160.0,
            "readback": 160.0,
            "matched": True,
        }
    ]
    assert result["parameterMismatch"] == []
    assert result["volumeBefore"] == 240000.0
    assert result["volumeAfterRollback"] == 240000.0
    assert result["bboxBefore"] == {"x": 160.0, "y": 60.0, "z": 10.0}
    assert result["bboxAfterRollback"] == {"x": 160.0, "y": 60.0, "z": 10.0}
    assert result["holesBefore"] == 1
    assert result["holesAfterRollback"] == 1
    # The evidence is journaled with the operation id.
    events = coordinator.journal.events(SESSION_ID)
    assert [event["kind"] for event in events] == ["rollback"]
    assert events[0]["payload"]["restored"] is True


def test_rollback_accepts_a_raw_model_as_baseline_without_list_get(
    coordinator: EditCoordinator,
) -> None:
    """Journals from older builds stored the raw model (parameters is a list)."""
    doc = make_document(length=200.0)
    raw_model = baseline_model()

    result = coordinator._bounded_rollback(
        doc, PLAN, raw_model, OPERATION_ID, SESSION_ID
    )

    assert result["error"] is None
    assert result["paramsMatch"] is True
    assert result["restored"] is True
    assert doc.Part.Parameters.Item(DRIVER_LENGTH).Value == 160.0


# ---------------------------------------------------------------------------
# _bounded_rollback: any single mismatch defeats the proof
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("mismatch", "failed_key"),
    [
        ("parameters", "paramsMatch"),
        ("volume", "volumeMatch"),
        ("bbox", "bboxMatch"),
        ("holes", "holesMatch"),
    ],
)
def test_rollback_any_dimension_mismatch_is_not_restored(
    coordinator: EditCoordinator, mismatch: str, failed_key: str
) -> None:
    baseline = coordinator._baseline_from_model(baseline_model())
    doc = make_document(length=200.0, width=70.0 if mismatch == "parameters" else 60.0)
    if mismatch == "volume":
        doc.geometry["volumeMm3"] = 240000.0 + 1.0
    elif mismatch == "bbox":
        doc.geometry["bbox"]["dimensions"]["x"] = 160.1
    elif mismatch == "holes":
        doc.holes[0]["radius"] = 6.1

    result = coordinator._bounded_rollback(
        doc, PLAN, baseline, OPERATION_ID, SESSION_ID
    )

    assert result[failed_key] is False
    assert result["restored"] is False
    for other in ("paramsMatch", "volumeMatch", "bboxMatch", "holesMatch"):
        if other != failed_key:
            assert result[other] is True
    assert result["error"] is None
    # The parameter write itself still happened and read back at baseline.
    assert doc.Part.Parameters.Item(DRIVER_LENGTH).Value == 160.0


def test_rollback_without_any_evidence_is_not_restored(coordinator: EditCoordinator) -> None:
    baseline = {"parameters": {}, "volumeMm3": None, "bbox": None, "holes": None}
    doc = make_document(length=200.0)

    result = coordinator._bounded_rollback(
        doc, PLAN, baseline, OPERATION_ID, SESSION_ID
    )

    assert result["restored"] is False
    assert result["paramsMatch"] is None
    assert result["volumeMatch"] is None
    assert result["bboxMatch"] is None
    assert result["holesMatch"] is None
    assert result["unavailable"] == ["parameters", "volume", "bbox", "holes"]
    assert result["steps"] == []
    assert doc.Part.Parameters.Item(DRIVER_LENGTH).Value == 200.0


def test_empty_hole_lists_are_not_evidence_of_restore(coordinator: EditCoordinator) -> None:
    """Two empty hole lists must not count as a successful hole comparison."""
    baseline = {"parameters": {}, "volumeMm3": None, "bbox": None, "holes": []}
    doc = make_document(length=200.0)
    doc.holes = []

    result = coordinator._bounded_rollback(
        doc, PLAN, baseline, OPERATION_ID, SESSION_ID
    )

    assert result["holesMatch"] is None
    assert "holes" in result["unavailable"]
    assert result["restored"] is False
