"""Offline tests for requirement validation and plan compilation (T2/T3).

No CATIA and no PDF: the evidence manifest is built by hand so each rule can be
exercised in isolation.
"""

from __future__ import annotations

import copy

import pytest

from catia_mcp.drawing.plan import PlanError, compile_build_plan
from catia_mcp.drawing.requirements import validate_requirements, _without_multiplicity

MANIFEST = {
    "schema": "catia-pdf-evidence/1",
    "source": {"path": "drawing.pdf", "sha256": "0" * 64, "bytes": 100, "pageCount": 1},
    "pages": [
        {
            "page": 1,
            "spans": [
                {"id": "p1:s1", "text": "UNITS: MILLIMETRES", "bboxMm": [10, 10, 50, 14]},
                {"id": "p1:s2", "text": "120", "bboxMm": [10, 20, 20, 24]},
                {"id": "p1:s3", "text": "80", "bboxMm": [30, 20, 40, 24]},
                {"id": "p1:s4", "text": "10", "bboxMm": [50, 20, 60, 24]},
                {"id": "p1:s5", "text": "4x Ø10 THRU", "bboxMm": [10, 30, 40, 34]},
                {"id": "p1:s6", "text": "20", "bboxMm": [10, 40, 20, 44]},
                {"id": "p1:s7", "text": "20", "bboxMm": [30, 40, 40, 44]},
            ],
        }
    ],
}

BASE = {
    "schema": "catia-build-requirements/1",
    "unit": "mm",
    "partName": "PLATE",
    "drawing": {"path": "drawing.pdf", "page": 1},
    "body": {"type": "flat_plate", "length": 120, "width": 80, "thickness": 10},
    "holes": {
        "count": 4,
        "diameter": 10,
        "through": True,
        "pattern": {"type": "symmetric", "edgeX": 20, "edgeY": 20},
    },
    "evidence": {
        "unit": ["p1:s1"],
        "length": ["p1:s2"],
        "width": ["p1:s3"],
        "thickness": ["p1:s4"],
        "holeDiameter": ["p1:s5"],
        "holeCount": ["p1:s5"],
        "edgeX": ["p1:s6"],
        "edgeY": ["p1:s7"],
    },
}


def req(**body_changes):
    document = copy.deepcopy(BASE)
    document.update(body_changes)
    return document


def test_hole_count_prefix_does_not_hide_chamfer_size():
    assert _without_multiplicity("4x Ø10 THRU") == "Ø10 THRU"
    assert _without_multiplicity("(8x) ∅6") == "∅6"
    assert _without_multiplicity("2x45°") == "2x45°"


def test_ready_requirements_normalize_to_millimetres():
    result = validate_requirements(BASE, MANIFEST)
    assert result["decision"] == "ready", result["problems"]
    normalized = result["normalized"]
    assert normalized["unit"] == "mm"
    assert normalized["body"] == {"type": "flat_plate", "length": 120.0, "width": 80.0,
                                  "thickness": 10.0}
    assert normalized["holes"]["diameter"] == 10.0
    assert normalized["holes"]["centres"] == [
        [-40.0, -20.0], [40.0, -20.0], [40.0, 20.0], [-40.0, 20.0]
    ]


def test_flat_plate_can_cite_declared_rounded_context_dimension():
    document, manifest = copy.deepcopy(BASE), copy.deepcopy(MANIFEST)
    manifest["contextEvidence"] = [{
        "id": "ctx:dimensions:drawing_test:1", "text": "120",
        "source": "drawing_context", "sourceKind": "dimensions", "assetId": "drawing_test",
        "entityId": "drawing_test:1", "sourceSha256": "a" * 64,
        "rawMeasurement": {"value": 120.3, "unit": "drawing_unit",
            "basis": "declared_drawing_unit", "dimensionType": 0,
            "textOverride": "", "lengthScaleFactor": 1},
    }]
    document["body"]["length"] = 120.3
    document["evidence"]["length"] = ["ctx:dimensions:drawing_test:1"]
    assert validate_requirements(document, manifest)["decision"] == "ready"
    manifest["contextEvidence"][0]["rawMeasurement"]["basis"] = None
    assert validate_requirements(document, manifest)["decision"] == "invalid"
