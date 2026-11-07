/** Bounded, untrusted CATIA build failure facts; these can only restrict retries. */
export type CatiaFailureStatus = {
  code: "VERIFIER_SESSION_UNKNOWN" | "MATERIAL_COMPARISON_INCONCLUSIVE" | "BUILD_FAILED" | "CATIA_BUILD_UNCLASSIFIED";
  automaticRetryAllowed: false;
  modified: boolean | null;
  sessionHealth?: "unknown";
  summary?: { reason: string; consistencyBudgetMm3: number; attemptCount: number; candidateCount: number };
  /** Deterministic builder failure text when `modified === false`. */
  reason?: string;
};

export const CATIA_BUILD_TOOL_NAME = "catia_build_part";

/** CATIA MCP servers use the production id `catia` or a per-run `catia_*` id. */
export function isCatiaServerId(serverId: string): boolean {
  return serverId === "catia" || serverId.startsWith("catia_");
}

export function isCatiaBuildToolName(toolName: string): boolean {
  if (toolName === CATIA_BUILD_TOOL_NAME) return true;
  const match = /^mcp__(.+?)__catia_build_part$/u.exec(toolName);
  return match !== null && isCatiaServerId(match[1]!);
}

export function isCatiaBuildCall(serverId: string, toolName: string): boolean {
  return isCatiaServerId(serverId) && toolName === CATIA_BUILD_TOOL_NAME;
}

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown> : undefined;
}

function summary(value: unknown): CatiaFailureStatus["summary"] | undefined {
  const s = record(value);
  if (!s || Object.keys(s).sort().join(",") !== "attemptCount,candidateCount,consistencyBudgetMm3,reason"
    || typeof s.reason !== "string" || !["COMMON_UNION_DISAGREES", "INSUFFICIENT_CONSISTENT_BOOLEANS", "FUZZY_LEVELS_DISAGREE"].includes(s.reason)
    || typeof s.consistencyBudgetMm3 !== "number" || !Number.isFinite(s.consistencyBudgetMm3) || s.consistencyBudgetMm3 <= 0
    || !Number.isSafeInteger(s.attemptCount) || Number(s.attemptCount) < 1 || Number(s.attemptCount) > 10
    || !Number.isSafeInteger(s.candidateCount) || Number(s.candidateCount) < 0 || Number(s.candidateCount) > Number(s.attemptCount)) return;
  return { reason: s.reason, consistencyBudgetMm3: s.consistencyBudgetMm3,
    attemptCount: Number(s.attemptCount), candidateCount: Number(s.candidateCount) };
}

export function unclassifiedCatiaFailure(): CatiaFailureStatus {
  return { code: "CATIA_BUILD_UNCLASSIFIED", automaticRetryAllowed: false, modified: null, sessionHealth: "unknown" };
}

export function isCatiaBuildSuccess(value: unknown): boolean {
  try {
    const envelope = record(value);
    const data = record(envelope?.data);
    const verification = record(data?.verification);
    const editability = record(data?.editability);
    const delivery = record(data?.delivery);
    const checks = verification?.checks;
    const fullVerification = Array.isArray(checks) && checks.length > 0
      && checks.every(item => record(item)?.passed === true);
    // Feature builds carry a compact receipt; the detailed checks live in the
    // hashed report. This only validates the receipt shape, not the report.
    const expectedStages = ["requirements", "plan", "geometry_preflight", "modeling", "verification", "editability", "delivery"];
    const stages = data?.stages;
    const step = record(verification?.stepExport);
    const digest = (value: unknown) => typeof value === "string" && /^[a-f0-9]{64}$/i.test(value);
    const nonempty = (value: unknown) => typeof value === "string" && value.trim().length > 0;
    const isBounded = delivery?.reportJsonSha256 !== undefined || editability?.declaredParameterCount !== undefined
      || (Array.isArray(stages) && stages.some(item => record(item)?.stage === "geometry_preflight"));
    const boundedVerification = (checks === undefined || fullVerification) && Array.isArray(stages) && stages.length === expectedStages.length
      && stages.every((item, index) => record(item)?.stage === expectedStages[index]
        && record(item)?.status === (index === expectedStages.length - 1 ? "completed" : "passed"))
      && nonempty(data?.planId) && nonempty(delivery?.path) && digest(delivery?.sha256)
      && nonempty(delivery?.reportJson) && digest(delivery?.reportJsonSha256)
      && nonempty(step?.path) && digest(step?.sha256) && delivery?.stepPath === step?.path
      && Number.isSafeInteger(editability?.declaredParameterCount)
      && Number(editability?.declaredParameterCount) > 0
      && editability?.verifiedParameterCount === editability?.declaredParameterCount
      && editability?.sampled === false;
    return envelope?.ok === true && envelope.code === "BUILD_COMPLETED"
      && data?.businessState === "completed" && data.sessionHealth === undefined
      && record(envelope.effects)?.modified === true && verification?.verdict === "PASS"
      && (isBounded ? boundedVerification : fullVerification)
      && editability?.passed === true && typeof delivery?.path === "string" && delivery.path.length > 0;
  } catch { return false; }
}

