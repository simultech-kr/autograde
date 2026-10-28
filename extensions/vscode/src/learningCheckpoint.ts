import { randomUUID } from "node:crypto";
import type { SubmissionBundle } from "./bundle";

export interface LearningCheckpoint {
  readonly checkpoint_id: string;
  readonly assignment_id: string;
  readonly archive_sha256: string;
  readonly created_at: string;
  readonly file_count: number;
  readonly source_bytes: number;
  readonly version: number;
}
export interface LearningCheckpointReceipt extends LearningCheckpoint { readonly received_archive_sha256?: string; }
export interface SavedLearningSnapshot {
  readonly checkpoint: LearningCheckpointReceipt;
  readonly filePaths: readonly string[];
}
export interface LearningCheckpointScope {
  readonly baseUrl: string;
  readonly generation: number;
  readonly assignmentId: string;
  readonly rootPath: string;
  readonly scheme: string;
  readonly authority: string;
}
export interface LearningCheckpointStatus {
  readonly state: "inactive" | "waiting" | "saving" | "saved" | "retrying" | "failed" | "unavailable";
  readonly savedAt?: string;
}
export function parseLearningCheckpoint(value: unknown, assignmentId: string): LearningCheckpoint {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("서버 저장본 정보를 확인할 수 없습니다.");
  const item = value as Record<string, unknown>;
  if (typeof item.checkpoint_id !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(item.checkpoint_id)
    || item.assignment_id !== assignmentId || typeof item.archive_sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.archive_sha256)
    || typeof item.created_at !== "string" || !Number.isFinite(Date.parse(item.created_at))
    || !Number.isSafeInteger(item.file_count) || Number(item.file_count) < 0 || Number(item.file_count) > 5000
    || !Number.isSafeInteger(item.source_bytes) || Number(item.source_bytes) < 0 || Number(item.source_bytes) > 100 * 1024 * 1024
    || !Number.isSafeInteger(item.version) || Number(item.version) < 1) throw new Error("서버 저장본 정보가 올바르지 않습니다.");
  return { checkpoint_id: item.checkpoint_id, assignment_id: assignmentId, archive_sha256: item.archive_sha256,
    created_at: item.created_at, file_count: Number(item.file_count), source_bytes: Number(item.source_bytes), version: Number(item.version) };
}
export function parseLearningCheckpoints(value: unknown, assignmentId: string): LearningCheckpoint[] {
  if (!value || typeof value !== "object" || !Array.isArray((value as {checkpoints?: unknown}).checkpoints)) throw new Error("서버 저장 목록을 확인할 수 없습니다.");
  const items = (value as {checkpoints: unknown[]}).checkpoints;
  if (items.length > 100) throw new Error("서버 저장 목록이 너무 큽니다.");
  return items.map(item => parseLearningCheckpoint(item, assignmentId));
}
export function sameLearningScope(a: LearningCheckpointScope | undefined, b: LearningCheckpointScope | undefined): boolean {
  return a?.baseUrl === b?.baseUrl && a?.generation === b?.generation && a?.assignmentId === b?.assignmentId
    && a?.rootPath === b?.rootPath && a?.scheme === b?.scheme && a?.authority === b?.authority;
}
export function learningCheckpointLabel(status: LearningCheckpointStatus): string {
  const labels = { inactive: "대기 · 연결된 과제 폴더 없음", waiting: status.savedAt ? "현재 변경 저장 대기" : "변경 대기 · 이 세션에서 아직 서버 저장 안 됨",
    saving: "서버 저장 중", saved: "서버 저장 확인됨", retrying: "저장 실패 · 연결 확인 후 재시도 대기",
    failed: "자동 저장 중단 · 파일·연결 확인 후 지금 저장을 누르세요", unavailable: "저장 불가 · 서버 지원 또는 인증 확인 필요" };
  return `${labels[status.state]}${status.savedAt ? ` · 마지막 성공 ${new Date(status.savedAt).toLocaleString()}` : ""}`;
}
interface Clock {
  setTimeout(callback: () => void, milliseconds: number): ReturnType<typeof setTimeout>;
  clearTimeout(timer: ReturnType<typeof setTimeout>): void;
}
export const LEARNING_CHECKPOINT_INTERVAL_MS = 30_000;
const MAX_ATTEMPTS = 3;

/** In-memory, single-flight snapshots. Official submission and grading are never invoked here. */
export class LearningCheckpointAutosaver {
  private scope?: LearningCheckpointScope;
  private revision = 0;
  private timer?: ReturnType<typeof setTimeout>;
  private flight?: {controller: AbortController; promise: Promise<void>};
  private baseline?: string;
  private observedEdit = false;
  private editRevision = 0;
  private savedHash?: string;
  private savedSnapshot?: SavedLearningSnapshot;
  private attemptedHash?: string;
  private attemptKey = "";
  private attempts = 0;
  private blocked = false;
  private disposed = false;
  private status: LearningCheckpointStatus = {state: "inactive"};

  constructor(private readonly options: {
    currentScope(): LearningCheckpointScope | undefined;
    capture(scope: LearningCheckpointScope, saveDirty: boolean, current: () => boolean): Promise<SubmissionBundle | undefined>;
    upload(scope: LearningCheckpointScope, bundle: SubmissionBundle, signal: AbortSignal, idempotencyKey: string): Promise<LearningCheckpointReceipt>;
    onStatus(status: LearningCheckpointStatus): void;
    /** Shared sessions start from downloaded templates; preserve prior server work until an edit. */
    baselineOnlyUntilChange?: boolean;
  }, private readonly clock: Clock = {setTimeout, clearTimeout}) {}

