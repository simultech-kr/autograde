import assert from "node:assert/strict";
import test from "node:test";

import type * as vscode from "vscode";

import { type ApiTransport, HttpTransport, type RequestOptions } from "../api";
import {
  CHECK_SERVER_CONNECTION_COMMAND,
  MAX_SERVER_HEALTH_RESPONSE_BYTES,
  SERVER_HEALTH_INTERVAL_MS,
  SERVER_HEALTH_JITTER_MS,
  SERVER_HEALTH_TIMEOUT_MS,
  ServerConnectionMonitor,
} from "../connectionMonitor";

interface TransportCall {
  readonly endpoint: string;
  readonly init: RequestInit;
  readonly bearerToken?: string;
  readonly options?: RequestOptions;
}

class FakeTransport implements ApiTransport {
  public readonly calls: TransportCall[] = [];

  public constructor(
    public baseUrl: string,
    private readonly handler: (call: TransportCall) => Promise<Uint8Array>,
  ) {}

  public getBaseUrl(): string {
    return this.baseUrl;
  }

  public async request<T>(
    _endpoint: string,
    _init: RequestInit,
    _bearerToken?: string,
    _options?: RequestOptions,
  ): Promise<T> {
    throw new Error("health check must not use the uncapped JSON request path");
  }

  public async requestBytes(
    endpoint: string,
    init: RequestInit,
    bearerToken?: string,
    options?: RequestOptions,
  ): Promise<Uint8Array> {
    const call = { endpoint, init, bearerToken, options };
    this.calls.push(call);
    return this.handler(call);
  }
}

class FakeStatusBar {
  public name = "";
  public command: string | undefined;
  public text = "";
  public tooltip: string | undefined;
  public showCalls = 0;
  public disposeCalls = 0;

  public show(): void {
    this.showCalls += 1;
  }

  public dispose(): void {
    this.disposeCalls += 1;
  }
}

interface MonitorFixture {
  readonly monitor: ServerConnectionMonitor;
  readonly statusBar: FakeStatusBar;
  readonly timer: {
    callback?: () => void;
    milliseconds?: number;
    cancelCalls: number;
  };
}

const CHECKED_AT = new Date("2026-09-06T03:04:05.000Z");
const HEALTHY_RESPONSE = new TextEncoder().encode('{"status":"ok"}');

test("start immediately checks the unauthenticated health endpoint and schedules 30-second checks", async () => {
  const transport = new FakeTransport(
    "https://203.0.113.10:20000",
    async () => HEALTHY_RESPONSE,
  );
  const fixture = createMonitor(transport);

  fixture.monitor.start();
  await fixture.monitor.checkNow();

  assert.equal(fixture.statusBar.showCalls, 1);
  assert.equal(fixture.statusBar.name, "Autograde 서버 연결 상태");
  assert.equal(fixture.statusBar.command, CHECK_SERVER_CONNECTION_COMMAND);
  assert.match(fixture.statusBar.text, /서버: 연결됨/);
  assert.match(fixture.statusBar.tooltip ?? "", /서버: https:\/\/203\.0\.113\.10:20000/);
  assert.match(fixture.statusBar.tooltip ?? "", /최근 확인:/);
  assert.match(fixture.statusBar.tooltip ?? "", /로그인 여부가 아니라 서버 접속 상태/);
  assert.equal(fixture.timer.milliseconds, SERVER_HEALTH_INTERVAL_MS);
  assert.equal(transport.calls.length, 1);
  assert.equal(transport.calls[0]?.endpoint, "/healthz");
  assert.equal(transport.calls[0]?.init.method, "GET");
  assert.equal(transport.calls[0]?.bearerToken, undefined);
  assert.equal(transport.calls[0]?.options?.timeoutMs, SERVER_HEALTH_TIMEOUT_MS);
  assert.equal(transport.calls[0]?.options?.expectedBaseUrl, transport.baseUrl);
  assert.equal(
    transport.calls[0]?.options?.maxResponseBytes,
    MAX_SERVER_HEALTH_RESPONSE_BYTES,
  );
  assert.deepEqual(transport.calls[0]?.options?.acceptedContentTypes, ["application/json"]);

  fixture.timer.callback?.();
  assert.match(fixture.statusBar.text, /서버: 확인 중/);
  assert.match(
    fixture.statusBar.tooltip ?? "",
    /2026-09-06T03:04:05\.000Z/,
    "a periodic check keeps the most recent completed check time visible",
  );

  fixture.monitor.dispose();
});

test("failed and malformed health responses are shown as a disconnected server", async () => {
  for (const handler of [
    async (): Promise<Uint8Array> => new TextEncoder().encode('{"status":"degraded"}'),
    async (): Promise<Uint8Array> => new TextEncoder().encode('{"status":"ok","extra":true}'),
    async (): Promise<Uint8Array> => Uint8Array.from([0xff]),
    async (): Promise<Uint8Array> => { throw new Error("network down"); },
  ]) {
    const transport = new FakeTransport("https://grade.example.edu", handler);
    const fixture = createMonitor(transport);

    await fixture.monitor.checkNow();

    assert.match(fixture.statusBar.text, /서버: 연결 끊김/);
    assert.match(fixture.statusBar.tooltip ?? "", /서버: https:\/\/grade\.example\.edu/);
    assert.match(fixture.statusBar.tooltip ?? "", /2026-09-06T03:04:05\.000Z/);
    fixture.monitor.dispose();
  }
});

