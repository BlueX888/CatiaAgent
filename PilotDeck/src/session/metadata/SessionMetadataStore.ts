import type { AgentTranscriptWriter } from "../transcript/TranscriptWriter.js";
import type { SessionMetadataValue } from "../transcript/TranscriptEntry.js";
import type { CatiaRunContext } from "../../tool/protocol/catiaRunContext.js";
import type { CatiaRecoveryProjection } from "../../tool/protocol/catiaRunRecovery.js";

export type CatiaRunMetadataRecord = {
  context: CatiaRunContext;
  stateSource: "host" | "coordinator" | "journal";
  sequence: number;
  updatedAt: string;
};

export type CatiaRecoveryMetadataRecord = {
  projection: CatiaRecoveryProjection;
  sequence: number;
  updatedAt: string;
};

export type SessionMetadataStoreOptions = {
  transcript: AgentTranscriptWriter;
  sessionId: string;
  now?: () => Date;
};

export class SessionMetadataStore {
  private readonly now: () => Date;
  private metadata: SessionMetadataValue = {};
  private catiaRunContext: CatiaRunMetadataRecord | undefined;
  private catiaRecovery: CatiaRecoveryMetadataRecord | undefined;

  constructor(private readonly options: SessionMetadataStoreOptions) {
    this.now = options.now ?? (() => new Date());
  }

  getSnapshot(): SessionMetadataValue {
    return cloneMetadata(this.metadata);
  }

  async saveTitle(title: string, turnId = "metadata"): Promise<void> {
    await this.record(turnId, { title, updatedAt: this.now().toISOString() });
  }

  async saveAiTitle(aiTitle: string, turnId = "metadata"): Promise<void> {
    if (this.metadata.title) {
      await this.record(turnId, { aiTitle, updatedAt: this.now().toISOString() });
      return;
    }
    await this.record(turnId, { aiTitle, updatedAt: this.now().toISOString() });
  }

  async saveTag(tag: string, turnId = "metadata"): Promise<void> {
    await this.record(turnId, { tag, updatedAt: this.now().toISOString() });
  }

  async savePullRequest(
    linkedPullRequest: NonNullable<SessionMetadataValue["linkedPullRequest"]>,
    turnId = "metadata",
  ): Promise<void> {
    await this.record(turnId, { linkedPullRequest, updatedAt: this.now().toISOString() });
  }

  async saveMode(mode: NonNullable<SessionMetadataValue["mode"]>, turnId = "metadata"): Promise<void> {
    await this.record(turnId, { mode, updatedAt: this.now().toISOString() });
  }

  /**
   * Restore previously-loaded metadata into the in-memory snapshot without
   * writing to the transcript. Called during resume to seed the store from
   * the replayed metadata.
   */
  restoreFromReplay(metadata: SessionMetadataValue): void {
    this.metadata = mergeMetadata(this.metadata, metadata);
    const restored = (metadata as unknown as { catiaRunContext?: CatiaRunMetadataRecord }).catiaRunContext;
    if (restored && restored.context && typeof restored.sequence === "number") {
      this.catiaRunContext = cloneCatiaRunRecord(restored);
    }
    const recovery = (metadata as unknown as { catiaRecovery?: CatiaRecoveryMetadataRecord }).catiaRecovery;
    if (recovery && recovery.projection && typeof recovery.sequence === "number") {
      this.catiaRecovery = recovery;
    }
  }

  /**
   * Re-append the current metadata snapshot as a new session_metadata entry
   * at the tail of the transcript. This ensures `readSessionLite()` (which
   * reads head + tail) can see the latest metadata without scanning the
   * entire file. Legacy calls this `reAppendSessionMetadata()`.
   */
  async reappendTail(turnId = "metadata-reappend"): Promise<void> {
    const snapshot = this.getSnapshot();
    const hasValues = Object.values(snapshot).some((value) => value !== undefined);
    if (!hasValues) {
      return;
    }
    if (!this.options.transcript.recordSessionMetadata) {
      return;
    }
    await this.options.transcript.recordSessionMetadata(
      this.options.sessionId,
      turnId,
      { ...snapshot, isSnapshot: true, updatedAt: this.now().toISOString() },
    );
  }

