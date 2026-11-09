/**
 * Read-only recovery projection.
 *
 * It turns persisted operation records into an observable state. It never
 * sends a tool call, never opens a new connection, and never turns `unknown`
 * into `completed` or suggests a retry. The edit coordinator/journal remain
 * the execution authority.
 */

export type CatiaRecoveryRecordState =
  | "completed"
  | "failed"
  | "recovered"
  | "unknown"
  | "rejected"
  | "applied"
  | "intent";

export type CatiaRecoveryRecord = {
  operationId: string;
  planHash: string;
  state: CatiaRecoveryRecordState;
  modified?: boolean | null;
  evidence?: string[];
  observedAt?: string;
};

export type CatiaRecoveryInput = {
  sessionId?: string;
  records: CatiaRecoveryRecord[];
  fenceActive?: boolean;
};

export type CatiaRecoveryState = "none" | "completed" | "failed" | "recovered" | "unknown" | "conflict";

export type CatiaRecoveryProjection = {
  state: CatiaRecoveryState;
  operationId?: string;
  modified: boolean | null;
  /** A recovery projection never authorizes a retry. */
  retryAllowed: false;
  blockers: string[];
  evidence: string[];
};

const UNKNOWN_STATES: CatiaRecoveryRecordState[] = ["unknown", "applied", "intent"];

export function projectCatiaRecovery(input: CatiaRecoveryInput): CatiaRecoveryProjection {
  const records = Array.isArray(input.records) ? input.records : [];
  if (records.length === 0) {
    return { state: "none", modified: null, retryAllowed: false, blockers: [],
      evidence: ["no persisted operation records for this session"] };
  }
  const byOperation = new Map<string, CatiaRecoveryRecord[]>();
  for (const record of records) {
    const list = byOperation.get(record.operationId) ?? [];
    list.push(record);
    byOperation.set(record.operationId, list);
  }
  for (const [operationId, list] of byOperation) {
    const hashes = new Set(list.map((item) => item.planHash));
    if (hashes.size > 1) {
      return {
        state: "conflict", operationId, modified: null, retryAllowed: false,
        blockers: [`operationId ${operationId} was recorded with different plan hashes; read-only reconciliation is required`],
        evidence: [...hashes],
      };
    }
    const states = new Set(list.map((item) => item.state));
    if (states.size > 1) {
      return {
        state: "conflict", operationId, modified: null, retryAllowed: false,
        blockers: [`operationId ${operationId} has contradictory recorded states: ${[...states].join(", ")}`],
        evidence: [...states],
      };
    }
  }

  const latest = [...records].sort((left, right) =>
    (left.observedAt ?? "").localeCompare(right.observedAt ?? ""))[records.length - 1]!;
  const operationId = latest.operationId;
  const evidence = latest.evidence ?? [];

  if (UNKNOWN_STATES.includes(latest.state)) {
    return {
      state: "unknown", operationId, modified: latest.modified ?? null, retryAllowed: false,
      blockers: [`operation ${operationId} has no verified outcome; use read-only reconciliation, not a new operation id`],
      evidence,
    };
  }
  if (latest.state === "rejected") {
    return { state: "failed", operationId, modified: false, retryAllowed: false,
      blockers: [], evidence };
  }
  if (latest.state === "failed") {
    return { state: "failed", operationId, modified: latest.modified ?? false,
      retryAllowed: false, blockers: [], evidence };
  }
  if (latest.state === "recovered") {
    return { state: "recovered", operationId, modified: latest.modified ?? false,
      retryAllowed: false, blockers: [], evidence };
  }
  // completed requires explicit effect evidence; a recorded state alone is not
  // proof that the model was verified.
  if (evidence.length === 0 || latest.modified === undefined || latest.modified === null) {
    return {
      state: "unknown", operationId, modified: null, retryAllowed: false,
      blockers: [`operation ${operationId} claims completion without effect evidence`],
      evidence,
    };
  }
  return { state: "completed", operationId, modified: latest.modified,
    retryAllowed: false, blockers: [], evidence };
}