@pytest.mark.parametrize("text,unit,accepted", [
    ("ALL DIMENSIONS ARE IN MILLIMITERS", "mm", True),
    ("DIMENSIONSAREINMILLIMITERS", "mm", True),
    ("NODIMENSIONSAREINMILLIMITERS", "mm", False),
    ("ALL DIMENSIONS ARE IN MILLIMITER", "inch", False),
    ("ALL DIMENSIONS ARE IN MILLIMETERS", "inch", False),
    ("ALL DIMENSIONS ARE IN", "inch", False),
    ("UNITS: IN", "inch", True),
    ("UNITS mm AND inches", "mm", False),
])
def test_unit_evidence_uses_the_declared_unit(text, unit, accepted):
    document, manifest = req(unit=unit), copy.deepcopy(MANIFEST)
    manifest["pages"][0]["spans"][0]["text"] = text
    result = validate_requirements(document, manifest)
    check = next(c for c in result["evidenceChecks"] if c["field"] == "unit")
    assert check["passed"] is accepted, result


def test_flat_plate_requires_unit_citation_when_evidence_is_required():
    document = req()
    del document["evidence"]["unit"]
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] != "ready"
    assert any(p["code"] == "EVIDENCE_MISSING" for p in result["problems"])


def test_flat_plate_context_citation_requires_separate_provenance():
    document, manifest = req(), copy.deepcopy(MANIFEST)
    document["evidence"]["unit"] = ["ctx:texts:drawing_a:u"]
    manifest["contextEvidence"] = [{"id": "ctx:texts:drawing_a:u", "text": "UNITS mm",
        "source": "drawing_context", "sourceKind": "texts", "sourceSha256": "a" * 64,
        "assetId": "drawing_a", "entityId": "drawing_a:u"}]
    assert validate_requirements(document, manifest)["decision"] == "ready"
    del manifest["contextEvidence"][0]["sourceSha256"]
    assert validate_requirements(document, manifest)["decision"] != "ready"


def test_inch_drawing_is_converted_once():
    document = req(unit="inch")
    document["body"] = {"type": "flat_plate", "length": 5, "width": 3, "thickness": 0.5}
    document["holes"] = {
        "count": 4, "diameter": 0.5, "through": True,
        "pattern": {"type": "symmetric", "edgeX": 1, "edgeY": 1},
    }
    document["evidence"] = {
        "unit": ["p1:s1"], "length": ["p1:s2"], "width": ["p1:s3"], "thickness": ["p1:s4"],
        "holeDiameter": ["p1:s5"], "holeCount": ["p1:s5"],
        "edgeX": ["p1:s6"], "edgeY": ["p1:s7"],
    }
    data = copy.deepcopy(MANIFEST)
    spans = data["pages"][0]["spans"]
    spans[0]["text"] = "UNITS: INCHES"
    spans[1]["text"] = "5"
    spans[2]["text"] = "3"
    spans[3]["text"] = "0.5"
    spans[4]["text"] = "4x Ø0.5 THRU"
    spans[5]["text"] = "1"
    spans[6]["text"] = "1"
    result = validate_requirements(document, data)
    assert result["decision"] == "ready", result["problems"]
    assert result["normalized"]["body"]["length"] == pytest.approx(127.0)
    assert result["normalized"]["body"]["thickness"] == pytest.approx(12.7)
    assert result["normalized"]["holes"]["diameter"] == pytest.approx(12.7)


def test_missing_unit_asks_one_question():
    document = req()
    document.pop("unit")
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "needs_clarification"
    assert [q["topic"] for q in result["questions"]] == ["unit"]


def test_unit_from_user_answer_is_accepted_and_recorded():
    document = req()
    document.pop("unit")
    document["clarifications"] = [
        {"topic": "unit", "question": "mm or inch?", "answer": "mm", "source": "user"}
    ]
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "ready", result["problems"]


