"""MCP transport with one shared CATIA connection and one serial STA worker."""

from __future__ import annotations

import argparse
import asyncio
import base64
import copy
import json
import logging
import os
from pathlib import Path
import stat
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import uuid4

from jsonschema import ValidationError
from jsonschema.validators import validator_for
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ImageContent, TextContent, Tool

from catia_mcp.connection import CATIAConnection
from catia_mcp.generation_scope import GenerationScope, GENERATION_TOOLS
from catia_mcp.reference_binding import ReferenceBinding
from catia_mcp.reference_views import ReferenceViews
from catia_mcp.reference_tools import ReferenceTools
from catia_mcp.results import operation_result
from catia_mcp.tools.assembly import AssemblyTools
from catia_mcp.tools.diagnostics import DiagnosticsTools
from catia_mcp.tools.document import DocumentTools
from catia_mcp.tools.drawing import DraftingTools
from catia_mcp.drawing.tools import DrawingTools, GenerationBuildConnectionError
from catia_mcp.tools.editing import EditingTools
from catia_mcp.tools.export import ExportTools
from catia_mcp.tools.gsd import GSDTools
from catia_mcp.tools.knowledge import KnowledgeTools
from catia_mcp.tools.measurement import MeasurementTools
from catia_mcp.tools.part_design import PartDesignTools
from catia_mcp.tools.sketcher import SketcherTools

def _log_target() -> tuple[str, str]:
    """Each assisted MCP instance owns a new log within its generation run."""
    if not os.environ.get("CATIA_REFERENCE_SHA256"):
        return os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "catia_mcp.log"), "a"
    root_value, log_value = os.environ.get("CATIA_GENERATION_ROOT"), os.environ.get("CATIA_MCP_LOG_DIR")
    if not root_value or not log_value:
        raise ValueError("Assisted generation requires a bound per-run log directory.")
    root, directory = Path(root_value), Path(log_value)
    if (not root.is_absolute() or not directory.is_absolute()
            or ":" in str(directory)[len(directory.anchor):]
            or os.path.normcase(str(directory)) != os.path.normcase(str(root / ".runtime" / "logs"))):
        raise ValueError("Invalid assisted generation log directory.")
    for item in (directory, *directory.parents):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Assisted generation log path contains a link.")
    if not directory.is_dir() or os.path.normcase(str(directory.resolve(strict=True))) != os.path.normcase(str(directory)):
        raise ValueError("Assisted generation log directory changed.")
    return str(directory / f"mcp-{uuid4().hex}.log"), "x"


_log_path, _log_mode = _log_target()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.FileHandler(
            _log_path,
            mode=_log_mode,
            encoding="utf-8",
        ),
        logging.StreamHandler(sys.stderr),
    ],
)
logger = logging.getLogger("catia_mcp")

_GENERATION_OFFLINE_TOOLS = frozenset({
    "catia_inspect_drawing", "catia_crop_drawing", "catia_validate_requirements",
    "catia_compile_build_plan", "catia_query_reference",
})
_PROBE_TOOLS = frozenset({"catia_inspect_drawing", "catia_crop_drawing", "catia_query_reference"})


class _ReadOnlyProbeConnection:
    """No COM connection can be obtained from the host-only probe."""

    def __getattr__(self, _name):
        raise RuntimeError("CATIA access is disabled in read-only probe mode.")

    def disconnect(self):
        return None


def structured_result(data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(
        content=[
            TextContent(
                type="text", text=json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2)
            )
        ],
        structuredContent=data,
        isError=data.get("ok") is not True,
    )


def failure(
    code: str, message: str, stage: str, modified: bool | None, tool: str
) -> CallToolResult:
    return structured_result(
        operation_result(
            tool=tool,
            ok=False,
            code=code,
            message=message,
            data={"failure_stage": stage},
            modified=modified,
        )
    )


