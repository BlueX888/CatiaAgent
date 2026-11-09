"""Deadlines may reject a build, but never imply that a running build stopped."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from catia_mcp import server as module


def test_expired_queued_build_does_not_enter_worker(monkeypatch):
    now = [1_000_000_000]
    monkeypatch.setattr(module.time, "time_ns", lambda: now[0])
    server = module.CATIAMCPServer.__new__(module.CATIAMCPServer)
    server._executor = ThreadPoolExecutor(max_workers=1)
    entered, release = Event(), Event()
    calls = []
    server._execute_tool = lambda *args: calls.append(args)

    def block():
        entered.set()
        release.wait(5)

    server._executor.submit(block)
    assert entered.wait(5)

    async def run():
        pending = asyncio.create_task(server.call_tool("catia_build_part", {}, operation_deadline_ms=2000))
        await asyncio.sleep(0)
        now[0] = 2_000_000_000
        release.set()
        return await pending

    try:
        response = asyncio.run(run())
        assert response.structuredContent["code"] == "OPERATION_DEADLINE_EXPIRED"
        assert response.structuredContent["effects"]["modified"] is False
        assert not calls
    finally:
        release.set()
        server._executor.shutdown(wait=True)


def _bare_server(monkeypatch, now):
    monkeypatch.setattr(module.time, "time_ns", lambda: now[0])
    server = module.CATIAMCPServer.__new__(module.CATIAMCPServer)
    server._executor = ThreadPoolExecutor(max_workers=1)
    return server


def test_expired_before_enqueue_makes_zero_calls(monkeypatch):
    now = [1_000_000_000]
    server = _bare_server(monkeypatch, now)
    calls = []
    server._execute_tool = lambda *args: calls.append(args)
    try:
        response = asyncio.run(server.call_tool("catia_build_part", {}, operation_deadline_ms=500))
        body = response.structuredContent
        assert body["code"] == "OPERATION_DEADLINE_EXPIRED"
        assert "before enqueue" in body["message"]
        assert body["effects"]["modified"] is False
        assert calls == []
    finally:
        server._executor.shutdown(wait=True)


def test_invalid_deadlines_are_rejected_before_any_call(monkeypatch):
    now = [1_000_000_000]
    for deadline in (True, 1.5, -1, 2 ** 53):
        server = _bare_server(monkeypatch, now)
        calls = []
        server._execute_tool = lambda *args: calls.append(args)
        try:
            response = asyncio.run(server.call_tool("catia_build_part", {}, operation_deadline_ms=deadline))
            assert response.structuredContent["code"] == "INVALID_OPERATION_DEADLINE"
            assert calls == []
        finally:
            server._executor.shutdown(wait=True)


def test_deadline_expiring_during_execution_is_recorded_not_cancelled(monkeypatch):
    now = [1_000_000_000]
    server = _bare_server(monkeypatch, now)
    success = module.operation_result(
        tool="catia_build_part", ok=True, code="BUILD_COMPLETED", message="built",
        data={"businessState": "completed"}, modified=True, operation_id="op-deadline")

    def execute(_name, _arguments):
        now[0] = 3_000_000_000  # deadline passes while the worker runs
        return module.structured_result(success)

    server._execute_tool = execute
    try:
        response = asyncio.run(server.call_tool("catia_build_part", {}, operation_deadline_ms=2000))
        body = response.structuredContent
        assert body["ok"] is True and body["effects"]["modified"] is True
        assert body["diagnostics"][-1]["code"] == "OPERATION_DEADLINE_EXPIRED_DURING_EXECUTION"
    finally:
        server._executor.shutdown(wait=True)


def test_old_call_without_deadline_is_unchanged(monkeypatch):
    now = [1_000_000_000]
    server = _bare_server(monkeypatch, now)
    calls = []
    success = module.operation_result(tool="catia_build_part", ok=True, code="BUILD_COMPLETED",
                                      message="built", data={}, modified=True, operation_id="op-old")
    server._execute_tool = lambda *args: calls.append(args) or module.structured_result(success)
    try:
        response = asyncio.run(server.call_tool("catia_build_part", {}))
        assert calls and response.structuredContent["ok"] is True
        assert response.structuredContent["diagnostics"] == []
    finally:
        server._executor.shutdown(wait=True)
