import assert from "node:assert/strict";
import test from "node:test";
import { McpClient } from "../../src/mcp/client/McpClient.js";
import { createMcpToolDefinitionsFromRuntime } from "../../src/mcp/runtime/PluginToToolBridge.js";
import { buildToolErrorRecovery } from "../../src/tool/execution/errorRecovery.js";

const toolName = "catia_build_part";

const ctx = {
  sessionId: "s",
  turnId: "t",
  cwd: process.cwd(),
  permissionMode: "bypassPermissions" as const,
  permissionContext: {
    mode: "bypassPermissions" as const,
    cwd: process.cwd(),
    additionalWorkingDirectories: [],
    canPrompt: false,
    bypassAvailable: true,
    rules: { allow: [], deny: [], ask: [] },
  },
};

const unknown = {
  ok: false,
  code: "VERIFIER_SESSION_UNKNOWN",
  data: { businessState: "session_unknown", sessionHealth: "unknown" },
  effects: { modified: true },
  recovery: { automatic_retry_allowed: false },
};

const inconclusive = {
  ok: false,
  code: "MATERIAL_COMPARISON_INCONCLUSIVE",
  data: {
    businessState: "verification_failed",
    failure: {
      stage: "verification",
      classification: "material_comparison_inconclusive",
      summary: {
        reason: "INSUFFICIENT_CONSISTENT_BOOLEANS",
        consistencyBudgetMm3: 0.01,
        attemptCount: 10,
        candidateCount: 0,
      },
    },
  },
  effects: { modified: true },
  recovery: { automatic_retry_allowed: false },
};

function fakeClient(action: () => unknown) {
  const client = new McpClient({ id: "catia_generation", transport: "stdio", command: "unused" });
  const state = client as unknown as {
    start(): Promise<void>;
    client: unknown;
  };
  state.start = async () => {};
  state.client = {
    callTool: async () => action(),
    close: async () => {},
    listTools: async () => ({ tools: [] }),
  };
  return client;
}

async function execute(result: unknown, textOverride?: string) {
  const wireName = `mcp__catia_generation__${toolName}`;
  const client = fakeClient(() => result);
  const runtime = {
    async listAllTools() {
      return [{
        serverId: "catia_generation",
        toolName,
        wireName,
        description: "fixture",
        inputSchema: { type: "object", properties: {} },
      }];
    },
    getClient() { return client; },
  };
  const [tool] = await createMcpToolDefinitionsFromRuntime(runtime as never);
  return tool!.execute({}, ctx as never);
}

function envelope(structured: unknown, isError: boolean, textOverride?: string) {
  return {
    content: [{ type: "text", text: textOverride ?? JSON.stringify(structured) }],
    structuredContent: structured,
    isError,
  };
}

for (const [envelopeValue, code] of [[unknown, "VERIFIER_SESSION_UNKNOWN"], [inconclusive, "MATERIAL_COMPARISON_INCONCLUSIVE"]] as const) {
  test(`CATIA ${code} becomes a bounded, typed tool error`, async () => {
    const error = await execute(envelope({ ...envelopeValue, extra: "untrusted ".repeat(10000) }, true))
      .then(() => undefined, (value: unknown) => value as { details?: Record<string, unknown> });
    assert.ok(error);
    const status = error.details?.reportedCatiaStatus as Record<string, unknown>;
    assert.equal(status.code, code);
    assert.equal(status.automaticRetryAllowed, false);

    const recovery = buildToolErrorRecovery({
      code: "tool_execution_failed",
      toolName: `mcp__catia_generation__${toolName}`,
      message: "CATIA build failed",
      cwd: process.cwd(),
      permissionMode: "bypassPermissions",
      details: error.details,
    });
    assert.match(recovery.message, new RegExp(`^CATIA_STATUS .*${code}`));
    assert.doesNotMatch(recovery.message, /untrusted/);
    assert.equal(recovery.advice.avoidRetryReason, "Automatic retry is not allowed until the reported status is resolved.");
  });
}

for (const malformed of [
  undefined,
  { ...unknown, ok: true },
  { ...unknown, recovery: { automatic_retry_allowed: true } },
  { ...unknown, data: { businessState: "session_unknown", sessionHealth: "healthy" } },
]) {
  test("contradictory or missing CATIA failure facts fail closed", async () => {
    const error = await execute(envelope(malformed, true))
      .then(() => undefined, (value: unknown) => value as { details?: Record<string, unknown> });
    assert.ok(error);
    assert.equal((error.details?.reportedCatiaStatus as Record<string, unknown>).code, "CATIA_BUILD_UNCLASSIFIED");
  });
}

test("contradictory CATIA success cannot turn a failure into success", async () => {
  await assert.rejects(execute(envelope(unknown, false)));
});

test("valid CATIA completion still succeeds", async () => {
  const success = {
    ok: true,
    code: "BUILD_COMPLETED",
    data: {
      businessState: "completed",
      verification: { verdict: "PASS", checks: [{ passed: true }] },
      editability: { passed: true },
      delivery: { path: "part.CATPart" },
    },
    effects: { modified: true },
  };
  const output = await execute(envelope(success, false));
  assert.equal((output.data as Record<string, unknown>).code, "BUILD_COMPLETED");
});

test("conflicting CATIA success text and structured envelope fail closed", async () => {
  const success = {
    ok: true,
    code: "BUILD_COMPLETED",
    data: {
      businessState: "completed",
      verification: { verdict: "PASS", checks: [{ passed: true }] },
      editability: { passed: true },
      delivery: { path: "part.CATPart" },
    },
    effects: { modified: true },
  };
  await assert.rejects(execute(envelope(success, false, JSON.stringify(unknown))));
});

test("an oversized CATIA result is rejected before previews truncate it", async () => {
  await assert.rejects(execute(envelope({ ...unknown, padding: "x".repeat(200_001) }, true)));
});
