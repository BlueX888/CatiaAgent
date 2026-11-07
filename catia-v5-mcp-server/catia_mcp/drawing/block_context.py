"""Validate and query raw 2D block records without expanding or treating them as evidence."""
from __future__ import annotations

import math
import re
from typing import Any

_BLOCK = re.compile(r"block:[A-Za-z0-9_.-]{1,128}\Z")
_DEFINITION_FIELDS = {"record_type", "block_id", "name", "base_point", "entity_ids", "has_external_reference"}
_INSTANCE_FIELDS = {"record_type", "entity_id", "spatial_frame_id", "coordinate_system", "container_id",
                    "definition_id", "transform_column_vectors", "rows", "columns", "row_spacing", "column_spacing",
                    "display", "layer_id", "unit"}
_DISPLAY_FIELDS = {"linetype", "ltscale", "lineweight", "color", "true_color", "transparency", "invisible",
                   "explicit_fields", "linetype_id"}


def _short_text(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 256


def _number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _display_value(value: Any) -> bool:
    if value is None or type(value) is bool or _number(value):
        return True
    if isinstance(value, str):
        return len(value) <= 256
    return isinstance(value, list) and len(value) <= 32 and all(isinstance(v, str) and len(v) <= 80 for v in value)


def validate_blocks(blocks: Any, asset_id: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Reject incomplete, mixed-drawing or malformed block records at the context boundary."""
    if not isinstance(blocks, dict) or set(blocks) != {"definitions", "instances"}:
        raise ValueError("Invalid block context")
    definitions, instances = blocks["definitions"], blocks["instances"]
    if (not isinstance(definitions, list) or not isinstance(instances, list)
            or len(definitions) > 20000 or len(instances) > 20000):
        raise ValueError("Invalid block record counts")
    by_id: dict[str, set[str]] = {}
    for definition in definitions:
        if (not isinstance(definition, dict) or set(definition) - _DEFINITION_FIELDS
                or definition.get("record_type") != "block_definition"
                or not isinstance(definition.get("block_id"), str) or not _BLOCK.fullmatch(definition["block_id"])
                or definition["block_id"] in by_id):
            raise ValueError("Invalid or duplicate block definition")
        if "name" in definition and not _short_text(definition["name"]):
            raise ValueError("Invalid block definition name")
        base, members = definition.get("base_point"), definition.get("entity_ids")
        if (not isinstance(base, list) or len(base) != 3 or not all(_number(v) for v in base)
                or not isinstance(members, list) or len(members) > 20000
                or any(not _short_text(v) or not v.startswith(asset_id + ":") for v in members)
                or len(set(members)) != len(members)
                or ("has_external_reference" in definition and type(definition["has_external_reference"]) is not bool)):
            raise ValueError("Invalid or mixed-drawing block definition")
        by_id[definition["block_id"]] = set(members)
    seen: set[str] = set()
    for instance in instances:
        if (not isinstance(instance, dict) or set(instance) - _INSTANCE_FIELDS
                or instance.get("record_type") != "block_instance"):
            raise ValueError("Invalid block instance")
        entity, frame, coords = instance.get("entity_id"), instance.get("spatial_frame_id"), instance.get("coordinate_system")
        container, definition = instance.get("container_id"), instance.get("definition_id")
        matrix = instance.get("transform_column_vectors")
        if (not _short_text(entity) or not entity.startswith(asset_id + ":") or entity in seen
                or not _short_text(frame) or not _short_text(coords) or container not in by_id or definition not in by_id
                or frame != container or entity not in by_id[container]
                or not isinstance(matrix, list) or len(matrix) != 4
                or any(not isinstance(row, list) or len(row) != 4 or not all(_number(v) for v in row) for row in matrix)):
            raise ValueError("Invalid, unbound or mixed-drawing block instance")
        for key in ("rows", "columns"):
            if type(instance.get(key)) is not int or not 1 <= instance[key] <= 10000:
                raise ValueError("Invalid block array dimensions")
        if not all(_number(instance.get(key)) for key in ("row_spacing", "column_spacing")):
            raise ValueError("Invalid block array spacing")
        for key in ("unit", "layer_id"):
            if key in instance and instance[key] is not None and not _short_text(instance[key]):
                raise ValueError("Invalid block instance field")
        if "display" in instance:
            display = instance["display"]
            if (not isinstance(display, dict) or set(display) - _DISPLAY_FIELDS
                    or not all(_display_value(value) for value in display.values())):
                raise ValueError("Invalid block display metadata")
        seen.add(entity)
    return definitions, instances


def query_context_blocks(path: str, pdf_sha256: str, query: dict[str, Any],
                         expected_context_sha256: str | None = None, *, data: bytes | None = None) -> dict[str, Any]:
    """Page unexpanded definitions or instances in their original frame."""
    from catia_mcp.drawing.pdf_evidence import _load_drawing_context
    _, _, payload = _load_drawing_context(path, pdf_sha256, expected_context_sha256, data=data)
    if "blocks" not in payload:
        raise ValueError("Drawing context has no block records")
    definitions, instances = validate_blocks(payload["blocks"], payload["assetId"])
    allowed = {"kind", "frame", "coordinateSystem", "definitionId", "entityId", "offset", "limit",
               "summaryOnly", "includeMembers", "memberOffset", "memberLimit"}
    if not isinstance(query, dict) or set(query) - allowed:
        raise ValueError("Invalid block query")
    kind = query.get("kind", "instances")
    frame, coords, definition_id, entity_id = (query.get(k) for k in ("frame", "coordinateSystem", "definitionId", "entityId"))
    offset, limit, summary = query.get("offset", 0), query.get("limit", 30), query.get("summaryOnly", False)
    include = query.get("includeMembers", False)
    member_offset, member_limit = query.get("memberOffset", 0), query.get("memberLimit", 30)
    if (kind not in ("definitions", "instances") or any(v is not None and not _short_text(v) for v in (frame, coords, definition_id, entity_id))
            or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100
            or type(summary) is not bool or type(include) is not bool
            or type(member_offset) is not int or member_offset < 0 or type(member_limit) is not int or not 1 <= member_limit <= 100):
        raise ValueError("Invalid block query parameters")
    if kind == "definitions" and (frame is not None or coords is not None or entity_id is not None):
        raise ValueError("Definition queries cannot filter instance frame or entity")
    if include and (kind != "definitions" or definition_id is None or summary):
        raise ValueError("includeMembers requires one definitionId and a non-summary definition query")
    if not include and ("memberOffset" in query or "memberLimit" in query):
        raise ValueError("Member pagination requires includeMembers")
    groups: dict[tuple[str, str], int] = {}
    for instance in instances:
        key = instance["spatial_frame_id"], instance["coordinate_system"]
        groups[key] = groups.get(key, 0) + 1
    if len(groups) > 1000:
        raise ValueError("Too many block coordinate groups")
    if kind == "instances":
        filtered = [r for r in instances if (frame is None or r["spatial_frame_id"] == frame)
                    and (coords is None or r["coordinate_system"] == coords)
                    and (definition_id is None or r["definition_id"] == definition_id)
                    and (entity_id is None or r["entity_id"] == entity_id)]
        if not summary and len({(r["spatial_frame_id"], r["coordinate_system"]) for r in filtered}) > 1:
            raise ValueError("Choose one frame and coordinateSystem, or use summaryOnly to list groups")
        records = filtered[offset:offset + limit]
    else:
        filtered = [r for r in definitions if definition_id is None or r["block_id"] == definition_id]
        records = []
        for definition in filtered[offset:offset + limit]:
            record = {k: v for k, v in definition.items() if k != "entity_ids"}
            members = definition["entity_ids"]
            record["entityCount"] = len(members)
            if include:
                record["entity_ids"] = members[member_offset:member_offset + member_limit]
                record["memberOffset"] = member_offset
                record["memberNextOffset"] = member_offset + member_limit if member_offset + member_limit < len(members) else None
            records.append(record)
    sources = {item["kind"]: item["sha256"] for item in payload["sources"]}
    return {"assetId": payload["assetId"], "sourceSha256": sources["blocks"], "kind": kind,
            "groups": [{"frame": f, "coordinateSystem": c, "count": n} for (f, c), n in sorted(groups.items())],
            "matchedCount": len(filtered), "offset": offset, "records": [] if summary else records,
            "nextOffset": None if summary or offset + limit >= len(filtered) else offset + limit,
            "note": "Raw 2D block definitions and instance transforms are not expanded or unified. Visibility and external references are not resolved; these records are not PDF or dimension evidence."}
