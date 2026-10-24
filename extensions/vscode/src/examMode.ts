/** Exam Control is a separate trust boundary: no Autograde credentials or persistent secrets. */
export const EXAM_MODE_COMMAND = "autograde.openExamMode";
export const EXAM_POLL_MS = 5_000;
export const EXAM_STALE_MS = 15_000;
const MAX_RESPONSE_BYTES = 64 * 1024;

export interface ExamScope {
  readonly generation: number;
  readonly assignmentId: string;
  readonly autogradeOrigin: string;
}
export interface ExamStatus {
  readonly exam: { readonly id: string; readonly title: string; readonly status: string };
  readonly seat_id: string;
  readonly assignment_id: string;
  readonly autograde_origin: string;
  readonly server_time: number;
  readonly expires_at: number;
  readonly agent: {
    readonly online: boolean; readonly mode: "simulation" | "enforced";
    readonly backend: string | null; readonly state: string;
    readonly last_seen: number | null; readonly lease_expires_at: number | null;
    readonly verified: false;
  };
  readonly permissions: { readonly can_release: false };
}
export interface ExamViewState {
  readonly phase: "disconnected" | "connecting" | "connected" | "error";
  readonly origin?: string;
  readonly status?: ExamStatus;
  readonly checkedAt?: number;
  readonly fresh: boolean;
  readonly leaseRemainingSeconds?: number;
  readonly message?: string;
}
export class ExamError extends Error {
  public constructor(message: string, public readonly status?: number) { super(message); this.name = "ExamError"; }
}

function object(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ExamError("시험 서버 응답 형식이 올바르지 않습니다.");
  return value as Record<string, unknown>;
}
function text(value: unknown, limit = 256): string {
  if (typeof value !== "string" || !value.trim() || value.length > limit || /[\u0000-\u001f\u007f]/.test(value)) {
    throw new ExamError("시험 서버 응답 값이 올바르지 않습니다.");
  }
  return value;
}
function timestamp(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0 || value > 253402300799) {
    throw new ExamError("시험 서버의 시간 정보가 올바르지 않습니다.");
  }
  return value;
}
function optionalTimestamp(value: unknown): number | null { return value === null ? null : timestamp(value); }

