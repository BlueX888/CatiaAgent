"""Per-variant structure for ``catia-feature-requirements/1``.

The union mirrors the published feature language: pad/pocket carry a length and
optional direction (through only on a pocket); shaft/groove carry an angle,
normal direction and optional axisX; fillet carries a radius plus one selector
or a 2-64 selector group; chamfer carries a length, 45 degrees and one selector.

Structural acceptance here does not replace the domain compiler.  Dependency
order, the single-body chain, expression evaluation, evidence checks and the
pre-execution recompile all stay in ``feature_model`` / ``feature_executor``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Optional, Union

from pydantic import ConfigDict, Field, StrictBool, field_validator, model_validator

from .base import WireModel
from .errors import PydanticValidationError, problems_from_pydantic
from .feature_primitives import (
    AnyProfileWire,
    CoordWire,
    EdgeSelectorWire,
    ExprWire,
    RequirementParameterWire,
    RevolutionAngleWire,
    SketchWire,
)

FEATURE_SCHEMA = "catia-feature-requirements/1"
FEATURE_PLAN_SCHEMA = "catia-feature-plan/1"

BATCHES = {
    "pad_pocket": ("pad", "pocket"),
    "shaft_groove": ("shaft", "groove"),
    "fillet_chamfer": ("fillet", "chamfer"),
}


class FeatureWireModel(WireModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, validate_assignment=True)

    id: str = Field(pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
    dependsOn: list[str] = Field(default_factory=list)


class PadFeature(FeatureWireModel):
    operation: Literal["pad"]
    sketch: SketchWire
    length: ExprWire
    direction: Optional[Literal["normal", "reverse"]] = None


class PocketFeature(FeatureWireModel):
    operation: Literal["pocket"]
    sketch: SketchWire
    length: ExprWire
    direction: Optional[Literal["normal", "reverse"]] = None
    through: Optional[StrictBool] = None


class ShaftFeature(FeatureWireModel):
    operation: Literal["shaft"]
    sketch: SketchWire
    angle: RevolutionAngleWire
    direction: Optional[Literal["normal"]] = None
    axisX: Optional[CoordWire] = None


class GrooveFeature(FeatureWireModel):
    operation: Literal["groove"]
    sketch: SketchWire
    angle: RevolutionAngleWire
    direction: Optional[Literal["normal"]] = None
    axisX: Optional[CoordWire] = None


class FilletFeature(FeatureWireModel):
    operation: Literal["fillet"]
    radius: ExprWire
    edges: Union[EdgeSelectorWire, list[EdgeSelectorWire]]

    @field_validator("edges")
    @classmethod
    def edge_group_is_bounded_and_unique(cls, value):
        if isinstance(value, list):
            if not 2 <= len(value) <= 64:
                raise ValueError("a fillet group needs 2-64 selectors")
            import json
            signatures = [json.dumps(item.model_dump(), sort_keys=True) for item in value]
            if len(set(signatures)) != len(signatures):
                raise ValueError("a fillet group contains duplicate selectors")
        return value


class ChamferFeature(FeatureWireModel):
    operation: Literal["chamfer"]
    length: ExprWire
    angle: Literal[45]
    edges: EdgeSelectorWire


FeatureWire = Annotated[
    Union[PadFeature, PocketFeature, ShaftFeature, GrooveFeature, FilletFeature, ChamferFeature],
    Field(discriminator="operation"),
]


class FeatureRequirementsWire(WireModel):
    draft_schema: Literal["catia-feature-requirements/1"] = Field(alias="schema")
    unit: Literal["mm", "inch"]
    unitEvidence: list[str] = Field(default_factory=list)
    partName: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
    parameters: list[RequirementParameterWire] = Field(min_length=1, max_length=512)
    profiles: dict[str, AnyProfileWire] = Field(default_factory=dict)
    features: list[FeatureWire] = Field(min_length=1, max_length=256)

    @field_validator("unitEvidence")
    @classmethod
    def unit_evidence_is_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("unitEvidence references must be unique")
        return value

    @model_validator(mode="after")
    def profile_names_match_references(self) -> "FeatureRequirementsWire":
        for name in self.profiles:
            if not (isinstance(name, str) and name.startswith("P") and name[1:].isdigit()
                    and 1 <= len(name[1:]) <= 4):
                raise ValueError(f"profile name {name!r} must be P<1-4 digits>")
        return self


def structure_problems(requirements: Any) -> list[dict[str, str]]:
    """Return structural problems only; domain rules remain in feature_model."""
    try:
        FeatureRequirementsWire.model_validate(requirements)
    except PydanticValidationError as error:
        return problems_from_pydantic(error)
    return []


def variant_batch(feature: Any) -> str | None:
    operation = feature.get("operation") if isinstance(feature, dict) else getattr(feature, "operation", None)
    for batch, operations in BATCHES.items():
        if operation in operations:
            return batch
    return None
