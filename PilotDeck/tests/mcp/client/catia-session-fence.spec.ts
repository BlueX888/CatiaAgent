import assert from "node:assert/strict";
import test from "node:test";
import { McpClient } from "../../../src/mcp/client/McpClient.js";

function fixture(call: (...args: any[]) => Promise<any>) {
  const owner = new McpClient({ id: "catia_generation", transport: "stdio", command: "unused" });
  let closes = 0;
  const state = owner as unknown as {
    client: unknown;
    start(): Promise<void>;
    reconnect(): Promise<void>;
  };
  state.client = {
    callTool: call,
    close: async () => { closes++; },
    listTools: async () => ({ tools: [] }),
  };
  state.start = async () => {};
  state.reconnect = async () => {};
  return { owner, closes: () => closes };
}

test("build deadline is metadata and a failed transport fences this client without replay", async () => {
  const seen: any[] = [];
  const { owner, closes } = fixture(async request => { seen.push(request); throw new Error("session expired"); });
  await assert.rejects(
    owner.callTool("catia_build_part", { value: 1 }, { operationDeadlineMs: 123 }),
    /may have executed|not replayed/,
  );
  assert.deepEqual(seen, [{
    name: "catia_build_part",
    arguments: { value: 1 },
    _meta: { "pilotdeck/operationDeadlineMs": 123 },
  }]);
  await assert.rejects(owner.callTool("catia_inspect_drawing", {}), /fenced/);
  await assert.rejects(owner.close(), /unresolved/);
  assert.equal(closes(), 0);
});

test("an invalid deadline is rejected before the build starts", async () => {
  let calls = 0;
  const { owner } = fixture(async () => { calls++; return { content: [], isError: false }; });
  await assert.rejects(
    owner.callTool("catia_build_part", {}, { operationDeadlineMs: -1 }),
    /Invalid CATIA operation deadline/,
  );
  assert.equal(calls, 0);
});

test("active build blocks calls, discovery and close; an ambiguous reply keeps the fence", async () => {
  let finish: ((value: unknown) => void) | undefined;
  const { owner, closes } = fixture(() => new Promise(resolve => { finish = resolve; }));
  const build = owner.callTool("catia_build_part", {});
  for (let index = 0; index < 20 && finish === undefined; index++) {
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal(typeof finish, "function");
  await assert.rejects(owner.callTool("catia_inspect_drawing", {}), /busy/);
  await assert.rejects(owner.listTools(), /active or its outcome is unknown/);
  await assert.rejects(owner.close(), /active or unresolved/);
  finish!({ content: [], isError: false });
  await build;
  await assert.rejects(owner.callTool("catia_build_part", {}), /fenced/);
  await assert.rejects(owner.close(), /unresolved/);
  assert.equal(closes(), 0);
});
