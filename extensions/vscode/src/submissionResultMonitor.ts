import { ApiError, RequestCancelledError, isRetryableApiError } from "./api";
import type { GradeResult, SubmissionSummary } from "./types";

export interface ResultScope { isCurrent(): boolean }
export interface ResultUpdate {
  readonly result: GradeResult;
  readonly notice: string;
  readonly watching: boolean;
}
export interface ResultWatchOptions {
  readonly submission: SubmissionSummary;
  readonly isSessionCurrent: () => boolean;
  readonly onUpdate: (update: ResultUpdate) => void;
  readonly onAuthenticationRequired: () => void;
}
interface MonitorClient {
  getSubmission(id: string, signal?: AbortSignal): Promise<SubmissionSummary>;
  getResult(id: string, signal?: AbortSignal): Promise<GradeResult>;
}
interface Watch extends ResultWatchOptions {
  readonly scope: ResultScope;
  result: GradeResult;
  expiresAt: number;
  failures: number;
  timer?: ReturnType<typeof setTimeout>;
  request?: Promise<void>;
  abort?: AbortController;
  paused: boolean;
}
const WAITING = new Set(["received", "pinning", "verifying", "accepted", "queued", "running", "result_pending"]);
const MAX_WATCH_MS = 10 * 60 * 1000;

/** One displayed receipt at a time. Responses from replaced views never reach the UI. */
export class SubmissionResultMonitor {
  private generation = 0;
  private watch?: Watch;

  public constructor(
    private readonly client: MonitorClient,
    private readonly now: () => number = Date.now,
    private readonly intervalMs = 3_000,
    private readonly maxWatchMs = MAX_WATCH_MS,
  ) {}

  public begin(): ResultScope {
    this.cancel();
    const generation = this.generation;
    return { isCurrent: () => this.generation === generation };
  }

  public cancel(): void {
    this.generation += 1;
    if (this.watch?.timer) clearTimeout(this.watch.timer);
    this.watch?.abort?.abort();
    this.watch = undefined;
  }

  public start(scope: ResultScope, options: ResultWatchOptions): void {
    if (!scope.isCurrent() || !options.isSessionCurrent()) return;
    const initial = options.submission;
    const watch: Watch = { ...options, scope,
      result: { state: initial.state === "published" ? "graded" : initial.state,
        sourceDigest: initial.sourceDigest, headSha: initial.headSha,
        previousBest: initial.previousBest, rubric: [], diagnostics: [] },
      failures: 0, expiresAt: this.now() + this.maxWatchMs, paused: false };
    this.watch = watch;
    this.emit(watch, "접수번호를 기준으로 채점 상태를 자동 확인합니다.", true);
    void this.check(watch);
  }

  public refresh(): Promise<void> {
    const watch = this.watch;
    if (!watch || !this.current(watch)) return Promise.resolve();
    if (watch.request) {
      return watch.paused ? watch.request.then(() => this.current(watch) ? this.refresh() : undefined) : watch.request;
    }
    watch.paused = false;
    watch.failures = 0;
    watch.expiresAt = this.now() + this.maxWatchMs;
    if (watch.timer) clearTimeout(watch.timer);
    watch.timer = undefined;
    this.emit(watch, "이번 접수의 결과를 다시 확인합니다.", true);
    return this.check(watch);
  }

  public pause(): void {
    const watch = this.watch;
    if (!watch || !this.current(watch)) return;
    watch.paused = true;
    if (watch.timer) clearTimeout(watch.timer);
    watch.timer = undefined;
    watch.abort?.abort();
    this.emit(watch, "자동 확인을 중지했습니다. 서버의 접수·채점은 계속됩니다. 결과 다시 확인으로 재개할 수 있습니다.", false);
  }

  private current(watch: Watch): boolean {
    return this.watch === watch && watch.scope.isCurrent() && watch.isSessionCurrent();
  }

  private emit(watch: Watch, notice: string, watching: boolean): void {
    if (this.current(watch)) watch.onUpdate({ result: watch.result, notice, watching });
  }

