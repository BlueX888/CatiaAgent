"""Generation-only drawing tools avoid COM; builds connect under the CATIA lock."""

import asyncio
import threading
from contextlib import contextmanager

import fitz
import pytest

from catia_mcp.results import operation_result
from catia_mcp.server import CATIAMCPServer


def _pdf(path):
    doc = fitz.open()
    page = doc.new_page(width=200, height=200)
    page.insert_text((20, 30), "UNITS mm")
    page.insert_text((20, 50), "40")
    doc.save(str(path))
    doc.close()


def test_generation_drawing_and_plan_work_without_catia(tmp_path, monkeypatch):
    root = tmp_path / "generation"
    root.mkdir()
    pdf = root / "drawing.pdf"
    _pdf(pdf)
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))

    async def run():
        server = CATIAMCPServer()
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: (_ for _ in ()).throw(AssertionError("COM accessed")))
        try:
            inspected = await server.call_tool("catia_inspect_drawing", {"path": str(pdf)})
            assert not inspected.isError, inspected.structuredContent
            data = inspected.structuredContent["data"]
            spans = data["pages"][0]["spans"]
            unit = next(span["id"] for span in spans if "UNITS mm" in span["text"])
            dimension = next(span["id"] for span in spans if span["text"].strip() == "40")
            req = {"schema": "catia-feature-requirements/1", "unit": "mm", "unitEvidence": [unit],
                   "parameters": [{"name": "L", "value": 40, "evidence": [dimension]}],
                   "features": [{"id": "base", "operation": "pad", "length": "L",
                                 "sketch": {"plane": "xy", "profiles": [{"type": "circle", "radius": "L"}]}}]}
            evidence = data["evidencePath"]
            validated = await server.call_tool("catia_validate_requirements", {"requirements": req, "evidencePath": evidence})
            assert not validated.isError, validated.structuredContent
            compiled = await server.call_tool("catia_compile_build_plan", {"requirements": req, "evidencePath": evidence})
            assert not compiled.isError, compiled.structuredContent
            cropped = await server.call_tool("catia_crop_drawing", {
                "path": str(pdf), "page": 1, "bboxMm": [0, 0, 50, 50], "dpi": 72})
            assert not cropped.isError, cropped.structuredContent
            assert cropped.content[1].type == "image"
        finally:
            await server.close()

    asyncio.run(run())


def test_generation_build_connects_and_guards_under_cross_client_lock(tmp_path, monkeypatch):
    from catia_mcp.drawing import tools

    root = tmp_path / "generation"
    root.mkdir()
    evidence = root / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(tmp_path / "catia.lock"))
    monkeypatch.setattr(tools, "validate_requirements", lambda *a, **k: {
        "decision": "ready", "normalized": {}, "questions": [], "problems": []})
    monkeypatch.setattr(tools, "compile_build_plan", lambda *a, **k: {"planId": "test", "features": []})
    monkeypatch.setattr(tools, "plan_summary", lambda *_: "test")
    first_inside = threading.Event()
    release_first = threading.Event()
    second_connected = threading.Event()
    order = []

    async def run():
        first, second = CATIAMCPServer(), CATIAMCPServer()
        for server in (first, second):
            server._schemas["catia_build_part"] = {"type": "object"}
        monkeypatch.setattr(first.connection, "ensure_connected", lambda: order.append("connect-1"))
        monkeypatch.setattr(second.connection, "ensure_connected", lambda: (order.append("connect-2"), second_connected.set()))
        monkeypatch.setattr(first.editing_tools, "guard", lambda _: order.append("guard-1"))
        monkeypatch.setattr(second.editing_tools, "guard", lambda _: order.append("guard-2"))

        def build_first(*_):
            order.append("build-1")
            first_inside.set()
            assert release_first.wait(5)
            return operation_result(tool="catia_build_part", ok=True, code="STUB", message="", data={}, modified=False)

        def build_second(*_):
            order.append("build-2")
            return operation_result(tool="catia_build_part", ok=True, code="STUB", message="", data={}, modified=False)

        monkeypatch.setattr(first.drawing_tools, "_execute_build", build_first)
        monkeypatch.setattr(second.drawing_tools, "_execute_build", build_second)
        args = {"requirements": {}, "evidencePath": str(evidence), "deliveryPath": str(root / "part.CATPart")}
        try:
            task1 = asyncio.create_task(first.call_tool("catia_build_part", args))
            assert await asyncio.to_thread(first_inside.wait, 5)
            task2 = asyncio.create_task(second.call_tool("catia_build_part", args))
            await asyncio.sleep(.2)
            assert not second_connected.is_set(), "second client connected before first left the lock"
            release_first.set()
            results = await asyncio.wait_for(asyncio.gather(task1, task2), 5)
            assert all(not result.isError for result in results)
            assert order == ["connect-1", "guard-1", "build-1", "connect-2", "guard-2", "build-2"]
        finally:
            release_first.set()
            await first.close()
            await second.close()

    asyncio.run(run())


