"""Wire and canonical structure for ``cad-intent-draft/1``.

The wire model describes the whole accepted set, including the decision aliases
the compiler already understands.  The canonical model keeps missing values
missing: it never selects a candidate, never defaults a unit and never rewrites
the text supplied by the model.  Host-only fields (``userText``,
``userConfirmed``, ``confirmationText``) are not part of the draft and are
rejected here; the host keeps ownership of them.

Semantic compilation, candidate selection and clarification gates stay in
``catia_mcp.editing.intent.compile_intent``.  This module only normalises the
aliases the compiler already documents.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import Field

from .base import (
    CanonicalModel,
    WireFloat,
    WireInt,
    WireModel,
)

INTENT_SCHEMA = "cad-intent-draft/1"

WireDecision = Literal[
    "planned", "plan", "propose",
    "needs_clarification", "clarify", "clarification",
    "unsupported", "reject", "rejected", "refuse",
]
CanonicalDecision = Literal["planned", "needs_clarification", "unsupported"]

_DECISION_ALIASES = {
    "planned": "planned", "plan": "planned", "propose": "planned",
    "needs_clarification": "needs_clarification", "clarify": "needs_clarification",
    "clarification": "needs_clarification",
    "unsupported": "unsupported", "reject": "unsupported",
    "rejected": "unsupported", "refuse": "unsupported",
}

_ATTR_ALIASES = {
    "长": "length", "长度": "length", "长边": "length",
    "宽": "width", "宽度": "width", "高": "height", "高度": "height",
    "厚": "thickness", "厚度": "thickness", "深度": "depth", "距离": "distance",
    "尺寸": "size", "直径": "diameter", "孔径": "diameter", "半径": "radius",
    "角度": "angle", "未知": "unknown",
}
_UNIT_ALIASES = {
    "mm": "mm", "millimeter": "mm", "millimetre": "mm", "毫米": "mm",
    "cm": "cm", "centimeter": "cm", "centimetre": "cm", "厘米": "cm",
    "m": "m", "meter": "m", "metre": "m", "米": "m",
    "in": "in", "inch": "in", "inches": "in", "英寸": "in",
    "deg": "deg", "degree": "deg", "degrees": "deg", "度": "deg",
    "none": "none", "unitless": "none", "": "none",
}
_SCOPE_ALIASES = {
    "overall": "overall", "total": "overall", "total_length": "overall",
    "whole": "overall", "总长": "overall", "总共": "overall", "整体": "overall",
    "each_side": "each_side", "eachside": "each_side", "both_sides": "each_side",
    "both": "each_side", "two_sides": "each_side", "每侧": "each_side",
    "两侧": "each_side", "左右各": "each_side",
    "single_side": "single_side", "one_side": "single_side", "left": "single_side",
    "right": "single_side", "单侧": "single_side", "左侧": "single_side",
    "右侧": "single_side",
}
_BASIS_ALIASES = {
    "current": "current", "current_model": "current", "now": "current", "当前": "current",
    "original": "original", "source": "original", "baseline": "original",
    "原始": "original", "最初": "original",
}
_DIRECTION_ALIASES = {
    "positive": "positive", "+": "positive", "plus": "positive",
    "increase": "positive", "正向": "positive",
    "negative": "negative", "-": "negative", "minus": "negative",
    "decrease": "negative", "负向": "negative",
}
_OP_ALIASES = {
    "set": "set", "set_to": "set", "absolute": "set", "改为": "set", "设为": "set",
    "increase": "increase", "add": "increase", "增加": "increase", "加": "increase",
    "decrease": "decrease", "reduce": "decrease", "减少": "decrease", "减": "decrease",
}
_PRESERVE_ALIASES = {
    "center_position": "center_position", "center": "center_position",
    "centre": "center_position", "center_fixed": "center_position",
    "中心": "center_position", "保持中心": "center_position",
    "hole_edge_distance": "hole_edge_distance", "holes_edge_distance": "hole_edge_distance",
    "edge_distance": "hole_edge_distance", "hole_margin": "hole_edge_distance",
    "孔边距": "hole_edge_distance", "边距": "hole_edge_distance",
    "hole_centers": "hole_centers", "hole_center": "hole_centers",
    "hole_centres": "hole_centers", "hole_position": "hole_centers",
    "hole_world_position": "hole_world_position",
    "hole_world_positions": "hole_world_position",
    "other_dimensions": "other_dimensions", "other": "other_dimensions",
}
WireOperationType = Literal[
    "set", "set_to", "absolute", "改为", "设为",
    "increase", "add", "增加", "加",
    "decrease", "reduce", "减少", "减",
]


def _norm(value: Any, aliases: dict[str, str]) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return aliases.get(text, text)


class TargetWire(WireModel):
    parameter: Optional[str] = None
    attribute: Optional[str] = None
    binding: Optional[str] = None
    holeCount: Optional[WireInt] = Field(default=None, gt=0)
    kind: Optional[str] = None
    sketch: Optional[str] = None


class OperationWire(WireModel):
    type: Optional[WireOperationType] = None
    amount: Optional[WireFloat] = None
    unit: Optional[str] = None
    scope: Optional[str] = None


class ReferenceWire(WireModel):
    basis: Optional[str] = None
    revision: Optional[WireFloat] = None
    frame: Optional[str] = None
    axis: Optional[str] = None
    direction: Optional[str] = None
    directionText: Optional[str] = None


class IntentDraftWire(WireModel):
    """Complete accepted wire shape for a model-authored intent draft."""

    draft_schema: Literal["cad-intent-draft/1"] = Field(alias="schema")
    text: Optional[str] = None
    decision: Optional[WireDecision] = None
    target: Optional[TargetWire] = None
    operation: Optional[OperationWire] = None
    reference: Optional[ReferenceWire] = None
    preserve: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class CanonicalTarget(CanonicalModel):
    parameter: Optional[str] = None
    attribute: Optional[str] = None
    binding: Optional[str] = None
    hole_count: Optional[int] = None
    kind: Optional[str] = None
    sketch: Optional[str] = None


class CanonicalOperation(CanonicalModel):
    type: Optional[str] = None
    amount: Optional[float] = None
    unit: Optional[str] = None
    scope: Optional[str] = None


class CanonicalReference(CanonicalModel):
    basis: Optional[str] = None
    revision: Optional[int] = None
    frame: Optional[str] = None
    axis: Optional[str] = None
    direction: Optional[str] = None
    direction_text: Optional[str] = None


class CanonicalIntentDraft(CanonicalModel):
    draft_schema: Literal["cad-intent-draft/1"] = Field(default=INTENT_SCHEMA, alias="schema")
    decision: CanonicalDecision
    text: Optional[str] = None
    target: Optional[CanonicalTarget] = None
    operation: Optional[CanonicalOperation] = None
    reference: Optional[CanonicalReference] = None
    preserve: list[str] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


def canonicalize_intent(draft: IntentDraftWire) -> CanonicalIntentDraft:
    """Normalise only the aliases the existing compiler already maps."""
    decision = _DECISION_ALIASES[str(draft.decision or "planned").strip().lower()]
    target = None
    if draft.target is not None:
        target = CanonicalTarget(
            parameter=draft.target.parameter.strip() if isinstance(draft.target.parameter, str) and draft.target.parameter.strip() else None,
            attribute=_norm(draft.target.attribute, _ATTR_ALIASES),
            binding=draft.target.binding,
            hole_count=draft.target.holeCount,
            kind=str(draft.target.kind).strip().lower() if draft.target.kind is not None else None,
            sketch=draft.target.sketch,
        )
    operation = None
    if draft.operation is not None:
        operation = CanonicalOperation(
            type=_norm(draft.operation.type, _OP_ALIASES),
            amount=draft.operation.amount,
            unit=_norm(draft.operation.unit, _UNIT_ALIASES) if draft.operation.unit is not None and str(draft.operation.unit).strip() else None,
            scope=_norm(draft.operation.scope, _SCOPE_ALIASES) or "overall",
        )
    if draft.reference is None:
        # The compiler treats an absent reference as basis=current; this is an
        # existing documented default, not a fabricated value.
        reference = CanonicalReference(basis="current")
    else:
        revision = draft.reference.revision
        reference = CanonicalReference(
            basis=_norm(draft.reference.basis, _BASIS_ALIASES) or "current",
            revision=int(revision) if revision is not None else None,
            frame=draft.reference.frame,
            axis=str(draft.reference.axis).strip().lower() if draft.reference.axis is not None else None,
            direction=_norm(draft.reference.direction, _DIRECTION_ALIASES),
            direction_text=draft.reference.directionText,
        )
    preserve = [_norm(item, _PRESERVE_ALIASES) or "" for item in draft.preserve]

    missing: list[str] = []
    if target is None:
        missing.extend(["target", "target.parameter", "target.attribute"])
    else:
        if target.parameter is None:
            missing.append("target.parameter")
        if target.attribute is None:
            missing.append("target.attribute")
    if operation is None:
        missing.extend(["operation", "operation.type", "operation.amount", "operation.unit"])
    else:
        if operation.type is None:
            missing.append("operation.type")
        if operation.amount is None:
            missing.append("operation.amount")
        if operation.unit is None:
            missing.append("operation.unit")
    if operation is not None and operation.scope == "single_side" and (
            reference is None or reference.direction is None):
        missing.append("reference.direction")

    return CanonicalIntentDraft(
        decision=decision,
        text=draft.text,
        target=target,
        operation=operation,
        reference=reference,
        preserve=preserve,
        unresolved=list(draft.unresolved),
        missing=missing,
    )