test("a parseable health body with the wrong media type is not considered connected", async () => {
  const transport = new HttpTransport(
    () => "https://grade.example.edu",
    async () => new Response('{"status":"ok"}', {
      status: 200,
      headers: { "Content-Type": "text/plain" },
    }),
  );
  const fixture = createMonitor(transport);

  await fixture.monitor.checkNow();

  assert.match(fixture.statusBar.text, /서버: 연결 끊김/);
  fixture.monitor.dispose();
});

test("manual and periodic checks share one in-flight request instead of overlapping", async () => {
  let resolveHealth: ((value: Uint8Array) => void) | undefined;
  const transport = new FakeTransport(
    "https://grade.example.edu",
    () => new Promise((resolve) => { resolveHealth = resolve; }),
  );
  const fixture = createMonitor(transport);
  fixture.monitor.start();

  const manual = fixture.monitor.checkNow();
  assert.equal(transport.calls.length, 1);
  assert.match(fixture.statusBar.text, /서버: 확인 중/);

  resolveHealth?.(HEALTHY_RESPONSE);
  await manual;
  await Promise.resolve();
  fixture.timer.callback?.();
  const concurrentManual = fixture.monitor.checkNow();
  assert.equal(transport.calls.length, 2);
  resolveHealth?.(HEALTHY_RESPONSE);
  await concurrentManual;

  fixture.monitor.dispose();
});

test("an address change aborts the stale check and immediately checks only the new origin", async () => {
  let activeRequests = 0;
  let maximumActiveRequests = 0;
  const transport = new FakeTransport(
    "https://old.example.edu",
    async (call) => {
      activeRequests += 1;
      maximumActiveRequests = Math.max(maximumActiveRequests, activeRequests);
      try {
        if (transport.calls.length === 1) {
          await new Promise<never>((_resolve, reject) => {
            call.options?.signal?.addEventListener(
              "abort",
              () => reject(new Error("aborted")),
              { once: true },
            );
          });
        }
        return HEALTHY_RESPONSE;
      } finally {
        activeRequests -= 1;
      }
    },
  );
  const fixture = createMonitor(transport);
  fixture.monitor.start();
  assert.equal(transport.calls.length, 1);

  transport.baseUrl = "https://new.example.edu:20000";
  await fixture.monitor.handleAddressChange();

  assert.equal(transport.calls[0]?.options?.signal?.aborted, true);
  assert.equal(transport.calls.length, 2);
  assert.equal(maximumActiveRequests, 1);
  assert.match(fixture.statusBar.text, /서버: 연결됨/);
  assert.match(fixture.statusBar.tooltip ?? "", /서버: https:\/\/new\.example\.edu:20000/);
  assert.doesNotMatch(fixture.statusBar.tooltip ?? "", /old\.example\.edu/);

  fixture.monitor.dispose();
});

test("dispose cancels the timer and in-flight request and prevents later checks", async () => {
  let callNumber = 0;
  const transport = new FakeTransport(
    "https://grade.example.edu",
    async (call) => {
      callNumber += 1;
      if (callNumber === 1) {
        return HEALTHY_RESPONSE;
      }
      return new Promise<never>((_resolve, reject) => {
        call.options?.signal?.addEventListener(
          "abort",
          () => reject(new Error("aborted")),
          { once: true },
        );
      });
    },
  );
  const fixture = createMonitor(transport);
  fixture.monitor.start();
  await fixture.monitor.checkNow();
  await Promise.resolve();
  const activeCheck = fixture.monitor.checkNow();
  const activeSignal = transport.calls[1]?.options?.signal;

  fixture.monitor.dispose();
  await activeCheck;
  fixture.timer.callback?.();
  await fixture.monitor.checkNow();

  assert.equal(activeSignal?.aborted, true);
  assert.equal(fixture.timer.cancelCalls, 1);
  assert.equal(fixture.statusBar.disposeCalls, 1);
  assert.equal(transport.calls.length, 2);
});

test("recurring checks use bounded jitter to avoid synchronized classroom bursts", async () => {
  for (const [random, expectedDelay] of [
    [0, SERVER_HEALTH_INTERVAL_MS - SERVER_HEALTH_JITTER_MS],
    [0.5, SERVER_HEALTH_INTERVAL_MS],
    [1, SERVER_HEALTH_INTERVAL_MS + SERVER_HEALTH_JITTER_MS],
  ] as const) {
    const fixture = createMonitor(
      new FakeTransport("https://grade.example.edu", async () => HEALTHY_RESPONSE),
      random,
    );
    fixture.monitor.start();
    await fixture.monitor.checkNow();
    assert.equal(fixture.timer.milliseconds, expectedDelay);
    fixture.monitor.dispose();
  }
});

function createMonitor(transport: ApiTransport, random = 0.5): MonitorFixture {
  const statusBar = new FakeStatusBar();
  const timer: MonitorFixture["timer"] = { cancelCalls: 0 };
  const monitor = new ServerConnectionMonitor(
    transport,
    statusBar as unknown as vscode.StatusBarItem,
    (callback, milliseconds) => {
      timer.callback = callback;
      timer.milliseconds = milliseconds;
      return () => { timer.cancelCalls += 1; };
    },
    () => CHECKED_AT,
    () => random,
  );
  return { monitor, statusBar, timer };
}