def test_generation_waiter_rechecks_target_before_connecting(tmp_path, monkeypatch):
    from catia_mcp.drawing import tools

    root = tmp_path / "generation"
    root.mkdir()
    evidence = root / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    target = root / "part.CATPart"
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(tmp_path / "catia.lock"))
    monkeypatch.setattr(tools, "validate_requirements", lambda *a, **k: {
        "decision": "ready", "normalized": {}, "questions": [], "problems": []})
    monkeypatch.setattr(tools, "compile_build_plan", lambda *a, **k: {"planId": "test", "features": []})
    monkeypatch.setattr(tools, "plan_summary", lambda *_: "test")
    first_inside = threading.Event()
    second_waiting = threading.Event()
    release_first = threading.Event()
    original_lock = tools.automation_lock
    calls = 0

    @contextmanager
    def observed_lock(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            second_waiting.set()
        with original_lock(*args, **kwargs):
            yield

    monkeypatch.setattr(tools, "automation_lock", observed_lock)

    async def run():
        first, second = CATIAMCPServer(), CATIAMCPServer()
        for server in (first, second):
            server._schemas["catia_build_part"] = {"type": "object"}
        monkeypatch.setattr(first.connection, "ensure_connected", lambda: None)
        monkeypatch.setattr(first.editing_tools, "guard", lambda _: None)
        monkeypatch.setattr(second.connection, "ensure_connected", lambda: pytest.fail("waiter connected"))
        monkeypatch.setattr(second.editing_tools, "guard", lambda _: pytest.fail("waiter accessed guard"))
        monkeypatch.setattr(second.drawing_tools, "_execute_build", lambda *_: pytest.fail("waiter built"))

        def first_build(*_):
            first_inside.set()
            assert release_first.wait(5)
            target.write_bytes(b"first build")
            return operation_result(tool="catia_build_part", ok=True, code="STUB", message="", data={}, modified=True)

        monkeypatch.setattr(first.drawing_tools, "_execute_build", first_build)
        args = {"requirements": {}, "evidencePath": str(evidence), "deliveryPath": str(target)}
        try:
            task1 = asyncio.create_task(first.call_tool("catia_build_part", args))
            assert await asyncio.to_thread(first_inside.wait, 5)
            task2 = asyncio.create_task(second.call_tool("catia_build_part", args))
            assert await asyncio.to_thread(second_waiting.wait, 5)
            assert not target.exists()
            release_first.set()
            result1, result2 = await asyncio.wait_for(asyncio.gather(task1, task2), 5)
            assert not result1.isError
            assert result2.isError
            assert result2.structuredContent["code"] == "SAVE_CONFLICT"
            assert result2.structuredContent["effects"]["modified"] is False
            assert target.read_bytes() == b"first build"
        finally:
            release_first.set()
            await first.close()
            await second.close()

    asyncio.run(run())


@pytest.mark.parametrize(("error", "code"), [(RuntimeError("connection unavailable"), "COM_ERROR"),
                                              (AttributeError("API unavailable"), "UNSUPPORTED_CAPABILITY")])
def test_generation_connect_failure_releases_lock_and_preserves_connect_error(tmp_path, monkeypatch, error, code):
    from catia_mcp.drawing import tools

    root = tmp_path / "generation"
    root.mkdir()
    evidence = root / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(tmp_path / "catia.lock"))
    monkeypatch.setattr(tools, "validate_requirements", lambda *a, **k: {
        "decision": "ready", "normalized": {}, "questions": [], "problems": []})
    monkeypatch.setattr(tools, "compile_build_plan", lambda *a, **k: {"planId": "test", "features": []})
    monkeypatch.setattr(tools, "plan_summary", lambda *_: "test")

    async def run():
        first, second = CATIAMCPServer(), CATIAMCPServer()
        for server in (first, second):
            server._schemas["catia_build_part"] = {"type": "object"}
        def fail_connect():
            raise error

        monkeypatch.setattr(first.connection, "ensure_connected", fail_connect)
        monkeypatch.setattr(second.connection, "ensure_connected", lambda: None)
        monkeypatch.setattr(second.editing_tools, "guard", lambda _: None)
        monkeypatch.setattr(second.drawing_tools, "_execute_build", lambda *_: operation_result(
            tool="catia_build_part", ok=True, code="STUB", message="", data={}, modified=False))
        args = {"requirements": {}, "evidencePath": str(evidence), "deliveryPath": str(root / "part.CATPart")}
        try:
            failed = await first.call_tool("catia_build_part", args)
            assert failed.isError
            assert failed.structuredContent["code"] == code
            assert failed.structuredContent["data"]["failure_stage"] == "connect"
            assert failed.structuredContent["effects"]["modified"] is False
            recovered = await asyncio.wait_for(second.call_tool("catia_build_part", args), 3)
            assert not recovered.isError, recovered.structuredContent
        finally:
            await first.close()
            await second.close()

    asyncio.run(run())