export function normalizeExamOrigin(input: string): string {
  try {
    if (input.length > 2048 || /[\u0000-\u001f\u007f\\]/.test(input)) throw new Error();
    const trimmed = input.trim();
    if (!/^https?:\/\/[^/?#]+\/?$/i.test(trimmed)) throw new Error();
    const url = new URL(trimmed);
    const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(url.hostname);
    if (url.protocol !== "https:" && !(url.protocol === "http:" && loopback)) throw new Error();
    if (url.username || url.password || url.pathname !== "/" || url.search || url.hash) throw new Error();
    return url.origin;
  } catch {
    throw new ExamError("시험 관리 서버는 HTTPS 주소와 포트만 입력하세요. HTTP는 localhost·127.0.0.1·[::1]에서만 허용합니다.");
  }
}

export function parseExamStatus(payload: unknown, scope: ExamScope): ExamStatus {
  const value = object(payload); const exam = object(value.exam); const agent = object(value.agent);
  const permissions = object(value.permissions);
  if (value.assignment_id !== scope.assignmentId || value.autograde_origin !== scope.autogradeOrigin) {
    throw new ExamError("시험의 채점 서버 또는 과제가 현재 연결과 다릅니다. 감독자에게 새 연결 코드를 요청하세요.");
  }
  if (typeof agent.online !== "boolean" || !["simulation", "enforced"].includes(String(agent.mode))
    || agent.verified !== false || permissions.can_release !== false) throw new ExamError("지원하지 않는 시험 권한 또는 Agent 응답입니다.");
  const serverTime = timestamp(value.server_time); const expiresAt = timestamp(value.expires_at);
  if (expiresAt <= serverTime || expiresAt - serverTime > 24 * 60 * 60) throw new ExamError("시험 연결이 만료되었거나 유효 시간이 올바르지 않습니다.");
  return {
    exam: {id: text(exam.id), title: text(exam.title, 512), status: text(exam.status)},
    seat_id: text(value.seat_id), assignment_id: scope.assignmentId, autograde_origin: scope.autogradeOrigin,
    server_time: serverTime, expires_at: expiresAt,
    agent: {online: agent.online, mode: agent.mode as "simulation" | "enforced",
      backend: agent.backend === null ? null : text(agent.backend), state: text(agent.state),
      last_seen: optionalTimestamp(agent.last_seen), lease_expires_at: optionalTimestamp(agent.lease_expires_at), verified: false},
    permissions: {can_release: false},
  };
}

/** Only three fixed endpoints. Redirects/cookies are forbidden, body and time are bounded. */
export class ExamClient {
  public readonly origin: string;
  public constructor(origin: string, private readonly fetcher: typeof fetch = globalThis.fetch.bind(globalThis), private readonly timeoutMs = 8_000) {
    this.origin = normalizeExamOrigin(origin);
    if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) throw new ExamError("잘못된 요청 제한 시간입니다.");
  }
  private async request(endpoint: "connect" | "status" | "disconnect", token?: string, body?: unknown, signal?: AbortSignal): Promise<unknown> {
    const controller = new AbortController();
    const onAbort = (): void => controller.abort();
    signal?.addEventListener("abort", onAbort, {once: true});
    if (signal?.aborted) controller.abort();
    const timeout = setTimeout(() => controller.abort(), this.timeoutMs);
    const wait = async <T>(promise: Promise<T>): Promise<T> => {
      if (controller.signal.aborted) throw new ExamError("시험 서버 요청이 취소되었거나 응답 시간이 초과되었습니다.");
      let abort!: () => void;
      const cancelled = new Promise<never>((_resolve, reject) => {
        abort = () => reject(new ExamError("시험 서버 요청이 취소되었거나 응답 시간이 초과되었습니다."));
        controller.signal.addEventListener("abort", abort, {once: true});
      });
      try { return await Promise.race([promise, cancelled]); }
      finally { controller.signal.removeEventListener("abort", abort); }
    };
    let reader: ReadableStreamDefaultReader<Uint8Array> | undefined;
    let responseBody: ReadableStream<Uint8Array> | null | undefined;
    try {
      if (controller.signal.aborted) throw new ExamError("시험 서버 요청이 취소되었습니다.");
      const response = await wait(this.fetcher(`${this.origin}/api/ide/${endpoint}`, {
        method: endpoint === "status" ? "GET" : "POST", redirect: "error", credentials: "omit", cache: "no-store",
        headers: {Accept: "application/json", Origin: this.origin, ...(body ? {"Content-Type": "application/json"} : {}),
          ...(token ? {Authorization: `Bearer ${token}`} : {})},
        body: body ? JSON.stringify(body) : undefined, signal: controller.signal,
      }));
      responseBody = response.body;
      if (response.redirected || (response.url && new URL(response.url).origin !== this.origin)) throw new ExamError("시험 서버의 주소 이동은 허용되지 않습니다.");
      if (!response.ok) throw new ExamError(response.status === 401 || response.status === 403
        ? "시험 연결이 거절되었거나 만료되었습니다. 감독자에게 새 코드를 요청하세요."
        : `시험 서버 요청에 실패했습니다 (HTTP ${response.status}). 잠시 후 다시 확인하세요.`, response.status);
      if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get("Content-Type") ?? "")) throw new ExamError("시험 서버가 JSON이 아닌 응답을 보냈습니다. 주소·포트를 확인하세요.");
      const length = response.headers.get("Content-Length");
      if (length && (!/^\d+$/.test(length) || Number(length) > MAX_RESPONSE_BYTES)) throw new ExamError("시험 서버 응답이 너무 큽니다.");
      reader = response.body?.getReader();
      if (!reader) throw new ExamError("시험 서버 응답이 비어 있습니다.");
      const chunks: Uint8Array[] = []; let bytes = 0;
      while (true) {
        const part = await wait(reader.read());
        if (part.done) break;
        bytes += part.value.byteLength;
        if (bytes > MAX_RESPONSE_BYTES) throw new ExamError("시험 서버 응답이 너무 큽니다.");
        chunks.push(part.value);
      }
      const envelope = object(JSON.parse(Buffer.concat(chunks).toString("utf8")));
      if (envelope.ok !== true) throw new ExamError("시험 서버가 요청을 승인하지 않았습니다.");
      return envelope.result;
    } catch (error) {
      if (error instanceof ExamError) throw error;
      throw new ExamError("시험 관리 서버에 연결할 수 없습니다. 주소·인증서·네트워크를 확인하세요.");
    } finally {
      clearTimeout(timeout); signal?.removeEventListener("abort", onAbort);
      void reader?.cancel().catch(() => undefined);
      if (!reader) void responseBody?.cancel().catch(() => undefined);
    }
  }
  public async connect(code: string, scope: ExamScope, signal?: AbortSignal): Promise<{token: string; status: ExamStatus}> {
    if (!/^\d{8}$/.test(code)) throw new ExamError("감독자가 발급한 8자리 숫자 시험 연결 코드를 입력하세요. 과제 수령 코드와 다릅니다.");
    const result = object(await this.request("connect", undefined, {code, assignment_id: scope.assignmentId, autograde_origin: scope.autogradeOrigin}, signal));
    const token = text(result.token, 2048);
    if (!/^[A-Za-z0-9._~-]{16,2048}$/.test(token)) throw new ExamError("시험 연결 토큰의 형식이 올바르지 않습니다.");
    const status = parseExamStatus(result.status, scope);
    if (timestamp(result.expires_at) !== status.expires_at) throw new ExamError("시험 연결 만료 정보가 서로 다릅니다.");
    return {token, status};
  }
  public async status(token: string, scope: ExamScope, signal?: AbortSignal): Promise<ExamStatus> {
    return parseExamStatus(await this.request("status", token, undefined, signal), scope);
  }
  public async disconnect(token: string): Promise<void> {
    const value = object(await this.request("disconnect", token, {}));
    if (value.disconnected !== true) throw new ExamError("시험 화면 연결 종료를 확인하지 못했습니다.");
  }
}

