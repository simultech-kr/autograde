import type * as vscode from "vscode";

import type { ApiTransport } from "./api";

export const SERVER_HEALTH_TIMEOUT_MS = 5_000;
export const SERVER_HEALTH_INTERVAL_MS = 30_000;
export const SERVER_HEALTH_JITTER_MS = 3_000;
export const MAX_SERVER_HEALTH_RESPONSE_BYTES = 1_024;
export const CHECK_SERVER_CONNECTION_COMMAND = "autograde.checkServerConnection";

type ConnectionState = "checking" | "connected" | "disconnected";
type CancelTimer = () => void;
type TimerFactory = (callback: () => void, milliseconds: number) => CancelTimer;

const defaultTimerFactory: TimerFactory = (callback, milliseconds) => {
  const handle = setTimeout(callback, milliseconds);
  return () => clearTimeout(handle);
};

/**
 * Periodically checks the public health endpoint without using student tokens.
 *
 * Authentication and reachability are deliberately separate: this class never
 * reads a token and its status-bar wording always describes the server only.
 */
export class ServerConnectionMonitor implements vscode.Disposable {
  private cancelScheduledCheck: CancelTimer | undefined;
  private activeCheck: Promise<void> | undefined;
  private activeAbort: AbortController | undefined;
  private addressGeneration = 0;
  private disposed = false;
  private started = false;
  private lastChecked: { readonly origin: string; readonly at: Date } | undefined;

  public constructor(
    private readonly transport: ApiTransport,
    private readonly statusBar: vscode.StatusBarItem,
    private readonly timerFactory: TimerFactory = defaultTimerFactory,
    private readonly now: () => Date = () => new Date(),
    private readonly random: () => number = Math.random,
  ) {
    this.statusBar.name = "Autograde 서버 연결 상태";
    this.statusBar.command = CHECK_SERVER_CONNECTION_COMMAND;
  }

  /** Show the indicator, check immediately, then repeat every 30 seconds. */
  public start(): void {
    if (this.disposed || this.started) {
      return;
    }
    this.started = true;
    this.statusBar.show();
    void this.checkNow().finally(() => this.scheduleNextCheck());
  }

  /**
   * Check once. Concurrent callers share the existing request rather than
   * starting overlapping network traffic.
   */
  public checkNow(): Promise<void> {
    if (this.disposed) {
      return Promise.resolve();
    }
    if (this.activeCheck !== undefined) {
      return this.activeCheck;
    }

    const generation = this.addressGeneration;
    const controller = new AbortController();
    this.activeAbort = controller;
    const check = this.performCheck(generation, controller.signal);
    this.activeCheck = check;
    void check.finally(() => {
      if (this.activeCheck === check) {
        this.activeCheck = undefined;
        this.activeAbort = undefined;
      }
    });
    return check;
  }

  /**
   * Cancel a stale-origin request and check the newly configured origin. The
   * replacement waits for cancellation to settle, so health requests never
   * overlap.
   */
  public async handleAddressChange(): Promise<void> {
    if (this.disposed) {
      return;
    }
    this.addressGeneration += 1;
    const generation = this.addressGeneration;
    this.cancelNextCheck();
    this.render("checking", this.currentOrigin());
    const previousCheck = this.activeCheck;
    this.activeAbort?.abort();
    if (previousCheck !== undefined) {
      await previousCheck;
    }
    if (!this.disposed && generation === this.addressGeneration) {
      // A completion callback from the old request may have scheduled its next
      // cycle while cancellation settled. Replace it with a fresh-origin cycle.
      this.cancelNextCheck();
      await this.checkNow();
      this.scheduleNextCheck();
    }
  }

  public dispose(): void {
    if (this.disposed) {
      return;
    }
    this.disposed = true;
    this.addressGeneration += 1;
    this.cancelNextCheck();
    this.activeAbort?.abort();
    this.statusBar.dispose();
  }

