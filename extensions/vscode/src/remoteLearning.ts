import { sameLearningScope, type LearningCheckpointScope, type SavedLearningSnapshot } from "./learningCheckpoint";

export const REMOTE_LEARNING_COMMAND = "autograde.remoteLearning";
export const MAX_RUNTIME_OUTPUT = 256 * 1024;
export const MAX_RUNTIME_STDIN = 64 * 1024;
export type RuntimeLanguage = "c" | "cpp" | "python";
export interface LearningRuntimeSession {
  readonly id: string;
  readonly status: "ready" | "running" | "closed" | "expired" | "cleanup_failed";
  readonly checkpoint_id: string;
  readonly created_at: string;
  readonly expires_at: string;
}
export interface LearningRuntimeInfo {
  readonly enabled: boolean;
  readonly provider: "remote_linux";
  readonly languages: readonly RuntimeLanguage[];
  readonly session: LearningRuntimeSession | null;
}
export interface RuntimeOutput { readonly exit_code: number | null; readonly stdout: string; readonly stderr: string; }
export interface LearningRuntimeResult {
  readonly session: LearningRuntimeSession;
  readonly status: "completed" | "compile_error" | "runtime_error" | "timeout" | "output_limit";
  readonly checkpoint_id: string;
  readonly compile: RuntimeOutput;
  readonly run: RuntimeOutput | null;
}
export interface LearningRuntimeClosed { readonly status: "closed"; readonly checkpoint_id: string; readonly cleanup_verified: true; }
export interface RuntimeRunRequest { readonly language: RuntimeLanguage; readonly sources: readonly string[]; readonly stdin: string; }
export interface LearningRuntimeApi {
  getLearningRuntime(scope: LearningCheckpointScope, signal: AbortSignal): Promise<LearningRuntimeInfo>;
  createLearningRuntime(scope: LearningCheckpointScope, checkpointId: string, signal: AbortSignal): Promise<LearningRuntimeSession>;
  runLearningRuntime(scope: LearningCheckpointScope, sessionId: string, checkpointId: string, request: RuntimeRunRequest, signal: AbortSignal): Promise<LearningRuntimeResult>;
  closeLearningRuntime(scope: LearningCheckpointScope, sessionId: string, checkpointId: string, signal: AbortSignal): Promise<LearningRuntimeClosed>;
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("실습 서버 응답을 확인할 수 없습니다.");
  return value as Record<string, unknown>;
}
export function runtimeIdentifier(value: unknown): string {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(value)) throw new Error("실습 실행공간 식별자가 올바르지 않습니다.");
  return value;
}
export function parseRuntimeSession(value: unknown, checkpointId?: string, sessionId?: string): LearningRuntimeSession {
  const item = object(value), id = runtimeIdentifier(item.id), checkpoint = runtimeIdentifier(item.checkpoint_id);
  if (!(["ready", "running", "closed", "expired", "cleanup_failed"] as unknown[]).includes(item.status)
    || checkpointId !== undefined && checkpoint !== checkpointId || sessionId !== undefined && id !== sessionId
    || typeof item.created_at !== "string" || !Number.isFinite(Date.parse(item.created_at))
    || typeof item.expires_at !== "string" || !Number.isFinite(Date.parse(item.expires_at))) throw new Error("실습 실행공간 응답이 현재 요청과 일치하지 않습니다.");
  return {id, status: item.status as LearningRuntimeSession["status"], checkpoint_id: checkpoint,
    created_at: item.created_at, expires_at: item.expires_at};
}
export function parseRuntimeInfo(value: unknown): LearningRuntimeInfo {
  const item = object(value);
  if (typeof item.enabled !== "boolean" || item.provider !== "remote_linux" || !Array.isArray(item.languages)
    || item.languages.length > 3 || item.languages.some(lang => lang !== "c" && lang !== "cpp" && lang !== "python")
    || new Set(item.languages).size !== item.languages.length || item.enabled && !item.languages.length
    || !item.enabled && item.session !== null) throw new Error("실습 환경 지원 정보를 확인할 수 없습니다.");
  return {enabled: item.enabled, provider: "remote_linux", languages: item.languages, session: item.session === null ? null : parseRuntimeSession(item.session)};
}
function parseOutput(value: unknown): RuntimeOutput {
  const item = object(value);
  if (!(item.exit_code === null || Number.isSafeInteger(item.exit_code) && Math.abs(Number(item.exit_code)) <= 65536)
    || typeof item.stdout !== "string" || Buffer.byteLength(item.stdout) > MAX_RUNTIME_OUTPUT
    || typeof item.stderr !== "string" || Buffer.byteLength(item.stderr) > MAX_RUNTIME_OUTPUT) throw new Error("실습 출력 형식 또는 크기를 확인할 수 없습니다.");
  return {exit_code: item.exit_code as number | null, stdout: item.stdout, stderr: item.stderr};
}
export function parseRuntimeResult(value: unknown, sessionId: string, checkpointId: string): LearningRuntimeResult {
  const item = object(value);
  if (!(["completed", "compile_error", "runtime_error", "timeout", "output_limit"] as unknown[]).includes(item.status)
    || item.checkpoint_id !== checkpointId) throw new Error("실행 결과가 현재 코드 저장본과 일치하지 않습니다.");
  const result: LearningRuntimeResult = {session: parseRuntimeSession(item.session, checkpointId, sessionId), checkpoint_id: checkpointId,
    status: item.status as LearningRuntimeResult["status"], compile: parseOutput(item.compile), run: item.run === null ? null : parseOutput(item.run)};
  if (result.status === "completed" && (result.compile.exit_code !== 0 || result.run?.exit_code !== 0)) throw new Error("실행 성공 응답과 종료 코드가 일치하지 않습니다.");
  return result;
}
export function parseRuntimeClosed(value: unknown, checkpointId: string): LearningRuntimeClosed {
  const item = object(value);
  if (item.status !== "closed" || item.checkpoint_id !== checkpointId || item.cleanup_verified !== true) throw new Error("원격 실행공간의 삭제를 확인하지 못했습니다.");
  return {status: "closed", checkpoint_id: checkpointId, cleanup_verified: true};
}
export function validRuntimeSource(value: unknown, language: RuntimeLanguage): value is string {
  return typeof value === "string" && value.length <= 1024 && !value.includes("\\")
    && !/[\x00-\x1f\x7f]/.test(value) && !value.startsWith("/") && !value.startsWith("-")
    && value.split("/").every(part => Boolean(part) && part !== "." && part !== ".." && !part.includes(":"))
    && (language === "c" ? /\.c$/.test(value) : language === "python" ? /\.py$/.test(value) : /\.(?:cpp|cc|cxx)$/.test(value));
}
export function validateRuntimeRun(value: unknown): RuntimeRunRequest {
  const item = object(value);
  if (item.language !== "c" && item.language !== "cpp" && item.language !== "python") throw new Error("C, C++ 또는 Python을 선택하세요.");
  if (!Array.isArray(item.sources) || item.sources.length < 1 || item.sources.length > (item.language === "python" ? 1 : 32)
    || item.sources.some(source => !validRuntimeSource(source, item.language as RuntimeLanguage))
    || new Set(item.sources).size !== item.sources.length) throw new Error(item.language === "python"
      ? "실행할 Python 파일 1개를 선택하세요. 다른 모듈은 저장된 프로젝트에서 불러옵니다."
      : "컴파일할 소스 파일을 1~32개 선택하세요. 경로와 언어를 확인해 주세요.");
  if (typeof item.stdin !== "string" || Buffer.byteLength(item.stdin) > MAX_RUNTIME_STDIN || item.stdin.includes("\0")) throw new Error("표준입력은 64 KiB 이하의 텍스트여야 합니다.");
  return {language: item.language, sources: [...item.sources], stdin: item.stdin};
}
export function runtimeErrorMessage(error: unknown): string {
  const item = error && typeof error === "object" ? error as {status?: unknown; code?: unknown; retryAfterMs?: unknown} : {};
  if (item.status === 401 || item.status === 403) return "인증 또는 과제 권한을 확인할 수 없습니다. 필요한 코드를 내보낸 뒤 새 수령 코드로 연결하세요.";
  if (item.status === 404 || item.status === 503) return "원격 실습 환경을 사용할 수 없습니다. 서버의 실행 관리자와 과제 설정을 교수자에게 확인해 주세요.";
  if (item.status === 429) return "다른 작업을 처리 중이거나 저장 간격 제한에 걸렸습니다. 잠시 후 다시 시도하세요.";
  if (item.status === 409) return "실행공간의 상태가 변경되었습니다. 상태 새로고침 후 다시 준비하세요.";
  if (error instanceof Error && error.message.startsWith("현재 코드의 서버 저장")) return "현재 코드의 서버 저장을 확인하지 못했습니다. 편집을 잠시 멈추고 저장 상태와 연결을 확인하세요. 연속 저장 제한이면 10초 후 재시도하세요.";
  return "원격 실습 작업을 완료하지 못했습니다. 연결·서버 저장·소스 선택을 확인하고 다시 시도하세요. 삭제 완료가 표시되지 않으면 원격 공간의 종료는 확인되지 않은 상태입니다.";
}
export function runtimeSessionActive(session: LearningRuntimeSession | null | undefined): boolean {
  return Boolean(session && !["closed", "expired"].includes(session.status));
}
export interface RemoteLearningState {
  readonly phase: "idle" | "checking" | "saving" | "preparing" | "running" | "closing" | "ready" | "disabled" | "closed" | "error";
  readonly info?: LearningRuntimeInfo;
  readonly session?: LearningRuntimeSession;
  readonly snapshot?: SavedLearningSnapshot;
  readonly result?: LearningRuntimeResult;
  readonly resultLanguage?: RuntimeLanguage;
  readonly editedSinceResult?: boolean;
  readonly cleanupVerified?: boolean;
  readonly message?: string;
}