export function catiaContentAgrees(content: unknown, structured: unknown, maxTextBytes = 2_000_000): boolean {
  try {
    if (!Array.isArray(content) || content.length !== 1) return false;
    const block = record(content[0]);
    if (block?.type !== "text" || typeof block.text !== "string" || Buffer.byteLength(block.text, "utf8") > maxTextBytes) return false;
    const parsed: unknown = JSON.parse(block.text);
    const pending: Array<[unknown, unknown]> = [[parsed, structured]];
    const seen = new WeakSet<object>();
    let visited = 0;
    while (pending.length) {
      if (++visited > 100_000) return false;
      const [left, right] = pending.pop()!;
      if (left === right) continue;
      if (left === null || right === null || typeof left !== "object" || typeof right !== "object") return false;
      if (seen.has(right)) return false;
      seen.add(right);
      if (Array.isArray(left) || Array.isArray(right)) {
        if (!Array.isArray(left) || !Array.isArray(right) || left.length !== right.length || left.length > 100_000) return false;
        for (let i = 0; i < left.length; i++) pending.push([left[i], right[i]]);
      } else {
        const a = left as Record<string, unknown>;
        const b = right as Record<string, unknown>;
        const keys = Object.keys(a);
        if (keys.length !== Object.keys(b).length || keys.length > 100_000) return false;
        for (const key of keys) {
          if (!Object.hasOwn(b, key)) return false;
          pending.push([a[key], b[key]]);
        }
      }
    }
    return true;
  } catch { return false; }
}

/** Read only explicitly typed fields; reject contradictory or missing safety data. */
export function parseCatiaFailure(value: unknown): CatiaFailureStatus {
  try {
    const envelope = record(value);
    const data = record(envelope?.data);
    const effects = record(envelope?.effects);
    const recovery = record(envelope?.recovery);
    const modified = effects?.modified;
    if (envelope?.ok !== false || recovery?.automatic_retry_allowed !== false
      || !(modified === true || modified === false || modified === null)) return unclassifiedCatiaFailure();
    if (envelope.code === "VERIFIER_SESSION_UNKNOWN" && data?.businessState === "session_unknown" && data.sessionHealth === "unknown") {
      return { code: "VERIFIER_SESSION_UNKNOWN", automaticRetryAllowed: false, modified, sessionHealth: "unknown" };
    }
    const failure = record(data?.failure);
    const blockedCodes = ["NEEDS_CLARIFICATION", "UNSUPPORTED_CAPABILITY", "REQUIREMENTS_INVALID"];
    if (modified === false && (data?.businessState === "build_failed"
      || blockedCodes.includes(String(envelope.code)))) {
      // Deterministic builder/requirements failure that changed nothing:
      // CATIA's state is known, so the reason is safe to surface and the client
      // need not be fenced. Unknown session state keeps falling through.
      const reason = typeof failure?.reason === "string" ? failure.reason
        : typeof envelope.message === "string" ? envelope.message : "";
      return { code: "BUILD_FAILED", automaticRetryAllowed: false, modified: false,
        ...(reason ? { reason: reason.slice(0, 2000) } : {}) };
    }
    const detail = summary(failure?.summary);
    if (envelope.code === "MATERIAL_COMPARISON_INCONCLUSIVE" && data?.businessState === "verification_failed"
      && data.sessionHealth === undefined && modified === true
      && (failure?.stage === "verification" || failure?.stage === "editability")
      && failure.classification === "material_comparison_inconclusive" && detail) {
      return { code: "MATERIAL_COMPARISON_INCONCLUSIVE", automaticRetryAllowed: false, modified: true, summary: detail };
    }
  } catch { /* A malformed object never becomes a successful result. */ }
  return unclassifiedCatiaFailure();
}

export function readCatiaFailure(toolName: string, details: unknown): CatiaFailureStatus | undefined {
  if (!isCatiaBuildToolName(toolName)) return;
  try {
    const status = record(record(details)?.reportedCatiaStatus);
    if (!status || status.automaticRetryAllowed !== false) return;
    if (status.code === "CATIA_BUILD_UNCLASSIFIED" && status.modified === null && status.sessionHealth === "unknown") return unclassifiedCatiaFailure();
    if (status.code === "BUILD_FAILED" && status.modified === false) {
      const reason = typeof status.reason === "string" ? status.reason.slice(0, 2000) : "";
      return { code: "BUILD_FAILED", automaticRetryAllowed: false, modified: false,
        ...(reason ? { reason } : {}) };
    }
    if (status.code === "VERIFIER_SESSION_UNKNOWN" && status.sessionHealth === "unknown"
      && (typeof status.modified === "boolean" || status.modified === null)) {
      return { code: "VERIFIER_SESSION_UNKNOWN", automaticRetryAllowed: false, modified: status.modified, sessionHealth: "unknown" };
    }
    const detail = summary(status.summary);
    if (status.code === "MATERIAL_COMPARISON_INCONCLUSIVE" && status.modified === true && status.sessionHealth === undefined && detail) {
      return { code: "MATERIAL_COMPARISON_INCONCLUSIVE", automaticRetryAllowed: false, modified: true, summary: detail };
    }
  } catch { return; }
}