  getStatus(): LearningCheckpointStatus { return this.status; }
  getSavedSnapshot(): SavedLearningSnapshot | undefined {
    this.reconcile();
    return this.status.state === "saved" ? this.savedSnapshot : undefined;
  }
  private safeScope(): LearningCheckpointScope | undefined { try { return this.options.currentScope(); } catch { return undefined; } }
  reconcile(): void {
    if (this.disposed) return;
    const next = this.safeScope();
    if (sameLearningScope(next, this.scope)) return;
    this.revision++; this.flight?.controller.abort(); this.clearTimer();
    this.scope = next ? {...next} : undefined;
    this.baseline = this.savedHash = this.attemptedHash = undefined; this.observedEdit = false; this.editRevision = 0;
    this.savedSnapshot = undefined;
    this.attempts = 0; this.blocked = false;
    this.update({state: next ? "waiting" : "inactive"});
    if (next && !this.flight) void this.run(false);
  }
  recordEdit(): void {
    this.reconcile();
    if (this.scope) {
      this.observedEdit = true; this.editRevision++;
      if (this.status.state === "saved") this.update({...this.status, state: "waiting"});
    }
  }
  async saveNow(): Promise<boolean> {
    this.reconcile();
    const scope = this.scope, revision = this.revision;
    if (!scope) return false;
    if (this.flight) await this.flight.promise;
    if (!this.current(scope, revision)) return false;
    this.blocked = false; this.attempts = 0; this.attemptedHash = undefined;
    this.update({...this.status, state: "waiting"});
    await this.run(true);
    return this.current(scope, revision) && this.status.state === "saved";
  }
  abortCurrent(): void { this.flight?.controller.abort(); }
  dispose(): void {
    this.disposed = true; this.revision++; this.scope = undefined;
    this.savedSnapshot = undefined;
    this.flight?.controller.abort(); this.clearTimer();
    this.update({state: "inactive"});
  }
  private current(scope: LearningCheckpointScope, revision: number): boolean {
    return !this.disposed && revision === this.revision && sameLearningScope(scope, this.safeScope());
  }
  private run(force: boolean): Promise<void> {
    this.reconcile();
    if (this.disposed || this.blocked || !this.scope || this.flight) return Promise.resolve();
    this.clearTimer();
    const scope = this.scope, revision = this.revision, controller = new AbortController();
    const current = () => !controller.signal.aborted && this.current(scope, revision);
    let delay = LEARNING_CHECKPOINT_INTERVAL_MS;
    const flight = {controller, promise: Promise.resolve()};
    this.flight = flight;
    flight.promise = Promise.resolve().then(async () => {
      try {
        const baselineCapture = !this.baseline && !force;
        const capturedEditRevision = this.editRevision;
        const bundle = await this.options.capture(scope, !baselineCapture, current);
        if (!bundle || !current()) return;
        if (baselineCapture) { this.baseline = bundle.sha256; return; }
        if (!force && bundle.sha256 === this.savedHash) {
          this.update({...this.status, state: capturedEditRevision === this.editRevision ? "saved" : "waiting"}); return;
        }
        if (!force && !this.savedHash && !this.observedEdit && this.options.baselineOnlyUntilChange && bundle.sha256 === this.baseline) return;
        if (bundle.sha256 !== this.attemptedHash) { this.attemptedHash = bundle.sha256; this.attempts = 0; this.attemptKey = `learning-${randomUUID()}`; }
        if (!force && this.attempts >= MAX_ATTEMPTS) return;
        this.attempts++;
        this.update({...this.status, state: "saving"});
        const receipt = await this.options.upload(scope, bundle, controller.signal, this.attemptKey);
        if (!current()) return;
        if ((receipt.received_archive_sha256 ?? receipt.archive_sha256) !== bundle.sha256) throw new Error("서버 저장본 검증에 실패했습니다.");
        this.savedHash = bundle.sha256; this.attempts = 0;
        this.savedSnapshot = {checkpoint: receipt, filePaths: [...(bundle.filePaths ?? [])]};
        this.update({state: capturedEditRevision === this.editRevision ? "saved" : "waiting", savedAt: receipt.created_at});
      } catch (error) {
        if (!this.current(scope, revision)) return;
        const code = error && typeof error === "object" && "status" in error ? Number(error.status) : 0;
        if (code >= 400 && code < 500 && ![408, 429].includes(code)) {
          this.blocked = true; this.update({...this.status, state: "unavailable"});
        } else {
          // Capture errors also back off. At most three uploads per unchanged snapshot.
          if (!this.attemptedHash) this.attempts++;
          this.update({...this.status, state: this.attempts >= MAX_ATTEMPTS ? "failed" : "retrying"});
          const advertised = error && typeof error === "object" && "retryAfterMs" in error ? Number(error.retryAfterMs) : 0;
          delay = Math.min(120_000, Math.max(Number.isFinite(advertised) ? advertised : 0,
            LEARNING_CHECKPOINT_INTERVAL_MS * 2 ** Math.min(this.attempts, 2)));
        }
      } finally {
        if (this.flight === flight) this.flight = undefined;
        this.reconcile();
        if (!this.disposed && this.scope && !this.blocked && !this.flight) {
          this.timer = this.clock.setTimeout(() => { this.timer = undefined; void this.run(false); },
            revision === this.revision ? delay : 0);
        }
      }
    });
    return flight.promise;
  }
  private clearTimer(): void { if (this.timer) this.clock.clearTimeout(this.timer); this.timer = undefined; }
  private update(status: LearningCheckpointStatus): void { this.status = status; this.options.onStatus(status); }
}
