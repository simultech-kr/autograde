import type { ClassroomScope } from "./classroomActivity";

export const CLASSROOM_COMMAND_KINDS = ["notice", "open_assignment", "sync_materials", "check_environment", "save_submit"] as const;
export type ClassroomCommandKind = typeof CLASSROOM_COMMAND_KINDS[number];
export interface ClassroomCommandContext extends ClassroomScope { readonly clientSessionId: string; readonly workspaceKey?: string }
export interface ClassroomCommand {
  readonly command_id: string;
  readonly kind: ClassroomCommandKind;
  readonly assignment_id: string | null;
  readonly message: string;
  readonly status: "received" | "accepted";
  readonly created_by: string;
  readonly expires_at: string;
  /** Derived locally from server_time, never sent back or persisted. */
  readonly localDeadline?: number;
}
export interface ClassroomCommandChecks {
  readonly server: "ok" | "failed";
  readonly workspace: "ok" | "missing" | "untrusted";
  readonly compiler: "available" | "missing" | "not_checked";
  readonly os: "macos" | "linux" | "windows" | "other";
  readonly remote: "none" | "wsl" | "ssh-remote" | "other";
}
export type ClassroomCommandResultCode = "notice_acknowledged" | "assignment_opened" | "materials_synced" | "environment_checked" | "submission_received" |
  "student_declined" | "cancelled" | "action_failed" | "scope_changed" | "workspace_unavailable" | "unsupported" | "submission_unconfirmed";
export interface ClassroomCommandResponse {
  readonly status: "accepted" | "declined" | "completed" | "failed";
  readonly result_code?: ClassroomCommandResultCode;
  readonly result?: { readonly submission_id?: string; readonly checks?: ClassroomCommandChecks };
}
export interface ClassroomCommandsClient {
  pollClassroomCommands(context: ClassroomCommandContext, signal: AbortSignal): Promise<readonly ClassroomCommand[]>;
  respondClassroomCommand(id: string, response: ClassroomCommandResponse, context: ClassroomCommandContext, signal: AbortSignal): Promise<void>;
}
export interface ClassroomCommandClock {
  now(): number;
  setTimeout(callback: () => void, ms: number): ReturnType<typeof setTimeout>;
  clearTimeout(timer: ReturnType<typeof setTimeout>): void;
}
const systemClock: ClassroomCommandClock = {now: Date.now, setTimeout, clearTimeout};
export const CLASSROOM_COMMAND_LABELS: Record<ClassroomCommandKind, string> = {
  notice: "공지 확인", open_assignment: "현재 연결 과제 열기", sync_materials: "강의자료 동기화",
  check_environment: "학습 환경 점검", save_submit: "현재 과제 저장·제출",
};
const successCodes: Record<ClassroomCommandKind, ClassroomCommandResultCode> = {
  notice: "notice_acknowledged", open_assignment: "assignment_opened", sync_materials: "materials_synced",
  check_environment: "environment_checked", save_submit: "submission_received",
};
const safeId = /^[A-Za-z0-9_-]{1,128}$/;

/** Treat messages as text, never commands, paths, URIs, Markdown or executable instructions. */
export function parseClassroomCommands(payload: unknown, receivedAt = Date.now()): readonly ClassroomCommand[] {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) throw new Error("잘못된 교수자 요청 응답입니다.");
  const envelope = payload as Record<string, unknown>, rows = envelope.commands;
  const serverTime = typeof envelope.server_time === "string" ? Date.parse(envelope.server_time) : NaN;
  if (!Number.isFinite(serverTime)) throw new Error("교수자 요청의 서버 시각이 올바르지 않습니다.");
  if (!Array.isArray(rows) || rows.length > 20) throw new Error("교수자 요청 목록을 확인할 수 없습니다.");
  const ids = new Set<string>();
  return rows.map(value => {
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("잘못된 교수자 요청입니다.");
    const row = value as Record<string, unknown>;
    if (typeof row.command_id !== "string" || !safeId.test(row.command_id) || ids.has(row.command_id)
      || !CLASSROOM_COMMAND_KINDS.includes(row.kind as ClassroomCommandKind)
      || !["received", "accepted"].includes(String(row.status))
      || !(row.assignment_id === null || typeof row.assignment_id === "string" && safeId.test(row.assignment_id))
      || row.kind !== "notice" && row.assignment_id === null
      || typeof row.message !== "string" || row.message.length > 1000
      || typeof row.created_by !== "string" || row.created_by.length > 256
      || typeof row.expires_at !== "string" || !Number.isFinite(Date.parse(row.expires_at))) {
      throw new Error("교수자 요청 형식이 올바르지 않습니다.");
    }
    ids.add(row.command_id);
    return {command_id: row.command_id, kind: row.kind as ClassroomCommandKind, assignment_id: row.assignment_id as string | null,
      message: row.message, status: row.status as ClassroomCommand["status"], created_by: row.created_by, expires_at: row.expires_at,
      localDeadline: receivedAt + Math.max(0, Math.min(600000, Date.parse(row.expires_at) - serverTime))};
  });
}

