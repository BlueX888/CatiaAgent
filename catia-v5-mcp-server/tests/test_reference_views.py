import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path

import pytest

from catia_mcp.server import CATIAMCPServer
from catia_mcp.reference_binding import ReferenceBinding
from catia_mcp.reference_views import ReferenceViews


@pytest.mark.parametrize("invalid", [[], {"records": [{"record_type": "view", "view_id": "view_001",
    "png_path": "views/view_001.png", "projection_type": "orthographic",
    "camera_position": [10 ** 400, 0, 0], "target": [0, 0, 0], "up": [0, 1, 0]}], "record_count": 1}])
def test_malformed_view_index_is_rejected_as_value_error(tmp_path, invalid):
    step = b"ISO-10303-21;\nEND-ISO-10303-21;"
    (tmp_path / "reference.step").write_bytes(step)
    sha = hashlib.sha256(step).hexdigest()
    directory = tmp_path / "views"
    directory.mkdir()
    if isinstance(invalid, dict):
        invalid["records"][0]["geometry_sha256"] = sha
    index = json.dumps(invalid).encode()
    (directory / "index.json").write_bytes(index)
    metadata = {"index": {"sha256": hashlib.sha256(index).hexdigest(), "size": len(index)},
                "views": [{"id": "view_001", "sha256": "0" * 64, "size": 1}]}
    binding = ReferenceBinding(str(tmp_path), sha, len(step))
    with pytest.raises(ValueError, match="Invalid reference view"):
        ReferenceViews(binding, metadata)
    metadata["index"]["sha256"] = None
    with pytest.raises(ValueError, match="metadata"):
        ReferenceViews(binding, metadata)
    metadata["index"]["sha256"] = hashlib.sha256(index).hexdigest()
    metadata["views"][0]["sha256"] = None
    with pytest.raises(ValueError, match="reference view"):
        ReferenceViews(binding, metadata)


def test_reference_views_are_bound_paginated_and_embedded(tmp_path, monkeypatch):
    step = b"ISO-10303-21;\nEND-ISO-10303-21;"
    (tmp_path / "reference.step").write_bytes(step)
    sha = hashlib.sha256(step).hexdigest()
    views = tmp_path / "views"
    views.mkdir()
    png = b"\x89PNG\r\n\x1a\nfixture"
    (views / "view_001.png").write_bytes(png)
    index = json.dumps({"record_count": 1, "records": [{"record_type": "view", "view_id": "view_001",
        "png_path": "views/view_001.png", "geometry_sha256": sha, "projection_type": "orthographic",
        "camera_position": [1, 2, 3], "target": [0, 0, 0], "up": [0, 1, 0]}]}).encode()
    (views / "index.json").write_bytes(index)
    meta = {"index": {"sha256": hashlib.sha256(index).hexdigest(), "size": len(index)},
            "views": [{"id": "view_001", "sha256": hashlib.sha256(png).hexdigest(), "size": len(png)}]}
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", sha)
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(step)))
    monkeypatch.setenv("CATIA_REFERENCE_VIEWS", json.dumps(meta))

    async def run():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: (_ for _ in ()).throw(AssertionError("COM")))
        try:
            listing = await server.call_tool("catia_query_reference", {"kind": "views", "limit": 1})
            assert listing.structuredContent["data"]["views"][0]["view_id"] == "view_001"
            assert listing.structuredContent["data"]["sourceSha256"] == sha
            result = await server.call_tool("catia_query_reference", {"kind": "view", "viewId": "view_001"})
            assert result.structuredContent["data"]["evidenceStatus"] == "informational_only"
            assert base64.b64decode(result.content[-1].data) == png
            assert (await server.call_tool("catia_query_reference", {"kind": "view", "viewId": "../other"})).isError
            assert (await server.call_tool("catia_query_reference", {"kind": "view", "viewId": "view_002"})).isError
            (views / "view_001.png").write_bytes(b"x" * len(png))
            assert (await server.call_tool("catia_query_reference", {"kind": "view", "viewId": "view_001"})).isError
            (views / "view_001.png").write_bytes(png)
            (views / "index.json").write_bytes(index.replace(sha.encode(), b"0" * 64))
            assert (await server.call_tool("catia_query_reference", {"kind": "views"})).isError
            (views / "index.json").write_bytes(index)
            (tmp_path / "reference.step").write_bytes(b"x" * len(step))
            assert (await server.call_tool("catia_query_reference", {"kind": "view", "viewId": "view_001"})).isError
        finally:
            await server.close()
    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("CATIA_VIEW_TEST_RUN"), reason="Set a prepared run directory")
def test_real_prepared_view_uses_embedded_image_channel(monkeypatch):
    run = Path(os.environ["CATIA_VIEW_TEST_RUN"])
    plugin = json.loads((run / "host/plugin/plugin.json").read_text(encoding="utf-8"))
    env = plugin["mcpServers"]["catia_generation"]["env"]
    if "CATIA_INPUT_BINDING" not in env:
        pytest.skip("Prepared run predates paired PDF/context binding")
    for key in ("CATIA_GENERATION_ROOT", "CATIA_REFERENCE_SHA256", "CATIA_REFERENCE_SIZE", "CATIA_REFERENCE_VIEWS", "CATIA_INPUT_BINDING"):
        monkeypatch.setenv(key, env[key])
    async def run_query():
        server = CATIAMCPServer()
        try:
            listing = await server.call_tool("catia_query_reference", {"kind": "views"})
            assert not listing.isError
            row = listing.structuredContent["data"]["views"][0]
            result = await server.call_tool("catia_query_reference", {"kind": "view", "viewId": row["view_id"]})
            assert not result.isError
            assert result.content[-1].mimeType == "image/png"
            assert base64.b64decode(result.content[-1].data).startswith(b"\x89PNG\r\n\x1a\n")
        finally:
            await server.close()
    asyncio.run(run_query())
