"""Task-scoped context distillation (P2, guide section 5.4).

The point is not to summarise the snapshot in prose. Exact handles, values,
units, revision and evidence stay verbatim; the module only *selects* what the
current task needs and keeps a pointer to the full snapshot. A compressed view
that cannot be expanded back would hide a preserve condition, which is exactly
what the guide forbids.

Everything here is deterministic. It never talks to CATIA and never writes.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from catia_mcp.editing.intent import _confirmed_holes, _driver_candidates

_LINEAR_ATTRIBUTES = {"length", "width", "height", "thickness", "depth", "distance", "size"}
_HOLE_ATTRIBUTES = {"diameter", "radius"}


def _text_circle_kind(text: str | None) -> str | None:
    lowered = str(text or "").lower()
    if any(word in lowered for word in ("凸台", "圆柱", "凸起", "boss", "cylinder")):
        return "additive_feature"
    if any(word in lowered for word in ("孔", "hole")):
        return "hole"
    return None


def _matches(candidate: dict[str, Any], attribute: str | None) -> bool:
    if not attribute:
        return True
    attribute = attribute.strip().lower()
    if attribute in _LINEAR_ATTRIBUTES:
        return candidate.get("axis") in ("x", "y", "z")
    if attribute in _HOLE_ATTRIBUTES:
        return candidate.get("holeQuantity") in ("diameter", "radius")
    if attribute == "angle":
        return candidate.get("unit") == "deg"
    return True


def _relation_rows(snapshot: dict[str, Any], handle: str, limit: int = 12) -> list[dict[str, Any]]:
    graph = snapshot.get("relationGraph") or {}
    node = f"param:{handle}"
    rows = []
    for edge in graph.get("edges") or []:
        if edge.get("to") != node and edge.get("from") != node:
            continue
        rows.append(
            {
                "type": edge.get("type"),
                "from": edge.get("from"),
                "to": edge.get("to"),
                "evidence": edge.get("evidence"),
            }
        )
        if len(rows) >= limit:
            break
    return rows


def build_task_context(
    snapshot: dict[str, Any],
    *,
    attribute: str | None = None,
    text: str | None = None,
    operation_type: str | None = None,
    cases: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    candidates = [row for row in _driver_candidates(snapshot) if _matches(row, attribute)]
    circle_kind = _text_circle_kind(text)
    if circle_kind and attribute in _HOLE_ATTRIBUTES:
        preferred = [row for row in candidates if circle_kind in (row.get("circleRoles") or [])]
        if preferred:
            candidates = preferred
    candidates = sorted(candidates, key=lambda row: (row.get("axis") or "", row.get("handle") or ""))

    ambiguity: dict[str, list[str]] = {}
    for row in candidates:
        key = row.get("axis") if row.get("axis") else f"hole:{row.get('holeQuantity')}"
        ambiguity.setdefault(str(key), []).append(str(row.get("handle")))

    structure = snapshot.get("structure") or {}
    compact_structure = {
        "features": [
            {
                "name": feature.get("name"),
                "kind": feature.get("kind"),
                "materialRole": feature.get("materialRole"),
                "kindEvidence": feature.get("kindEvidence"),
                "sketch": feature.get("sketch"),
            }
            for feature in structure.get("features") or []
        ],
        "sketches": [
            {
                "name": sketch.get("name"),
                "plane": (sketch.get("frame") or {}).get("plane"),
                "normalAxis": (sketch.get("frame") or {}).get("normalAxis"),
                "usedByFeatures": sketch.get("usedByFeatures") or [],
                "circleRoles": [circle.get("role") for circle in sketch.get("circles") or []],
                "constraintCount": len(sketch.get("constraints") or []),
            }
            for sketch in structure.get("sketches") or []
        ],
    }

    relations = {
        row["handle"]: _relation_rows(snapshot, str(row["handle"])) for row in candidates
    }

    payload = {
        "schema": "catia-task-context/1",
        "snapshotDigest": {
            "editSessionId": snapshot.get("editSessionId"),
            "modelRevision": snapshot.get("modelRevision"),
            "capturedUtc": snapshot.get("capturedUtc"),
            "sourceSha256": (snapshot.get("source") or {}).get("sha256"),
            "workingCopySha256": (snapshot.get("workingCopy") or {}).get("sha256"),
        },
        "task": {
            "attribute": attribute,
            "operationType": operation_type,
            "textKind": circle_kind,
        },
        "candidates": candidates,
        "ambiguityGroups": {key: value for key, value in ambiguity.items() if len(value) > 1},
        "structure": compact_structure,
        "preserves": snapshot.get("preserves"),
        "relations": relations,
        "limitations": snapshot.get("limitations") or [],
        "measuredHoles": len(_confirmed_holes(snapshot)),
        "circularProfiles": len(snapshot.get("holes") or []),
        "fullSnapshot": {
            "tool": "catia_inspect_edit",
            "editSessionId": snapshot.get("editSessionId"),
            "note": "the full snapshot is authoritative; this context only selects from it",
        },
    }
    if cases is not None:
        payload["cases"] = cases
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    payload["contextDigest"] = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]
    payload["sizeBytes"] = len(canonical.encode("utf-8"))
    return payload