  private async performCheck(generation: number, signal: AbortSignal): Promise<void> {
    let baseUrl: string;
    let origin: string;
    try {
      baseUrl = this.transport.getBaseUrl();
      origin = new URL(baseUrl).origin;
    } catch {
      if (this.shouldRender(generation)) {
        this.render("disconnected", "설정 오류", this.now());
      }
      return;
    }

    if (this.shouldRender(generation)) {
      this.render("checking", origin);
    }

    try {
      const healthBytes = await this.transport.requestBytes(
        "/healthz",
        {
          method: "GET",
          headers: { Accept: "application/json" },
          cache: "no-store",
        },
        undefined,
        {
          timeoutMs: SERVER_HEALTH_TIMEOUT_MS,
          signal,
          expectedBaseUrl: baseUrl,
          maxResponseBytes: MAX_SERVER_HEALTH_RESPONSE_BYTES,
          acceptedContentTypes: ["application/json"],
        },
      );
      if (!isHealthyResponse(healthBytes)) {
        throw new Error("invalid health response");
      }
      if (this.shouldRender(generation)) {
        this.render("connected", origin, this.now());
      }
    } catch {
      if (this.shouldRender(generation)) {
        this.render("disconnected", origin, this.now());
      }
    }
  }

  private shouldRender(generation: number): boolean {
    return !this.disposed && generation === this.addressGeneration;
  }

  private currentOrigin(): string {
    try {
      return new URL(this.transport.getBaseUrl()).origin;
    } catch {
      return "설정 오류";
    }
  }

  private render(state: ConnectionState, origin: string, checkedAt?: Date): void {
    if (this.disposed) {
      return;
    }
    if (state === "checking") {
      this.statusBar.text = "$(sync~spin) Autograde 서버: 확인 중";
    } else if (state === "connected") {
      this.statusBar.text = "$(pass-filled) Autograde 서버: 연결됨";
    } else {
      this.statusBar.text = "$(error) Autograde 서버: 연결 끊김";
    }
    if (checkedAt !== undefined) {
      this.lastChecked = { origin, at: checkedAt };
    }
    const lastCheckedAt = this.lastChecked?.origin === origin
      ? this.lastChecked.at
      : undefined;
    const checkedLine = lastCheckedAt === undefined
      ? "최근 확인: 아직 완료된 확인 없음"
      : `최근 확인: ${lastCheckedAt.toLocaleString()} (${lastCheckedAt.toISOString()})`;
    this.statusBar.tooltip = [
      `서버: ${origin}`,
      checkedLine,
      "이 표시는 로그인 여부가 아니라 서버 접속 상태입니다.",
      "약 30초 간격의 도달 가능성 확인이며 지속 연결 상태가 아닙니다.",
      "클릭하면 지금 다시 확인합니다.",
    ].join("\n");
  }

  private scheduleNextCheck(): void {
    if (this.disposed || !this.started || this.cancelScheduledCheck !== undefined) {
      return;
    }
    const random = this.random();
    const unit = Number.isFinite(random) ? Math.min(1, Math.max(0, random)) : 0.5;
    const delay = Math.round(
      SERVER_HEALTH_INTERVAL_MS + ((unit * 2) - 1) * SERVER_HEALTH_JITTER_MS,
    );
    this.cancelScheduledCheck = this.timerFactory(() => {
      this.cancelScheduledCheck = undefined;
      void this.checkNow().finally(() => this.scheduleNextCheck());
    }, delay);
  }

  private cancelNextCheck(): void {
    this.cancelScheduledCheck?.();
    this.cancelScheduledCheck = undefined;
  }
}

function isHealthyResponse(bytes: Uint8Array): boolean {
  try {
    const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)) as unknown;
    if (typeof value !== "object" || value === null || Array.isArray(value)) {
      return false;
    }
    const record = value as Record<string, unknown>;
    return Object.keys(record).length === 1 && record.status === "ok";
  } catch {
    return false;
  }
}