def argument_error(error: ValidationError) -> str:
    """Keep the failing field visible when a large oneOf payload is rejected."""
    def relevant_children(item):
        children = item.context
        options = item.schema.get(item.validator) if isinstance(item.schema, dict) else None
        if item.validator not in ("oneOf", "anyOf") or not isinstance(options, list) or not isinstance(item.instance, dict):
            return children
        for key in ("schema", "operation", "type"):
            value = item.instance.get(key)
            if value is None:
                continue
            recognized = []
            matching = []
            for index, option in enumerate(options):
                prop = option.get("properties", {}).get(key, {}) if isinstance(option, dict) else {}
                if "const" in prop or "enum" in prop:
                    recognized.append(index)
                    if ("const" in prop and value == prop["const"]) or ("enum" in prop and value in prop["enum"]):
                        matching.append(index)
            if recognized and matching:
                selected = [child for child in children if child.schema_path and child.schema_path[0] in matching]
                if selected:
                    return selected
        return children

    pending,leaves=[error],[]
    while pending:
        item=pending.pop()
        if item.context:
            pending.extend(reversed(relevant_children(item)))
        else:
            leaves.append(item)
    depth=max(len(item.absolute_path) for item in leaves)
    deepest=[item for item in leaves if len(item.absolute_path)==depth]
    specific=[item for item in deepest if not (item.validator in ('const','enum')
              and item.absolute_path and item.absolute_path[-1] in ('type','operation','schema'))]
    details={}
    for item in specific or deepest:
        path='$'+''.join(f'[{p}]' if isinstance(p,int) else '.'+str(p) for p in item.absolute_path)
        if path not in details and len(details)==3:
            continue
        message=item.message
        if isinstance(item.instance,(dict,list)) and item.validator not in ('required','additionalProperties'):
            message=f'Expected {item.validator} {str(item.validator_value)[:80]}; received {type(item.instance).__name__}'
        details.setdefault(path,[]).append(message[:160])
    return 'Invalid argument: '+'; '.join(path[:140]+': '+' | '.join(dict.fromkeys(messages))[:320]
                                         for path,messages in details.items())