def test_missing_thickness_asks_instead_of_guessing():
    document = req()
    document["body"] = {"type": "flat_plate", "length": 120, "width": 80}
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "needs_clarification"
    assert "thickness" in [q["topic"] for q in result["questions"]]


def test_cited_span_must_contain_the_number():
    document = req()
    document["evidence"]["length"] = ["p1:s3"]  # 80, not 120
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "EVIDENCE_VALUE_MISMATCH" for p in result["problems"])


def test_unknown_span_id_is_rejected():
    document = req()
    document["evidence"]["width"] = ["p9:s99"]
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "EVIDENCE_REF_UNKNOWN" for p in result["problems"])


def test_missing_evidence_asks_which_dimension_to_use():
    document = req()
    document["evidence"].pop("thickness")
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "needs_clarification"
    assert any(p["code"] == "EVIDENCE_MISSING" for p in result["problems"])


def test_hole_outside_body_is_invalid():
    document = req()
    document["holes"]["pattern"] = {"type": "explicit", "centres": [[70, 0], [-40, -20],
                                                                    [40, -20], [40, 20]]}
    document["evidence"].pop("edgeX")
    document["evidence"].pop("edgeY")
    document["evidence"]["holePattern"] = ["p1:s6", "p1:s7"]
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "HOLE_OUTSIDE_BODY" for p in result["problems"])


def test_overlapping_holes_are_invalid():
    document = req()
    document["holes"]["pattern"] = {"type": "explicit", "centres": [[0, 0], [8, 0], [30, 0], [-30, 0]]}
    document["evidence"].pop("edgeX")
    document["evidence"].pop("edgeY")
    document["evidence"]["holePattern"] = ["p1:s6", "p1:s7"]
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "HOLES_OVERLAP" for p in result["problems"])


def test_blind_holes_are_unsupported():
    document = req()
    document["holes"]["through"] = False
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "unsupported"


def test_non_plate_body_is_unsupported():
    document = req(body={"type": "bent_bracket", "length": 120, "width": 80, "thickness": 3})
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "unsupported"


def test_unknown_unit_is_unsupported():
    document = req(unit="furlong")
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "unsupported"


def test_unresolved_conflict_asks_and_stops():
    document = req()
    document["conflicts"] = [
        {"field": "length", "message": "overall 120 conflicts with chain 80 + 50",
         "question": "Is the plate 120 or 130 long?"}
    ]
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "needs_clarification"
    assert "length" in [q["topic"] for q in result["questions"]]


def test_explicit_centres_are_supported():
    document = req()
    document["holes"]["pattern"] = {
        "type": "explicit",
        "centres": [[25, 12], [-30, -18], [0, 0], [10, 25]],
    }
    document["evidence"].pop("edgeX")
    document["evidence"].pop("edgeY")
    document["evidence"]["holePattern"] = ["p1:s6", "p1:s7"]
    result = validate_requirements(document, MANIFEST)
    assert result["decision"] == "ready", result["problems"]
    assert result["normalized"]["holes"]["centres"][0] == [25.0, 12.0]


# ---------------------------------------------------------------------------
# plan compilation
# ---------------------------------------------------------------------------

def _normalized():
    return validate_requirements(BASE, MANIFEST)["normalized"]


def test_plan_is_closed_and_carries_parameters_and_expectations():
    plan = compile_build_plan(_normalized())
    assert plan["schema"] == "catia-build-plan/1"
    assert plan["unit"] == "mm"
    ops = [step["op"] for step in plan["steps"]]
    assert ops[:4] == ["new_part", "create_parameters", "create_sketch", "rectangle"]
    assert ops[-2:] == ["pocket", "verify_geometry"]
    names = [p["name"] for p in plan["parameters"]]
    assert names == ["L", "W", "T", "D", "EX", "EY"]
    assert plan["expected"]["parameters"]["L"] == 120.0
    assert plan["expected"]["volumeMm3"] == pytest.approx(
        (120 * 80 - 4 * 3.141592653589793 * 25) * 10, abs=1e-5
    )
    assert plan["expected"]["featureCount"] == 2
    assert plan["planId"].startswith("sha256:")


