import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import {
  validateCatiaContract,
  type CatiaContractId,
} from "../../../src/tool/protocol/catiaContractValidator.js";

const CONTRACTS = resolve(process.cwd(), "../contracts/catia/v1");

function fixture(name: string): Record<string, any> {
  return JSON.parse(readFileSync(resolve(CONTRACTS, "fixtures/plans", name), "utf8"));
}

function registry(): { contracts: Array<{ contract_id: string; mode: string }> } {
  return JSON.parse(readFileSync(resolve(CONTRACTS, "registry.json"), "utf8"));
}

function intent(): Record<string, any> {
  return {
    schema: "cad-intent-draft/1",
    target: { parameter: "Part1\\L", attribute: "length" },
    operation: { type: "set", amount: 200, unit: "mm", scope: "overall" },
    reference: { basis: "current" },
  };
}

function feature(): Record<string, any> {
  return {
    schema: "catia-feature-requirements/1", unit: "mm", unitEvidence: [],
    parameters: [{ name: "T", value: 8, evidence: [] }],
    features: [{ id: "base", operation: "pad", length: "T",
      sketch: { plane: "xy", profiles: [["circle", 0, 0, "T"]] } }],
  };
}

function receipt(): Record<string, any> {
  const hex = "a".repeat(64);
  return {
    ok: true, code: "BUILD_COMPLETED", operation_id: "op-1", tool: "catia_build_part",
    message: "built", effects: { modified: true },
    data: {
      businessState: "completed", planId: "sha256:" + "b".repeat(64),
      stages: [{ stage: "requirements", status: "passed" }],
      verification: { verdict: "PASS", stepExport: { path: "C:\\out\\p.step", sha256: hex } },
      editability: { passed: true, declaredParameterCount: 2, verifiedParameterCount: 2, sampled: false },
      delivery: { path: "C:\\out\\p.CATPart", sha256: hex, reportJson: "C:\\out\\r.json",
        reportJsonSha256: hex, stepPath: "C:\\out\\p.step" },
    },
    diagnostics: [], warnings: [],
    recovery: { rollback_supported: false, automatic_retry_allowed: false, next_actions: [] },
  };
}

function summary(): Record<string, any> {
  return { reason: "FUZZY_LEVELS_DISAGREE", consistencyBudgetMm3: 0.01,
    attemptCount: 2, candidateCount: 1 };
}

const CASES: Array<[CatiaContractId, string, unknown, boolean]> = [
  ["intent-draft", "minimal", { schema: "cad-intent-draft/1" }, true],
  ["intent-draft", "decimal_amount", { ...intent(), operation: { ...intent().operation, amount: 200.0 } }, true],
  ["intent-draft", "string_amount", { ...intent(), operation: { ...intent().operation, amount: "200" } }, false],
  ["intent-draft", "boolean_amount", { ...intent(), operation: { ...intent().operation, amount: true } }, false],
  ["intent-draft", "unknown_field", { ...intent(), userText: "host" }, false],
  ["compile-intent-input", "valid", { editSessionId: "s", intent: intent() }, true],
  ["compile-intent-input", "missing_intent", { editSessionId: "s" }, false],
  ["apply-edit-plan-input", "frozen_plan",
    { editSessionId: "sess0000abcd", plan: fixture("edit-plan-v1.json") }, true],
  ["apply-edit-plan-input", "boolean_value",
    { editSessionId: "sess0000abcd",
      plan: { ...fixture("edit-plan-v1.json"), changes: [{ ...fixture("edit-plan-v1.json").changes[0], newValue: true }] } },
    false],
  ["apply-edit-plan-input", "integer_valued_revision",
    { editSessionId: "sess0000abcd", plan: { ...fixture("edit-plan-v1.json"), expectedRevision: 3.0 } }, true],
  ["feature-requirements", "compact_ok", feature(), true],
  ["feature-requirements", "short_tuple",
    { ...feature(), features: [{ ...feature().features[0], sketch: { plane: "xy", profiles: [["circle", 0, 0]] } }] }, false],
  ["feature-requirements", "chamfer_angle",
    { ...feature(), features: [feature().features[0], { id: "cut", operation: "chamfer", dependsOn: ["base"],
      length: "T", angle: 30, edges: { type: "linear", direction: [1, 0, 0], through: [0, 0, 0] } }] }, false],
  ["feature-requirements", "coord_out_of_range",
    { ...feature(), features: [{ ...feature().features[0],
      sketch: { plane: "xy", profiles: [["polygon", [[1_000_001, 0], [0, 0], [0, 1]]]] } }] }, false],
  ["feature-requirements", "extra_feature_key",
    { ...feature(), features: [{ ...feature().features[0], legacy: true }] }, false],
  ["build-result", "success", receipt(), true],
  ["build-result", "missing_modified", { ...receipt(), effects: {} }, false],
  ["build-result", "string_modified", { ...receipt(), effects: { modified: "true" } }, false],
  ["cad-support-inconclusive-summary", "valid", summary(), true],
  ["cad-support-inconclusive-summary", "extra_key", { ...summary(), extra: 1 }, false],
];

test("Ajv agrees with the declared cross-language matrix", () => {
  for (const [contractId, name, payload, expected] of CASES) {
    const validation = validateCatiaContract(contractId, payload);
    assert.equal(validation.ok, expected, `${contractId}.${name}`);
  }
});

test("the registry declares the active and shadow families", () => {
  const modes = Object.fromEntries(registry().contracts.map((item) => [item.contract_id, item.mode]));
  assert.equal(modes["compile-intent-input"], "active");
  assert.equal(modes["apply-edit-plan-input"], "active");
  assert.equal(modes["edit-plan"], "shadow");
  assert.equal(modes["feature-requirements"], "shadow");
});

test("Ajv validation does not mutate the payload", () => {
  const payload = { editSessionId: "sess0000abcd", plan: fixture("edit-plan-v1.json") };
  const before = JSON.stringify(payload);
  validateCatiaContract("apply-edit-plan-input", payload);
  assert.equal(JSON.stringify(payload), before);
});