class CATIAMCPServer:
    def __init__(self) -> None:
        generation_root = os.environ.get("CATIA_GENERATION_ROOT")
        reference_hash = os.environ.get("CATIA_REFERENCE_SHA256")
        reference_size = os.environ.get("CATIA_REFERENCE_SIZE")
        reference_views = os.environ.get("CATIA_REFERENCE_VIEWS")
        input_binding = os.environ.get("CATIA_INPUT_BINDING")
        probe_value = os.environ.get("CATIA_MCP_READONLY_PROBE")
        if probe_value not in (None, "1"):
            raise ValueError("Invalid read-only probe mode.")
        probe = probe_value == "1"
        if (reference_hash is None) != (reference_size is None) or (reference_hash is not None and generation_root is None):
            raise ValueError("Reference binding requires a generation root, SHA-256 and size.")
        reference_binding = None
        if reference_hash is not None:
            if not reference_size.isascii() or not reference_size.isdecimal():
                raise ValueError("Reference size must be a decimal integer.")
            reference_binding = ReferenceBinding(generation_root, reference_hash, int(reference_size))
        if reference_views is not None and reference_binding is None:
            raise ValueError("Reference views require a STEP binding.")
        if input_binding is not None and reference_binding is None:
            raise ValueError("Paired input binding requires a STEP binding.")
        if probe and (reference_binding is None or input_binding is None):
            raise ValueError("Read-only probe requires paired generation inputs.")
        views = ReferenceViews(reference_binding, json.loads(reference_views)) if reference_views is not None else None
        self.generation_scope = GenerationScope(generation_root, reference_enabled=reference_binding is not None,
                                                input_binding=json.loads(input_binding) if input_binding is not None else None) if generation_root is not None else None
        self.server = Server("catia-v5-mcp")
        self.connection = _ReadOnlyProbeConnection() if probe else CATIAConnection()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="catia-sta")
        self.editing_tools = None if probe else EditingTools(self.connection)
        reference_tools = ReferenceTools(reference_binding, views) if reference_binding is not None else None
        self.drawing_tools = DrawingTools(
            self.connection,
            before_build=self._generation_build_access if self.generation_scope else None,
            reference_evidence=reference_tools.evidence if reference_tools else None,
            input_snapshots=self.generation_scope.input_snapshots if self.generation_scope and self.generation_scope.bound_inputs else None,
        )
        if probe:
            self._tool_modules = [self.drawing_tools]
        else:
            self.document_tools = DocumentTools(self.connection)
            self.sketcher_tools = SketcherTools(self.connection)
            self.part_design_tools = PartDesignTools(self.connection)
            self.gsd_tools = GSDTools(self.connection)
            self.diagnostics_tools = DiagnosticsTools(self.connection)
            self.drafting_tools = DraftingTools(self.connection)
            self.knowledge_tools = KnowledgeTools(self.connection)
            self.assembly_tools = AssemblyTools(self.connection)
            self.measurement_tools = MeasurementTools(self.connection)
            self.export_tools = ExportTools(self.connection)
            self._tool_modules = [self.document_tools, self.sketcher_tools, self.part_design_tools,
                                  self.gsd_tools, self.diagnostics_tools, self.drafting_tools,
                                  self.knowledge_tools, self.assembly_tools, self.measurement_tools,
                                  self.export_tools, self.editing_tools, self.drawing_tools]
        if reference_tools is not None:
            self._tool_modules.append(reference_tools)
        self._denied_tools = {
            name.strip()
            for name in (os.environ.get("CATIA_MCP_TOOL_DENY") or "").split(",")
            if name.strip()
        }
        self._tool_router: dict[str, Any] = {}
        self._schemas: dict[str, dict] = {}
        for module in self._tool_modules:
            for definition in module.get_tool_definitions():
                name = definition["name"]
                if probe and name not in _PROBE_TOOLS:
                    continue
                if self.generation_scope and name not in GENERATION_TOOLS and not (reference_binding and name == "catia_query_reference"):
                    continue
                if name in self._denied_tools:
                    continue
                if name in self._tool_router:
                    raise ValueError(f"Duplicate tool: {name}")
                self._tool_router[name] = module
                schema = copy.deepcopy(definition["inputSchema"])
                if self.generation_scope and name in {
                    "catia_validate_requirements", "catia_compile_build_plan", "catia_build_part"
                }:
                    schema["required"] = list(dict.fromkeys([*schema.get("required", []), "evidencePath"]))
                    schema["properties"]["evidencePath"]["minLength"] = 1
                    schema["properties"]["evidencePath"]["description"] = (
                        "Required: the absolute evidencePath returned by catia_inspect_drawing for this workspace."
                    )
                    schema["properties"]["requireEvidence"] = {
                        "type": "boolean", "const": True, "default": True,
                        "description": "Drawing evidence is mandatory in generation mode."
                    }
                    if name == "catia_build_part":
                        schema["properties"]["editCheck"] = {
                            "type": "boolean", "const": True, "default": True,
                            "description": "An independent parameter edit check is mandatory in generation mode."
                        }
                        schema["properties"]["expected"] = {
                            "not": {}, "description": "Unavailable in generation mode; expectations are automatic."
                        }
                self._schemas[name] = schema
        if self._denied_tools:
            logger.info("Disabled tools via CATIA_MCP_TOOL_DENY: %s", ", ".join(sorted(self._denied_tools)))
        self._setup_handlers()

    def _generation_build_access(self) -> dict[str, Any] | None:
        # Called only after DrawingTools has acquired the machine-wide lock.
        try:
            self.connection.ensure_connected()
        except Exception as error:
            raise GenerationBuildConnectionError("CATIA connection failed") from error
        return self.editing_tools.guard("catia_build_part")

    def _setup_handlers(self) -> None:
        @self.server.list_tools()
        async def handle_list_tools() -> list[Tool]:
            return [
                Tool(**{**definition, "inputSchema": self._schemas[definition["name"]]})
                for module in self._tool_modules
                for definition in module.get_tool_definitions()
                if definition["name"] in self._tool_router
            ]

        @self.server.call_tool(validate_input=False)
        async def handle_call_tool(name: str, arguments: dict[str, Any] | None) -> CallToolResult:
            meta = self.server.request_context.meta
            deadline = (meta.model_extra or {}).get("pilotdeck/operationDeadlineMs") if meta else None
            return await self.call_tool(name, arguments or {}, operation_deadline_ms=deadline)

    async def call_tool(self, name: str, arguments: dict[str, Any], *, operation_deadline_ms=None) -> CallToolResult:
        # Metadata is advisory and untrusted: it may only reject a build sooner.
        # It cannot authorize execution, extend a timeout, or prove cancellation.
        if name == "catia_build_part" and operation_deadline_ms is not None:
            if type(operation_deadline_ms) is not int or not 0 <= operation_deadline_ms <= 2**53 - 1:
                return failure("INVALID_OPERATION_DEADLINE", "Invalid operation deadline.", "queue", False, name)
            if time.time_ns() // 1_000_000 >= operation_deadline_ms:
                return failure("OPERATION_DEADLINE_EXPIRED", "Operation deadline expired before enqueue.", "queue", False, name)
        future = asyncio.get_running_loop().run_in_executor(
            self._executor, self._execute_before_deadline, name, arguments, operation_deadline_ms
        )
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            future.add_done_callback(
                lambda completed: completed.exception() if not completed.cancelled() else None
            )
            raise

    def _execute_before_deadline(self, name: str, arguments: dict[str, Any], deadline_ms) -> CallToolResult:
        if (name == "catia_build_part" and deadline_ms is not None
                and time.time_ns() // 1_000_000 >= deadline_ms):
            return failure("OPERATION_DEADLINE_EXPIRED", "Operation deadline expired while queued.", "queue", False, name)
        return self._execute_tool(name, arguments)

    def _execute_tool(self, name: str, arguments: dict[str, Any]) -> CallToolResult:
        module = self._tool_router.get(name)
        if module is None:
            return failure("UNKNOWN_TOOL", f"Unknown tool: {name}", "validate", False, name)
        schema = self._schemas[name]
        error = next(validator_for(schema)(schema).iter_errors(arguments), None)
        if error is not None:
            return failure("INVALID_ARGUMENT", argument_error(error), "validate", False, name)
        if self.generation_scope:
            try:
                arguments = self.generation_scope.prepare(name, arguments)
            except (ValueError, OSError) as error:
                return failure("GENERATION_SCOPE_DENIED", str(error), "validate", False, name)
        logger.info("Tool call: %s(%s)", name, arguments)
        stage = "connect"
        try:
            generation_build = bool(self.generation_scope and name == "catia_build_part")
            offline_generation = bool(self.generation_scope and name in _GENERATION_OFFLINE_TOOLS)
            if name not in {"catia_connect", "catia_disconnect"} and not (generation_build or offline_generation):
                self.connection.ensure_connected()
            stage = "connect" if name == "catia_connect" else "execute"
            guard = None if generation_build or self.editing_tools is None else self.editing_tools.guard(name)
            if guard is not None:
                logger.info("Tool blocked by edit session: %s", name)
                return structured_result(guard)
            result = module.execute(name, arguments)
            if self.generation_scope:
                try:
                    self.generation_scope.check_bound_inputs()
                except (ValueError, OSError) as error:
                    return failure("GENERATION_INPUT_CHANGED", str(error), "post_execute",
                                   None if name == "catia_build_part" else False, name)
            if isinstance(result, dict):
                logger.info("Tool result: %s %s", name, result.get("code"))
                if result.get("ok") is not True and name == "catia_build_part":
                    failure_detail = ((result.get("data") or {}).get("failure") or {})
                    if failure_detail.get("reason"):
                        detail = str(failure_detail["reason"])
                        if len(detail) > 4000:
                            detail = detail[:2000] + " ...[middle omitted]... " + detail[-2000:]
                        logger.info("Build failure detail: %s", detail)
                response = structured_result(result)
                if self.generation_scope and name == "catia_crop_drawing" and result.get("ok") is True:
                    try:
                        data = self.generation_scope.crop_bytes(result)
                    except (ValueError, OSError) as error:
                        return failure("CROP_IMAGE_FAILED", str(error), "crop_image", False, name)
                    response.content.append(ImageContent(type="image", mimeType="image/png", data=base64.b64encode(data).decode("ascii")))
                if name == "catia_query_reference" and arguments.get("kind") == "view" and result.get("ok") is True:
                    try:
                        _, data = module.views.image(arguments["viewId"])
                    except (ValueError, OSError) as error:
                        return failure("REFERENCE_IMAGE_FAILED", str(error), "reference_image", False, name)
                    response.content.append(ImageContent(type="image", mimeType="image/png", data=base64.b64encode(data).decode("ascii")))
                return response
            logger.info("Tool result: %s", result[:200])
            return CallToolResult(content=[TextContent(type="text", text=result)])
        except Exception as error:
            logger.exception("Tool failed: %s: %s", name, error)  # noqa: TRY401 -- log preview includes the error message
            if isinstance(error, GenerationBuildConnectionError):
                stage = "connect"
                error = error.__cause__ or error
            code = "UNSUPPORTED_CAPABILITY" if isinstance(error, AttributeError) else "COM_ERROR"
            message = str(error)
            if code == "UNSUPPORTED_CAPABILITY":
                message = (
                    f"UNSUPPORTED_CAPABILITY in {name}: the required CATIA automation API "
                    f"did not resolve ({error}). The API may be absent from this CATIA release, "
                    "or a stale pywin32 gen_py cache may be blocking resolution. Clearing caches "
                    "cannot add an absent API. For a stale cache, close CATIA and the server, "
                    "delete the %TEMP%\\gen_py folder, restart and retry once. A missing "
                    "workbench licence can also lock methods. Run catia_diagnose for details."
                )
            return failure(code, message, stage, False if stage == "connect" else None, name)

    async def close(self) -> None:
        try:
            await asyncio.get_running_loop().run_in_executor(
                self._executor, self.connection.disconnect
            )
        finally:
            self._executor.shutdown(wait=True)

    async def run(self) -> None:
        logger.info("Starting CATIA V5 MCP Server: %d tools", len(self._tool_router))
        try:
            async with stdio_server() as (read_stream, write_stream):
                await self.server.run(
                    read_stream, write_stream, self.server.create_initialization_options()
                )
        finally:
            await self.close()


    async def run_http(self, host: str, port: int, token: str | None) -> None:
        from catia_mcp.http_transport import serve

        logger.info("Starting CATIA V5 MCP Server (HTTP): %d tools", len(self._tool_router))
        try:
            await serve(self.server, host, port, token)
        finally:
            await self.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    from catia_mcp.http_transport import DEFAULT_HOST, DEFAULT_PORT

    parser = argparse.ArgumentParser(
        prog="catia-mcp",
        description="CATIA V5 MCP server. Uses stdio by default; --http serves remote agents.",
    )
    parser.add_argument(
        "--http",
        action="store_true",
        help="Serve over Streamable HTTP at /mcp instead of stdio (remote agents, Mac clients)",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Port (default 8765)")
    parser.add_argument(
        "--token",
        default=os.environ.get("CATIA_MCP_TOKEN"),
        help="Bearer token required from clients (default: CATIA_MCP_TOKEN env var). "
        "Mandatory when --host is not a loopback address.",
    )
    parser.add_argument(
        "--new-token", action="store_true", help="Print a fresh random token and exit"
    )
    # MCP hosts may append their own scratch/profile flags to stdio servers
    # (PilotDeck adds --user-data-dir for per-session isolation). Ignore
    # anything we do not declare instead of exiting and failing the handshake.
    args, _unknown = parser.parse_known_args(argv)
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.new_token:
        from catia_mcp.http_transport import new_token

        print(new_token())
        return
    if args.http:
        from catia_mcp.http_transport import check_bind

        check_bind(args.host, args.token)
        asyncio.run(CATIAMCPServer().run_http(args.host, args.port, args.token))
        return
    asyncio.run(CATIAMCPServer().run())


if __name__ == "__main__":
    main()