  getCatiaRunContext(): CatiaRunMetadataRecord | undefined {
    return cloneCatiaRunRecord(this.catiaRunContext);
  }

  /**
   * Persist host-owned run identity as a sidecar metadata entry. The sequence is
   * monotonic across reopen; the CATIA coordinator/journal stay authoritative
   * for execution state.
   */
  async saveCatiaRunContext(
    context: CatiaRunContext,
    options: { stateSource?: CatiaRunMetadataRecord["stateSource"]; turnId?: string } = {},
  ): Promise<CatiaRunMetadataRecord> {
    const record: CatiaRunMetadataRecord = {
      context: { ...context },
      stateSource: options.stateSource ?? "host",
      sequence: (this.catiaRunContext?.sequence ?? 0) + 1,
      updatedAt: this.now().toISOString(),
    };
    this.catiaRunContext = record;
    await this.record(options.turnId ?? "catia-run-context", {
      catiaRunContext: record,
    } as unknown as SessionMetadataValue);
    return cloneCatiaRunRecord(record)!;
  }

  getCatiaRecovery(): CatiaRecoveryMetadataRecord | undefined {
    return this.catiaRecovery
      ? { ...this.catiaRecovery, projection: { ...this.catiaRecovery.projection,
          blockers: [...this.catiaRecovery.projection.blockers],
          evidence: [...this.catiaRecovery.projection.evidence] } }
      : undefined;
  }

  /** Persist a read-only recovery projection; it never triggers a CAD call. */
  async saveCatiaRecovery(
    projection: CatiaRecoveryProjection,
    turnId = "catia-recovery",
  ): Promise<CatiaRecoveryMetadataRecord> {
    const record: CatiaRecoveryMetadataRecord = {
      projection,
      sequence: (this.catiaRecovery?.sequence ?? 0) + 1,
      updatedAt: this.now().toISOString(),
    };
    this.catiaRecovery = record;
    await this.record(turnId, { catiaRecovery: record } as unknown as SessionMetadataValue);
    return this.getCatiaRecovery()!;
  }

  async record(turnId: string, metadata: SessionMetadataValue): Promise<void> {
    this.metadata = mergeMetadata(this.metadata, metadata);
    if (!this.options.transcript.recordSessionMetadata) {
      throw new Error("Transcript writer does not support session metadata entries.");
    }
    await this.options.transcript.recordSessionMetadata(this.options.sessionId, turnId, metadata);
  }
}

export function mergeMetadata(first: SessionMetadataValue, second: SessionMetadataValue): SessionMetadataValue {
  if (second.isSnapshot) return { ...second };
  return {
    ...first,
    ...second,
    title: second.title ?? first.title,
    linkedPullRequest: second.linkedPullRequest ?? first.linkedPullRequest,
  };
}

function cloneMetadata(metadata: SessionMetadataValue): SessionMetadataValue {
  const cloned = {
    ...metadata,
    linkedPullRequest: metadata.linkedPullRequest ? { ...metadata.linkedPullRequest } : undefined,
  } as SessionMetadataValue & { catiaRunContext?: CatiaRunMetadataRecord; catiaRecovery?: CatiaRecoveryMetadataRecord };
  const catia = (metadata as unknown as { catiaRunContext?: CatiaRunMetadataRecord }).catiaRunContext;
  if (catia) cloned.catiaRunContext = cloneCatiaRunRecord(catia);
  const recovery = (metadata as unknown as { catiaRecovery?: CatiaRecoveryMetadataRecord }).catiaRecovery;
  if (recovery) cloned.catiaRecovery = recovery;
  return cloned;
}

function cloneCatiaRunRecord(record: CatiaRunMetadataRecord | undefined): CatiaRunMetadataRecord | undefined {
  if (!record) return undefined;
  return { ...record, context: { ...record.context } };
}