/** In-memory orchestration only. Execution authority and ownership remain on the server. */
export class RemoteLearningController {
  private scope?: LearningCheckpointScope;
  private flight?: AbortController;
  private disposed = false;
  private remoteMayExist = false;
  private editRevision = 0;
  private state: RemoteLearningState = {phase: "idle"};
  constructor(private readonly api: LearningRuntimeApi, private readonly options: {
    currentScope(): LearningCheckpointScope | undefined;
    saveSnapshot(scope: LearningCheckpointScope): Promise<SavedLearningSnapshot>;
    onState(state: RemoteLearningState): void;
  }) {}
  getState(): RemoteLearningState { return this.state; }
  get isBusy(): boolean { return Boolean(this.flight); }
  get needsClose(): boolean { return this.remoteMayExist || runtimeSessionActive(this.state.session); }
  private currentScope(): LearningCheckpointScope | undefined { try { return this.options.currentScope(); } catch { return undefined; } }
  reconcile(): void {
    const next = this.currentScope();
    if (sameLearningScope(this.scope, next)) return;
    this.flight?.abort(); this.scope = next ? {...next} : undefined; this.remoteMayExist = false; this.editRevision = 0;
    this.update({phase: "idle"});
  }
  markEdited(): void {
    this.reconcile();
    this.editRevision++;
    if (this.state.snapshot) this.update({...this.state, editedSinceResult: true});
  }
  async refresh(): Promise<boolean> { return this.action(async (scope, signal, guard) => {
    this.update({...this.state, phase: "checking", message: undefined});
    const info = await this.api.getLearningRuntime(scope, signal); guard();
    this.remoteMayExist = runtimeSessionActive(info.session);
    this.update({...this.state, info, session: info.session ?? undefined,
      phase: !info.enabled ? "disabled" : info.session?.status === "closed" ? "closed" : "ready",
      cleanupVerified: info.session?.status === "closed" ? this.state.cleanupVerified : false});
  }); }
  async prepare(): Promise<boolean> { return this.action(async (scope, signal, guard) => {
    const info = await this.api.getLearningRuntime(scope, signal); guard();
    if (!info.enabled) {this.update({phase: "disabled", info}); return;}
    this.update({...this.state, phase: "saving", info, message: undefined, cleanupVerified: false});
    const revision = this.editRevision;
    const snapshot = await this.options.saveSnapshot(scope); guard();
    this.update({...this.state, phase: "preparing", snapshot, editedSinceResult: revision !== this.editRevision});
    this.remoteMayExist = true;
    const session = await this.api.createLearningRuntime(scope, snapshot.checkpoint.checkpoint_id, signal); guard();
    this.update({...this.state, phase: "ready", session, result: undefined, resultLanguage: undefined});
  }); }
  async run(request: RuntimeRunRequest): Promise<boolean> { return this.action(async (scope, signal, guard) => {
    request = validateRuntimeRun(request);
    if (!this.state.info?.enabled || !runtimeSessionActive(this.state.session)) throw new Error("먼저 실행공간을 준비하세요.");
    if (!this.state.info.languages.includes(request.language)) throw new Error("이 서버에서 선택한 언어를 지원하지 않습니다.");
    const session = this.state.session!;
    this.update({...this.state, phase: "saving", message: undefined, cleanupVerified: false});
    const revision = this.editRevision;
    const snapshot = await this.options.saveSnapshot(scope); guard();
    if (request.sources.some(source => !snapshot.filePaths.includes(source))) throw new Error("현재 저장본에 선택한 소스가 없습니다. 실행공간을 다시 준비하세요.");
    this.update({...this.state, phase: "running", snapshot, result: undefined, resultLanguage: request.language, editedSinceResult: revision !== this.editRevision});
    const result = await this.api.runLearningRuntime(scope, session.id, snapshot.checkpoint.checkpoint_id, request, signal); guard();
    // Edits made while the immutable version runs never relabel it as the newest code.
    this.update({...this.state, phase: "ready", session: result.session, result});
  }); }
  async close(): Promise<boolean> { return this.action(async (scope, signal, guard) => {
    let session = this.state.session;
    if (!session) {
      const info = await this.api.getLearningRuntime(scope, signal); guard();
      this.update({...this.state, info, session: info.session ?? undefined});
      session = info.session ?? undefined;
    }
    if (!session) { this.remoteMayExist = false; return; }
    if (session.status === "closed" && this.state.cleanupVerified) return;
    this.update({...this.state, phase: "saving", message: undefined});
    const snapshot = await this.options.saveSnapshot(scope); guard();
    this.update({...this.state, phase: "closing", snapshot});
    await this.api.closeLearningRuntime(scope, session.id, snapshot.checkpoint.checkpoint_id, signal); guard();
    this.remoteMayExist = false;
    this.update({...this.state, phase: "closed", cleanupVerified: true,
      session: {...session, checkpoint_id: snapshot.checkpoint.checkpoint_id, status: "closed"}});
  }); }
  private async action(operation: (scope: LearningCheckpointScope, signal: AbortSignal, guard: () => void) => Promise<void>): Promise<boolean> {
    this.reconcile();
    if (this.disposed || this.flight) return false;
    const scope = this.scope;
    if (!scope) {this.update({phase: "error", message: "새 수령 코드로 연결하고 신뢰한 과제 폴더를 먼저 여세요."}); return false;}
    const controller = new AbortController(); this.flight = controller;
    const guard = () => { if (this.disposed || controller.signal.aborted || !sameLearningScope(scope, this.currentScope())) throw new Error("연결된 과제가 변경되었습니다."); };
    try { await operation(scope, controller.signal, guard); guard(); return true; }
    catch (error) {
      if (!this.disposed && !controller.signal.aborted && sameLearningScope(scope, this.currentScope())) this.update({...this.state, phase: "error", message: runtimeErrorMessage(error)});
      return false;
    } finally { if (this.flight === controller) this.flight = undefined; this.reconcile(); this.options.onState(this.state); }
  }
  private update(state: RemoteLearningState): void { this.state = state; this.options.onState(state); }
  dispose(): void { this.disposed = true; this.flight?.abort(); this.scope = undefined; this.update({phase: "idle"}); }
}