def test_plan_id_is_deterministic():
    first = compile_build_plan(_normalized())
    second = compile_build_plan(_normalized())
    assert first["planId"] == second["planId"]


def test_plan_uses_construction_reference_lines_for_holes():
    plan = compile_build_plan(_normalized())
    construction = [s for s in plan["steps"] if s["op"] == "construction_rectangle"]
    assert len(construction) == 1
    circles = [s for s in plan["steps"] if s["op"] == "circle"]
    assert [c["geometry_index"] for c in circles] == [6, 7, 8, 9]
    bindings = [s["bind"] for s in plan["steps"] if s["op"] == "constraint" and s.get("bind")]
    assert bindings.count("D / 2") == 4
    assert bindings.count("EX - D / 2") == 4
    assert bindings.count("EY - D / 2") == 4
    # Centres are sorted canonically: (-x,-y), (-x,+y), (+x,-y), (+x,+y).
    # The reference line must follow the hole's side, not its list position.
    distance_steps = [
        step for step in plan["steps"]
        if step["op"] == "constraint" and step["type"] == "distance"
        and str(step.get("role", "")).startswith("hole_")
        and str(step.get("role", "")).endswith(("_x", "_y"))
    ]
    sides = [(step["geometry_index_1"], step["geometry_index_2"]) for step in distance_steps]
    assert sides[:8] == [
        (6, 5), (6, 2),   # hole 1: left, bottom
        (7, 5), (7, 4),   # hole 2: left, top
        (8, 3), (8, 2),   # hole 3: right, bottom
        (9, 3), (9, 4),   # hole 4: right, top
    ]


def test_explicit_plan_uses_per_hole_parameters():
    document = req()
    document["holes"]["pattern"] = {"type": "explicit", "centres": [[25, 12], [-30, -18],
                                                                    [0, 0], [10, 25]]}
    document["evidence"].pop("edgeX")
    document["evidence"].pop("edgeY")
    document["evidence"]["holePattern"] = ["p1:s6", "p1:s7"]
    normalized = validate_requirements(document, MANIFEST)["normalized"]
    plan = compile_build_plan(normalized)
    parameters = [p["name"] for p in plan["parameters"]]
    assert parameters[:4] == ["L", "W", "T", "D"]
    assert "LX1" in parameters and "LY4" in parameters
    binds = [s["bind"] for s in plan["steps"] if s.get("bind")]
    assert "LX1 - D / 2" in binds


def test_plan_refuses_bad_normalized_input():
    normalized = _normalized()
    normalized["holes"]["through"] = False
    with pytest.raises(PlanError):
        compile_build_plan(normalized)
    normalized = _normalized()
    normalized["holes"]["centres"] = normalized["holes"]["centres"][:2]
    with pytest.raises(PlanError):
        compile_build_plan(normalized)


# --- annular_ring -----------------------------------------------------------

RING_MANIFEST = {
    "schema": "catia-pdf-evidence/1",
    "source": {"path": "ring.pdf", "sha256": "1" * 64, "bytes": 100, "pageCount": 2},
    "pages": [
        {
            "page": 1,
            "spans": [
                {"id": "p1:o11", "text": "063 ±0.5AVG (O.D.)", "source": "ocr"},
                {"id": "p1:o15", "text": "032 ±0.3 AVG (I.D.)", "source": "ocr"},
                {"id": "p1:o28", "text": "5.UNITS:MILLIMETERS", "source": "ocr"},
                {"id": "p1:o29", "text": "2X R1.5 ±0.04", "source": "ocr"},
                {"id": "p2:o5", "text": "1.93 ±0.08", "source": "ocr"},
            ],
        }
    ],
}

