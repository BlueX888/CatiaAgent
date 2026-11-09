import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import {
  CATIA_CONTRACT_VERSIONS,
  clearCatiaContractShadowDifferences,
  compileCatiaContract,
  configureCatiaContractRouting,
  contractSchemaFor,
  nonLocalRefs,
  resetCatiaContractRouting,
  routeCatiaContract,
  routeCatiaToolInput,
  schemaDigest,
  validateCatiaContract,
  withoutDiscriminator,
  type CatiaContractId,
} from "../../../src/tool/protocol/catiaContractValidator.js";

const PLANS = resolve(process.cwd(), "../contracts/catia/v1/fixtures/plans");

function editPlan(): unknown {
  return JSON.parse(readFileSync(resolve(PLANS, "edit-plan-v1.json"), "utf8"));
}

function validIntent(): Record<string, unknown> {
  return {
    editSessionId: "sess0000abcd",
    intent: {
      schema: "cad-intent-draft/1",
      text: "length set 200 mm",
      target: { parameter: "Part1\\L", attribute: "length" },
      operation: { type: "set", amount: 200, unit: "mm", scope: "overall" },
      reference: { basis: "current" },
    },
  };
}

function featureRequirements(): Record<string, unknown> {
  return {
    schema: "catia-feature-requirements/1",
    unit: "mm",
    unitEvidence: [],
    parameters: [{ name: "T", value: 8, evidence: [] }],
    features: [{
      id: "base", operation: "pad", length: "T",
      sketch: { plane: "xy", profiles: [["circle", 0, 0, "T"]] },
    }],
  };
}

function hasKey(value: unknown, key: string): boolean {
  if (Array.isArray(value)) return value.some((item) => hasKey(item, key));
  if (value !== null && typeof value === "object") {
    return Object.entries(value as Record<string, unknown>)
      .some(([name, item]) => name === key || hasKey(item, key));
  }
  return false;
}

test("every generated contract compiles in strict Draft 2020-12 mode", () => {
  for (const contractId of Object.keys(CATIA_CONTRACT_VERSIONS) as CatiaContractId[]) {
    assert.doesNotThrow(() => compileCatiaContract(contractId), contractId);
  }
});

test("discriminator annotations are removed but oneOf/const stay", () => {
  const schema = contractSchemaFor("feature-requirements");
  assert.equal(hasKey(schema, "discriminator"), false);
  assert.equal(hasKey(schema, "oneOf"), true);
  assert.equal(hasKey(schema, "const"), true);
  assert.deepEqual(nonLocalRefs(schema), []);
});

test("positive and negative intent payloads behave structurally", () => {
  assert.equal(validateCatiaContract("compile-intent-input", validIntent()).ok, true);
  const broken = validIntent();
  (broken.intent as Record<string, unknown>).operation = { type: "set", amount: "200", unit: "mm" };
  assert.equal(validateCatiaContract("compile-intent-input", broken).ok, false);
  const unknownField = validIntent();
  (unknownField.intent as Record<string, unknown>).userText = "host";
  assert.equal(validateCatiaContract("compile-intent-input", unknownField).ok, false);
});

test("the frozen edit plan validates and boolean numbers do not", () => {
  assert.equal(validateCatiaContract("apply-edit-plan-input", {
    editSessionId: "sess0000abcd", plan: editPlan(),
  }).ok, true);
  const plan = editPlan() as Record<string, any>;
  plan.changes[0].newValue = true;
  assert.equal(validateCatiaContract("apply-edit-plan-input", {
    editSessionId: "sess0000abcd", plan,
  }).ok, false);
});

