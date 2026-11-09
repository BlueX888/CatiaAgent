"""Wire and canonical structure for ``catia-edit-plan/1``.

Legacy behaviour kept on purpose:

* unknown keys inside a plan, change, preserve item or acceptance item were
  ignored (not rejected) by ``validate_plan``; the wire models therefore allow
  extra keys and the canonical model keeps them under ``extra`` so nothing is
  silently dropped;
* domain judgement — writability, revision, old value, units, ranges,
  invariants and tolerance bounds — stays in ``validate_plan``;
* the original wire ``dict`` remains the input to the three plan hashes.  A
  canonical model is never substituted for it.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import ConfigDict, Field

from .base import CanonicalModel, WireFloat, WireInt, WireModel

EDIT_PLAN_SCHEMA = "catia-edit-plan/1"

WireUnit = Literal["mm", "deg", "none"]
WireAxis = Literal["x", "y", "z"]


class OpenWireModel(WireModel):
    """Legacy-compatible wire object: unknown keys are kept, not rejected."""

    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        validate_assignment=True,
    )


class OpenCanonicalModel(CanonicalModel):
    model_config = ConfigDict(
        extra="allow",
        populate_by_name=True,
        validate_assignment=True,
    )


class ParameterChangeWire(OpenWireModel):
    parameter: str
    oldValue: WireFloat
    newValue: WireFloat
    unit: WireUnit
    reason: Optional[str] = None


class BBoxRangeWire(OpenWireModel):
    type: Literal["bbox_range"]
    axis: WireAxis
    min: WireFloat
    max: WireFloat
    absTolerance: Optional[WireFloat] = None
    relTolerance: Optional[WireFloat] = None


class BBoxDimensionWire(OpenWireModel):
    type: Literal["bbox_dimension"]
    axis: WireAxis
    expected: WireFloat
    absTolerance: Optional[WireFloat] = None
    relTolerance: Optional[WireFloat] = None


class VolumeWire(OpenWireModel):
    type: Literal["volume"]
    expected: WireFloat
    absTolerance: Optional[WireFloat] = None
    relTolerance: Optional[WireFloat] = None


class XYHoleWire(OpenWireModel):
    x: WireFloat
    y: WireFloat
    r: Optional[WireFloat] = None


class WorldHoleWire(OpenWireModel):
    x: WireFloat
    y: WireFloat
    z: WireFloat


class HolePositionsWire(OpenWireModel):
    type: Literal["hole_positions"]
    holes: list[XYHoleWire] = Field(min_length=1)


class HoleWorldPositionsWire(OpenWireModel):
    type: Literal["hole_world_positions"]
    holes: list[WorldHoleWire] = Field(min_length=1)


class HoleRadiusWire(OpenWireModel):
    type: Literal["hole_radius"]
    expected: WireFloat
    parameter: Optional[str] = None
    absTolerance: Optional[WireFloat] = None
    relTolerance: Optional[WireFloat] = None


class UnchangedDimensionWire(OpenWireModel):
    type: Literal["unchanged_dimension"]
    parameter: str = Field(min_length=1)
    expected: WireFloat


class FeatureCountWire(OpenWireModel):
    type: Literal["feature_count"]
    expected: WireInt = Field(ge=0)


AcceptanceWire = Annotated[
    Union[
        BBoxRangeWire,
        BBoxDimensionWire,
        VolumeWire,
        HolePositionsWire,
        HoleWorldPositionsWire,
        HoleRadiusWire,
        UnchangedDimensionWire,
        FeatureCountWire,
    ],
    Field(discriminator="type"),
]


class EdgeDistancePreserveWire(OpenWireModel):
    type: Literal["holes_edge_distance"]
    holes: Optional[list[WireInt]] = None


class WorldPositionPreserveWire(OpenWireModel):
    type: Literal["hole_world_position"]
    holes: list[WorldHoleWire] = Field(min_length=1)


PreserveWire = Annotated[
    Union[EdgeDistancePreserveWire, WorldPositionPreserveWire],
    Field(discriminator="type"),
]


class EditPlanWire(OpenWireModel):
    draft_schema: Literal["catia-edit-plan/1"] = Field(alias="schema")
    editSessionId: str = Field(min_length=1)
    operationId: str = Field(min_length=1)
    expectedRevision: WireInt
    intent: Optional[dict[str, Any]] = None
    changes: list[ParameterChangeWire] = Field(min_length=1)
    preserve: list[PreserveWire] = Field(default_factory=list)
    acceptance: list[AcceptanceWire] = Field(default_factory=list)


class CanonicalParameterChange(OpenCanonicalModel):
    parameter: str
    old_value: float
    new_value: float
    unit: str
    reason: Optional[str] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class CanonicalAcceptance(OpenCanonicalModel):
    type: str
    fields: dict[str, Any]
    extra: dict[str, Any] = Field(default_factory=dict)


class CanonicalPreserve(OpenCanonicalModel):
    type: str
    fields: dict[str, Any]
    extra: dict[str, Any] = Field(default_factory=dict)


class CanonicalEditPlan(CanonicalModel):
    draft_schema: Literal["catia-edit-plan/1"] = EDIT_PLAN_SCHEMA
    edit_session_id: str
    operation_id: str
    expected_revision: int
    intent: Optional[dict[str, Any]] = None
    changes: list[CanonicalParameterChange]
    preserve: list[CanonicalPreserve]
    acceptance: list[CanonicalAcceptance]
    extra: dict[str, Any] = Field(default_factory=dict)


def _extras(model: Any, known: set[str] | None = None) -> dict[str, Any]:
    # ``model_extra`` only contains keys that are not declared fields.
    return dict(model.model_extra or {})


def canonicalize_edit_plan(plan: EditPlanWire) -> CanonicalEditPlan:
    """Normalise structure only; ranges, revisions and writability stay domain rules."""
    changes = [
        CanonicalParameterChange(
            parameter=change.parameter,
            old_value=change.oldValue,
            new_value=change.newValue,
            unit=change.unit,
            reason=change.reason,
            extra=_extras(change, {"parameter", "oldValue", "newValue", "unit", "reason"}),
        )
        for change in plan.changes
    ]
    preserve = []
    for item in plan.preserve:
        data = item.model_dump(exclude_none=True)
        item_type = data.pop("type")
        preserve.append(CanonicalPreserve(type=item_type, fields=data,
                                          extra=_extras(item, set(data) | {"type"})))
    acceptance = []
    for item in plan.acceptance:
        data = item.model_dump(exclude_none=True)
        item_type = data.pop("type")
        acceptance.append(CanonicalAcceptance(type=item_type, fields=data,
                                              extra=_extras(item, set(data) | {"type"})))
    return CanonicalEditPlan(
        edit_session_id=plan.editSessionId,
        operation_id=plan.operationId,
        expected_revision=plan.expectedRevision,
        intent=plan.intent,
        changes=changes,
        preserve=preserve,
        acceptance=acceptance,
        extra=_extras(plan, {"schema", "editSessionId", "operationId",
                             "expectedRevision", "intent", "changes", "preserve", "acceptance"}),
    )