interface Session { readonly client: ExamClient; readonly token: string; readonly scope: ExamScope; readonly deadline: number; readonly examId: string; readonly seatId: string }
function sameScope(first: ExamScope | undefined, second: ExamScope | undefined): boolean {
  return !!first && !!second && first.generation === second.generation && first.assignmentId === second.assignmentId && first.autogradeOrigin === second.autogradeOrigin;
}

export class ExamSession {
  private generation = 0;
  private session?: Session;
  private controller?: AbortController;
  private timer?: ReturnType<typeof setTimeout>;
  private checkedMono?: number;
  private statusValue?: ExamStatus;
  private phase: ExamViewState["phase"] = "disconnected";
  private message?: string;
  private checkedAt?: number;
  private pendingScope?: ExamScope;
  private disposed = false;
  private polling = false;
  public constructor(private readonly currentScope: () => ExamScope | undefined, private readonly changed: () => void = () => {},
    private readonly now = () => performance.now(), private readonly wallNow = () => Date.now()) {}

  public reconcile(): void {
    if ((this.session && !sameScope(this.session.scope, this.currentScope()))
      || (this.pendingScope && !sameScope(this.pendingScope, this.currentScope()))) this.clear();
    if (this.session && this.now() >= this.session.deadline) this.clear("시험 화면 연결 시간이 만료되었습니다. 감독자에게 새 코드를 요청하세요.");
  }
  public snapshot(): ExamViewState {
    this.reconcile();
    const age = this.checkedMono === undefined ? Infinity : this.now() - this.checkedMono;
    const fresh = this.phase === "connected" && age >= 0 && age <= EXAM_STALE_MS;
    const status = this.statusValue;
    return {phase: this.phase, origin: this.session?.client.origin, status, checkedAt: this.checkedAt, fresh, message: this.message,
      leaseRemainingSeconds: status?.agent.lease_expires_at === null || !status || !fresh ? undefined
        : Math.max(0, Math.ceil(status.agent.lease_expires_at - status.server_time - age / 1000))};
  }
  public async connect(client: ExamClient, code: string): Promise<void> {
    this.clear();
    if (this.disposed) return;
    const scope = this.currentScope();
    if (!scope) throw new ExamError("Autograde에 로그인하고 현재 수락한 과제를 먼저 선택하세요.");
    const generation = this.generation;
    const requestStarted = this.now();
    this.pendingScope = scope; this.phase = "connecting"; this.controller = new AbortController(); this.changed();
    try {
      const result = await client.connect(code, scope, this.controller.signal);
      if (this.disposed || generation !== this.generation || !sameScope(scope, this.currentScope())) {
        void client.disconnect(result.token).catch(() => undefined);
        if (generation === this.generation) this.clear();
        return;
      }
      const deadline = requestStarted + (result.status.expires_at - result.status.server_time) * 1000;
      if (this.now() >= deadline) { void client.disconnect(result.token).catch(() => undefined); throw new ExamError("시험 연결이 이미 만료되었습니다."); }
      this.session = {client, token: result.token, scope, deadline, examId: result.status.exam.id, seatId: result.status.seat_id};
      this.acceptStatus(result.status, requestStarted); this.schedule();
    } catch (error) {
      if (generation !== this.generation || this.disposed) return;
      if (!sameScope(scope, this.currentScope())) { this.clear(); return; }
      this.phase = "error"; this.message = error instanceof ExamError ? error.message : "시험 연결에 실패했습니다.";
      throw new ExamError(this.message);
    } finally {
      if (generation === this.generation) { this.pendingScope = undefined; this.controller = undefined; this.changed(); }
    }
  }
  private acceptStatus(status: ExamStatus, requestStarted: number): void {
    this.statusValue = status; this.checkedMono = requestStarted; this.checkedAt = this.wallNow(); this.phase = "connected"; this.message = undefined;
  }
  private schedule(): void {
    if (this.timer) clearTimeout(this.timer);
    if (!this.session || this.disposed) return;
    this.timer = setTimeout(() => { this.timer = undefined; void this.refresh(); }, EXAM_POLL_MS);
    this.timer.unref?.();
  }
  public async refresh(): Promise<void> {
    this.reconcile();
    const session = this.session; if (!session || this.polling || this.disposed) return;
    if (this.timer) { clearTimeout(this.timer); this.timer = undefined; }
    const generation = this.generation;
    const requestStarted = this.now();
    this.polling = true; this.controller = new AbortController();
    try {
      const status = await session.client.status(session.token, session.scope, this.controller.signal);
      if (generation !== this.generation || this.disposed) return;
      if (!sameScope(session.scope, this.currentScope())) { this.clear(); return; }
      if (status.exam.id !== session.examId || status.seat_id !== session.seatId) throw new ExamError("시험 또는 좌석 정보가 변경되었습니다. 감독자에게 새 코드를 요청하세요.", 401);
      if (this.now() >= session.deadline) { this.reconcile(); return; }
      const deadline = Math.min(session.deadline, requestStarted + (status.expires_at - status.server_time) * 1000);
      this.session = {...session, deadline};
      this.acceptStatus(status, requestStarted);
    } catch (error) {
      if (generation !== this.generation || this.disposed) return;
      if (!sameScope(session.scope, this.currentScope())) { this.clear(); return; }
      if (error instanceof ExamError && (error.status === 401 || error.status === 403)) {
        this.clear(error.message); return;
      }
      this.phase = "error"; this.message = error instanceof ExamError ? error.message : "시험 상태를 확인하지 못했습니다.";
    } finally {
      if (generation === this.generation) { this.polling = false; this.controller = undefined; this.changed(); this.schedule(); }
    }
  }
  /** Disconnects this UI session, NEVER Agent filters or the exam. */
  public clear(message?: string): void {
    const previous = this.session; this.generation++; this.session = undefined; this.pendingScope = undefined;
    this.controller?.abort(); this.controller = undefined; this.polling = false;
    if (this.timer) clearTimeout(this.timer); this.timer = undefined;
    this.statusValue = undefined; this.checkedMono = undefined; this.checkedAt = undefined;
    this.phase = "disconnected"; this.message = message;
    if (previous) void previous.client.disconnect(previous.token).catch(() => undefined);
    this.changed();
  }
  public dispose(): void { this.disposed = true; this.clear(); }
}

export function examAgentLabel(state: ExamViewState): string {
  if (!state.status) return state.phase === "connecting" ? "시험 연결 중" : "시험 미연결";
  if (!state.fresh) return "상태 확인 불가 · 이전 응답";
  const agent = state.status.agent;
  if (!agent.online) return "Agent 오프라인 · 현재 제한 확인 불가";
  if (agent.mode === "simulation") return "모의 모드 · 실제 통신 차단 아님";
  if (agent.state === "installed") return state.leaseRemainingSeconds && state.leaseRemainingSeconds > 0
    ? "제한 설치 보고 · 실제 차단 미검증" : "제한 기한 경과 · 해제 여부 확인 필요";
  if (agent.state === "released") return "제한 해제 보고 · 실제 통신 미검증";
  if (agent.state === "recovery_required") return "복구 필요 · 감독자에게 문의";
  return "제한 상태 미확인 · 감독자에게 문의";
}