test("compact prefixItems, unions, const and numeric ranges are enforced", () => {
  assert.equal(validateCatiaContract("feature-requirements", featureRequirements()).ok, true);
  const badTuple = featureRequirements() as Record<string, any>;
  badTuple.features[0].sketch.profiles = [["circle", 0, 0]];
  assert.equal(validateCatiaContract("feature-requirements", badTuple).ok, false);

  const badConst = featureRequirements() as Record<string, any>;
  badConst.features.push({ id: "cut", operation: "chamfer", dependsOn: ["base"], length: "T",
    angle: 30, edges: { type: "linear", direction: [1, 0, 0], through: [0, 0, 0] } });
  assert.equal(validateCatiaContract("feature-requirements", badConst).ok, false);

  const badRange = featureRequirements() as Record<string, any>;
  badRange.features[0].sketch.profiles = [["polygon", [[1_000_001, 0], [0, 0], [0, 1]]]];
  assert.equal(validateCatiaContract("feature-requirements", badRange).ok, false);
});

test("validation never mutates the caller's input", () => {
  const payload = validIntent();
  const before = JSON.stringify(payload);
  validateCatiaContract("compile-intent-input", payload);
  assert.equal(JSON.stringify(payload), before);
  const planPayload = { editSessionId: "sess0000abcd", plan: editPlan() };
  const planBefore = JSON.stringify(planPayload);
  validateCatiaContract("apply-edit-plan-input", planPayload);
  assert.equal(JSON.stringify(planPayload), planBefore);
});

test("shadow routing records differences and never rejects", () => {
  resetCatiaContractRouting();
  clearCatiaContractShadowDifferences();
  const broken = validIntent();
  (broken.intent as Record<string, unknown>).operation = { type: "set", amount: "200", unit: "mm" };
  const route = routeCatiaContract("compile-intent-input", broken);
  assert.equal(route.decision, "shadow");
  assert.equal(route.decision === "shadow" && route.validation.ok, false);
});

test("legacy routing skips Ajv", () => {
  configureCatiaContractRouting({ "compile-intent-input": { mode: "legacy" } });
  try {
    assert.deepEqual(routeCatiaContract("compile-intent-input", {}), { decision: "legacy" });
  } finally {
    resetCatiaContractRouting();
  }
});

test("active routing requires a matching version and digest", () => {
  const digest = schemaDigest("intent-draft");
  const active = {
    mode: "active" as const,
    version: CATIA_CONTRACT_VERSIONS["intent-draft"],
    digest,
    allowedDigests: [digest],
  };
  configureCatiaContractRouting({ "intent-draft": active });
  try {
    const good = routeCatiaContract("intent-draft", { schema: "cad-intent-draft/1" });
    assert.equal(good.decision, "ajv");
    assert.equal(good.decision === "ajv" && good.validation.ok, true);
    const bad = routeCatiaContract("intent-draft", { schema: "wrong" });
    assert.equal(bad.decision, "ajv");
    assert.equal(bad.decision === "ajv" && bad.validation.ok, false);
    const unknownDigest = routeCatiaContract("intent-draft", { schema: "cad-intent-draft/1" }, {
      mode: "active", version: CATIA_CONTRACT_VERSIONS["intent-draft"], digest: "f".repeat(64),
    });
    assert.equal(unknownDigest.decision, "reject");
    const wrongVersion = routeCatiaContract("intent-draft", { schema: "cad-intent-draft/1" }, {
      mode: "active", version: "v0", digest, allowedDigests: [digest],
    });
    assert.equal(wrongVersion.decision, "reject");
  } finally {
    resetCatiaContractRouting();
  }
});

test("unknown contracts reject and non-CATIA tools are not routed", () => {
  assert.equal(routeCatiaContract("nope" as CatiaContractId, {}).decision, "reject");
  assert.equal(routeCatiaToolInput("catia_export", {}), undefined);
  assert.equal(routeCatiaToolInput("other_tool", {}), undefined);
  assert.equal(routeCatiaToolInput("catia_compile_intent", validIntent())?.decision, "shadow");
});

test("discriminator removal is import-safe for non-schema values", () => {
  assert.deepEqual(withoutDiscriminator({ a: 1, discriminator: { x: 1 }, b: [1, 2] }), { a: 1, b: [1, 2] });
  assert.equal(withoutDiscriminator("text"), "text");
});