RING_REQ = {
    "schema": "catia-build-requirements/1",
    "unit": "mm",
    "partName": "RING",
    "body": {
        "type": "annular_ring",
        "outerDiameter": 63,
        "innerDiameter": 32,
        "thickness": 1.93,
        "edge": {"style": "rolled", "radius": 1.5},
    },
    "evidence": {
        "outerDiameter": ["p1:o11"],
        "innerDiameter": ["p1:o15"],
        "thickness": ["p2:o5"],
        "edgeRadius": ["p1:o29"],
        "unit": ["p1:o28"],
    },
}


def test_ring_requirements_are_ready_and_normalized():
    result = validate_requirements(RING_REQ, RING_MANIFEST)
    assert result["decision"] == "ready", result["problems"]
    body = result["normalized"]["body"]
    assert body["type"] == "annular_ring"
    assert body["outerDiameter"] == 63.0
    assert body["innerDiameter"] == 32.0
    assert body["thickness"] == 1.93
    assert body["edge"] == {"style": "rolled", "radius": 1.5}


def test_ring_can_cite_declared_rounded_context_dimension():
    document, manifest = copy.deepcopy(RING_REQ), copy.deepcopy(RING_MANIFEST)
    manifest["contextEvidence"] = [{
        "id": "ctx:dimensions:drawing_test:2", "text": "Ø63",
        "source": "drawing_context", "sourceKind": "dimensions", "assetId": "drawing_test",
        "entityId": "drawing_test:2", "sourceSha256": "a" * 64,
        "rawMeasurement": {"value": 63.3, "unit": "drawing_unit",
            "basis": "declared_drawing_unit", "dimensionType": 3,
            "textOverride": "", "lengthScaleFactor": 1},
    }]
    document["body"]["outerDiameter"] = 63.3
    document["evidence"]["outerDiameter"] = ["ctx:dimensions:drawing_test:2"]
    assert validate_requirements(document, manifest)["decision"] == "ready"
    manifest["contextEvidence"][0]["rawMeasurement"]["dimensionType"] = 4
    assert validate_requirements(document, manifest)["decision"] == "invalid"


def test_ring_plan_uses_pad_and_one_shaft_and_expected_volume():
    result = validate_requirements(RING_REQ, RING_MANIFEST)
    plan = compile_build_plan(result["normalized"])
    ops = [step["op"] for step in plan["steps"]]
    assert ops.count("shaft") == 1
    assert ops.count("pad") == 1
    assert plan["expected"]["kind"] == "annular_ring"
    assert plan["expected"]["volumeMm3"] == pytest.approx(5139.237233, abs=1e-6)
    assert plan["expected"]["bbox"]["max"]["z"] == 3.0
    assert plan["expected"]["bbox"]["max"]["x"] == 31.5


def test_ring_rejects_a_chamfer_edge_as_unsupported():
    document = copy.deepcopy(RING_REQ)
    document["body"]["edge"]["style"] = "chamfer"
    result = validate_requirements(document, RING_MANIFEST)
    assert result["decision"] == "unsupported"
    assert any(p["code"] == "EDGE_STYLE_UNSUPPORTED" for p in result["problems"])


def test_ring_rejects_an_edge_radius_that_swallows_the_web():
    document = copy.deepcopy(RING_REQ)
    document["body"]["edge"]["radius"] = 0.9  # 2*0.9 = 1.8 <= 1.93
    result = validate_requirements(document, None, require_evidence=False)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "EDGE_RADIUS_TOO_SMALL" for p in result["problems"])


def test_ring_rejects_radius_larger_than_the_inner_hole():
    document = copy.deepcopy(RING_REQ)
    document["body"]["edge"]["radius"] = 16.5
    result = validate_requirements(document, None, require_evidence=False)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "EDGE_RADIUS_TOO_LARGE" for p in result["problems"])


def test_ring_asks_for_a_missing_thickness():
    document = copy.deepcopy(RING_REQ)
    del document["body"]["thickness"]
    del document["evidence"]["thickness"]
    result = validate_requirements(document, RING_MANIFEST)
    assert result["decision"] == "needs_clarification"
    assert any(q["topic"] == "thickness" for q in result["questions"])