def test_generation_edit_guard_still_blocks_build_and_releases_lock(tmp_path, monkeypatch):
    from catia_mcp.drawing import tools
    from catia_mcp.locking import automation_lock

    root = tmp_path / "generation"
    root.mkdir()
    evidence = root / "evidence.json"
    evidence.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("CATIA_GENERATION_ROOT", str(root))
    monkeypatch.setenv("CATIA_MCP_LOCK_FILE", str(tmp_path / "catia.lock"))
    monkeypatch.setattr(tools, "validate_requirements", lambda *a, **k: {
        "decision": "ready", "normalized": {}, "questions": [], "problems": []})
    monkeypatch.setattr(tools, "compile_build_plan", lambda *a, **k: {"planId": "test", "features": []})
    monkeypatch.setattr(tools, "plan_summary", lambda *_: "test")

    async def run():
        server = CATIAMCPServer()
        server._schemas["catia_build_part"] = {"type": "object"}
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: None)
        monkeypatch.setattr(server.editing_tools, "guard", lambda _: operation_result(
            tool="catia_build_part", ok=False, code="BUSY", message="edit active", data={}, modified=False))
        monkeypatch.setattr(server.drawing_tools, "_execute_build", lambda *_: pytest.fail("blocked build executed"))
        try:
            result = await server.call_tool("catia_build_part", {
                "requirements": {}, "evidencePath": str(evidence), "deliveryPath": str(root / "part.CATPart")})
            assert result.isError
            assert result.structuredContent["code"] == "BUSY"
            assert result.structuredContent["effects"]["modified"] is False
            with automation_lock(timeout=0):
                pass
        finally:
            await server.close()

    asyncio.run(run())


def test_regular_non_generation_tools_still_connect(monkeypatch):
    monkeypatch.delenv("CATIA_GENERATION_ROOT", raising=False)

    async def run():
        server = CATIAMCPServer()
        connected = []
        monkeypatch.setattr(server.connection, "ensure_connected", lambda: connected.append(True))
        monkeypatch.setattr(server.document_tools, "execute", lambda *_: operation_result(
            tool="catia_list_documents", ok=True, code="STUB", message="", data={}, modified=False))
        try:
            result = await server.call_tool("catia_list_documents", {})
            assert not result.isError
            assert connected == [True]
        finally:
            await server.close()

    asyncio.run(run())
