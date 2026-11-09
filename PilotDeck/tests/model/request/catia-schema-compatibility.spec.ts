import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import test from "node:test";

import { normalizeGoogleToolSchema } from "../../../src/model/providers/google/schema.js";
import { normalizeOpenAISchema } from "../../../src/model/providers/openai/schema.js";

const SCHEMA_DIR = resolve(process.cwd(), "../contracts/catia/v1/schema");

type Json = Record<string, unknown>;

function load(name: string): Json {
  return JSON.parse(readFileSync(resolve(SCHEMA_DIR, `${name}.json`), "utf8")) as Json;
}

function walk(node: unknown, visit: (value: Json) => void): void {
  if (Array.isArray(node)) {
    node.forEach((item) => walk(item, visit));
  } else if (node !== null && typeof node === "object") {
    visit(node as Json);
    Object.values(node as Json).forEach((value) => walk(value, visit));
  }
}

function keysNamed(schema: unknown, key: string): boolean {
  let found = false;
  walk(schema, (node) => {
    if (key in node) found = true;
  });
  return found;
}

function literalValues(schema: unknown): Set<string> {
  const values = new Set<string>();
  walk(schema, (node) => {
    if (Array.isArray(node.enum)) node.enum.forEach((item) => values.add(JSON.stringify(item)));
    if ("const" in node) values.add(JSON.stringify(node.const));
  });
  return values;
}

function propertyNames(schema: Json): Set<string> {
  const properties = schema.properties;
  return new Set(Object.keys(properties && typeof properties === "object" ? properties as Json : {}));
}

function requiredNames(schema: Json): string[] {
  return Array.isArray(schema.required) ? schema.required.map(String) : [];
}

const CONTRACTS = ["compile-intent-input", "apply-edit-plan-input", "feature-requirements"];

test("OpenAI normalization inlines every local CATIA $ref", () => {
  for (const name of CONTRACTS) {
    const source = load(name);
    const before = JSON.stringify(source);
    const normalized = normalizeOpenAISchema(source);
    assert.equal(JSON.stringify(source), before, `${name} input mutated`);
    assert.equal(keysNamed(normalized, "$ref"), false, `${name} still has $ref`);
    assert.equal(keysNamed(normalized, "$defs"), false, `${name} still has $defs`);
    assert.deepEqual(propertyNames(normalized), propertyNames(source), name);
    assert.deepEqual(requiredNames(normalized), requiredNames(source), name);
    assert.deepEqual(literalValues(normalized), literalValues(source), name);
  }
});

test("OpenAI normalization keeps array nodes valid", () => {
  for (const name of CONTRACTS) {
    const normalized = normalizeOpenAISchema(load(name));
    walk(normalized, (node) => {
      const type = node.type;
      if (type === "array" || (Array.isArray(type) && type.includes("array"))) {
        assert.ok("items" in node, `${name} array without items`);
      }
    });
  }
});

test("Google normalization removes refs and preserves parameters, required and enums", () => {
  for (const name of CONTRACTS) {
    const source = load(name);
    const before = JSON.stringify(source);
    const normalized = normalizeGoogleToolSchema(source) as Json;
    assert.equal(JSON.stringify(source), before, `${name} input mutated`);
    assert.equal(keysNamed(normalized, "$ref"), false, `${name} still has $ref`);
    assert.equal(keysNamed(normalized, "$defs"), false, `${name} still has $defs`);
    assert.deepEqual(propertyNames(normalized), propertyNames(source), name);
    assert.deepEqual(requiredNames(normalized), requiredNames(source), name);
    assert.deepEqual(literalValues(normalized), literalValues(source), name);
  }
});

test("Google drops range/length keywords by design and keeps the name/required/enum contract", () => {
  const source = load("feature-requirements");
  assert.equal(keysNamed(source, "maxLength"), true);
  const normalized = normalizeGoogleToolSchema(source);
  assert.equal(keysNamed(normalized, "maxLength"), false);
  assert.deepEqual(propertyNames(normalized as Json), propertyNames(source));
  assert.deepEqual(requiredNames(normalized as Json), requiredNames(source));
});

test("unresolvable remote refs fail instead of being silently dropped", () => {
  assert.throws(
    () => normalizeOpenAISchema({ type: "object", properties: { x: { $ref: "https://example.com/x.json" } } }),
    /non-local \$ref/,
  );
});
