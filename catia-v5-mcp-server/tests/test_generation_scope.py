import asyncio
import base64

import pytest
from mcp.types import ListToolsRequest

from catia_mcp.generation_scope import GENERATION_TOOLS, GenerationScope
from catia_mcp.server import CATIAMCPServer


@pytest.fixture
def scope(tmp_path):
    root = tmp_path / "generation"
    root.mkdir()
    (root / "input.pdf").write_bytes(b"%PDF-1.4\n")
    (root / "evidence.json").write_text("{}")
    return GenerationScope(str(root))


def test_scope_checks_input_and_default_outputs(scope):
    source = str(scope.root / "input.pdf")
    inspected = scope.prepare("catia_inspect_drawing", {"path": source})
    assert inspected["evidenceDir"] == str(scope.root / "evidence")
    crop = scope.prepare("catia_crop_drawing", {"path": source})
    assert crop["outputPath"].startswith(str(scope.root / "evidence"))
    assert crop["outputPath"].endswith(".png")


@pytest.mark.parametrize("kind", ["outside", "traversal", "relative", "stream", "wrong_type", "output", "evidence"])
def test_paths_cannot_escape_or_read_other_files(scope, kind):
    outside = scope.root.parent / "reference.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    args = {"path": str(scope.root / "input.pdf")}
    if kind == "outside":
        args["path"] = str(outside)
    elif kind == "traversal":
        args["path"] = str(scope.root / ".." / "reference.pdf")
    elif kind == "relative":
        args["path"] = "input.pdf"
    elif kind == "stream":
        args["path"] += ":secret.pdf"
    elif kind == "wrong_type":
        args["path"] = str(scope.root / "evidence.json")
    elif kind == "output":
        args["evidenceDir"] = str(scope.root.parent / "outside")
    else:
        args = {"requirements": {}, "evidencePath": str(outside)}
    with pytest.raises((ValueError, OSError)):
        scope.prepare("catia_validate_requirements" if kind == "evidence" else "catia_inspect_drawing", args)


def test_resolved_link_escape_is_denied(scope):
    outside = scope.root.parent / "reference.pdf"
    outside.write_bytes(b"%PDF-1.4\n")
    link = scope.root / "linked.pdf"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"Symlink creation unavailable: {exc}")
    with pytest.raises(ValueError, match="outside"):
        scope.prepare("catia_inspect_drawing", {"path": str(link)})


@pytest.mark.parametrize("override", [{"expected": {}}, {"editCheck": False}, {"requireEvidence": False}])
def test_generation_cannot_disable_verification(scope, override):
    args = {"requirements": {}, "evidencePath": str(scope.root / "evidence.json"),
            "deliveryPath": str(scope.root / "part.CATPart"), **override}
    with pytest.raises(ValueError):
        scope.prepare("catia_build_part", args)


def test_scope_mode_is_fail_closed_before_com(scope, monkeypatch):
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(scope.root))

    async def run():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: pytest.fail("COM accessed"))
        try:
            assert set(server._tool_router) == GENERATION_TOOLS
            listed = await server.server.request_handlers[ListToolsRequest](ListToolsRequest(method="tools/list"))
            assert {tool.name for tool in listed.root.tools} == GENERATION_TOOLS
            for tool in listed.root.tools:
                if tool.name in {"catia_validate_requirements", "catia_compile_build_plan", "catia_build_part"}:
                    assert "evidencePath" in tool.inputSchema["required"]
                    assert tool.inputSchema == server._schemas[tool.name]
                    assert tool.inputSchema["properties"]["evidencePath"]["minLength"] == 1
                    if tool.name != "catia_build_part":
                        assert tool.inputSchema["properties"]["requireEvidence"]["const"] is True
                    else:
                        assert tool.inputSchema["properties"]["editCheck"]["const"] is True
                        assert tool.inputSchema["properties"]["expected"]["not"] == {}
            denied = await server.call_tool("catia_verify_part", {"partPath": "reference.CATPart", "expected": {}})
            assert denied.isError and denied.structuredContent["code"] == "UNKNOWN_TOOL"
            outside = scope.root.parent / "reference.pdf"
            outside.write_bytes(b"%PDF-1.4\n")
            denied = await server.call_tool("catia_inspect_drawing", {"path": str(outside)})
            assert denied.isError and denied.structuredContent["code"] == "GENERATION_SCOPE_DENIED"
            assert denied.structuredContent["effects"]["modified"] is False
        finally:
            await server.close()
    asyncio.run(run())


def test_missing_evidence_path_has_an_actionable_scope_error(scope):
    with pytest.raises(ValueError, match="evidencePath returned by catia_inspect_drawing"):
        scope.prepare("catia_validate_requirements", {"requirements": {}})


def test_crop_image_is_returned_without_general_file_reader(scope, monkeypatch):
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(scope.root))
    import fitz
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text((20, 20), "UNITS mm")
    doc.save(str(scope.root / "drawing.pdf"))
    doc.close()

    async def run():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        try:
            result = await server.call_tool("catia_crop_drawing", {
                "path": str(scope.root / "drawing.pdf"), "page": 1, "bboxMm": [0, 0, 50, 50], "dpi": 72})
            assert not result.isError
            assert result.content[1].type == "image"
            data = base64.b64decode(result.content[1].data)
            assert data.startswith(b"\x89PNG\r\n\x1a\n")
            assert result.content[1].mimeType == "image/png"
        finally:
            await server.close()
    asyncio.run(run())


def test_bad_config_does_not_fall_back_to_unrestricted(monkeypatch):
    monkeypatch.setenv("CATIA_GENERATION_ROOT", "")
    with pytest.raises(ValueError, match="existing absolute"):
        CATIAMCPServer()
