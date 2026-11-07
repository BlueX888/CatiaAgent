"""End-to-end through the real MCP stdio transport (not a direct import).

Proves the tool schemas, the text JSON envelope and structuredContent agree,
that the write-exclusivity guard answers BUSY over the wire, and that a full
begin -> apply -> finalize round trip works through the protocol.

Run: .venv/Scripts/python.exe tests/acceptance/mcp_transport_e2e.py
"""

from __future__ import annotations

import asyncio
import time
import json
import os
import shutil
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = r"F:\CatiaAgent\work\stage1\transport-e2e"
SOURCE = os.path.join(ROOT, "src", "FixtureA-mounting-plate.CATPart")
FIXTURE = os.path.join(os.path.dirname(HERE), "fixtures", "FixtureA-mounting-plate.CATPart")


def envelope(result) -> dict:
    """The server returns the same JSON in structuredContent and in the text block."""
    structured = result.structuredContent
    text = None
    for block in result.content:
        if getattr(block, "type", None) == "text":
            text = block.text
            break
    parsed_text = json.loads(text) if text else None
    assert structured is not None, "structuredContent missing"
    assert parsed_text is not None, "text envelope missing"
    assert parsed_text.get("code") == structured.get("code"), "text and structured code disagree"
    assert result.isError == (structured.get("ok") is not True), "isError disagrees with ok"
    return structured


async def main() -> int:
    run = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(os.path.dirname(SOURCE), exist_ok=True)
    shutil.copy2(FIXTURE, SOURCE)
    delivery = os.path.join(ROOT, "delivered", f"E2E-L200-{run}.CATPart")
    os.makedirs(os.path.dirname(delivery), exist_ok=True)
    working = os.path.join(ROOT, "work", run)
    journal = os.path.join(ROOT, f"journal-{run}.sqlite3")

    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "CATIA_MCP_EDIT_DB": journal}
    params = StdioServerParameters(command=sys.executable, args=["-m", "catia_mcp"], env=env, cwd=os.path.dirname(HERE) + r"\..")
    report: dict = {"steps": [], "passed": False}
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            report["toolCount"] = len(tools.tools)
            report["hasEditTools"] = all(
                name in {tool.name for tool in tools.tools}
                for name in (
                    "catia_begin_edit",
                    "catia_inspect_edit",
                    "catia_apply_edit_plan",
                    "catia_get_edit_status",
                    "catia_finalize_edit",
                    "catia_recover_edit",
                )
            )
            begin = envelope(
                await session.call_tool(
                    "catia_begin_edit", {"sourcePath": SOURCE, "workingDir": working}
                )
            )
            report["steps"].append({"step": "begin", "ok": begin.get("ok"), "code": begin.get("code")})
            if not begin.get("ok"):
                print(json.dumps(report, ensure_ascii=False, indent=2))
                return 1
            session_id = begin["data"]["editSessionId"]
            snapshot = begin["data"]["snapshot"]
            drivers = {p["handle"].split("\\")[-1]: p for p in snapshot["parameters"] if p.get("kind") == "driver"}
            report["drivers"] = {name: {"value": p["value"], "writable": p.get("writable")} for name, p in drivers.items()}
            plan = {
                "schema": "catia-edit-plan/1",
                "editSessionId": session_id,
                "operationId": f"e2e-l200-{run}",
                "expectedRevision": begin["data"]["modelRevision"],
                "intent": {"text": "闀垮害鏀逛负 200 mm", "resolved": "absolute L=200mm"},
                "changes": [
                    {"parameter": drivers["L"]["handle"], "oldValue": 160.0, "newValue": 200.0, "unit": "mm"}
                ],
                "preserve": [{"type": "holes_edge_distance"}],
                "acceptance": [
                    {"type": "bbox_range", "axis": "x", "min": -100, "max": 100},
                    {"type": "volume", "expected": 238115.044408},
                    {"type": "hole_positions", "holes": [{"x": -85, "y": 35}, {"x": 85, "y": 35}]},
                ],
            }
            applied = envelope(
                await session.call_tool(
                    "catia_apply_edit_plan", {"editSessionId": session_id, "plan": plan}
                )
            )
            report["steps"].append(
                {"step": "apply", "ok": applied.get("ok"), "code": applied.get("code"),
                 "verdict": applied.get("data", {}).get("verdict"),
                 "businessState": applied.get("data", {}).get("businessState")}
            )
            blocked = envelope(await session.call_tool("catia_set_parameter", {"name": "Part1\\L", "value": 1.0}))
            report["steps"].append(
                {"step": "generic-write-while-session-active", "ok": blocked.get("ok"), "code": blocked.get("code")}
            )
            status = envelope(await session.call_tool("catia_get_edit_status", {"editSessionId": session_id}))
            report["steps"].append(
                {"step": "status", "businessState": status.get("data", {}).get("businessState"),
                 "operations": [op["operationId"] for op in status.get("data", {}).get("operations", [])]}
            )
            final = envelope(
                await session.call_tool(
                    "catia_finalize_edit", {"editSessionId": session_id, "deliveryPath": delivery}
                )
            )
            report["steps"].append(
                {"step": "finalize", "ok": final.get("ok"), "code": final.get("code"),
                 "delivery": (final.get("data") or {}).get("delivery"),
                 "verificationPassed": ((final.get("data") or {}).get("verification") or {}).get("passed")}
            )
    ok = (
        report["hasEditTools"]
        and all(step.get("ok") is True for step in report["steps"] if step["step"] in ("begin", "apply", "finalize"))
        and report["steps"][1].get("verdict") == "PASS"
        and report["steps"][2].get("code") == "BUSY"
        and os.path.exists(delivery)
    )
    report["passed"] = bool(ok)
    out = os.path.join(ROOT, "transport-e2e-report.json")
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    print(f"\n{'PASS' if ok else 'FAIL'} -> {out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
