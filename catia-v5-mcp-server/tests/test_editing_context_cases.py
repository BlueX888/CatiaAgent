"""Offline tests for P2 context distillation and structural case retrieval."""

from __future__ import annotations

import json

from catia_mcp.editing.cases import compatible_cases, snapshot_conditions
from catia_mcp.editing.context import build_task_context


def _param(handle, value, unit="mm"):
    return {"handle": handle, "name": handle, "value": value, "unit": unit, "kind": "driver", "writable": True}


def _impact(*, x=0.0, y=0.0, z=0.0, radius_deltas=None, volume=0.0):
    return {
        "handle": "unused",
        "probeDelta": 1.0,
        "probeValue": 1.0,
        "writeStuck": True,
        "bboxDeltaX": x,
        "bboxDeltaY": y,
        "bboxDeltaZ": z,
        "bboxEdgeDeltas": {
            "x": {"min": -x / 2, "max": x / 2},
            "y": {"min": -y / 2, "max": y / 2},
            "z": {"min": 0.0, "max": z},
        },
        "holeCentersMoved": False,
        "holeRadiiChanged": bool(radius_deltas),
        "holeRadiusDelta": 0.5 if radius_deltas and any(abs(value) > 1e-9 for value in radius_deltas) else 0.0,
        "holeRadiusDeltas": radius_deltas or [0.0, 0.0, 0.0],
        "volumeDelta": volume,
        "geometryChanged": True,
        "edgeDistancesPreserved": True,
        "restored": True,
    }


def fixture_snapshot():
    holes = [
        {
            "sketch": "Sketch.A",
            "center": {"x": -65.0, "y": 35.0, "z": 0.0},
            "centerWorld": {"x": -65.0, "y": 35.0, "z": 0.0},
            "radius": 5.0,
            "role": "hole",
            "radiusDrivers": ["Part1\\D"],
        },
        {
            "sketch": "Sketch.A",
            "center": {"x": 65.0, "y": 35.0, "z": 0.0},
            "centerWorld": {"x": 65.0, "y": 35.0, "z": 0.0},
            "radius": 5.0,
            "role": "hole",
            "radiusDrivers": ["Part1\\D"],
        },
        {
            "sketch": "Sketch.B",
            "center": {"x": 0.0, "y": 0.0, "z": 0.0},
            "centerWorld": {"x": 0.0, "y": 0.0, "z": 0.0},
            "radius": 12.0,
            "role": "additive_feature",
            "radiusDrivers": ["Part1\\R"],
        },
        {
            "sketch": "Sketch.C",
            "center": {"x": 0.0, "y": 6.0, "z": 0.0},
            "centerWorld": {"x": 0.0, "y": 0.0, "z": 6.0},
            "radius": 5.0,
            "role": "hole",
            "radiusDrivers": ["Part1\\S"],
        },
    ]
    return {
        "schema": "catia-model-snapshot/1",
        "editSessionId": "sess-ctx",
        "modelRevision": 2,
        "source": {"sha256": "a" * 64},
        "workingCopy": {"sha256": "b" * 64},
        "capturedUtc": "2026-10-26T00:00:00.000+00:00",
        "parameters": [
            _param("Part1\\L", 160.0),
            _param("Part1\\W", 100.0),
            _param("Part1\\D", 10.0),
            _param("Part1\\R", 24.0),
            _param("Part1\\S", 10.0),
        ],
        "holes": holes,
        "impacts": {
            "Part1\\L": _impact(x=1.0),
            "Part1\\W": _impact(y=1.0),
            "Part1\\D": _impact(radius_deltas=[0.5, 0.5, 0.0, 0.0], volume=-395.0),
            "Part1\\R": _impact(radius_deltas=[0.0, 0.0, 0.5, 0.0], volume=307.0),
            "Part1\\S": _impact(radius_deltas=[0.0, 0.0, 0.0, 0.5], volume=-2638.0),
        },
        "structure": {
            "features": [
                {"name": "Pad.1", "kind": "pad", "materialRole": "additive", "sketch": "Sketch.A",
                 "kindEvidence": {"strength": "name_only"}},
                {"name": "Pad.2", "kind": "pad", "materialRole": "additive", "sketch": "Sketch.B",
                 "kindEvidence": {"strength": "name_only"}},
                {"name": "Pocket.1", "kind": "pocket", "materialRole": "subtractive", "sketch": "Sketch.C",
                 "kindEvidence": {"strength": "name_only"}},
            ],
            "sketches": [
                {"name": "Sketch.A", "frame": {"plane": "XY", "normalAxis": "z"}, "usedByFeatures": ["Pad.1"],
                 "circles": holes[:2], "constraints": [{"name": "d1"}, {"name": "d2"}]},
                {"name": "Sketch.B", "frame": {"plane": "XY", "normalAxis": "z"}, "usedByFeatures": ["Pad.2"],
                 "circles": [holes[2]], "constraints": [{"name": "d3"}]},
                {"name": "Sketch.C", "frame": {"plane": "YZ", "normalAxis": "x"}, "usedByFeatures": ["Pocket.1"],
                 "circles": [holes[3]], "constraints": [{"name": "d4"}]},
            ],
            "bodies": ["PartBody"],
        },
        "relationGraph": {
            "nodes": [],
            "edges": [
                {"from": "relation:f_L", "to": "param:Part1\\L", "type": "explicit_formula",
                 "evidence": {"expression": "L"}},
                {"from": "constraint:Sketch.A:d1", "to": "param:Part1\\L", "type": "explicit_geometric_constraint",
                 "evidence": {"value": 160.0}},
            ],
        },
        "preserves": {
            "hole_centers": {"supported": True, "worldSupported": True, "reason": "measured"},
            "hole_edge_distance": {"supported": True, "reason": "L/W keep margins"},
        },
        "capabilities": {"writableDrivers": ["Part1\\L", "Part1\\D"]},
        "limitations": ["feature kind is name evidence"],
    }


