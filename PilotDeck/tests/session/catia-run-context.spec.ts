import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import type { AgentTranscriptWriter } from "../../src/session/transcript/TranscriptWriter.js";
import { SessionMetadataStore } from "../../src/session/metadata/SessionMetadataStore.js";
import {
  CATIA_RUN_CONTEXT_SCHEMA,
  hostCatiaRunContext,
  validateCatiaRunContext,
} from "../../src/tool/protocol/catiaRunContext.js";
import { validateCatiaContract } from "../../src/tool/protocol/catiaContractValidator.js";

const CONTRACTS = resolve(process.cwd(), "../contracts/catia/v1");

function sample(): Record<string, any> {
  return JSON.parse(readFileSync(resolve(CONTRACTS, "fixtures/run_context/sample.json"), "utf8"));
}

function store(sessionId = "sess-abc"): { store: SessionMetadataStore; calls: unknown[][] } {
  const calls: unknown[][] = [];
  const transcript = {
    recordSessionMetadata: async (...args: unknown[]) => { calls.push(args); },
  } as unknown as AgentTranscriptWriter;
  return { store: new SessionMetadataStore({ transcript, sessionId }), calls };
}

test("the host fixture validates in TypeScript and in Ajv", () => {
  const context = sample();
  const validation = validateCatiaRunContext(context);
  assert.equal(validation.ok, true);
  assert.equal(validation.ok && Object.isFrozen(validation.context), true);
  assert.equal(validateCatiaContract("run-context", context).ok, true);
});

test("missing, malformed and unknown identity fields are rejected", () => {
  const missing = sample();
  delete missing.runId;
  assert.equal(validateCatiaRunContext(missing).ok, false);

  const badDigest = sample();
  badDigest.sourceFingerprint = "not-a-digest";
  assert.equal(validateCatiaRunContext(badDigest).ok, false);

  const unknown = sample();
  unknown.permission = "admin";
  assert.equal(validateCatiaRunContext(unknown).ok, false);
  assert.equal(validateCatiaContract("run-context", unknown).ok, false);
});

test("host context is never invented when identity is missing", () => {
  assert.equal(hostCatiaRunContext({ sessionId: "s", turnId: "t", cwd: "F:\\CatiaAgent", env: {} }), null);
  const context = hostCatiaRunContext({
    sessionId: "s", turnId: "t", cwd: "F:\\CatiaAgent", operationId: "op-1",
    env: { PILOTDECK_RUN_ID: "run-1", PILOTDECK_SOURCE_FINGERPRINT: "a".repeat(64),
      PILOTDECK_VERSION: "b58e40b" },
    now: new Date("2026-11-09T07:00:00Z"),
  });
  assert.ok(context);
  assert.equal(context?.schema, CATIA_RUN_CONTEXT_SCHEMA);
  assert.equal(context?.runId, "run-1");
  assert.equal(context?.attemptId, "t");
});

test("metadata sequence is monotonic and survives replay", async () => {
  const { store: first, calls } = store();
  const context = sample();
  const one = await first.saveCatiaRunContext(validateCatiaRunContext(context).ok
    ? (validateCatiaRunContext(context) as any).context : context);
  const two = await first.saveCatiaRunContext(context as any, { stateSource: "coordinator" });
  assert.equal(one.sequence, 1);
  assert.equal(two.sequence, 2);
  assert.equal(two.stateSource, "coordinator");
  assert.equal(calls.length, 2);

  const snapshot = first.getSnapshot();
  const { store: reopened } = store();
  reopened.restoreFromReplay(snapshot);
  const restored = reopened.getCatiaRunContext();
  assert.equal(restored?.sequence, 2);
  assert.equal(restored?.context.runId, context.runId);
  const three = await reopened.saveCatiaRunContext(context as any, { stateSource: "journal" });
  assert.equal(three.sequence, 3);
  assert.equal(three.stateSource, "journal");
});

test("registry marks run-context as host-only", () => {
  const registry = JSON.parse(readFileSync(resolve(CONTRACTS, "registry.json"), "utf8")) as {
    contracts: Array<{ contract_id: string; model_fillable?: boolean; mode: string }>;
  };
  const entry = registry.contracts.find((item) => item.contract_id === "run-context");
  assert.equal(entry?.model_fillable, false);
  assert.equal(entry?.mode, "shadow");
});
