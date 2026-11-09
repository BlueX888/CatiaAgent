import { createHash } from "node:crypto";
import { Ajv2020, type ErrorObject, type ValidateFunction } from "ajv/dist/2020.js";
import {
  CATIA_CONTRACT_SCHEMAS,
  type CatiaContractId,
} from "./generated/catiaContracts.js";

export type { CatiaContractId } from "./generated/catiaContracts.js";

export type CatiaContractMode = "legacy" | "shadow" | "active";

export type CatiaContractIdentity = {
  mode: CatiaContractMode;
  version?: string;
  digest?: string;
  allowedVersions?: string[];
  allowedDigests?: string[];
};

export type CatiaContractIssue = {
  path: string;
  code: string;
  message: string;
};

export type CatiaContractValidation =
  | { ok: true }
  | { ok: false; issues: CatiaContractIssue[] };

export type CatiaContractRoute =
  | { decision: "legacy" }
  | { decision: "shadow"; validation: CatiaContractValidation; contractId: CatiaContractId }
  | { decision: "ajv"; validation: CatiaContractValidation; contractId: CatiaContractId }
  | { decision: "reject"; reasons: string[]; contractId?: string };

const schemas = CATIA_CONTRACT_SCHEMAS as unknown as Record<string, Record<string, unknown>>;

/** Versions published with the generated schemas; an active route must match. */
export const CATIA_CONTRACT_VERSIONS: Record<CatiaContractId, string> = {
  "intent-draft": "cad-intent-draft/1",
  "compile-intent-input": "catia-compile-intent-input/1",
  "edit-plan": "catia-edit-plan/1",
  "apply-edit-plan-input": "catia-apply-edit-plan-input/1",
  "build-result": "catia-build-result/1",
  "feature-requirements": "catia-feature-requirements/1",
  "cad-support-inconclusive-summary": "catia-cad-support/1",
  "cad-support-volume-diagnostics": "catia-cad-support/1",
  "run-context": "catia-run-context/1",
};

const routing: Record<string, CatiaContractIdentity> = Object.fromEntries(
  (Object.keys(CATIA_CONTRACT_VERSIONS) as CatiaContractId[]).map((id) => [
    id,
    { mode: "shadow" as const, version: CATIA_CONTRACT_VERSIONS[id] },
  ]),
);

const compiled = new Map<string, ValidateFunction>();
const differences: Array<{ contractId: string; issues: CatiaContractIssue[] }> = [];
const MAX_DIFFERENCES = 20;

/**
 * Pydantic emits `discriminator` as an annotation. Ajv does not implement the
 * OpenAPI discriminator extension; the complete oneOf/const assertions stay.
 * This converter removes only that annotation and nothing else.
 */
export function withoutDiscriminator(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(withoutDiscriminator);
  if (value !== null && typeof value === "object") {
    const source = value as Record<string, unknown>;
    const result: Record<string, unknown> = {};
    for (const [key, item] of Object.entries(source)) {
      if (key === "discriminator") continue;
      result[key] = withoutDiscriminator(item);
    }
    return result;
  }
  return value;
}

export function stableSchemaText(schema: unknown): string {
  return JSON.stringify(withoutDiscriminator(schema), Object.keys(schema as object).sort());
}

export function schemaDigest(contractId: CatiaContractId): string {
  const schema = schemas[contractId];
  if (!schema) throw new Error(`unknown contract ${contractId}`);
  return createHash("sha256").update(stableSchemaText(schema)).digest("hex");
}

export function contractSchemaFor(contractId: CatiaContractId): Record<string, unknown> {
  const schema = schemas[contractId];
  if (!schema) throw new Error(`unknown contract ${contractId}`);
  return withoutDiscriminator(schema) as Record<string, unknown>;
}

export function nonLocalRefs(value: unknown): string[] {
  const found: string[] = [];
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      node.forEach(walk);
    } else if (node !== null && typeof node === "object") {
      for (const [key, item] of Object.entries(node as Record<string, unknown>)) {
        if (key === "$ref" && typeof item === "string" && !item.startsWith("#/")) found.push(item);
        else walk(item);
      }
    }
  };
  walk(value);
  return found;
}

