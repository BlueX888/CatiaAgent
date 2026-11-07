"""Real stdio generation boundary and image transport probe; no model answers."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


async def main():
    run = ROOT.parent / "work" / f"generation-scope-{time.time_ns()}"
    workspace = run / "generation"
    workspace.mkdir(parents=True)
    source = ROOT.parent / "datasets/pdf-modeling/drawings/DP-01.pdf"
    drawing = workspace / "drawing.pdf"
    shutil.copyfile(source, drawing)
    report = {"workspace": str(workspace), "inputSha256": hashlib.sha256(drawing.read_bytes()).hexdigest(), "checks": []}
    env = {**os.environ, "CATIA_GENERATION_ROOT": str(workspace), "PYTHONIOENCODING": "utf-8"}
    params = StdioServerParameters(command=sys.executable, args=["-m", "catia_mcp"], env=env, cwd=str(ROOT))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            expected = {"catia_inspect_drawing", "catia_crop_drawing", "catia_validate_requirements", "catia_compile_build_plan", "catia_build_part"}
            report["checks"].append({"name": "exact_tool_allowlist", "passed": names == expected})
            for name, args, code in [
                ("catia_verify_part", {"partPath": "reference.CATPart", "expected": {}}, "UNKNOWN_TOOL"),
                ("catia_inspect_drawing", {"path": str(source)}, "GENERATION_SCOPE_DENIED"),
                ("catia_crop_drawing", {"path": str(drawing), "page": 1, "bboxMm": [0, 0, 60, 60],
                                       "outputPath": str(run / "outside.png")}, "GENERATION_SCOPE_DENIED"),
            ]:
                result = await session.call_tool(name, args)
                report["checks"].append({"name": name + "_denial", "code": result.structuredContent["code"],
                                         "passed": result.isError and result.structuredContent["code"] == code})
            inspected = await session.call_tool("catia_inspect_drawing", {"path": str(drawing)})
            data = inspected.structuredContent
            report["checks"].append({"name": "inspect_allowed_pdf", "passed": data["ok"] is True,
                                     "evidencePath": data.get("data", {}).get("evidencePath")})
            cropped = await session.call_tool("catia_crop_drawing", {
                "path": str(drawing), "page": 1, "bboxMm": [0, 0, 180, 180], "dpi": 100})
            images = [block for block in cropped.content if block.type == "image"]
            image_ok = len(images) == 1 and base64.b64decode(images[0].data).startswith(b"\x89PNG\r\n\x1a\n")
            report["checks"].append({"name": "inline_crop_image", "passed": not cropped.isError and image_ok})
    report["passed"] = all(check["passed"] for check in report["checks"])
    (run / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "report": str(run / "report.json")}), flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
