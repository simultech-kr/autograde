import { randomUUID } from "node:crypto";
import { lstat, realpath } from "node:fs/promises";
import * as path from "node:path";
import type { ClassroomCommandContext } from "./classroomCommands";

export type ClassroomEventKind = "heartbeat" | "workspace_opened" | "file_saved" |
  "submission_started" | "submission_finished" | "session_closed";
export interface ClassroomEnvironment {
  readonly os: "macos" | "linux" | "windows";
  readonly ide: "vscode" | "autograde";
  readonly remote_name?: string;
}
export interface ClassroomEvent {
  readonly client_session_id: string;
  readonly sequence: number;
  readonly kind: ClassroomEventKind;
  readonly assignment_id: string;
  readonly environment: ClassroomEnvironment;
}
export interface ClassroomScope {
  readonly baseUrl: string;
  readonly generation: number;
  readonly assignmentId: string;
}
export type ClassroomSharingStatus = "inactive" | "sharing" | "retrying" | "unavailable";
export interface ClassroomEventClient {
  reportClassroomEvent(event: ClassroomEvent, scope: ClassroomScope, signal: AbortSignal): Promise<void>;
}
interface Clock {
  now(): number;
  setTimeout(callback: () => void, milliseconds: number): ReturnType<typeof setTimeout>;
  clearTimeout(timer: ReturnType<typeof setTimeout>): void;
}
const clock: Clock = { now: Date.now, setTimeout, clearTimeout };
export const CLASSROOM_HEARTBEAT_MS = 20_000;
export const CLASSROOM_SAVE_COALESCE_MS = 5_000;
const MAX_RETRY_MS = 120_000;
const MAX_PENDING_EVENTS = 8;

/** Ephemeral, bounded, best-effort activity hints; never a submission receipt or code backup. */
export class ClassroomActivityReporter {
  private scope?: ClassroomScope;
  private sessionId = "";
  private sequence = 0;
  private pending: ClassroomEventKind[] = [];
  private timer?: ReturnType<typeof setTimeout>;
  private saveTimer?: ReturnType<typeof setTimeout>;
  private flight?: AbortController;
  private blocked = false;
  private disposed = false;
  private failures = 0;
  private retryAt = 0;
  private status: ClassroomSharingStatus = "inactive";
  private registered = false;

  public constructor(
    private readonly client: ClassroomEventClient,
    private readonly currentScope: () => ClassroomScope | undefined,
    private readonly environment: ClassroomEnvironment,
    private readonly onStatus: (status: ClassroomSharingStatus) => void = () => {},
    private readonly time: Clock = clock,
    private readonly newSessionId: () => string = randomUUID,
  ) {}

  public getStatus(): ClassroomSharingStatus { return this.status; }

  /** Commands share the authenticated, server-registered presence identity. */
  public getCommandContext(): ClassroomCommandContext | undefined {
    this.reconcile();
    return this.scope && this.registered && !this.blocked && !this.disposed
      ? {...this.scope, clientSessionId: this.sessionId} : undefined;
  }

  /** Re-read identity before every timer and request. No event survives an identity boundary. */
  public reconcile(): void {
    if (this.disposed) return;
    let next: ClassroomScope | undefined;
    try { next = this.currentScope(); } catch { next = undefined; }
    if (sameScope(this.scope, next)) return;
    this.reset();
    this.scope = next ? { ...next } : undefined;
    if (!next) { this.setStatus("inactive"); return; }
    this.sessionId = this.newSessionId();
    this.pending.push("workspace_opened");
    void this.pump();
  }

  public record(kind: Exclude<ClassroomEventKind, "heartbeat" | "workspace_opened" | "session_closed">, assignmentId: string): void {
    this.reconcile();
    if (this.disposed || this.blocked || this.scope?.assignmentId !== assignmentId) return;
    if (kind === "file_saved") {
      if (!this.saveTimer) this.saveTimer = this.time.setTimeout(() => {
        this.saveTimer = undefined;
        this.reconcile();
        if (this.scope?.assignmentId === assignmentId) this.enqueue("file_saved");
      }, CLASSROOM_SAVE_COALESCE_MS);
      return;
    }
    this.enqueue(kind);
  }

  public dispose(): void {
    this.disposed = true;
    this.reset();
    this.scope = undefined;
    this.setStatus("inactive");
    // Extension hosts may terminate immediately. Presence expires server-side;
    // do not promise reliable delivery of a shutdown event or retain credentials.
  }

  private enqueue(kind: ClassroomEventKind): void {
    if (!this.scope || this.blocked || this.disposed) return;
    if (!this.pending.includes(kind) && this.pending.length < MAX_PENDING_EVENTS) this.pending.push(kind);
    void this.pump();
  }

