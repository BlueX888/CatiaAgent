/**
 * Azure/OpenAI-compatible endpoints can require `items` whenever a schema node
 * allows `array` (including union types like `type: ["string", "array"]`).
 * Moonshot/Kimi also requires explicit `type` on literal `enum`/`const` nodes.
 *
 * CATIA contracts are generated with local `$defs`/`$ref`. The OpenAI tool
 * path does not accept those keywords, so they are inlined here. The inlining
 * is local-only: an unresolved or remote reference throws instead of being
 * silently dropped, and the server-side schema stays authoritative.
 */
export function normalizeOpenAISchema(schema: Record<string, unknown>): Record<string, unknown> {
  const defs = isRecord(schema.$defs) ? schema.$defs : undefined;
  const normalized = normalizeOpenAISchemaNode(schema, defs, new Set());
  if (isRecord(normalized)) {
    delete normalized.$defs;
    delete normalized.$schema;
    delete normalized.$id;
    return normalized;
  }
  return normalized as Record<string, unknown>;
}

function normalizeOpenAISchemaNode(
  node: unknown,
  defs: Record<string, unknown> | undefined,
  refStack: Set<string>,
): unknown {
  if (Array.isArray(node)) {
    return node.map((item) => normalizeOpenAISchemaNode(item, defs, refStack));
  }
  if (!isRecord(node)) {
    return node;
  }

  const ref = typeof node.$ref === "string" ? node.$ref : undefined;
  if (ref) {
    const match = /^#\/\$defs\/([^/]+)$/.exec(ref);
    if (!match) {
      throw new Error(`OpenAI schema normalization cannot resolve non-local $ref: ${ref}`);
    }
    const name = match[1]!;
    if (!defs || !(name in defs)) {
      throw new Error(`OpenAI schema normalization cannot resolve $ref: ${ref}`);
    }
    if (refStack.has(name)) return {};
    const nextStack = new Set(refStack);
    nextStack.add(name);
    const resolved = normalizeOpenAISchemaNode(defs[name], defs, nextStack);
    if (!isRecord(resolved)) return resolved;
    const merged: Record<string, unknown> = { ...resolved };
    for (const metaKey of ["description", "title", "default"] as const) {
      if (metaKey in node) merged[metaKey] = node[metaKey];
    }
    return normalizeOpenAISchemaNode(merged, defs, refStack);
  }

  const normalized: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(node)) {
    if (key === "$defs" || key === "$schema" || key === "$id") continue;
    normalized[key] = normalizeOpenAISchemaNode(value, defs, refStack);
  }

  if (!("type" in normalized)) {
    const inferredType = inferLiteralSchemaType(normalized);
    if (inferredType) {
      normalized.type = inferredType;
    }
  }

  const typeField = normalized.type;
  const allowsArray = typeField === "array"
    || (Array.isArray(typeField) && typeField.includes("array"));
  if (allowsArray && !("items" in normalized)) {
    normalized.items = {};
  }

  return normalized;
}

function inferLiteralSchemaType(node: Record<string, unknown>): string | undefined {
  if ("const" in node) {
    return schemaTypeForLiteral(node.const);
  }

  const enumValues = node.enum;
  if (!Array.isArray(enumValues) || enumValues.length === 0) {
    return undefined;
  }

  const types = enumValues.map(schemaTypeForLiteral);
  if (types.some((type) => !type)) {
    return undefined;
  }

  const uniqueTypes = new Set(types);
  if (uniqueTypes.size === 1) {
    return types[0];
  }

  if (uniqueTypes.size === 2 && uniqueTypes.has("integer") && uniqueTypes.has("number")) {
    return "number";
  }

  return undefined;
}

function schemaTypeForLiteral(value: unknown): string | undefined {
  if (typeof value === "string") {
    return "string";
  }
  if (typeof value === "boolean") {
    return "boolean";
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return Number.isInteger(value) ? "integer" : "number";
  }
  if (value === null) {
    return "null";
  }
  if (Array.isArray(value)) {
    return "array";
  }
  if (isRecord(value)) {
    return "object";
  }
  return undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}
