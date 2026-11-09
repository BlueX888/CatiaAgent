import assert from "node:assert/strict";
import test from "node:test";

import { createMcpToolDefinitionsFromRuntime } from "../../src/mcp/runtime/PluginToToolBridge.js";

const HEX = "a".repeat(64);

function successEnvelope(): Record<string, unknown> {
  return {
    ok: true,
    code: "BUILD_COMPLETED",
    operation_id: "op-1",
    tool: "catia_build_part",
    message: "built",
    effects: { modified: true },
    data: {
      businessState: "completed",
      planId: "sha256:" + "b".repeat(64),
      stages: [
        { stage: "requirements", status: "passed" },
        { stage: "plan", status: "passed" },
        { stage: "geometry_preflight", status: "passed" },
        { stage: "modeling", status: "passed" },
        { stage: "verification", status: "passed" },
        { stage: "editability", status: "passed" },
        { stage: "delivery", status: "completed" },
      ],
      verification: { verdict: "PASS", stepExport: { path: "C:\\out\\p.step", sha256: HEX } },
      editability: { passed: true, declaredParameterCount: 2, verifiedParameterCount: 2, sampled: false },
      delivery: {
        path: "C:\\out\\p.CATPart", sha256: HEX, reportJson: "C:\\out\\r.json",
        reportJsonSha256: HEX, stepPath: "C:\\out\\p.step",
      },
    },
    diagnostics: [],
    warnings: [],
    recovery: { rollback_supported: false, automatic_retry_allowed: false, next_actions: [] },
  };
}

function runtime(captured: Array<Record<string, unknown> | undefined>): any {
  const envelope = successEnvelope();
  const client = {
    spec: { id: "catia", transport: "stdio", concurrencySafe: true },
    callTool: async (_name: string, _args: unknown, options: Record<string, unknown> = {}) => {
      captured.push(options);
      return {
        content: [{ type: "text", text: JSON.stringify(envelope) }],
        structuredContent: envelope,
        isError: false,
      };
    },
  };
  return {
    listAllTools: async () => [{
      serverId: "catia",
      toolName: "catia_build_part",
      wireName: "mcp__catia__catia_build_part",
      description: "Build a CATIA part",
      inputSchema: { type: "object", properties: {} },
      annotations: { readOnlyHint: false, destructiveHint: true, openWorldHint: false },
    }],
    getClient: () => client,
  };
}

function context(operationDeadlineMs?: number): any {
  return {
    sessionId: "session-1",
    turnId: "turn-1",
    cwd: "F:\\CatiaAgent",
    permissionMode: "default",
    permissionContext: { cwd: "F:\\CatiaAgent" },
    ...(operationDeadlineMs !== undefined ? { operationDeadlineMs } : {}),
  };
}

test("the bridge forwards the host deadline to the MCP call", async () => {
  const captured: Array<Record<string, unknown> | undefined> = [];
  const tools = await createMcpToolDefinitionsFromRuntime(runtime(captured));
  const tool = tools.find((item) => item.name === "mcp__catia__catia_build_part");
  assert.ok(tool);
  await tool!.execute({}, context(12345));
  assert.equal(captured[0]?.operationDeadlineMs, 12345);
});

test("a call without a host deadline keeps the old call shape", async () => {
  const captured: Array<Record<string, unknown> | undefined> = [];
  const tools = await createMcpToolDefinitionsFromRuntime(runtime(captured));
  const tool = tools.find((item) => item.name === "mcp__catia__catia_build_part");
  await tool!.execute({}, context());
  assert.equal("operationDeadlineMs" in (captured[0] ?? {}), false);
});

test("the run context callback receives host-only identity when configured", async () => {
  const captured: Array<Record<string, unknown> | undefined> = [];
  const seen: unknown[] = [];
  const tools = await createMcpToolDefinitionsFromRuntime(runtime(captured), {
    onCatiaRunContext: (value) => seen.push(value),
  });
  const tool = tools.find((item) => item.name === "mcp__catia__catia_build_part");
  const previous = { ...process.env };
  process.env.PILOTDECK_RUN_ID = "run-1";
  process.env.PILOTDECK_SOURCE_FINGERPRINT = "c".repeat(64);
  process.env.PILOTDECK_VERSION = "b58e40b";
  try {
    await tool!.execute({}, context());
    assert.equal(seen.length, 1);
    assert.equal((seen[0] as { runId: string }).runId, "run-1");
  } finally {
    for (const key of ["PILOTDECK_RUN_ID", "PILOTDECK_SOURCE_FINGERPRINT", "PILOTDECK_VERSION"]) {
      if (previous[key] === undefined) delete process.env[key];
      else process.env[key] = previous[key];
    }
  }
});
