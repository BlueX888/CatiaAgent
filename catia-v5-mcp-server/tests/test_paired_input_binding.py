import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest

from catia_mcp.generation_scope import GenerationScope
from catia_mcp.server import CATIAMCPServer


def metadata(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


@pytest.fixture
def paired(tmp_path):
    pdf = b"%PDF-1.4\nfixture"
    context = b'{"schema":"test"}'
    step = b"ISO-10303-21;\nEND-ISO-10303-21;"
    (tmp_path / "drawing.pdf").write_bytes(pdf)
    (tmp_path / "drawing-context.json").write_bytes(context)
    (tmp_path / "reference.step").write_bytes(step)
    return tmp_path, {"pdf": metadata(pdf), "context": metadata(context)}, step


def test_scope_rejects_other_files_and_changed_input(paired):
    root, binding, _ = paired
    scope = GenerationScope(str(root), reference_enabled=True, input_binding=binding)
    drawing = str(root / "drawing.pdf")
    context = str(root / "drawing-context.json")
    scope.prepare("catia_inspect_drawing", {"path": drawing, "drawingContextPath": context})
    (root / "other.pdf").write_bytes(b"%PDF-other")
    with pytest.raises(ValueError, match="bound drawing PDF"):
        scope.prepare("catia_inspect_drawing", {"path": str(root / "other.pdf")})
    (root / "other.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="bound drawing context"):
        scope.prepare("catia_inspect_drawing", {"path": drawing, "drawingContextPath": str(root / "other.json")})
    (root / "drawing-context.json").write_bytes(b"x" * binding["context"]["size"])
    with pytest.raises(ValueError, match="hash differs"):
        scope.prepare("catia_inspect_drawing", {"path": drawing})


def test_scope_rejects_same_bytes_replaced_by_new_file(paired):
    root, binding, _ = paired
    scope = GenerationScope(str(root), reference_enabled=True, input_binding=binding)
    other = root / "replacement.pdf"
    other.write_bytes((root / "drawing.pdf").read_bytes())
    other.replace(root / "drawing.pdf")
    with pytest.raises(ValueError, match="identity changed"):
        scope.check_bound_inputs()


def test_assisted_build_requires_explicit_workspace_lock(paired, monkeypatch):
    root, binding, _ = paired
    evidence = root / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    scope = GenerationScope(str(root), reference_enabled=True, input_binding=binding)
    args = {"evidencePath": str(evidence), "deliveryPath": str(root / "part.CATPart")}
    monkeypatch.delenv("CATIA_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("CATIA_MCP_LOCK_FILE", raising=False)
    with pytest.raises(ValueError, match="explicit workspace"):
        scope.prepare("catia_build_part", args)
    artifact_root = root.parent
    lock_dir = artifact_root / "work" / "catia-agent-lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CATIA_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(root / "other.lock"))
    with pytest.raises(ValueError, match="fixed host workspace lock"):
        scope.prepare("catia_build_part", args)
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(lock_dir / "catia-automation.lock"))
    assert scope.prepare("catia_build_part", args)["deliveryPath"] == str(root / "part.CATPart")


def test_assisted_build_denies_missing_lock_before_com(paired, monkeypatch):
    root, binding, step = paired
    evidence = root / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", metadata(step)["sha256"])
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(step)))
    monkeypatch.setenv("CATIA_INPUT_BINDING", json.dumps(binding))
    monkeypatch.delenv("CATIA_ARTIFACT_ROOT", raising=False)
    monkeypatch.delenv("CATIA_MCP_LOCK_FILE", raising=False)

    async def run():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: pytest.fail("COM accessed"))
        server._schemas["catia_build_part"] = {"type": "object"}
        try:
            result = await server.call_tool("catia_build_part", {
                "evidencePath": str(evidence), "deliveryPath": str(root / "part.CATPart"), "requirements": {}})
            assert result.isError and result.structuredContent["code"] == "GENERATION_SCOPE_DENIED"
            assert result.structuredContent["effects"]["modified"] is False
        finally:
            await server.close()
    asyncio.run(run())


