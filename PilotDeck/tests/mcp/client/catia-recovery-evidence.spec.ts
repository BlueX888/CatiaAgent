import assert from "node:assert/strict";
import test from "node:test";

import type { AgentTranscriptWriter } from "../../../src/session/transcript/TranscriptWriter.js";
import { SessionMetadataStore } from "../../../src/session/metadata/SessionMetadataStore.js";
import { McpRuntime } from "../../../src/mcp/runtime/McpRuntime.js";
import {
  projectCatiaRecovery,
  type CatiaRecoveryRecord,
} from "../../../src/tool/protocol/catiaRunRecovery.js";

const HASH_A = "a".repeat(64);
const HASH_B = "b".repeat(64);

function record(overrides: Partial<CatiaRecoveryRecord> = {}): CatiaRecoveryRecord {
  return {
    operationId: "op-1",
    planHash: HASH_A,
    state: "completed",
    modified: true,
    evidence: ["journal: verified"],
    observedAt: "2026-11-09T07:00:00Z",
    ...overrides,
  };
}

test("a completed record with effect evidence projects completed", () => {
  const projection = projectCatiaRecovery({ records: [record()] });
  assert.equal(projection.state, "completed");
  assert.equal(projection.modified, true);
  assert.equal(projection.retryAllowed, false);
});

test("completion without effect evidence stays unknown", () => {
  for (const recordValue of [record({ evidence: [] }), record({ modified: undefined }),
    record({ modified: null })]) {
    const projection = projectCatiaRecovery({ records: [recordValue] });
    assert.equal(projection.state, "unknown");
    assert.equal(projection.retryAllowed, false);
    assert.ok(projection.blockers.length > 0);
  }
});

test("an unknown operation is never rewritten as completed", () => {
  const projection = projectCatiaRecovery({
    records: [record({ state: "unknown", modified: null, evidence: [] })],
    fenceActive: true,
  });
  assert.equal(projection.state, "unknown");
  assert.equal(projection.modified, null);
  assert.equal(projection.retryAllowed, false);
  assert.match(projection.blockers[0]!, /read-only reconciliation/);
});

test("the same operation id with different hashes is a conflict", () => {
  const projection = projectCatiaRecovery({
    records: [record(), record({ planHash: HASH_B })],
  });
  assert.equal(projection.state, "conflict");
  assert.equal(projection.retryAllowed, false);
});

test("contradictory recorded states are a conflict", () => {
  const projection = projectCatiaRecovery({
    records: [record(), record({ state: "failed", modified: false })],
  });
  assert.equal(projection.state, "conflict");
});

test("rejected and failed records remain terminal observations", () => {
  assert.equal(projectCatiaRecovery({ records: [record({ state: "rejected", modified: false })] }).state, "failed");
  assert.equal(projectCatiaRecovery({ records: [record({ state: "failed", modified: false })] }).state, "failed");
});

test("no records is an explicit none, not success", () => {
  const projection = projectCatiaRecovery({ records: [] });
  assert.equal(projection.state, "none");
  assert.equal(projection.modified, null);
  assert.equal(projection.retryAllowed, false);
});

test("the runtime projects at start without sending a tool call", async () => {
  const inputs: unknown[] = [];
  const runtime = new McpRuntime([], {
    catiaRecovery: () => {
      inputs.push("called");
      return { serverId: "catia", input: { records: [record()] } };
    },
  });
  const statuses = await runtime.start();
  assert.deepEqual(statuses, []);
  assert.equal(inputs.length, 1);
  const projection = runtime.getCatiaRecovery();
  assert.equal(projection?.state, "completed");
  assert.equal(projection?.retryAllowed, false);
});

test("the recovery sidecar survives replay with a monotonic sequence", async () => {
  const calls: unknown[][] = [];
  const transcript = {
    recordSessionMetadata: async (...args: unknown[]) => { calls.push(args); },
  } as unknown as AgentTranscriptWriter;
  const store = new SessionMetadataStore({ transcript, sessionId: "s", now: () => new Date("2026-11-09T07:00:00Z") });
  const first = await store.saveCatiaRecovery(projectCatiaRecovery({ records: [record()] }));
  const second = await store.saveCatiaRecovery(projectCatiaRecovery({ records: [record({ state: "unknown", modified: null, evidence: [] })] }));
  assert.equal(first.sequence, 1);
  assert.equal(second.sequence, 2);
  assert.equal(second.projection.state, "unknown");
  assert.equal(calls.length, 2);

  const reopened = new SessionMetadataStore({ transcript, sessionId: "s" });
  reopened.restoreFromReplay(store.getSnapshot());
  assert.equal(reopened.getCatiaRecovery()?.projection.state, "unknown");
  assert.equal(reopened.getCatiaRecovery()?.sequence, 2);
});