  private async pump(): Promise<void> {
    this.reconcile();
    if (this.disposed || !this.scope || this.blocked || this.flight) return;
    if (this.retryAt > this.time.now()) { this.schedule(this.retryAt - this.time.now()); return; }
    const scope = this.scope;
    const sessionId = this.sessionId;
    const kind = this.pending.shift() ?? "heartbeat";
    const controller = new AbortController();
    this.flight = controller;
    const event: ClassroomEvent = {
      client_session_id: sessionId, sequence: ++this.sequence, kind,
      assignment_id: scope.assignmentId, environment: this.environment,
    };
    try {
      await this.client.reportClassroomEvent(event, scope, controller.signal);
      if (this.isCurrent(scope, sessionId)) {
        this.registered = true;
        this.failures = 0; this.retryAt = 0;
        this.setStatus("sharing");
      }
    } catch (error) {
      if (this.isCurrent(scope, sessionId) && !controller.signal.aborted) {
        const status = error && typeof error === "object" && "status" in error ? Number(error.status) : 0;
        // Missing capability, denied scope, expired session, or invalid protocol:
        // stop until a new scope; repeated activity must not hammer an old server.
        if (status >= 400 && status < 500 && status !== 408 && status !== 429) {
          this.blocked = true;
          this.pending = [];
          this.setStatus("unavailable");
        } else {
          this.failures += 1;
          const advertised = error && typeof error === "object" && "retryAfterMs" in error ? Number(error.retryAfterMs) : 0;
          const retry = Math.max(CLASSROOM_HEARTBEAT_MS, Math.min(MAX_RETRY_MS,
            Number.isFinite(advertised) && advertised > 0 ? advertised : CLASSROOM_HEARTBEAT_MS * 2 ** Math.min(this.failures - 1, 3)));
          this.retryAt = this.time.now() + retry;
          // Do not replay old offline activities under a later session or date.
          this.pending = [];
          this.setStatus("retrying");
        }
      }
    } finally {
      if (this.flight === controller) this.flight = undefined;
      this.reconcile();
      if (!this.disposed && this.scope && !this.blocked) {
        if (this.pending.length && this.retryAt <= this.time.now()) void this.pump();
        else this.schedule(Math.max(CLASSROOM_HEARTBEAT_MS, this.retryAt - this.time.now()));
      }
    }
  }

  private isCurrent(scope: ClassroomScope, sessionId: string): boolean {
    this.reconcile();
    return !this.disposed && sameScope(scope, this.scope) && sessionId === this.sessionId;
  }

  private schedule(milliseconds: number): void {
    if (this.timer) this.time.clearTimeout(this.timer);
    this.timer = this.time.setTimeout(() => { this.timer = undefined; void this.pump(); }, milliseconds);
  }

  private reset(): void {
    if (this.timer) this.time.clearTimeout(this.timer);
    if (this.saveTimer) this.time.clearTimeout(this.saveTimer);
    this.timer = undefined; this.saveTimer = undefined;
    this.flight?.abort();
    this.pending = []; this.sequence = 0; this.blocked = false; this.failures = 0; this.retryAt = 0; this.registered = false;
  }

  private setStatus(value: ClassroomSharingStatus): void {
    if (this.status !== value) { this.status = value; this.onStatus(value); }
  }
}

function sameScope(left: ClassroomScope | undefined, right: ClassroomScope | undefined): boolean {
  return left?.baseUrl === right?.baseUrl && left?.generation === right?.generation && left?.assignmentId === right?.assignmentId;
}

/** Never transmit machine names, remote addresses, versions, paths, or free-form OS labels. */
export function classroomEnvironment(platform: string, appName: string, remoteName?: string): ClassroomEnvironment | undefined {
  const os = platform === "darwin" ? "macos" : platform === "win32" ? "windows" : platform === "linux" ? "linux" : undefined;
  if (!os) return undefined;
  const remote = new Set(["wsl", "ssh-remote", "dev-container", "attached-container", "tunnel"]).has(remoteName ?? "") ? remoteName : undefined;
  return { os, ide: /^Autograde IDE(?: Dev)?$/.test(appName) ? "autograde" : "vscode", ...(remote ? { remote_name: remote } : {}) };
}

export function isClassroomFileUri(
  file: { readonly scheme: string; readonly authority?: string },
  workspace: { readonly scheme: string; readonly authority?: string } | undefined,
): boolean {
  return Boolean(workspace && ["file", "vscode-remote"].includes(workspace.scheme)
    && file.scheme === workspace.scheme && (file.authority ?? "") === (workspace.authority ?? ""));
}

/** Only regular files under the explicitly connected assignment may emit a nameless save hint. */
export async function isManagedClassroomSave(rootPath: string, filePath: string): Promise<boolean> {
  const relative = path.relative(path.resolve(rootPath), path.resolve(filePath));
  if (!relative || relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) return false;
  if (relative.split(path.sep).some(segment => segment.startsWith(".") || ["node_modules", "build", "dist", "out"].includes(segment))) return false;
  const stat = await lstat(filePath).catch(() => undefined);
  if (!stat?.isFile() || stat.isSymbolicLink()) return false;
  const [root, file] = await Promise.all([realpath(rootPath).catch(() => undefined), realpath(filePath).catch(() => undefined)]);
  if (!root || !file) return false;
  const actual = path.relative(root, file);
  return actual !== "" && actual !== ".." && !actual.startsWith(`..${path.sep}`) && !path.isAbsolute(actual);
}
