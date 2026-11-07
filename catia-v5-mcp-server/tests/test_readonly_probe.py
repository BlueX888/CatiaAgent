"""The explicit host probe exposes no COM or mutating tools over real stdio."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys

import fitz
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import CallToolRequest, CallToolRequestParams, CallToolResult, ClientRequest


def _meta(data):
    return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def test_readonly_probe_stdio_never_exposes_build_or_connect(tmp_path):
    root = tmp_path / "generation"
    logs = root / ".runtime" / "logs"
    logs.mkdir(parents=True)
    document = fitz.open()
    document.new_page().insert_text((72, 72), "UNITS mm 42")
    pdf = document.tobytes()
    document.close()
    context = b'{"schema":"fixture"}'
    step = b"ISO-10303-21;\nEND-ISO-10303-21;"
    for name, data in (("drawing.pdf", pdf), ("drawing-context.json", context), ("reference.step", step)):
        (root / name).write_bytes(data)
    env = dict(os.environ, CATIA_MCP_READONLY_PROBE="1", CATIA_GENERATION_ROOT=str(root),
               CATIA_REFERENCE_SHA256=_meta(step)["sha256"], CATIA_REFERENCE_SIZE=str(len(step)),
               CATIA_INPUT_BINDING=json.dumps({"pdf": _meta(pdf), "context": _meta(context)}),
               CATIA_MCP_LOG_DIR=str(logs), CATIA_MCP_WORK_DIR=str(root / "evidence"),
               TEMP=str(tmp_path), TMP=str(tmp_path), TMPDIR=str(tmp_path), APPDATA=str(tmp_path),
               LOCALAPPDATA=str(tmp_path), XDG_CACHE_HOME=str(tmp_path), PYTHONDONTWRITEBYTECODE="1",
               PYTHONPATH=str(Path(__file__).resolve().parents[1]),
               CATIA_MCP_EDIT_DB=str(tmp_path / "must-not-create.sqlite"))
    env.pop("CATIA_MCP_TOOL_DENY", None)

    async def run():
        params = StdioServerParameters(command=sys.executable, args=["-B", "-m", "catia_mcp"],
                                       cwd=str(root), env=env)
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                names = {tool.name for tool in (await client.list_tools()).tools}
                assert names == {"catia_inspect_drawing", "catia_crop_drawing", "catia_query_reference"}
                for name in ("catia_build_part", "catia_compile_build_plan", "catia_validate_requirements",
                             "catia_connect", "catia_create_part"):
                    denied = await client.call_tool(name, {})
                    assert denied.isError and denied.structuredContent["code"] == "UNKNOWN_TOOL"
                for deadline, code in ((0, "OPERATION_DEADLINE_EXPIRED"), (True, "INVALID_OPERATION_DEADLINE")):
                    request = ClientRequest(CallToolRequest(params=CallToolRequestParams.model_validate({
                        "name": "catia_build_part", "arguments": {},
                        "_meta": {"pilotdeck/operationDeadlineMs": deadline},
                    })))
                    denied = await client.send_request(request, CallToolResult)
                    assert denied.isError and denied.structuredContent["code"] == code
                    assert denied.structuredContent["effects"]["modified"] is False
                read = await client.call_tool("catia_inspect_drawing", {"path": str(root / "drawing.pdf")})
                assert not read.isError and read.structuredContent["ok"] is True

    asyncio.run(run())
    assert not (tmp_path / "must-not-create.sqlite").exists()