def test_ring_rejects_a_dimension_cited_from_the_wrong_span():
    document = copy.deepcopy(RING_REQ)
    document["evidence"]["innerDiameter"] = ["p1:o11"]  # span says 63, not 32
    result = validate_requirements(document, RING_MANIFEST)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "EVIDENCE_VALUE_MISMATCH" for p in result["problems"])


# --- extruded_profile -------------------------------------------------------

PROFILE_MANIFEST = {
    "schema": "catia-pdf-evidence/1",
    "source": {"path": "l-plate.pdf", "sha256": "2" * 64, "bytes": 100, "pageCount": 1},
    "pages": [
        {
            "page": 1,
            "spans": [
                {"id": "p1:s1", "text": "UNITS: MILLIMETRES"},
                {"id": "p1:s2", "text": "80"},
                {"id": "p1:s3", "text": "60"},
                {"id": "p1:s4", "text": "30"},
                {"id": "p1:s5", "text": "40"},
                {"id": "p1:s6", "text": "8"},
                {"id": "p1:s7", "text": "2x Ø10 THRU"},
                {"id": "p1:s8", "text": "20"},
                {"id": "p1:s9", "text": "15"},
                {"id": "p1:s10", "text": "60"},
                {"id": "p1:s11", "text": "15"},
            ],
        }
    ],
}

L_PROFILE = [
    {"type": "line", "x1": 0, "y1": 0, "x2": 80, "y2": 0},
    {"type": "line", "x1": 80, "y1": 0, "x2": 80, "y2": 30},
    {"type": "line", "x1": 80, "y1": 30, "x2": 40, "y2": 30},
    {"type": "line", "x1": 40, "y1": 30, "x2": 40, "y2": 60},
    {"type": "line", "x1": 40, "y1": 60, "x2": 0, "y2": 60},
    {"type": "line", "x1": 0, "y1": 60, "x2": 0, "y2": 0},
]

PROFILE_REQ = {
    "schema": "catia-build-requirements/1",
    "unit": "mm",
    "partName": "L-PLATE",
    "body": {"type": "extruded_profile", "thickness": 8, "profile": copy.deepcopy(L_PROFILE)},
    "holes": {"count": 2, "diameter": 10, "through": True,
              "pattern": {"type": "explicit", "centres": [[20, 15], [60, 15]]}},
    "evidence": {
        "unit": ["p1:s1"], "profile": ["p1:s2", "p1:s3", "p1:s4", "p1:s5"],
        "thickness": ["p1:s6"], "holeDiameter": ["p1:s7"], "holeCount": ["p1:s7"],
        "holePattern": ["p1:s8", "p1:s9", "p1:s10", "p1:s11"],
    },
}


def test_profile_can_cite_declared_rounded_context_dimension():
    document, manifest = copy.deepcopy(PROFILE_REQ), copy.deepcopy(PROFILE_MANIFEST)
    manifest["contextEvidence"] = [{
        "id": "ctx:dimensions:drawing_test:3", "text": "8",
        "source": "drawing_context", "sourceKind": "dimensions", "assetId": "drawing_test",
        "entityId": "drawing_test:3", "sourceSha256": "a" * 64,
        "rawMeasurement": {"value": 8.3, "unit": "drawing_unit",
            "basis": "declared_drawing_unit", "dimensionType": 0,
            "textOverride": "", "lengthScaleFactor": 1},
    }]
    document["body"]["thickness"] = 8.3
    document["evidence"]["thickness"] = ["ctx:dimensions:drawing_test:3"]
    assert validate_requirements(document, manifest)["decision"] == "ready"
    manifest["contextEvidence"][0]["rawMeasurement"]["lengthScaleFactor"] = 0
    assert validate_requirements(document, manifest)["decision"] == "invalid"


@pytest.mark.parametrize("document", [BASE, RING_REQ, PROFILE_REQ])
def test_required_evidence_manifest_cannot_be_omitted(document):
    result = validate_requirements(copy.deepcopy(document), None, require_evidence=True)
    assert result["decision"] != "ready"
    assert any(p["code"] == "EVIDENCE_NOT_SUPPLIED" for p in result["problems"])
    assert validate_requirements(copy.deepcopy(document), None, require_evidence=False)["decision"] == "ready"


