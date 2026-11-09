/**
 * Host-owned CATIA run identity.
 *
 * This record links platform sessions/turns to CATIA operation ids, source and
 * contract versions. It is metadata only: it never grants permissions, is not
 * exposed as a tool argument, and never replaces the edit coordinator or its
 * journal as the execution truth.
 */

export const CATIA_RUN_CONTEXT_SCHEMA = "catia-run-context/1";

export type CatiaRunContext = {
  schema: typeof CATIA_RUN_CONTEXT_SCHEMA;
  runId: string;
  attemptId: string;
  sourceFingerprint: string;
  codeVersion: string;
  contractVersion: string;
  modelId?: string;
  promptId?: string;
  workingDirectory: string;
  sessionId?: string;
  operationId?: string;
  deadlineMs?: number;
  createdAt: string;
};

export type CatiaRunContextValidation =
  | { ok: true; context: CatiaRunContext }
  | { ok: false; issues: Array<{ path: string; message: string }> };

export type HostCatiaRunContextInput = {
  sessionId: string;
  turnId: string;
  cwd: string;
  operationId?: string;
  deadlineMs?: number;
  env?: NodeJS.ProcessEnv;
  now?: Date;
};

const FINGERPRINT = /^[a-f0-9]{64}$/;
const FIELDS = new Set([
  "schema", "runId", "attemptId", "sourceFingerprint", "codeVersion", "contractVersion",
  "modelId", "promptId", "workingDirectory", "sessionId", "operationId", "deadlineMs", "createdAt",
]);

function text(value: unknown, max = 4096): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max;
}

export function validateCatiaRunContext(value: unknown): CatiaRunContextValidation {
  const issues: Array<{ path: string; message: string }> = [];
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return { ok: false, issues: [{ path: "$", message: "must be an object" }] };
  }
  const record = value as Record<string, unknown>;
  for (const key of Object.keys(record)) {
    if (!FIELDS.has(key)) issues.push({ path: `$.${key}`, message: "unknown field" });
  }
  if (record.schema !== CATIA_RUN_CONTEXT_SCHEMA) issues.push({ path: "$.schema", message: "invalid schema" });
  for (const field of ["runId", "attemptId", "codeVersion", "contractVersion", "workingDirectory", "createdAt"]) {
    if (!text(record[field])) issues.push({ path: `$.${field}`, message: "must be a non-empty string" });
  }
  if (typeof record.sourceFingerprint !== "string" || !FINGERPRINT.test(record.sourceFingerprint)) {
    issues.push({ path: "$.sourceFingerprint", message: "must be 64 lowercase hex characters" });
  }
  for (const field of ["modelId", "promptId", "sessionId", "operationId"]) {
    if (record[field] !== undefined && !text(record[field])) {
      issues.push({ path: `$.${field}`, message: "must be a non-empty string when present" });
    }
  }
  if (record.deadlineMs !== undefined
    && (!Number.isSafeInteger(record.deadlineMs) || (record.deadlineMs as number) < 0)) {
    issues.push({ path: "$.deadlineMs", message: "must be a non-negative safe integer" });
  }
  if (issues.length > 0) return { ok: false, issues };
  return { ok: true, context: Object.freeze({ ...(record as CatiaRunContext) }) };
}

/**
 * Build a context from host-owned inputs. Missing host identity yields null:
 * the bridge records nothing rather than inventing a run id or fingerprint.
 */
export function hostCatiaRunContext(input: HostCatiaRunContextInput): CatiaRunContext | null {
  const env = input.env ?? process.env;
  const runId = env.PILOTDECK_RUN_ID;
  const sourceFingerprint = env.PILOTDECK_SOURCE_FINGERPRINT;
  const codeVersion = env.PILOTDECK_VERSION ?? env.PILOTDECK_CODE_VERSION;
  if (!text(runId) || !text(sourceFingerprint) || !text(codeVersion)) return null;
  const candidate: CatiaRunContext = {
    schema: CATIA_RUN_CONTEXT_SCHEMA,
    runId,
    attemptId: input.turnId,
    sourceFingerprint,
    codeVersion,
    contractVersion: CATIA_RUN_CONTEXT_SCHEMA,
    ...(env.PILOTDECK_MODEL_ID ? { modelId: env.PILOTDECK_MODEL_ID } : {}),
    ...(env.PILOTDECK_PROMPT_ID ? { promptId: env.PILOTDECK_PROMPT_ID } : {}),
    workingDirectory: input.cwd,
    sessionId: input.sessionId,
    ...(input.operationId ? { operationId: input.operationId } : {}),
    ...(input.deadlineMs !== undefined ? { deadlineMs: input.deadlineMs } : {}),
    createdAt: (input.now ?? new Date()).toISOString(),
  };
  const validation = validateCatiaRunContext(candidate);
  return validation.ok ? validation.context : null;
}
