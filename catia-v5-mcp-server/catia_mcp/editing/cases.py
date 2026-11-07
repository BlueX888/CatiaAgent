"""Case retrieval by structural and operation compatibility (P2).

A case is never replayed. It stores the *shape* of a verified edit (attribute,
operation, scope, preserve types, acceptance types) plus the structural
conditions it was verified under, and the independent verification result.
Handles, revisions, operation ids and absolute target values are deliberately
not stored: they belong to the snapshot that produced them and are invalid in
a new session.

Retrieval is compatibility-first, not text similarity: a matching sentence in
an incompatible part is not evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_CASES = Path(__file__).with_name("cases.jsonl")


def load_cases(path: str | Path | None = None) -> list[dict[str, Any]]:
    target = Path(path) if path else Path(os.environ.get("CATIA_MCP_CASES") or DEFAULT_CASES)
    if not target.exists():
        return []
    rows = []
    for line in target.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("schema") == "catia-intent-case/1":
            rows.append(value)
    return rows


def snapshot_conditions(snapshot: dict[str, Any]) -> dict[str, Any]:
    holes = snapshot.get("holes") or []
    roles: dict[str, int] = {}
    for hole in holes:
        role = str(hole.get("role") or "unclassified")
        roles[role] = roles.get(role, 0) + 1
    structure = snapshot.get("structure") or {}
    features = structure.get("features") or []
    sketches = structure.get("sketches") or []
    return {
        "hasHoles": roles.get("hole", 0) > 0,
        "holeCount": roles.get("hole", 0),
        "circleCount": len(holes),
        "roles": roles,
        "featureKinds": sorted({str(feature.get("kind")) for feature in features}),
        "planes": sorted({str((sketch.get("frame") or {}).get("plane")) for sketch in sketches}),
        "hasEditableDriver": bool(snapshot.get("capabilities", {}).get("writableDrivers")),
    }


def compatible_cases(
    snapshot: dict[str, Any],
    *,
    attribute: str | None = None,
    operation_type: str | None = None,
    limit: int = 5,
    path: str | Path | None = None,
) -> list[dict[str, Any]]:
    conditions = snapshot_conditions(snapshot)
    rows = []
    for case in load_cases(path):
        applies = case.get("appliesTo") or {}
        if applies.get("attribute") and attribute and applies["attribute"] != attribute:
            continue
        if applies.get("operationType") and operation_type and applies["operationType"] != operation_type:
            continue
        if applies.get("hasHoles") is not None and bool(applies["hasHoles"]) != conditions["hasHoles"]:
            continue
        if applies.get("minHoleCount") and conditions["holeCount"] < int(applies["minHoleCount"]):
            continue
        required_roles = applies.get("requiresRoles") or {}
        if any(conditions["roles"].get(role, 0) < int(count) for role, count in required_roles.items()):
            continue
        required_kinds = set(applies.get("featureKinds") or [])
        if required_kinds and not required_kinds.issubset(set(conditions["featureKinds"])):
            continue
        required_planes = set(applies.get("planes") or [])
        if required_planes and not required_planes.issubset(set(conditions["planes"])):
            continue
        rows.append(
            {
                "caseId": case.get("caseId"),
                "appliesTo": applies,
                "planTemplate": case.get("planTemplate"),
                "verification": case.get("verification"),
                "source": case.get("source"),
                "note": case.get("note", "template only; recompile against the current snapshot"),
            }
        )
        if len(rows) >= limit:
            break
    return rows