@pytest.mark.parametrize("tool,expected_modified", [("catia_inspect_drawing", False), ("catia_build_part", None)])
def test_server_reports_midcall_change_without_fabricating_success(paired, monkeypatch, tool, expected_modified):
    root, binding, step = paired
    (root / "evidence.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", hashlib.sha256(step).hexdigest())
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(step)))
    monkeypatch.setenv("CATIA_INPUT_BINDING", json.dumps(binding))
    artifact_root = root.parent
    lock_dir = artifact_root / "work" / "catia-agent-lock"
    lock_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CATIA_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(lock_dir / "catia-automation.lock"))

    async def run():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: pytest.fail("COM accessed"))
        server._schemas[tool] = {"type": "object"}
        def execute(_name, _args):
            (root / "drawing.pdf").write_bytes(b"x" * binding["pdf"]["size"])
            return {"ok": True, "code": "OK"}
        monkeypatch.setattr(server.drawing_tools, "execute", execute)
        try:
            args = ({"path": str(root / "drawing.pdf")} if tool == "catia_inspect_drawing" else
                    {"evidencePath": str(root / "evidence.json"), "requirements": {},
                     "deliveryPath": str(root / "part.CATPart")})
            result = await server.call_tool(tool, args)
            assert result.isError and result.structuredContent["code"] == "GENERATION_INPUT_CHANGED"
            assert result.structuredContent["effects"]["modified"] is expected_modified
        finally:
            await server.close()
    asyncio.run(run())


def test_evidence_is_session_bound_and_not_reloaded_from_disk(tmp_path, monkeypatch):
    import fitz

    doc = fitz.open()
    doc.new_page(width=200, height=200).insert_text((20, 30), "UNITS mm 42")
    pdf = doc.tobytes()
    doc.close()
    context = json.dumps({"schema": "catia-drawing-context/1", "pdfSha256": metadata(pdf)["sha256"],
                          "assetId": "drawing_abc", "sources": [
                              {"kind": kind, "sha256": digest * 64} for kind, digest in
                              [("dimensions", "a"), ("texts", "b"), ("titleBlock", "c")]],
                          "dimensions": [{"entity_id": "drawing_abc:1", "display_text": "42"}],
                          "texts": [{"entity_id": "drawing_abc:2", "plain_text": "UNITS mm"}],
                          "titleBlock": []}).encode()
    step = b"ISO-10303-21;\nEND-ISO-10303-21;"
    for name, data in (("drawing.pdf", pdf), ("drawing-context.json", context), ("reference.step", step)):
        (tmp_path / name).write_bytes(data)
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", metadata(step)["sha256"])
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(step)))
    monkeypatch.setenv("CATIA_INPUT_BINDING", json.dumps({"pdf": metadata(pdf), "context": metadata(context)}))

    async def run():
        server = CATIAMCPServer()
        args = {"path": str(tmp_path / "drawing.pdf"), "drawingContextPath": str(tmp_path / "drawing-context.json")}
        try:
            first, second = await asyncio.gather(server.call_tool("catia_inspect_drawing", args),
                                                 server.call_tool("catia_inspect_drawing", args))
            assert not first.isError and not second.isError
            evidence_path = Path(first.structuredContent["data"]["evidencePath"])
            original_bytes = evidence_path.read_bytes()
            forged = json.loads(evidence_path.read_text(encoding="utf-8"))
            forged["contextEvidence"][0]["text"] = "99"
            evidence_path.write_text(json.dumps(forged), encoding="utf-8")
            with pytest.raises(RuntimeError, match="changed since inspection"):
                server.drawing_tools._evidence_for({}, {"evidencePath": str(evidence_path)}, require=True)
            rejected = await server.call_tool("catia_inspect_drawing", args)
            assert rejected.isError and "changed since inspection" in rejected.structuredContent["message"]
            evidence_path.write_bytes(original_bytes)
            evidence = server.drawing_tools._evidence_for({}, {"evidencePath": str(evidence_path)}, require=True)
            assert evidence["contextEvidence"][0]["text"] == "42"
            hardlink = tmp_path / "evidence-link.json"
            try:
                os.link(evidence_path, hardlink)
            except OSError:
                hardlink = None
            if hardlink:
                with pytest.raises(RuntimeError, match="unlinked regular file"):
                    server.drawing_tools._evidence_for({}, {"evidencePath": str(evidence_path)}, require=True)
                rejected = await server.call_tool("catia_inspect_drawing", args)
                assert rejected.isError and hardlink.read_bytes() == original_bytes
                hardlink.unlink()
            with pytest.raises(ValueError, match="not produced by this generation session"):
                server.drawing_tools._evidence_for({}, {"evidencePath": str(tmp_path / "forged.json")}, require=True)
        finally:
            await server.close()
        reopened = CATIAMCPServer()
        try:
            with pytest.raises(ValueError, match="not produced by this generation session"):
                reopened.drawing_tools._evidence_for({}, {"evidencePath": str(evidence_path)}, require=True)
            rejected = await reopened.call_tool("catia_inspect_drawing", args)
            assert rejected.isError and "already exists" in rejected.structuredContent["message"]
        finally:
            await reopened.close()
    asyncio.run(run())


