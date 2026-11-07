"""Query bounded raw layer definitions without resolving visibility."""
from __future__ import annotations

import re
from typing import Any


def validate_layers(layers: Any) -> list[dict[str, Any]]:
    if not isinstance(layers, list) or len(layers) > 20000:
        raise ValueError("Invalid drawing layer records")
    def short(value):
        return isinstance(value, str) and 0 < len(value) <= 256

    seen = set()
    for record in layers:
        if not isinstance(record, dict) or set(record) - {"record_type", "layer_id", "name", "properties", "linetype_id", "off", "frozen"}:
            raise ValueError("Invalid drawing layer record")
        layer_id = record.get("layer_id")
        properties = record.get("properties")
        if (record.get("record_type") != "layer" or not isinstance(layer_id, str)
                or not re.fullmatch(r"layer:[A-Za-z0-9_.-]{1,128}", layer_id) or layer_id in seen
                or not short(record.get("name")) or not isinstance(properties, dict)
                or set(properties) - {"linetype"}
                or ("linetype" in properties and not short(properties["linetype"]))
                or ("linetype_id" in record and not short(record["linetype_id"]))
                or any(key in record and type(record[key]) is not bool for key in ("off", "frozen"))):
            raise ValueError("Invalid drawing layer record")
        seen.add(layer_id)
    return layers


def query_context_layers(path: str, pdf_sha256: str, query: dict[str, Any],
                         expected_context_sha256: str | None = None, *, data: bytes | None = None) -> dict[str, Any]:
    from catia_mcp.drawing.pdf_evidence import _load_drawing_context
    _, _, payload = _load_drawing_context(path, pdf_sha256, expected_context_sha256, data=data)
    if "layers" not in payload:
        raise ValueError("Drawing context has no layer records")
    if (not isinstance(query, dict) or set(query) != {"layerId"}
            or not isinstance(query["layerId"], str)
            or not re.fullmatch(r"layer:[A-Za-z0-9_.-]{1,128}", query["layerId"])):
        raise ValueError("Layer query requires one valid layerId")
    records = [item for item in validate_layers(payload["layers"]) if item["layer_id"] == query["layerId"]]
    hashes = {item["kind"]: item["sha256"] for item in payload["sources"]}
    return {"assetId": payload["assetId"], "sourceSha256": hashes["layers"],
            "layerId": query["layerId"], "records": records,
            "note": "Raw layer definitions are untrusted 2D data, not PDF or dimension evidence. "
                    "Layer names and linetypes do not establish actual visibility, geometry or through features."}