@pytest.mark.parametrize("document,manifest,unit_span", [
    (RING_REQ, RING_MANIFEST, "p1:o28"),
    (PROFILE_REQ, PROFILE_MANIFEST, "p1:s1"),
])
@pytest.mark.parametrize("text,unit,accepted", [
    ("ALL DIMENSIONS ARE IN MILLIMITERS", "mm", True),
    ("ALL DIMENSIONS ARE IN MILLIMETERS", "inch", False),
    ("UNITS: IN", "inch", True),
    ("UNITS mm AND inches", "mm", False),
])
def test_ring_and_profile_apply_same_unit_evidence_rules(document, manifest, unit_span, text, unit, accepted):
    document, manifest = copy.deepcopy(document), copy.deepcopy(manifest)
    document["unit"] = unit
    next(s for s in manifest["pages"][0]["spans"] if s["id"] == unit_span)["text"] = text
    result = validate_requirements(document, manifest)
    mismatch = any(p["code"] == "EVIDENCE_VALUE_MISMATCH" and "unit" in p["message"].lower()
                   for p in result["problems"])
    assert mismatch is not accepted, result


def test_profile_is_ready_and_plan_volume_is_exact():
    result = validate_requirements(PROFILE_REQ, PROFILE_MANIFEST)
    assert result["decision"] == "ready", result["problems"]
    plan = compile_build_plan(result["normalized"])
    assert plan["expected"]["profileAreaMm2"] == pytest.approx(3600.0)
    assert plan["expected"]["volumeMm3"] == pytest.approx(27543.362939, abs=1e-6)
    ops = [step["op"] for step in plan["steps"]]
    assert ops.count("line") == 6
    assert "pad" in ops and "pocket" in ops


def test_profile_with_an_arc_uses_green_theorem_area():
    profile = [
        {"type": "line", "x1": -10, "y1": 0, "x2": 10, "y2": 0},
        {"type": "arc", "cx": 0, "cy": 0, "radius": 10, "startAngle": 0, "endAngle": 180},
    ]
    document = copy.deepcopy(PROFILE_REQ)
    document["body"]["profile"] = profile
    del document["holes"]
    document["evidence"] = {"unit": ["p1:s1"], "profile": ["p1:s2"], "thickness": ["p1:s6"]}
    result = validate_requirements(document, PROFILE_MANIFEST, require_evidence=False)
    assert result["decision"] == "ready", result["problems"]
    plan = compile_build_plan(result["normalized"])
    assert plan["expected"]["profileAreaMm2"] == pytest.approx(157.079633, abs=1e-5)
    assert plan["expected"]["volumeMm3"] == pytest.approx(1256.637061, abs=1e-5)


def test_profile_must_be_closed():
    document = copy.deepcopy(PROFILE_REQ)
    document["body"]["profile"][-1]["x2"] = 5
    result = validate_requirements(document, PROFILE_MANIFEST)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "PROFILE_NOT_CLOSED" for p in result["problems"])


def test_profile_hole_must_sit_inside_the_outline():
    document = copy.deepcopy(PROFILE_REQ)
    document["holes"]["pattern"]["centres"] = [[70, 50]]  # in the L cut-out
    document["holes"]["count"] = 1
    result = validate_requirements(document, None, require_evidence=False)
    assert result["decision"] == "invalid"
    assert any(p["code"] == "HOLE_OUTSIDE_BODY" for p in result["problems"])


def test_ring_and_profile_results_always_carry_evidence_checks():
    """The MCP tool reads this key unconditionally; every branch must have it."""
    ring = validate_requirements(RING_REQ, RING_MANIFEST)
    profile = validate_requirements(PROFILE_REQ, PROFILE_MANIFEST)
    assert "evidenceChecks" in ring
    assert "evidenceChecks" in profile
