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