  private check(watch: Watch): Promise<void> {
    if (!this.current(watch) || watch.paused) return Promise.resolve();
    if (watch.request) return watch.request;
    const request = this.fetch(watch);
    watch.request = request;
    void request.finally(() => { if (watch.request === request) watch.request = undefined; });
    return request;
  }

  private async fetch(watch: Watch): Promise<void> {
    const controller = new AbortController();
    watch.abort = controller;
    let nextDelay = this.intervalMs;
    try {
      const submission = await this.client.getSubmission(watch.submission.id, controller.signal);
      if (!this.current(watch) || watch.paused) return;
      if (submission.id !== watch.submission.id ||
          (watch.submission.sourceDigest && submission.sourceDigest && watch.submission.sourceDigest !== submission.sourceDigest) ||
          (watch.submission.headSha && submission.headSha && watch.submission.headSha !== submission.headSha)) {
        throw new ApiError("조회한 결과의 접수번호 또는 제출 파일 정보가 다릅니다.", 409, "submission_mismatch");
      }
      let result: GradeResult = { state: submission.state, sourceDigest: submission.sourceDigest,
        headSha: submission.headSha, previousBest: submission.previousBest, rubric: [], diagnostics: [] };
      if (["graded", "published"].includes(submission.state)) {
        try {
          result = await this.client.getResult(watch.submission.id, controller.signal);
        } catch (error) {
          if (!(error instanceof ApiError && [404, 409, 425].includes(error.status))) throw error;
          result = { ...result, state: submission.state === "published" ? "result_pending" : "graded" };
        }
        if (!this.current(watch) || watch.paused) return;
        if ((submission.sourceDigest && result.sourceDigest && submission.sourceDigest !== result.sourceDigest) ||
            (submission.headSha && result.headSha && submission.headSha !== result.headSha)) {
          throw new ApiError("조회한 채점 결과의 제출 파일 정보가 다릅니다.", 409, "submission_mismatch");
        }
      }
      watch.result = result;
      watch.failures = 0;
      const waiting = WAITING.has(result.state);
      this.emit(watch, result.state === "result_pending" ? "채점이 완료되어 결과를 준비 중입니다. 같은 접수번호로 자동 확인을 계속합니다." :
        waiting ? "서버가 처리 중입니다. 약 3초 간격으로 이번 접수 결과를 확인합니다." :
        result.state === "graded" ? "채점이 끝났으며 결과 공개를 기다립니다. 공개 후 결과 다시 확인을 누르세요." :
        "이번 접수의 상태를 확인했습니다.", waiting);
      if (!waiting) return;
    } catch (error) {
      if (!this.current(watch) || watch.paused || error instanceof RequestCancelledError || controller.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401) {
        watch.onAuthenticationRequired();
        this.cancel();
        return;
      }
      watch.failures += 1;
      const retry = error instanceof ApiError && isRetryableApiError(error) && watch.failures < 3;
      const notice = error instanceof ApiError && error.status === 403 ? "이 제출을 조회할 권한이 없습니다. 수강 상태를 확인하거나 교수자에게 문의하세요." :
        error instanceof ApiError && error.code === "submission_mismatch" ? error.message :
        retry ? "채점 상태 연결이 잠시 끊겼습니다. 접수는 유지되며 자동으로 다시 확인합니다." :
        "채점 상태를 확인하지 못했습니다. 접수는 유지됩니다. 연결을 확인한 뒤 결과 다시 확인을 누르세요.";
      this.emit(watch, notice, retry);
      if (!retry) return;
      nextDelay = error instanceof ApiError && error.retryAfterMs !== undefined
        ? Math.min(30_000, Math.max(this.intervalMs, error.retryAfterMs))
        : this.intervalMs * 2 ** watch.failures;
    } finally {
      if (watch.abort === controller) watch.abort = undefined;
    }
    if (!this.current(watch) || watch.paused) return;
    if (this.now() >= watch.expiresAt) {
      this.emit(watch, "10분 동안 확인하여 자동 확인을 쉬고 있습니다. 서버 채점은 계속됩니다. 결과 다시 확인으로 재개하세요.", false);
      return;
    }
    watch.timer = setTimeout(() => { watch.timer = undefined; void this.check(watch); }, nextDelay);
  }
}