function ajv(): Ajv2020 {
  // No loadSchema hook: a remote or unknown $ref must fail compilation, never
  // be fetched over the network.
  return new Ajv2020({
    strict: true,
    allErrors: true,
    coerceTypes: false,
    useDefaults: false,
    removeAdditional: false,
    validateFormats: false,
  });
}

export function compileCatiaContract(contractId: CatiaContractId): ValidateFunction {
  const cached = compiled.get(contractId);
  if (cached) return cached;
  const validate = ajv().compile(contractSchemaFor(contractId));
  compiled.set(contractId, validate);
  return validate;
}

function issuesFrom(errors: ErrorObject[] | null | undefined): CatiaContractIssue[] {
  return (errors ?? []).map((error) => ({
    path: error.instancePath ? `$${error.instancePath}` : "$",
    code: error.keyword,
    message: error.message ?? "invalid value",
  }));
}

export function validateCatiaContract(
  contractId: CatiaContractId,
  input: unknown,
): CatiaContractValidation {
  const validate = compileCatiaContract(contractId);
  return validate(input) ? { ok: true } : { ok: false, issues: issuesFrom(validate.errors) };
}

export function catiaContractShadowDifferences(): Array<{ contractId: string; issues: CatiaContractIssue[] }> {
  return differences.map((item) => ({ contractId: item.contractId, issues: [...item.issues] }));
}

export function clearCatiaContractShadowDifferences(): void {
  differences.length = 0;
}

export function configureCatiaContractRouting(
  overrides: Partial<Record<CatiaContractId, Partial<CatiaContractIdentity>>>,
): void {
  for (const [contractId, override] of Object.entries(overrides)) {
    const current = routing[contractId];
    if (!current) throw new Error(`unknown contract ${contractId}`);
    routing[contractId] = { ...current, ...override, mode: override?.mode ?? current.mode };
  }
}

export function resetCatiaContractRouting(): void {
  for (const id of Object.keys(CATIA_CONTRACT_VERSIONS) as CatiaContractId[]) {
    routing[id] = { mode: "shadow", version: CATIA_CONTRACT_VERSIONS[id] };
  }
}

export function routeCatiaContract(
  contractId: CatiaContractId,
  input: unknown,
  identity?: Partial<CatiaContractIdentity>,
): CatiaContractRoute {
  const configured = routing[contractId];
  if (!configured) return { decision: "reject", reasons: [`unknown contract ${contractId}`], contractId };
  const effective: CatiaContractIdentity = { ...configured, ...identity };
  if (effective.mode === "legacy") return { decision: "legacy" };
  if (effective.mode === "shadow") {
    const validation = validateCatiaContract(contractId, input);
    if (!validation.ok) {
      if (differences.length >= MAX_DIFFERENCES) differences.shift();
      differences.push({ contractId, issues: validation.issues.slice(0, 10) });
    }
    return { decision: "shadow", validation, contractId };
  }
  const publishedVersion = CATIA_CONTRACT_VERSIONS[contractId];
  const allowedVersions = effective.allowedVersions ?? [publishedVersion];
  const digest = effective.digest;
  const computedDigest = schemaDigest(contractId);
  const allowedDigests = effective.allowedDigests ?? [computedDigest];
  if (!effective.version || !allowedVersions.includes(effective.version)
    || effective.version !== publishedVersion) {
    return { decision: "reject", reasons: ["unsupported contract version"], contractId };
  }
  if (!digest || !allowedDigests.includes(digest) || digest !== computedDigest) {
    return { decision: "reject", reasons: ["unknown contract digest"], contractId };
  }
  return { decision: "ajv", validation: validateCatiaContract(contractId, input), contractId };
}

/** Only the editing tools with a complete exported input schema are routed. */
export function routeCatiaToolInput(toolName: string, input: unknown): CatiaContractRoute | undefined {
  if (toolName === "catia_compile_intent") return routeCatiaContract("compile-intent-input", input);
  if (toolName === "catia_apply_edit_plan") return routeCatiaContract("apply-edit-plan-input", input);
  return undefined;
}