def test_context_for_a_hole_question_prefers_holes_and_keeps_exact_handles():
    context = build_task_context(fixture_snapshot(), attribute="diameter", text="把孔径改为 12")
    handles = {row["handle"] for row in context["candidates"]}
    assert handles == {"Part1\\D", "Part1\\S"}
    assert context["measuredHoles"] == 3
    assert context["circularProfiles"] == 4
    assert context["snapshotDigest"]["modelRevision"] == 2
    assert context["fullSnapshot"]["tool"] == "catia_inspect_edit"
    assert context["contextDigest"].startswith("sha256:")
    assert context["sizeBytes"] < 40_000


def test_context_for_a_boss_question_prefers_the_additive_profile():
    context = build_task_context(fixture_snapshot(), attribute="diameter", text="把凸台直径改为 30")
    assert [row["handle"] for row in context["candidates"]] == ["Part1\\R"]
    assert context["structure"]["sketches"][2]["plane"] == "YZ"


def test_context_exposes_ambiguity_and_relations():
    context = build_task_context(fixture_snapshot(), attribute="diameter", text="把孔径改为 12")
    assert "Part1\\D" in context["ambiguityGroups"]["hole:diameter"]
    assert context["relations"]["Part1\\D"] == []
    length_context = build_task_context(fixture_snapshot(), attribute="length")
    types = {edge["type"] for edge in length_context["relations"]["Part1\\L"]}
    assert "explicit_formula" in types


def test_case_retrieval_is_structural_not_textual(tmp_path):
    path = tmp_path / "cases.jsonl"
    rows = [
        {
            "schema": "catia-intent-case/1",
            "caseId": "case-boss",
            "appliesTo": {"attribute": "diameter", "operationType": "set", "hasHoles": True,
                          "requiresRoles": {"additive_feature": 1}, "planes": ["XY"]},
            "planTemplate": {"attribute": "diameter", "preserve": []},
            "verification": {"passed": True, "taskId": "F02"},
            "source": {"run": "runs/b2/F02"},
        },
        {
            "schema": "catia-intent-case/1",
            "caseId": "case-rotated-only",
            "appliesTo": {"attribute": "diameter", "planes": ["YZ"]},
            "planTemplate": {"attribute": "diameter"},
            "verification": {"passed": True},
        },
        {
            "schema": "catia-intent-case/1",
            "caseId": "case-other-op",
            "appliesTo": {"attribute": "length", "operationType": "increase"},
            "planTemplate": {"attribute": "length"},
            "verification": {"passed": True},
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    conditions = snapshot_conditions(fixture_snapshot())
    assert conditions["roles"]["hole"] == 3
    assert "YZ" in conditions["planes"]

    matches = compatible_cases(
        fixture_snapshot(), attribute="diameter", operation_type="set", path=path
    )
    ids = [row["caseId"] for row in matches]
    # The rotated-only case is structurally compatible (YZ exists) and operation
    # compatible; the boss case requires an additive role (present); the length
    # case is excluded by operation/attribute compatibility.
    assert ids == ["case-boss", "case-rotated-only"]
    assert all("handle" not in json.dumps(row["planTemplate"]) for row in matches)


def test_case_with_a_missing_required_role_is_not_returned(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps(
            {
                "schema": "catia-intent-case/1",
                "caseId": "needs-two-bosses",
                "appliesTo": {"attribute": "diameter", "requiresRoles": {"additive_feature": 2}},
                "planTemplate": {"attribute": "diameter"},
                "verification": {"passed": True},
            }
        ),
        encoding="utf-8",
    )
    assert compatible_cases(fixture_snapshot(), attribute="diameter", path=path) == []
