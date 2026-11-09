"""T11 feature primitives: literals, compact forms, canonical views."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from catia_mcp.contracts.feature_primitives import (
    AnyProfileWire,
    CoordWire,
    EdgeSelectorWire,
    ExprWire,
    RequirementParameterWire,
    RevolutionAngleWire,
    SketchWire,
    canonicalize_expr,
    expand_compact_point,
    expand_compact_profile,
    expand_compact_segment,
)
from catia_mcp.drawing.feature_model import SCHEMA, FeatureError, compile_features

PACKAGE = Path(__file__).resolve().parents[1] / "catia_mcp" / "contracts"


class Holder(BaseModel):
    value: Any = None


class ExprHolder(BaseModel):
    value: ExprWire


class RevolutionHolder(BaseModel):
    value: RevolutionAngleWire


class CoordHolder(BaseModel):
    value: CoordWire


class PointHolder(BaseModel):
    value: AnyProfileWire = None


class ProfileHolder(BaseModel):
    value: AnyProfileWire


class SketchHolder(BaseModel):
    value: SketchWire


class EdgeHolder(BaseModel):
    value: EdgeSelectorWire


class ParameterHolder(BaseModel):
    value: RequirementParameterWire


# -- scalar literals ---------------------------------------------------------


@pytest.mark.parametrize("value", [0, 0.0, "0", "-0", "+0.00", ".0", "T / 2"])
def test_expr_accepts_literal_zero_or_expression(value):
    assert ExprHolder(value=value).value == value


@pytest.mark.parametrize("value", [5, 5.0, True, False, "x" * 257, float("nan"), float("inf")])
def test_expr_rejects_nonzero_literals_boolean_and_oversize(value):
    with pytest.raises(ValidationError):
        ExprHolder(value=value)


@pytest.mark.parametrize("value", [360, 360.0, "360", "360.0", "+360"])
def test_revolution_accepts_full_turn_only(value):
    assert RevolutionHolder(value=value).value == value


@pytest.mark.parametrize("value", [180, 359.999, True, float("nan")])
def test_revolution_rejects_partial_or_nonfinite(value):
    with pytest.raises(ValidationError):
        RevolutionHolder(value=value)


def test_partial_angle_string_stays_a_symbolic_expression():
    assert RevolutionHolder(value="180").value == "180"
    assert canonicalize_expr("180", revolution=True).kind == "expression"


@pytest.mark.parametrize("value", [-1_000_000, 1_000_000, 0, "-20", "L / 2"])
def test_coord_accepts_bounded_literal_or_symbol(value):
    assert CoordHolder(value=value).value == value


@pytest.mark.parametrize("value", [1_000_001, -1_000_001, True, float("nan"), "x" * 257])
def test_coord_rejects_out_of_range_and_nonfinite(value):
    with pytest.raises(ValidationError):
        CoordHolder(value=value)


# -- compact and object forms ------------------------------------------------


@pytest.mark.parametrize("compact,obj", [
    ([1, 2], {"x": 1, "y": 2}),
    (["line", 3, 4], {"type": "line", "end": {"x": 3, "y": 4}}),
    (["arc", 3, 4, "R"], {"type": "arc", "end": {"x": 3, "y": 4}, "radius": "R"}),
    (["arc", 3, 4, "R", True, False],
     {"type": "arc", "end": {"x": 3, "y": 4}, "radius": "R", "clockwise": True, "largeArc": False}),
])
def test_compact_point_and_segment_expand_to_the_object_form(compact, obj):
    if compact[0] in {"line", "arc"}:
        assert expand_compact_segment(compact) == obj
    else:
        assert expand_compact_point(compact) == obj


@pytest.mark.parametrize("compact,obj", [
    (["circle", 0, 0, "R"], {"type": "circle", "x": 0, "y": 0, "radius": "R"}),
    (["rectangle", 0, 0, "L", "W"],
     {"type": "rectangle", "x": 0, "y": 0, "width": "L", "height": "W"}),
    (["polygon", [[0, 0], [1, 0], [0, 1]]],
     {"type": "polygon", "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 0, "y": 1}]}),
    (["wire", [0, 0], [["line", 1, 0], ["line", 1, 1]]],
     {"type": "wire", "start": {"x": 0, "y": 0},
      "segments": [{"type": "line", "end": {"x": 1, "y": 0}},
                   {"type": "line", "end": {"x": 1, "y": 1}}]}),
])
def test_compact_profiles_expand_to_the_object_form(compact, obj):
    ProfileHolder(value=compact)
    assert expand_compact_profile(compact) == obj


def test_sketch_accepts_plane_name_and_plane_frame():
    SketchHolder(value={"plane": "xy", "profiles": [["circle", 0, 0, "R"]]})
    SketchHolder(value={"plane": {"normal": [0, 0, 1], "xDirection": [1, 0, 0]},
                        "offset": 0, "profiles": ["P1"]})
    with pytest.raises(ValidationError):
        SketchHolder(value={"plane": "view", "profiles": ["P1"]})
    with pytest.raises(ValidationError):
        SketchHolder(value={"plane": {"normal": [0, 0, 2], "xDirection": [1, 0, 0]},
                            "profiles": ["P1"]})


def test_edge_selectors_are_typed():
    EdgeHolder(value={"type": "linear", "direction": [1, 0, 0], "through": [0, 0, 0]})
    EdgeHolder(value={"type": "circular", "radius": "R", "centre": [0, 0, 0],
                      "axis": [0, 0, 1]})
    with pytest.raises(ValidationError):
        EdgeHolder(value={"type": "linear", "direction": [1_000_001, 0, 0], "through": [0, 0, 0]})


def test_parameter_model_enforces_name_value_and_unique_evidence():
    ParameterHolder(value={"name": "L", "value": 40, "evidence": ["p1:L"]})
    with pytest.raises(ValidationError):
        ParameterHolder(value={"name": "lower", "value": 40, "evidence": []})
    with pytest.raises(ValidationError):
        ParameterHolder(value={"name": "L", "value": True, "evidence": []})
    with pytest.raises(ValidationError):
        ParameterHolder(value={"name": "L", "value": 40, "evidence": ["p1", "p1"]})


# -- canonical views ---------------------------------------------------------


def test_literal_zero_and_full_turn_are_described_exactly():
    zero = canonicalize_expr("0")
    assert (zero.kind, zero.value, zero.formula) == ("literal_zero", 0.0, "0mm")
    turn = canonicalize_expr("360.0", revolution=True)
    assert (turn.kind, turn.value, turn.formula) == ("full_turn", 360.0, "360deg")
    assert canonicalize_expr(0).kind == "literal_zero"
    assert canonicalize_expr(360, revolution=True).kind == "full_turn"


def test_symbolic_expression_is_not_floated():
    result = canonicalize_expr("T / 2")
    assert result.kind == "expression"
    assert result.raw == "T / 2"
    assert result.value is None and result.formula is None and result.parameters == []
    # A literal-looking string that is not zero stays symbolic too.
    assert canonicalize_expr("0.1").kind == "expression"


def test_unreferenced_dimension_is_rejected_by_the_domain_not_coerced():
    # Structure accepts a string expression; the canonical view does not float it.
    assert ExprHolder(value="5").value == "5"
    assert canonicalize_expr("5").value is None
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "T", "value": 8, "evidence": []}],
           "features": [{"id": "base", "operation": "pad", "length": "5",
                         "sketch": {"plane": "xy",
                                    "profiles": [{"type": "rectangle", "width": "T", "height": "T"}]}}]}
    with pytest.raises(FeatureError) as caught:
        compile_features(req, None, require_evidence=False)
    assert caught.value.code in {"UNIT_MISMATCH", "EXPRESSION_INVALID"}


def test_full_revolution_keeps_the_existing_compiler_behaviour():
    req = {"schema": SCHEMA, "unit": "mm", "unitEvidence": [],
           "parameters": [{"name": "R", "value": 5, "evidence": []}],
           "features": [{"id": "body", "operation": "shaft", "angle": "360",
                         "sketch": {"plane": "xy",
                                    "profiles": [{"type": "circle", "x": "0", "y": "0", "radius": "R"}]}}]}
    plan = compile_features(req, None, require_evidence=False)
    extent = plan["features"][0]["extent"]
    assert extent["value"] == 360.0 and extent["formula"] == "360deg"
    assert canonicalize_expr("360", revolution=True).kind == "full_turn"


# -- module boundary ---------------------------------------------------------


def test_module_has_no_com_or_io_imports():
    forbidden = {"win32com", "pythoncom", "subprocess", "socket", "pathlib", "os", "open"}
    for path in PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
            else:
                continue
            assert not (names & forbidden), (path.name, names & forbidden)
