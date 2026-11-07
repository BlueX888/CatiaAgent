import assert from "node:assert/strict";
import test from "node:test";
import {
  catiaContentAgrees,
  isCatiaBuildCall,
  isCatiaBuildSuccess,
  isCatiaServerId,
  parseCatiaFailure,
  readCatiaFailure,
  unclassifiedCatiaFailure,
} from "../../../src/tool/protocol/catiaFailureStatus.js";

const verifierUnknown = {
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

const success = {
  ok: true,
  code: "BUILD_COMPLETED",
  effects: { modified: true },
  data: {
    businessState: "completed",
    verification: { verdict: "PASS", checks: [{ passed: true }] },
    editability: { passed: true },
    delivery: { path: "part.CATPart" },
  },
};

test("CATIA build detection accepts the production and per-run server ids only", () => {
  assert.equal(isCatiaServerId("catia"), true);
  assert.equal(isCatiaServerId("catia_generation"), true);
  assert.equal(isCatiaServerId("other"), false);
  assert.equal(isCatiaBuildCall("catia", "catia_build_part"), true);
  assert.equal(isCatiaBuildCall("catia_generation", "catia_build_part"), true);
  assert.equal(isCatiaBuildCall("other", "catia_build_part"), false);
  assert.equal(isCatiaBuildCall("catia", "catia_inspect_drawing"), false);
});

test("checkpoint failure facts are accepted without trusting extra fields", () => {
  const parsed = parseCatiaFailure({ ...verifierUnknown, untrusted: "x" });
  assert.equal(parsed.code, "VERIFIER_SESSION_UNKNOWN");
  assert.equal(parsed.automaticRetryAllowed, false);
  assert.equal(parsed.modified, true);
  assert.equal(parsed.sessionHealth, "unknown");

  const material = parseCatiaFailure(inconclusive);
  assert.equal(material.code, "MATERIAL_COMPARISON_INCONCLUSIVE");
  assert.equal(material.summary?.reason, "INSUFFICIENT_CONSISTENT_BOOLEANS");
});

test("contradictory or incomplete failure facts degrade to unclassified", () => {
  for (const malformed of [
    undefined,
    { ...verifierUnknown, ok: true },
    { ...verifierUnknown, recovery: { automatic_retry_allowed: true } },
    { ...verifierUnknown, data: { businessState: "session_unknown", sessionHealth: "healthy" } },
    { ...inconclusive, data: { ...inconclusive.data, failure: { ...inconclusive.data.failure, summary: { ...inconclusive.data.failure.summary, consistencyBudgetMm3: Infinity } } } },
  ]) {
    assert.deepEqual(parseCatiaFailure(malformed), unclassifiedCatiaFailure());
  }
});

test("only a complete verified build envelope is a successful result", () => {
  assert.equal(isCatiaBuildSuccess(success), true);
  assert.equal(isCatiaBuildSuccess(undefined), false);
  assert.equal(isCatiaBuildSuccess({ ...success, ok: false }), false);
  assert.equal(isCatiaBuildSuccess({ ...success, data: { ...success.data, verification: { verdict: "PASS", checks: [{ passed: false }] } } }), false);
  assert.equal(isCatiaBuildSuccess({ ...success, data: { ...success.data, editability: { passed: false } } }), false);
  assert.equal(isCatiaBuildSuccess({ ...success, effects: { modified: false } }), false);
});

const deterministicFailure = {
  ok: false,
  code: "FEATURE_BUILD_FAILED",
  data: {
    businessState: "build_failed",
    failure: { stage: "modeling", reason: "EDGE_OPERATION_FAILED: no coincident reference" },
  },
  effects: { modified: false },
  recovery: { automatic_retry_allowed: false },
};

test("a deterministic build failure keeps its reason and stays outside the unknown-session fence", () => {
  const parsed = parseCatiaFailure(deterministicFailure);
  assert.equal(parsed.code, "BUILD_FAILED");
  assert.equal(parsed.modified, false);
  assert.equal(parsed.sessionHealth, undefined);
  assert.equal(parsed.reason, "EDGE_OPERATION_FAILED: no coincident reference");
  assert.equal(readCatiaFailure("catia_build_part", { reportedCatiaStatus: parsed })?.code, "BUILD_FAILED");
  // A build that modified the document or lost its state stays unclassified.
  assert.equal(parseCatiaFailure({ ...deterministicFailure, effects: { modified: true } }).code,
    "CATIA_BUILD_UNCLASSIFIED");
  assert.equal(parseCatiaFailure({ ...deterministicFailure, data: { businessState: "session_unknown" } }).code,
    "CATIA_BUILD_UNCLASSIFIED");
});

test("requirements decisions that changed nothing do not fence the client", () => {
  const blocked = {
    ok: false,
    code: "NEEDS_CLARIFICATION",
    message: "The bore depth is ambiguous.",
    data: { decision: "needs_clarification" },
    effects: { modified: false },
    recovery: { automatic_retry_allowed: false },
  };
  const parsed = parseCatiaFailure(blocked);
  assert.equal(parsed.code, "BUILD_FAILED");
  assert.equal(parsed.modified, false);
  assert.equal(parsed.reason, "The bore depth is ambiguous.");
  assert.equal(parseCatiaFailure({ ...blocked, effects: { modified: true } }).code,
    "CATIA_BUILD_UNCLASSIFIED");
});

test("text and structured content must agree", () => {
  const content = [{ type: "text", text: JSON.stringify(verifierUnknown) }];
  assert.equal(catiaContentAgrees(content, verifierUnknown), true);
  assert.equal(catiaContentAgrees(content, inconclusive), false);
  assert.equal(catiaContentAgrees([], verifierUnknown), false);
  assert.equal(catiaContentAgrees(content, verifierUnknown, 10), false);
});

test("failure status is readable from production and per-run wire names", () => {
  const details = { reportedCatiaStatus: parseCatiaFailure(verifierUnknown) };
  assert.equal(readCatiaFailure("mcp__catia__catia_build_part", details)?.code, "VERIFIER_SESSION_UNKNOWN");
  assert.equal(readCatiaFailure("mcp__catia_generation__catia_build_part", details)?.code, "VERIFIER_SESSION_UNKNOWN");
  assert.equal(readCatiaFailure("mcp__other__catia_build_part", details), undefined);
  assert.equal(readCatiaFailure("catia_build_part", details)?.code, "VERIFIER_SESSION_UNKNOWN");
});