export class ClassroomActionError extends Error {
  public constructor(public readonly resultCode: Extract<ClassroomCommandResultCode, "cancelled" | "action_failed" | "scope_changed" | "workspace_unavailable" | "unsupported" | "submission_unconfirmed">) {
    super(resultCode);
  }
}
const sameContext = (a: ClassroomCommandContext | undefined, b: ClassroomCommandContext | undefined) =>
  a?.generation === b?.generation && a?.baseUrl === b?.baseUrl && a?.assignmentId === b?.assignmentId && a?.clientSessionId === b?.clientSessionId && a?.workspaceKey === b?.workspaceKey;

/** Memory-only, one prompt/action at a time. Lost ACKs may retry, actions never do. */
export class ClassroomCommands {
  private timer?: ReturnType<typeof setTimeout>;
  private flight?: AbortController;
  private context?: ClassroomCommandContext;
  private disposed = false;
  private busy = false;
  private failures = 0;
  private blocked = false;
  private readonly seen = new Set<string>();
  private readonly pending = new Map<string, ClassroomCommandResponse>();
  public constructor(
    private readonly client: ClassroomCommandsClient,
    private readonly current: () => ClassroomCommandContext | undefined,
    private readonly confirm: (command: ClassroomCommand, signal: AbortSignal) => Promise<boolean>,
    private readonly execute: (command: ClassroomCommand, assertCurrent: () => void, signal: AbortSignal) => Promise<ClassroomCommandResponse>,
    private readonly status: (text: string) => void = () => {},
    private readonly clock: ClassroomCommandClock = systemClock,
  ) {}

