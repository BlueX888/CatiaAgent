"""Primitive types for the bounded feature requirement language.

This module mirrors the published wire language in
``catia_mcp.drawing.feature_schema``: literal dimensions accept only zero (or
360 for a revolution), coordinates accept a bounded literal or a symbolic
string, and compact arrays are accepted alongside their object form.

It does not compile or evaluate expressions.  In particular a symbolic string
stays a string in the canonical view; it is never floated before the existing
expression parser and evidence rules have seen it.  No COM, file or network
access is allowed here.
"""

from __future__ import annotations

import math
import re
from typing import Annotated, Any, Literal, Optional, Union

from pydantic import BeforeValidator, Field, StrictBool, field_validator

from .base import CanonicalModel, ContractModel, WireFloat, WireModel

MAX_EXPR = 256
COORD_LIMIT = 1_000_000


class ClosedWireModel(WireModel):
    pass


# -- scalar expressions ------------------------------------------------------


def _finite_number(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("expected a finite JSON number; booleans and strings are not numbers")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite numbers are not accepted")
    return number


# Literal dimensions accept only the documented numeric constant; every other
# dimension must be a symbolic string. Bounds and lengths are in the schema.
def _reject_boolean(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError("a boolean is not a dimension")
    return value


ExprWire = Annotated[Union[Literal[0], Annotated[str, Field(max_length=MAX_EXPR)]],
                     BeforeValidator(_reject_boolean)]
RevolutionAngleWire = Annotated[Union[Literal[360], Annotated[str, Field(max_length=MAX_EXPR)]],
                                BeforeValidator(_reject_boolean)]
CoordNumber = Annotated[float, BeforeValidator(_finite_number),
                        Field(ge=-COORD_LIMIT, le=COORD_LIMIT)]
CoordWire = Union[CoordNumber, Annotated[str, Field(max_length=MAX_EXPR)]]
UnitNumber = Annotated[float, BeforeValidator(_finite_number),
                       Field(ge=-1.000001, le=1.000001)]
VectorNumber = Annotated[float, BeforeValidator(_finite_number),
                         Field(ge=-COORD_LIMIT, le=COORD_LIMIT)]


class PointWire(ClosedWireModel):
    x: CoordWire
    y: CoordWire


class PlaneFrameWire(ClosedWireModel):
    normal: tuple[UnitNumber, UnitNumber, UnitNumber]
    xDirection: tuple[UnitNumber, UnitNumber, UnitNumber]


ProfileReference = Annotated[str, Field(pattern=r"^P[0-9]{1,4}$")]


class RectangleWire(ClosedWireModel):
    type: Literal["rectangle"]
    x: Optional[CoordWire] = None
    y: Optional[CoordWire] = None
    width: ExprWire
    height: ExprWire


class CircleWire(ClosedWireModel):
    type: Literal["circle"]
    x: Optional[CoordWire] = None
    y: Optional[CoordWire] = None
    radius: ExprWire


class PolygonWire(ClosedWireModel):
    type: Literal["polygon"]
    points: list[PointWire] = Field(min_length=3, max_length=256)


class LineSegmentWire(ClosedWireModel):
    type: Literal["line"]
    end: PointWire


class ArcSegmentWire(ClosedWireModel):
    type: Literal["arc"]
    end: PointWire
    radius: ExprWire
    clockwise: Optional[StrictBool] = None
    largeArc: Optional[StrictBool] = None


SegmentWire = Union[LineSegmentWire, ArcSegmentWire]


class WireProfileWire(ClosedWireModel):
    type: Literal["wire"]
    start: PointWire
    segments: list[SegmentWire] = Field(min_length=2, max_length=256)


ObjectProfileWire = Union[RectangleWire, CircleWire, PolygonWire, WireProfileWire]

# -- compact arrays ----------------------------------------------------------

CompactPointWire = Union[PointWire, tuple[CoordWire, CoordWire]]

LineCompact = tuple[Literal["line"], CoordWire, CoordWire]
ArcCompact = Union[
    tuple[Literal["arc"], CoordWire, CoordWire, ExprWire],
    tuple[Literal["arc"], CoordWire, CoordWire, ExprWire, StrictBool],
    tuple[Literal["arc"], CoordWire, CoordWire, ExprWire, StrictBool, StrictBool],
]
CompactSegmentWire = Union[LineCompact, ArcCompact]

CircleCompact = tuple[Literal["circle"], CoordWire, CoordWire, ExprWire]
RectangleCompact = tuple[Literal["rectangle"], CoordWire, CoordWire, ExprWire, ExprWire]
PolygonCompact = tuple[Literal["polygon"], list[CompactPointWire]]
WireCompact = tuple[Literal["wire"], CompactPointWire, list[CompactSegmentWire]]
CompactProfileWire = Union[CircleCompact, RectangleCompact, PolygonCompact, WireCompact]

AnyProfileWire = Union[ObjectProfileWire, CompactProfileWire, ProfileReference]


class SketchWire(ClosedWireModel):
    plane: Union[Literal["xy", "yz", "zx"], PlaneFrameWire]
    offset: Optional[CoordWire] = None
    profiles: list[AnyProfileWire] = Field(min_length=1, max_length=256)


class EdgeLinearWire(ClosedWireModel):
    type: Literal["linear"]
    direction: tuple[VectorNumber, VectorNumber, VectorNumber]
    through: list[CoordWire] = Field(min_length=3, max_length=3)
    withinSegment: Optional[StrictBool] = None


class EdgeCircularWire(ClosedWireModel):
    type: Literal["circular"]
    radius: ExprWire
    centre: list[CoordWire] = Field(min_length=3, max_length=3)
    axis: tuple[VectorNumber, VectorNumber, VectorNumber]


EdgeSelectorWire = Union[EdgeLinearWire, EdgeCircularWire]


class RequirementParameterWire(ClosedWireModel):
    name: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,31}$")
    type: Optional[Literal["length", "angle"]] = None
    value: WireFloat
    evidence: list[str] = Field(min_length=0)
    source: Optional[Literal["drawing", "reference_inference"]] = None

    @field_validator("evidence")
    @classmethod
    def evidence_is_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("evidence references must be unique")
        if any(not item or len(item) > 256 for item in value):
            raise ValueError("evidence references must be non-empty and short")
        return value


# -- canonical views ---------------------------------------------------------


class CanonicalExpr(CanonicalModel):
    raw: Union[int, str]
    kind: Literal["literal_zero", "full_turn", "expression"]
    value: Optional[float] = None
    formula: Optional[str] = None
    parameters: list[str] = Field(default_factory=list)


def canonicalize_expr(raw: Union[int, str], *, revolution: bool = False) -> CanonicalExpr:
    """Describe a literal exactly; never evaluate a symbolic expression."""
    if not isinstance(raw, (int, float, str)) or isinstance(raw, bool):
        raise TypeError("expression must be a literal zero/360 or a string")
    if isinstance(raw, str):
        cleaned = raw.strip()
        if re.fullmatch(r"[+-]?(?:0+(?:\.0*)?|\.0+)", cleaned):
            return CanonicalExpr(raw=raw, kind="literal_zero", value=0.0,
                                 formula="0deg" if revolution else "0mm")
        if revolution and re.fullmatch(r"[+-]?360(?:\.0+)?", cleaned):
            return CanonicalExpr(raw=raw, kind="full_turn", value=360.0, formula="360deg")
        return CanonicalExpr(raw=raw, kind="expression")
    number = float(raw)
    if revolution and number == 360:
        return CanonicalExpr(raw=int(raw), kind="full_turn", value=360.0, formula="360deg")
    return CanonicalExpr(raw=int(raw), kind="literal_zero", value=0.0, formula="0mm")


def expand_compact_point(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return {"x": value.get("x"), "y": value.get("y")}
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return {"x": value[0], "y": value[1]}
    return None


def expand_compact_segment(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        return None
    kind = value[0]
    if kind == "line" and len(value) == 3:
        return {"type": "line", "end": {"x": value[1], "y": value[2]}}
    if kind == "arc" and 4 <= len(value) <= 6:
        segment = {"type": "arc", "end": {"x": value[1], "y": value[2]}, "radius": value[3]}
        if len(value) > 4:
            segment["clockwise"] = value[4]
        if len(value) > 5:
            segment["largeArc"] = value[5]
        return segment
    return None


def expand_compact_profile(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        return {"ref": value}
    if not isinstance(value, (list, tuple)) or not value:
        return None
    kind = value[0]
    if kind == "circle" and len(value) == 4:
        return {"type": "circle", "x": value[1], "y": value[2], "radius": value[3]}
    if kind == "rectangle" and len(value) == 5:
        return {"type": "rectangle", "x": value[1], "y": value[2], "width": value[3], "height": value[4]}
    if kind == "polygon" and len(value) == 2:
        return {"type": "polygon", "points": [expand_compact_point(item) for item in value[1]]}
    if kind == "wire" and len(value) == 3:
        return {"type": "wire", "start": expand_compact_point(value[1]),
                "segments": [expand_compact_segment(item) for item in value[2]]}
    return None