def test_binding_cannot_be_enabled_without_reference(tmp_path):
    with pytest.raises(ValueError, match="reference-enabled"):
        GenerationScope(str(tmp_path), input_binding={})


@pytest.mark.parametrize("tool", ["catia_inspect_drawing", "catia_crop_drawing"])
def test_parsers_use_verified_snapshot_during_transient_file_replacement(tmp_path, monkeypatch, tool):
    import fitz

    doc = fitz.open()
    doc.new_page(width=200, height=200).insert_text((20, 30), "ORIGINAL 42")
    pdf = doc.tobytes()
    doc.close()
    context = json.dumps({"schema": "catia-drawing-context/1", "pdfSha256": metadata(pdf)["sha256"],
                          "assetId": "drawing_abc", "sources": [
                              {"kind": kind, "sha256": digest * 64} for kind, digest in
                              [("dimensions", "a"), ("texts", "b"), ("titleBlock", "c"), ("geometry", "d")]],
                          "dimensions": [{"entity_id": "drawing_abc:1", "display_text": "42"}],
                          "texts": [], "titleBlock": [], "geometry": [
                              {"entity_id": "drawing_abc:circle", "spatial_frame_id": "block:1",
                               "coordinate_system": "ocs", "geometry_type": "CIRCLE",
                               "geometry": {"center_ocs": [0, 0, 0], "radius": 5}}]}).encode()
    step = b"ISO-10303-21;\nEND-ISO-10303-21;"
    paths = [tmp_path / "drawing.pdf", tmp_path / "drawing-context.json"]
    for path, data in zip(paths, (pdf, context)):
        path.write_bytes(data)
    (tmp_path / "reference.step").write_bytes(step)
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(tmp_path))
    monkeypatch.setenv("CATIA_REFERENCE_SHA256", metadata(step)["sha256"])
    monkeypatch.setenv("CATIA_REFERENCE_SIZE", str(len(step)))
    monkeypatch.setenv("CATIA_INPUT_BINDING", json.dumps({"pdf": metadata(pdf), "context": metadata(context)}))

    async def run():
        server = CATIAMCPServer()
        original_snapshot = server.drawing_tools.input_snapshots
        original_execute = server.drawing_tools.execute
        times = [path.stat().st_mtime_ns for path in paths]
        def replace_after_snapshot(include_context):
            result = original_snapshot(include_context)
            for path, value in zip(paths, (pdf, context)):
                path.write_bytes(b"x" * len(value))
            return result
        def restore_after_execute(name, arguments):
            try:
                return original_execute(name, arguments)
            finally:
                for path, value, timestamp in zip(paths, (pdf, context), times):
                    path.write_bytes(value)
                    os.utime(path, ns=(timestamp, timestamp))
        server.drawing_tools.input_snapshots = replace_after_snapshot
        server.drawing_tools.execute = restore_after_execute
        try:
            args = {"path": str(paths[0])}
            if tool == "catia_inspect_drawing":
                args.update(drawingContextPath=str(paths[1]), geometryQuery={"kind": "CIRCLE"})
            else:
                args.update(page=1, bboxMm=[0, 0, 50, 50])
            result = await server.call_tool(tool, args)
            assert not result.isError, result.structuredContent
            if tool == "catia_inspect_drawing":
                data = result.structuredContent["data"]
                assert "ORIGINAL 42" in data["pages"][0]["text"]
                assert data["contextEvidence"][0]["text"] == "42"
                assert data["contextGeometry"]["records"][0]["geometry"]["radius"] == 5
                assert data["source"]["sha256"] == metadata(pdf)["sha256"]
            else:
                assert any(item.type == "image" for item in result.content)
        finally:
            await server.close()
    asyncio.run(run())