  public reconcile(): void {
    if (this.disposed) return;
    const next = this.current();
    if (!sameContext(next, this.context)) {
      this.flight?.abort(); this.context = next ? {...next} : undefined;
      this.seen.clear(); this.pending.clear(); this.failures = 0; this.blocked = false;
      this.status(next ? "교수자 요청 수신 대기 · 실행 전 확인" : "교수자 요청 대기 · 과제 연결 필요");
    }
    if (!this.busy && !this.blocked) this.schedule(0);
  }
  public dispose(): void {
    this.disposed = true; this.flight?.abort();
    if (this.timer) this.clock.clearTimeout(this.timer);
    this.seen.clear(); this.pending.clear(); this.context = undefined;
  }
  private schedule(ms: number): void {
    if (this.timer) this.clock.clearTimeout(this.timer);
    if (!this.disposed) this.timer = this.clock.setTimeout(() => {this.timer = undefined; void this.pump();}, ms);
  }
  private async pump(): Promise<void> {
    if (this.disposed || this.busy || this.blocked) return;
    const next = this.current();
    if (!sameContext(next, this.context)) {this.reconcile(); return;}
    const context = this.context;
    if (!context) {this.schedule(5000); return;}
    this.busy = true; const controller = new AbortController(); this.flight = controller;
    let deadlineTimer: ReturnType<typeof setTimeout> | undefined;
    const guard = () => {
      if (this.disposed || controller.signal.aborted || !sameContext(context, this.current())) throw new ClassroomActionError("scope_changed");
    };
    try {
      const commands = await this.client.pollClassroomCommands(context, controller.signal); guard();
      this.failures = 0;
      for (const command of commands) {
        if (deadlineTimer) this.clock.clearTimeout(deadlineTimer);
        deadlineTimer = undefined;
        guard();
        if (command.assignment_id !== null && command.assignment_id !== context.assignmentId) throw new ClassroomActionError("scope_changed");
        const deadline = command.localDeadline ?? Date.parse(command.expires_at);
        const assertCurrent = () => {guard(); if (deadline <= this.clock.now()) throw new ClassroomActionError("cancelled");};
        if (deadline <= this.clock.now()) {this.pending.delete(command.command_id); continue;}
        const pending = this.pending.get(command.command_id);
        if (pending) {
          const sent = await this.sendFinal(command, pending, context, controller.signal); guard();
          this.pending.delete(command.command_id); this.status(sent.status === "completed" ? "교수자에게 처리 결과 전달 완료" : "교수자에게 취소·실패 결과 전달 완료"); continue;
        }
        // Received from the server as already accepted means a previous process
        // may have executed it. Never infer it is safe to execute again.
        if (command.status === "accepted" || this.seen.has(command.command_id)) continue;
        if (this.seen.size >= 1000) {this.blocked = true; this.status("교수자 요청 수신 한도 · 새 코드로 다시 연결하세요."); break;}
        this.seen.add(command.command_id);
        this.status(`${CLASSROOM_COMMAND_LABELS[command.kind]} · 학생 확인 대기`);
        deadlineTimer = this.clock.setTimeout(() => {
          controller.abort(); this.status("교수자 요청이 만료되어 이후 처리를 중단합니다. 진행 중이던 업로드는 접수될 수 있으므로 제출 이력을 확인하세요.");
        }, Math.max(0, deadline - this.clock.now()));
        const allowed = await this.confirm(command, controller.signal); assertCurrent();
        if (!allowed) {
          const response: ClassroomCommandResponse = {status: "declined", result_code: "student_declined"};
          this.pending.set(command.command_id, response);
          await this.client.respondClassroomCommand(command.command_id, response, context, controller.signal); guard();
          this.pending.delete(command.command_id); this.status("교수자 요청을 거절했습니다."); continue;
        }
        // If this acknowledgement is lost, do not execute or retry the action.
        await this.client.respondClassroomCommand(command.command_id, {status: "accepted"}, context, controller.signal); assertCurrent();
        this.status(`${CLASSROOM_COMMAND_LABELS[command.kind]} · 처리 중`);
        let response: ClassroomCommandResponse;
        try {
          response = await this.execute(command, assertCurrent, controller.signal); assertCurrent();
          if (response.status === "completed" && (response.result_code !== successCodes[command.kind]
            || command.kind === "save_submit" && !response.result?.submission_id)) throw new ClassroomActionError("action_failed");
          if (!["completed", "failed"].includes(response.status)) throw new ClassroomActionError("action_failed");
        } catch (error) {
          guard(); response = {status: "failed", result_code: error instanceof ClassroomActionError ? error.resultCode : "action_failed"};
        }
        guard(); this.pending.set(command.command_id, response);
        response = await this.sendFinal(command, response, context, controller.signal); guard();
        this.pending.delete(command.command_id);
        this.status(response.status === "completed" ? "교수자 요청 처리·결과 전달 완료" : "요청 취소 또는 처리 실패 · 교수자에게 전달했습니다.");
      }
    } catch (error) {
      if (sameContext(context, this.current()) && !this.disposed && !controller.signal.aborted) {
        const status = error && typeof error === "object" && "status" in error ? Number(error.status) : 0;
        if ([401, 403, 404, 405].includes(status)) {this.blocked = true; this.status("교수자 요청 수신 중단 · 인증 또는 서버 지원을 확인하세요.");}
        else {this.failures++; this.status("교수자 요청 응답 확인 지연 · 실행을 반복하지 않습니다. 필요한 경우 새 요청을 받아 주세요.");}
      }
    } finally {
      if (deadlineTimer) this.clock.clearTimeout(deadlineTimer);
      if (this.flight === controller) this.flight = undefined;
      this.busy = false;
      if (!sameContext(context, this.current())) this.reconcile();
      else if (!this.disposed && !this.blocked) this.schedule(Math.min(60000, 5000 * 2 ** Math.min(this.failures, 4)));
    }
  }
  private async sendFinal(command: ClassroomCommand, response: ClassroomCommandResponse, context: ClassroomCommandContext, signal: AbortSignal): Promise<ClassroomCommandResponse> {
    try {await this.client.respondClassroomCommand(command.command_id, response, context, signal); return response;}
    catch (error) {
      // A semantic submission retry can return a receipt predating this request.
      // The server is authoritative: never call that a newly completed submission.
      const status = error && typeof error === "object" && "status" in error ? Number(error.status) : 0;
      if (status !== 409 || command.kind !== "save_submit" || response.status !== "completed") throw error;
      const failed: ClassroomCommandResponse = {status: "failed", result_code: "submission_unconfirmed"};
      this.pending.set(command.command_id, failed);
      await this.client.respondClassroomCommand(command.command_id, failed, context, signal);
      return failed;
    }
  }
}
