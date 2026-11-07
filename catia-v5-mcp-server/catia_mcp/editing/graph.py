"""Evidence-tagged relation graph for the editing layer (P1, guide section 5.3).

This is deliberately a plain JSON adjacency list, not a graph database. Every
edge carries the evidence that produced it and its strength, because the guide
forbids presenting a name match or a single-perturbation observation as a
proven design intent:

``explicit_formula``            a relation expression literally references the parameter
``explicit_geometric_constraint`` a sketch constraint's Dimension names the parameter
``explicit_feature_reference``  the CATIA feature itself returns this sketch
``perturbation_impact``         a measured +N perturbation changed the other node
``unconfirmed``                 name/visual similarity only; never proves a preserve

The graph is rebuilt from a snapshot; nothing here talks to CATIA.
"""

from __future__ import annotations

import re
from typing import Any

_IDENT = re.compile(r"[A-Za-z_一-鿿][\w一-鿿.]*")


def _token_occurs(expression: str, token: str) -> bool:
    if not token:
        return False
    return any(match.group(0) == token for match in _IDENT.finditer(expression or ""))


def _tail(handle: str) -> str:
    return str(handle).split("\\")[-1]


def build_relations_graph(
    model: dict[str, Any], impacts: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build nodes/edges with per-edge evidence.

    Node identifiers are stable for one snapshot; callers must not persist them
    as handles. The real write target is always ``param:<exact handle>``.
    """
    impacts = impacts or {}
    parameters = [p for p in model.get("parameters") or [] if isinstance(p, dict)]
    by_handle = {str(p.get("handle")): p for p in parameters}
    by_tail: dict[str, list[str]] = {}
    for handle in by_handle:
        by_tail.setdefault(_tail(handle), []).append(handle)

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    limitations: list[str] = []
    seen_edges: set[tuple[str, str, str]] = set()

    def add_edge(source: str, target: str, kind: str, evidence: dict[str, Any]) -> None:
        key = (source, target, kind)
        if key in seen_edges:
            return
        seen_edges.add(key)
        edges.append({"from": source, "to": target, "type": kind, "evidence": evidence})

    for handle, param in by_handle.items():
        nodes.append(
            {
                "id": f"param:{handle}",
                "type": "parameter",
                "handle": handle,
                "name": param.get("name"),
                "value": param.get("value"),
                "unit": param.get("unit"),
                "kind": param.get("kind"),
                "writable": param.get("writable"),
            }
        )

    structure = model.get("structure") or {}
    for feature in structure.get("features") or []:
        fid = f"feature:{feature.get('name')}"
        nodes.append(
            {
                "id": fid,
                "type": "feature",
                "name": feature.get("name"),
                "kind": feature.get("kind"),
                "materialRole": feature.get("materialRole"),
                "kindEvidence": feature.get("kindEvidence"),
            }
        )
        if feature.get("sketch"):
            add_edge(
                fid,
                f"sketch:{feature.get('sketch')}",
                "explicit_feature_reference",
                {"source": "MainBody.Shapes.Item(i).Sketch", "sketch": feature.get("sketch")},
            )
        add_edge(
            f"body:{model.get('features', {}).get('mainBody')}",
            fid,
            "explicit_containment",
            {"source": "MainBody.Shapes"},
        )

    for sketch in structure.get("sketches") or []:
        nodes.append(
            {
                "id": f"sketch:{sketch.get('name')}",
                "type": "sketch",
                "name": sketch.get("name"),
                "frame": sketch.get("frame"),
                "frameError": sketch.get("frameError"),
                "usedByFeatures": sketch.get("usedByFeatures") or [],
                "circleCount": len(sketch.get("circles") or []),
            }
        )
        for constraint in sketch.get("constraints") or []:
            cid = f"constraint:{sketch.get('name')}:{constraint.get('name')}"
            nodes.append(
                {
                    "id": cid,
                    "type": "constraint",
                    "name": constraint.get("name"),
                    "kind": constraint.get("kind"),
                    "type": constraint.get("type"),
                }
            )
            parameter = constraint.get("parameter")
            if parameter:
                add_edge(
                    cid,
                    f"param:{parameter}",
                    "explicit_geometric_constraint",
                    {
                        "constraintName": constraint.get("name"),
                        "constraintKind": constraint.get("kind"),
                        "dimensionName": parameter,
                        "value": constraint.get("value"),
                        "unit": constraint.get("unit"),
                    },
                )

    for relation in model.get("relations") or []:
        name = str(relation.get("name"))
        expression = str(relation.get("expression") or "")
        rid = f"relation:{name}"
        nodes.append({"id": rid, "type": "relation", "name": name, "expression": expression})
        referenced: set[str] = set()
        for token, handles in by_tail.items():
            if not _token_occurs(expression, token):
                continue
            for handle in handles:
                referenced.add(handle)
                add_edge(
                    rid,
                    f"param:{handle}",
                    "explicit_formula",
                    {"relation": name, "expression": expression, "token": token},
                )
        # Name-only similarity is recorded but never promoted to explicit.
        for handle in by_handle:
            tail = _tail(handle)
            if handle in referenced or tail not in name:
                continue
            add_edge(
                rid,
                f"param:{handle}",
                "unconfirmed",
                {
                    "reason": "relation name contains the parameter tail but the expression does not reference it",
                    "relation": name,
                    "expression": expression,
                    "token": tail,
                },
            )
        if not referenced and expression:
            limitations.append(
                f"relation {name}: expression {expression!r} references no parameter handle; "
                f"the destination parameter is not exposed by this CATIA build"
            )

    for handle, impact in impacts.items():
        if not isinstance(impact, dict):
            continue
        source = f"param:{handle}"
        for change in impact.get("affectedParameters") or []:
            other = change.get("handle")
            if not other or other == handle:
                continue
            add_edge(
                source,
                f"param:{other}",
                "perturbation_impact",
                {
                    "probeDelta": impact.get("probeDelta"),
                    "before": change.get("before"),
                    "after": change.get("after"),
                    "restored": impact.get("restored"),
                },
            )
        deltas = impact.get("holeRadiusDeltas")
        holes = model.get("holes") or []
        if isinstance(deltas, list) and len(deltas) == len(holes):
            for index, delta in enumerate(deltas):
                if delta is None or abs(float(delta)) <= 1e-9:
                    continue
                hole = holes[index]
                add_edge(
                    source,
                    f"hole:{hole.get('sketch')}:{index}",
                    "perturbation_impact",
                    {
                        "probeDelta": impact.get("probeDelta"),
                        "radiusDelta": delta,
                        "volumeDelta": impact.get("volumeDelta"),
                        "holeRole": hole.get("role"),
                        "restored": impact.get("restored"),
                    },
                )

    for body_name in structure.get("bodies") or []:
        nodes.append({"id": f"body:{body_name}", "type": "body", "name": body_name})
    main_body = (model.get("features") or {}).get("mainBody")
    if main_body:
        nodes.append({"id": f"body:{main_body}", "type": "body", "name": main_body})

    return {
        "schema": "catia-relation-graph/1",
        "nodes": nodes,
        "edges": edges,
        "limitations": limitations,
        "evidenceTypes": [
            "explicit_formula",
            "explicit_geometric_constraint",
            "explicit_feature_reference",
            "explicit_containment",
            "perturbation_impact",
            "unconfirmed",
        ],
    }


def query_graph(graph: dict[str, Any], node_id: str, *, hops: int = 1) -> dict[str, Any]:
    """Small adjacency query used by context distillation (P2)."""
    edges = graph.get("edges") or []
    frontier = {node_id}
    visited = {node_id}
    collected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for _ in range(max(1, hops)):
        next_frontier: set[str] = set()
        for edge in edges:
            if edge.get("from") not in frontier and edge.get("to") not in frontier:
                continue
            key = (str(edge.get("from")), str(edge.get("to")), str(edge.get("type")))
            if key not in seen:
                seen.add(key)
                collected.append(edge)
            for end in (edge.get("from"), edge.get("to")):
                if end not in visited:
                    next_frontier.add(end)
        if not next_frontier:
            break
        visited |= next_frontier
        frontier = next_frontier
    nodes = [node for node in graph.get("nodes") or [] if node.get("id") in visited]
    return {"nodes": nodes, "edges": collected}
