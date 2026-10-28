import assert from "node:assert/strict";
import test from "node:test";

import {
  ApiError,
  type ApiTransport,
  AutogradeClient,
  clearLegacyPersistedTokens,
  HttpTransport,
  type LegacySecretStore,
  type RequestOptions,
  type SessionTokenStore,
  SUBMISSION_REQUEST_TIMEOUT_MS,
  TokenManager,
} from "../api";
import type { TokenResponse } from "../types";
import { sha256Hex } from "../bundle";
import type { ClassroomEvent } from "../classroomActivity";

test("classroom activity is authenticated, bounded, origin-bound and projects only approved metadata", async () => {
  const transport = new FakeTransport([{ accepted: true }]);
  const tokens = new TokenManager(transport);
  await tokens.storeSession({ access_token: "access", refresh_token: "refresh", expires_in: 3600 }, transport.baseUrl);
  const client = new AutogradeClient(transport, tokens);
  const event = { client_session_id: "session", sequence: 1, kind: "workspace_opened", assignment_id: "a1",
    environment: { os: "macos", ide: "autograde", hostname: "private-host" }, path: "/private/student.cpp", code: "private" } as unknown as ClassroomEvent;
  const scope = { baseUrl: transport.baseUrl, generation: tokens.getSessionGeneration(), assignmentId: "a1" };
  const controller = new AbortController();
  await client.reportClassroomEvent(event, scope, controller.signal);
  const call = transport.calls[0]!;
  assert.equal(call.endpoint, "/v1/classroom/events"); assert.equal(call.init.method, "POST");
  assert.equal(call.bearerToken, "access"); assert.equal(call.options?.expectedBaseUrl, transport.baseUrl);
  assert.equal(call.options?.signal, controller.signal); assert.equal(call.options?.timeoutMs, 5_000);
  assert.deepEqual(JSON.parse(String(call.init.body)), { client_session_id: "session", sequence: 1, kind: "workspace_opened",
    assignment_id: "a1", environment: { os: "macos", ide: "autograde" } });
  await tokens.clear();
  await assert.rejects(client.reportClassroomEvent(event, scope, controller.signal), error => error instanceof ApiError && error.code === "login_required");
  assert.equal(transport.calls.length, 1);
});

test("classroom commands use the registered session and actual server acknowledgement contract", async () => {
  const command = {command_id: "cmd_" + "a".repeat(32), kind: "notice", assignment_id: "a1", message: "hello", status: "received",
    created_by: "teacher", expires_at: "2026-10-25T15:30:00Z"};
  const transport = new FakeTransport([{commands: [command], server_time: "2026-10-25T15:25:00Z"}, {command: {...command, status: "accepted"}, duplicate: false},
    {command: {...command, status: "completed", result_code: "notice_acknowledged"}, duplicate: true}]);
  const tokens = new TokenManager(transport); await tokens.storeSession({access_token: "access", refresh_token: "refresh", expires_in: 3600}, transport.baseUrl);
  const client = new AutogradeClient(transport, tokens), signal = new AbortController().signal;
  const scope = {baseUrl: transport.baseUrl, generation: tokens.getSessionGeneration(), assignmentId: "a1", clientSessionId: "client-test001", workspaceKey: "/private/local"};
  assert.equal((await client.pollClassroomCommands(scope, signal))[0]?.command_id, command.command_id);
  await client.respondClassroomCommand(command.command_id, {status: "accepted"}, scope, signal);
  await client.respondClassroomCommand(command.command_id, {status: "completed", result_code: "notice_acknowledged"}, scope, signal);
  assert.deepEqual(JSON.parse(String(transport.calls[0]?.init.body)), {client_session_id: "client-test001"});
  assert.deepEqual(JSON.parse(String(transport.calls[1]?.init.body)), {client_session_id: "client-test001", status: "accepted"});
  assert.ok(transport.calls.every(call => call.bearerToken === "access" && call.options?.signal === signal && call.options?.timeoutMs === 5000));
  assert.ok(transport.calls.every(call => !String(call.init.body).includes("private")));
});

test("classroom response rejects unrelated or malformed acknowledgement and projects approved checks only", async () => {
  const transport = new FakeTransport([{accepted: true}, {command: {command_id: "wrong", status: "completed"}, duplicate: false}]);
  const tokens = new TokenManager(transport); await tokens.storeSession({access_token: "access", refresh_token: "refresh", expires_in: 3600}, transport.baseUrl);
  const client = new AutogradeClient(transport, tokens);
  const scope = {baseUrl: transport.baseUrl, generation: tokens.getSessionGeneration(), assignmentId: "a1", clientSessionId: "client-test001"};
  const checks = {server: "ok", workspace: "ok", compiler: "not_checked", os: "macos", remote: "none", path: "/private/source"} as const;
  for (let n = 0; n < 2; n++) await assert.rejects(client.respondClassroomCommand("cmd_test", {
    status: "completed", result_code: "environment_checked", result: {checks},
  }, scope, new AbortController().signal), /확인하지 못했습니다/);
  assert.deepEqual(JSON.parse(String(transport.calls[0]?.init.body)).result.checks,
    {server: "ok", workspace: "ok", compiler: "not_checked", os: "macos", remote: "none"});
  await tokens.clear();
  await assert.rejects(client.pollClassroomCommands(scope, new AbortController().signal), /세션이 변경/);
  assert.equal(transport.calls.length, 2);
});

test("command network latency reduces rather than extends the local action deadline", async () => {
  let now = Date.parse("2026-10-25T15:00:00Z"); const began = now, original = Date.now;
  Date.now = () => now;
  try {
    class SlowTransport extends FakeTransport {
      public override async request<T>(endpoint: string, init: RequestInit, token?: string, options?: RequestOptions): Promise<T> {
        const result = await super.request<T>(endpoint, init, token, options); now += 4000; return result;
      }
    }
    const transport = new SlowTransport([{commands: [{command_id: "cmd_test", kind: "notice", assignment_id: "a1", message: "hello",
      status: "received", created_by: "teacher", expires_at: new Date(began + 10000).toISOString()}], server_time: new Date(began).toISOString()}]);
    const tokens = new TokenManager(transport); await tokens.storeSession({access_token: "access", refresh_token: "refresh", expires_in: 3600}, transport.baseUrl);
    const client = new AutogradeClient(transport, tokens);
    const commands = await client.pollClassroomCommands({baseUrl: transport.baseUrl, generation: tokens.getSessionGeneration(), assignmentId: "a1", clientSessionId: "client-test001"}, new AbortController().signal);
    assert.equal(now, began + 4000); assert.equal(commands[0]?.localDeadline, began + 10000);
    assert.equal(commands[0]!.localDeadline! - now, 6000);
  } finally {Date.now = original;}
});

test("classroom request waiting for access token cannot cross an account or service change", async () => {
  let release: ((value: string) => void) | undefined;
  class RacingTokens extends FakeTokens {
    public generation = 1;
    public getSessionGeneration(): number { return this.generation; }
    public override async getAccessToken(): Promise<string> { return new Promise(resolve => { release = resolve; }); }
  }
  for (const change of ["account", "service"]) {
    const transport = new FakeTransport([]); const tokens = new RacingTokens();
    const client = new AutogradeClient(transport, tokens);
    const event: ClassroomEvent = { client_session_id: "session", sequence: 1, kind: "heartbeat", assignment_id: "a1", environment: { os: "macos", ide: "vscode" } };
    const operation = client.reportClassroomEvent(event, { baseUrl: transport.baseUrl, generation: 1, assignmentId: "a1" }, new AbortController().signal);
    if (change === "account") tokens.generation += 1; else transport.baseUrl = "https://different.example.edu";
    release?.("old-token");
    await assert.rejects(operation, error => error instanceof ApiError && error.code === "login_required");
    assert.equal(transport.calls.length, 0);
  }
});

test("cancelled classroom requests do not ask for tokens or send a request", async () => {
  const transport = new FakeTransport([]); const tokens = new TokenManager(transport);
  const client = new AutogradeClient(transport, tokens); const controller = new AbortController(); controller.abort();
  await assert.rejects(client.reportClassroomEvent({ client_session_id: "session", sequence: 1, kind: "heartbeat", assignment_id: "a1",
    environment: { os: "linux", ide: "vscode" } }, { baseUrl: transport.baseUrl, generation: 0, assignmentId: "a1" }, controller.signal), /취소/);
  assert.equal(transport.calls.length, 0);
});

test("classroom token rotation preserves client identity and retries the exact event only once", async () => {
  const transport = new FakeTransport([new ApiError("expired", 401),
    { access_token: "replacement-access", refresh_token: "replacement-refresh", expires_in: 3600 }, { accepted: true }]);
  const tokens = new TokenManager(transport);
  await tokens.storeSession({ access_token: "old-access", refresh_token: "old-refresh", expires_in: 3600 }, transport.baseUrl);
  const identity = tokens.getSessionGeneration();
  const client = new AutogradeClient(transport, tokens);
  await client.reportClassroomEvent({ client_session_id: "same-client-session", sequence: 8, kind: "heartbeat", assignment_id: "a1",
    environment: { os: "macos", ide: "autograde" } }, { baseUrl: transport.baseUrl, generation: identity, assignmentId: "a1" }, new AbortController().signal);
  assert.deepEqual(transport.calls.map(call => call.endpoint), ["/v1/classroom/events", "/v1/tokens/refresh", "/v1/classroom/events"]);
  assert.equal(transport.calls[0]?.init.body, transport.calls[2]?.init.body);
  assert.equal(transport.calls[2]?.bearerToken, "replacement-access");
  assert.equal(tokens.getSessionGeneration(), identity);
});

test("student assignment list uses accepted-only endpoint and never falls back to catalog", async () => {
  const transport = new FakeTransport([{ assignments: [] }, new ApiError("Missing endpoint", 404, "not_found")]);
  const client = new AutogradeClient(transport, new FakeTokens());
  assert.deepEqual(await client.getAssignments(), []);
  await assert.rejects(client.getAssignments(), (error: unknown) => error instanceof ApiError && error.status === 404
    && error.code === "server_upgrade_required");
  assert.equal(transport.calls.length, 2);
  for (const call of transport.calls) {
    assert.equal(call.endpoint, "/v1/accepted-assignments");
    assert.equal(call.bearerToken, "access-token");
  }
});

test("optional accepted history uses its own endpoint and old servers do not break current assignments", async () => {
  const transport = new FakeTransport([{ assignments: [{ assignment_id: "a", title: "A", course_key: "course", accepted_at: "2026-09-29T00:00:00Z",
    current_session_authorized: true, can_reconnect: true, can_submit: true, status: "available" }] }, new ApiError("Missing endpoint", 404), { assignments: [] }]);
  const client = new AutogradeClient(transport, new FakeTokens());
  assert.equal((await client.getAcceptedAssignmentCatalog())[0]?.assignmentId, "a");
  await assert.rejects(client.getAcceptedAssignmentCatalog(), /현재 과제의 열기/);
  assert.deepEqual(await client.getAssignments(), []);
  assert.deepEqual(transport.calls.map(call => call.endpoint), ["/v1/accepted-assignments/catalog", "/v1/accepted-assignments/catalog", "/v1/accepted-assignments"]);
  assert.ok(transport.calls.every(call => call.init.method === "GET" && call.bearerToken === "access-token" && call.options?.expectedBaseUrl === transport.baseUrl));
});

test("routine refresh keeps the UI session identity, while new login and logout invalidate it", async () => {
  const transport = new FakeTransport([{ access_token: "rotated-access", refresh_token: "rotated-refresh", expires_in: 3600 }]);
  const tokens = new TokenManager(transport);
  await tokens.storeSession({ access_token: "a", refresh_token: "r", expires_in: 1 }, transport.baseUrl);
  const identity = tokens.getSessionGeneration();
  assert.equal(await tokens.getAccessToken(), "rotated-access");
  assert.equal(tokens.getSessionGeneration(), identity);
  await tokens.storeSession({ access_token: "new-a", refresh_token: "new-r", expires_in: 3600 }, transport.baseUrl);
  assert.notEqual(tokens.getSessionGeneration(), identity);
  const replacement = tokens.getSessionGeneration();
  await tokens.clear();
  assert.notEqual(tokens.getSessionGeneration(), replacement);
});

test("history and source use authenticated same-origin endpoints and verify source hash", async () => {
  const bytes = Buffer.from("archived source");
  const transport = new FakeTransport([{ submissions: [{ submission_id: "bsub_1", assignment_id: "asn_1",
    state: "queued", received_at: "2026-09-10T00:00:00Z", source_sha256: `sha256:${sha256Hex(bytes)}`,
    source_size_bytes: bytes.length }], has_more: false }, bytes, Buffer.alloc(bytes.length)]);
  const client = new AutogradeClient(transport, new FakeTokens());
  const version = (await client.getSubmissionHistory("asn_1")).submissions[0];
  assert.ok(version);
  assert.deepEqual(await client.getSubmissionSource(version), bytes);
  assert.equal(transport.calls[0]?.endpoint, "/v1/assignments/asn_1/history");
  assert.equal(transport.calls[1]?.endpoint, "/v1/submissions/bsub_1/source");
  for (const call of transport.calls) {
    assert.equal(call.bearerToken, "access-token");
    assert.equal(call.options?.expectedBaseUrl, transport.baseUrl);
  }
  assert.equal(transport.calls[1]?.options?.maxResponseBytes, 25 * 1024 * 1024);
  await assert.rejects(client.getSubmissionSource(version), /SHA-256/);
});

test("assignment descriptions use authenticated same-origin GET and gracefully handle old servers", async () => {
  const content = "# description\n";
  const document = {assignment_id: "asn_1", revision: 0, content, sha256: "sha256:" + sha256Hex(Buffer.from(content)),
    updated_at: "2026-09-22T04:00:00Z", change_note: "최초 배포 설명", history: []};
  const transport = new FakeTransport([{document}, new ApiError("Not found", 404), {document: null},
    new ApiError("Unavailable", 503)]);
  const client = new AutogradeClient(transport, new FakeTokens());
  assert.equal((await client.getAssignmentDocument("asn_1"))?.content, content);
  assert.equal(await client.getAssignmentDocument("asn_1"), undefined);
  assert.equal(await client.getAssignmentDocument("asn_1"), undefined);
  await assert.rejects(client.getAssignmentDocument("asn_1"), error => error instanceof ApiError && error.status === 503);
  assert.ok(transport.calls.every(call => call.endpoint === "/v1/assignments/asn_1/document" &&
    call.init.method === "GET" && call.bearerToken === "access-token" && call.options?.expectedBaseUrl === transport.baseUrl));
});

test("device preparation sends the normalized course-routing secret with an origin binding", async () => {
  const transport = new FakeTransport([{}]);
  const client = new AutogradeClient(transport, new FakeTokens());
  await client.createDeviceAuthorization("seat", "0.3.0", undefined, undefined, "ak1 2345 6789 abcd");
  assert.equal(transport.calls[0]?.options?.expectedBaseUrl, transport.baseUrl);
  assert.equal(transport.calls[0]?.bearerToken, undefined);
  assert.equal(JSON.parse(String(transport.calls[0]?.init.body)).claim_code, "AK1-2345-6789-ABCD");
  transport.baseUrl = "http://192.168.1.10:20000";
  await assert.rejects(client.createDeviceAuthorization("seat", "0.3.0", undefined, undefined, "AK1-2345-6789-ABCD"), /HTTPS/);
  transport.baseUrl = "https://other.example.edu:20000";
  await assert.rejects(client.createDeviceAuthorization("seat", "0.3.0", undefined, "https://grade.example.edu", "AK1-2345-6789-ABCD"), /변경/);
  assert.equal(transport.calls.length, 1);
});

class FakeTokens implements SessionTokenStore {
  public cleared = false;

  public async hasSession(): Promise<boolean> {
    return !this.cleared;
  }

  public async storeSession(_tokens: TokenResponse, _expectedBaseUrl: string): Promise<void> {}

  public async getAccessToken(_forceRefresh = false): Promise<string> {
    return "access-token";
  }

  public async clear(): Promise<void> {
    this.cleared = true;
  }
}

interface TransportCall {
  readonly endpoint: string;
  readonly init: RequestInit;
  readonly bearerToken?: string;
  readonly options?: RequestOptions;
}

class FakeTransport implements ApiTransport {
  public readonly calls: TransportCall[] = [];

  public constructor(
    private readonly outcomes: unknown[],
    public baseUrl = "https://grade.example.edu",
  ) {}

  public getBaseUrl(): string {
    return this.baseUrl;
  }

  public async request<T>(
    endpoint: string,
    init: RequestInit,
    bearerToken?: string,
    options?: RequestOptions,
  ): Promise<T> {
    this.calls.push({ endpoint, init, bearerToken, options });
    const outcome = this.outcomes.shift();
    if (outcome instanceof Error) {
      throw outcome;
    }
    return outcome as T;
  }

  public async requestBytes(
    endpoint: string,
    init: RequestInit,
    bearerToken?: string,
    options?: RequestOptions,
  ): Promise<Uint8Array> {
    this.calls.push({ endpoint, init, bearerToken, options });
    const outcome = this.outcomes.shift();
    if (outcome instanceof Error) {
      throw outcome;
    }
    return outcome as Uint8Array;
  }
}

class FakeSecretStore implements LegacySecretStore {
  public readonly values = new Map<string, string>();
  public readonly deleted: string[] = [];

  public async delete(key: string): Promise<void> {
    this.deleted.push(key);
    this.values.delete(key);
  }
}

test("session credentials live only in one TokenManager instance", async () => {
  const transport = new FakeTransport([]);
  const currentHost = new TokenManager(transport);
  await currentHost.storeSession({
    access_token: "access-current-host",
    refresh_token: "refresh-current-host",
    expires_in: 300,
  }, transport.getBaseUrl());

  assert.equal(await currentHost.hasSession(), true);
  assert.equal(await currentHost.getAccessToken(), "access-current-host");

  const restartedHost = new TokenManager(transport);
  assert.equal(await restartedHost.hasSession(), false);
  await assert.rejects(
    restartedHost.getAccessToken(),
    (error: unknown) => error instanceof ApiError && error.code === "login_required",
  );
});

test("refresh token rotation remains available within the current host session", async () => {
  const transport = new FakeTransport([{
    access_token: "access-rotated",
    refresh_token: "refresh-rotated",
    expires_in: 300,
  }]);
  const tokens = new TokenManager(transport);
  await tokens.storeSession({
    access_token: "access-original",
    refresh_token: "refresh-original",
    expires_in: 300,
  }, transport.getBaseUrl());

  assert.equal(await tokens.getAccessToken(true), "access-rotated");
  assert.equal(await tokens.getAccessToken(), "access-rotated");
  assert.equal(transport.calls.length, 1);
  assert.equal(transport.calls[0]?.endpoint, "/v1/tokens/refresh");
  assert.deepEqual(JSON.parse(String(transport.calls[0]?.init.body)), {
    refresh_token: "refresh-original",
  });
});

test("changing the configured service address discards the in-memory session", async () => {
  const transport = new FakeTransport([]);
  const tokens = new TokenManager(transport);
  await tokens.storeSession({
    access_token: "access",
    refresh_token: "refresh",
    expires_in: 300,
  }, transport.getBaseUrl());

  transport.baseUrl = "https://other-grade.example.edu";

  assert.equal(await tokens.hasSession(), false);
  await assert.rejects(
    tokens.getAccessToken(),
    (error: unknown) => error instanceof ApiError && error.code === "login_required",
  );
});

test("returned tokens cannot be installed for a different service origin", async () => {
  const transport = new FakeTransport([], "https://other-grade.example.edu");
  const tokens = new TokenManager(transport);

  await assert.rejects(
    tokens.storeSession({
      access_token: "access-from-original-origin",
      refresh_token: "refresh-from-original-origin",
      expires_in: 300,
    }, "https://grade.example.edu"),
    (error: unknown) => error instanceof ApiError && error.code === "login_required",
  );

  assert.equal(await tokens.hasSession(), false);
  await assert.rejects(
    tokens.getAccessToken(),
    (error: unknown) => error instanceof ApiError && error.code === "login_required",
  );
  assert.equal(transport.calls.length, 0);
});

test("refresh token is not sent when the service origin changes during refresh startup", async () => {
  const transport = new FakeTransport([]);
  const tokens = new TokenManager(transport);
  await tokens.storeSession({
    access_token: "access",
    refresh_token: "refresh-secret",
    expires_in: 1,
  }, transport.getBaseUrl());

  let reads = 0;
  transport.getBaseUrl = () => {
    reads += 1;
    return reads === 1
      ? "https://grade.example.edu"
      : "https://other-grade.example.edu";
  };

  await assert.rejects(
    tokens.getAccessToken(true),
    (error: unknown) => error instanceof ApiError && error.code === "login_required",
  );
  assert.equal(transport.calls.length, 0);
});

test("authorized bearer token is bound to the origin selected before token lookup", async () => {
  let configuredBaseUrl = "https://grade.example.edu";
  let fetchCalls = 0;
  const transport = new HttpTransport(
    () => configuredBaseUrl,
    async () => {
      fetchCalls += 1;
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  );
  const tokens: SessionTokenStore = {
    hasSession: async () => true,
    storeSession: async () => undefined,
    getAccessToken: async () => {
      configuredBaseUrl = "https://other-grade.example.edu";
      return "old-origin-token";
    },
    clear: async () => undefined,
  };
  const client = new AutogradeClient(transport, tokens);

  await assert.rejects(
    client.getAssignments(),
    (error: unknown) => error instanceof ApiError && error.code === "login_required",
  );
  assert.equal(fetchCalls, 0);
});

test("claim and device secrets carry an expected-origin transport binding", async () => {
  const transport = new FakeTransport([
    {
      device_code: "device",
      user_code: "USER",
      verification_uri: "https://grade.example.edu/activate",
      expires_in: 300,
    },
    {
      assignment_id: "asn_1",
      course_key: "course",
      delivery_mode: "bundle",
      acceptance_id: "acceptance",
    },
    {
      access_token: "access",
      refresh_token: "refresh",
      expires_in: 300,
    },
  ]);
  const client = new AutogradeClient(transport, new FakeTokens());
  const origin = "https://grade.example.edu";

  await client.createDeviceAuthorization("device", "0.2.1", undefined, origin);
  await client.redeemAssignmentClaim("AK1-2345-6789-ABCD", "device", undefined, origin);
  await client.exchangeDeviceCode("device", undefined, origin);

  assert.deepEqual(
    transport.calls.map((call) => call.options?.expectedBaseUrl),
    [origin, origin, origin],
  );
});

test("activation migration deletes all legacy persisted v1 token entries", async () => {
  const secrets = new FakeSecretStore();
  for (const key of [
    "autograde.auth.accessToken.v1",
    "autograde.auth.refreshToken.v1",
    "autograde.auth.accessExpiresAt.v1",
    "autograde.auth.serviceBaseUrl.v1",
  ]) {
    secrets.values.set(key, `old:${key}`);
  }
  secrets.values.set("unrelated", "keep");

  await clearLegacyPersistedTokens(secrets);

  assert.deepEqual([...secrets.values.entries()], [["unrelated", "keep"]]);
  assert.deepEqual(secrets.deleted.sort(), [
    "autograde.auth.accessExpiresAt.v1",
    "autograde.auth.accessToken.v1",
    "autograde.auth.refreshToken.v1",
    "autograde.auth.serviceBaseUrl.v1",
  ]);
});

test("successful non-JSON responses are rejected", async () => {
  const transport = new HttpTransport(
    () => "https://grade.example.edu",
    async () => new Response("<html>proxy login</html>", {
      status: 200,
      headers: { "Content-Type": "text/html" },
    }),
  );

  await assert.rejects(
    transport.request("/v1/assignments", { method: "GET" }),
    (error: unknown) => error instanceof ApiError && error.code === "invalid_response",
  );
});

test("Retry-After is preserved on API errors", async () => {
  const transport = new HttpTransport(
    () => "https://grade.example.edu",
    async () => new Response(JSON.stringify({ error: "rate_limited" }), {
      status: 429,
      headers: { "Content-Type": "application/json", "Retry-After": "3" },
    }),
  );

  await assert.rejects(
    transport.request("/v1/device-authorizations/token", { method: "POST", body: "{}" }),
    (error: unknown) =>
      error instanceof ApiError &&
      error.status === 429 &&
      error.retryAfterMs === 3_000,
  );
});

test("binary body remains covered by timeout after response headers arrive", async () => {
  const transport = new HttpTransport(() => "https://example.edu", async (_url, init) => {
    const stream = new ReadableStream<Uint8Array>({ start(controller) {
      controller.enqueue(new Uint8Array([1]));
      init?.signal?.addEventListener("abort", () => controller.error(new Error("aborted")), {once:true});
    }});
    return new Response(stream, {headers:{"Content-Type":"application/gzip"}});
  });
  await assert.rejects(transport.requestBytes("/file", {method:"GET"}, undefined, {timeoutMs:25}),
    (error: unknown) => error instanceof ApiError && error.code === "request_timeout");
});

test("binary responses enforce content type and byte limits", async () => {
  const valid = new HttpTransport(
    () => "https://grade.example.edu",
    async () => new Response(Uint8Array.from([1, 2, 3]), {
      status: 200,
      headers: { "Content-Type": "application/gzip" },
    }),
  );
  assert.deepEqual(
    await valid.requestBytes("/starter", { method: "GET" }, undefined, {
      maxResponseBytes: 3,
      acceptedContentTypes: ["application/gzip"],
    }),
    Uint8Array.from([1, 2, 3]),
  );

  await assert.rejects(
    valid.requestBytes("/starter", { method: "GET" }, undefined, {
      maxResponseBytes: 2,
      acceptedContentTypes: ["application/gzip"],
    }),
    (error: unknown) => error instanceof ApiError && error.code === "response_too_large",
  );

  const wrongType = new HttpTransport(
    () => "https://grade.example.edu",
    async () => new Response(Uint8Array.from([1]), {
      status: 200,
      headers: { "Content-Type": "text/html" },
    }),
  );
  await assert.rejects(
    wrongType.requestBytes("/starter", { method: "GET" }, undefined, {
      acceptedContentTypes: ["application/gzip"],
    }),
    (error: unknown) => error instanceof ApiError && error.code === "invalid_response",
  );
});

test("starter download stays on the authenticated assignment endpoint", async () => {
  const archive = Uint8Array.from([0x1f, 0x8b]);
  const transport = new FakeTransport([archive]);
  const client = new AutogradeClient(transport, new FakeTokens());

  assert.deepEqual(
    await client.getStarter(
      "asn /1",
      "https://grade.example.edu/v1/assignments/asn%20%2F1/starter?release=v2",
    ),
    archive,
  );
  assert.equal(
    transport.calls[0]?.endpoint,
    "/v1/assignments/asn%20%2F1/starter?release=v2",
  );
  assert.equal(new Headers(transport.calls[0]?.init.headers).get("Accept"), "application/gzip");
  assert.equal(transport.calls[0]?.bearerToken, "access-token");

  await assert.rejects(
    client.getStarter("asn /1", "https://evil.example/v1/assignments/asn%20%2F1/starter"),
    (error: unknown) => error instanceof ApiError && error.code === "invalid_response",
  );
  assert.equal(transport.calls.length, 1);
});

test("assignment claim redemption sends the one-time secret only in an unauthenticated JSON body", async () => {
  const transport = new FakeTransport([{
    assignment_id: "asn_observer_cpp",
    course_key: "cse101-2026f",
    delivery_mode: "bundle",
    acceptance_id: "aac_01",
  }]);
  const client = new AutogradeClient(transport, new FakeTokens());
  const controller = new AbortController();

  const redemption = await client.redeemAssignmentClaim(
    " ak1 2345-6789-abcd ",
    "pending-device-code",
    controller.signal,
  );

  assert.deepEqual(redemption, {
    assignmentId: "asn_observer_cpp",
    courseKey: "cse101-2026f",
    deliveryMode: "bundle",
    acceptanceId: "aac_01",
  });
  assert.equal(transport.calls.length, 1);
  const call = transport.calls[0];
  assert.equal(call?.endpoint, "/v1/assignment-claims/redeem");
  assert.equal(call?.init.method, "POST");
  assert.equal(call?.bearerToken, undefined);
  assert.equal(call?.options?.signal, controller.signal);
  assert.deepEqual(JSON.parse(String(call?.init.body)), {
    claim_code: "AK1-2345-6789-ABCD",
    device_code: "pending-device-code",
  });
  assert.doesNotMatch(call?.endpoint ?? "", /AK1/);
  assert.equal(new Headers(call?.init.headers).get("Authorization"), null);
});

test("assignment claim redemption rejects malformed claim metadata responses", async () => {
  for (const response of [
    {
      assignment_id: "asn_01",
      course_key: "cse101",
      acceptance_id: "aac_01",
    },
    {
      assignment_id: "asn_01",
      course_key: "cse101",
      delivery_mode: "bundle",
    },
  ]) {
    const client = new AutogradeClient(new FakeTransport([response]), new FakeTokens());
    await assert.rejects(
      client.redeemAssignmentClaim("AK1-2345-6789-ABCD", "pending-device-code"),
      (error: unknown) => error instanceof ApiError && error.code === "invalid_response",
    );
  }
});

test("assignment claim redemption never sends a claim code over private-LAN HTTP", async () => {
  const transport = new FakeTransport([], "http://192.168.50.34:18081");
  const client = new AutogradeClient(transport, new FakeTokens());

  await assert.rejects(
    client.redeemAssignmentClaim("AK1-2345-6789-ABCD", "pending-device-code"),
    (error: unknown) => error instanceof ApiError && error.code === "insecure_claim_redemption",
  );
  assert.equal(transport.calls.length, 0);
});

test("bundle submission retries the same bytes and idempotency key", async () => {
  const transport = new FakeTransport([
    new ApiError("unavailable", 503, "temporarily_unavailable"),
    { submission: { submission_id: "sub_bundle", state: "accepted" } },
  ]);
  const delays: number[] = [];
  const client = new AutogradeClient(
    transport,
    new FakeTokens(),
    async (milliseconds) => { delays.push(milliseconds); },
  );
  const result = await client.submitBundle("asn/1", Uint8Array.from([1, 2, 3]), "bundle-key");

  assert.equal(result.id, "sub_bundle");
  assert.equal(delays.length, 1);
  assert.equal(transport.calls.length, 2);
  for (const call of transport.calls) {
    assert.equal(call.endpoint, "/v1/assignments/asn%2F1/submissions");
    const headers = new Headers(call.init.headers);
    assert.equal(headers.get("Idempotency-Key"), "bundle-key");
    assert.equal(headers.get("Content-Type"), "application/gzip");
    assert.equal(call.bearerToken, "access-token");
    assert.deepEqual(new Uint8Array(call.init.body as ArrayBuffer), Uint8Array.from([1, 2, 3]));
    assert.equal(call.options?.timeoutMs, SUBMISSION_REQUEST_TIMEOUT_MS);
  }
});

for (const [name, transientError] of [
  ["timeout", new ApiError("timeout", 0, "request_timeout")],
  ["429", new ApiError("rate limited", 429, "rate_limited", 2_000)],
  ["503", new ApiError("unavailable", 503, "temporarily_unavailable")],
] as const) {
  test(`submission retries ${name} with the same idempotency key and long timeout`, async () => {
    const transport = new FakeTransport([
      transientError,
      { submission: { submission_id: "sub_1", state: "accepted" } },
    ]);
    const delays: number[] = [];
    const client = new AutogradeClient(
      transport,
      new FakeTokens(),
      async (milliseconds) => { delays.push(milliseconds); },
    );

    const result = await client.submit("asn_1", 42, "a".repeat(40), "stable-request-key");

    assert.equal(result.id, "sub_1");
    assert.equal(transport.calls.length, 2);
    assert.equal(delays.length, 1);
    assert.ok(SUBMISSION_REQUEST_TIMEOUT_MS >= 90_000);
    for (const call of transport.calls) {
      assert.equal(call.endpoint, "/v1/submissions");
      assert.equal(new Headers(call.init.headers).get("Idempotency-Key"), "stable-request-key");
      assert.equal(call.options?.timeoutMs, SUBMISSION_REQUEST_TIMEOUT_MS);
    }
    if (name === "429") {
      assert.equal(delays[0], 2_000);
    }
  });
}

test("learning checkpoint API sends only scoped authenticated bytes and validates the receipt", async () => {
  const archive = Buffer.from("sample bundle"), digest = sha256Hex(archive);
  const checkpoint = {checkpoint_id: "cp_one", assignment_id: "a1", archive_sha256: digest,
    created_at: "2026-10-26T10:00:00Z", file_count: 1, source_bytes: 12, version: 1};
  const transport = new FakeTransport([{checkpoint, replayed: false}, {checkpoints: [checkpoint], latest: checkpoint},
    {checkpoint: {...checkpoint, archive_sha256: "a".repeat(64)}}]);
  const tokens = new TokenManager(transport); await tokens.storeSession({access_token: "access", refresh_token: "refresh", expires_in: 3600}, transport.baseUrl);
  const client = new AutogradeClient(transport, tokens), signal = new AbortController().signal;
  const scope = {baseUrl: transport.baseUrl, generation: tokens.getSessionGeneration(), assignmentId: "a1",
    rootPath: "/private/local", scheme: "file", authority: ""};
  assert.deepEqual(await client.saveLearningCheckpoint(scope, archive, digest, signal), checkpoint);
  const call = transport.calls[0]!;
  assert.equal(call.endpoint, "/v1/assignments/a1/learning-checkpoints"); assert.equal(call.init.method, "POST");
  assert.equal(new Headers(call.init.headers).get("Idempotency-Key")?.startsWith("learning-"), true);
  assert.equal(new Headers(call.init.headers).get("Content-Type"), "application/gzip");
  assert.deepEqual(Buffer.from(call.init.body as ArrayBuffer), archive);
  assert.equal(call.bearerToken, "access"); assert.equal(call.options?.signal, signal);
  assert.equal(call.options?.timeoutMs, 20_000); assert.equal(call.options?.expectedBaseUrl, transport.baseUrl);
  assert.deepEqual(await client.getLearningCheckpoints(scope, signal), [checkpoint]);
  await assert.rejects(client.saveLearningCheckpoint(scope, archive, digest, signal), /지문/);
  await tokens.clear();
  await assert.rejects(client.getLearningCheckpoints(scope, signal), /세션이 변경/);
  assert.equal(transport.calls.length, 3);
});

test("learning checkpoint restore checks archive digest and does not follow an advertised URL", async () => {
  const archive = Buffer.from("restore bytes");
  const checkpoint = {checkpoint_id: "cp_one", assignment_id: "a1", archive_sha256: sha256Hex(archive),
    created_at: "2026-10-26T10:00:00Z", file_count: 1, source_bytes: 12, version: 1, download_url: "https://untrusted.invalid/"};
  class CheckpointTransport extends FakeTransport {
    override async requestBytes(endpoint: string, init: RequestInit, bearerToken?: string, options?: RequestOptions): Promise<Uint8Array> {
      this.calls.push({endpoint, init, bearerToken, options}); return archive;
    }
  }
  const transport = new CheckpointTransport([]), tokens = new TokenManager(transport);
  await tokens.storeSession({access_token: "access", refresh_token: "refresh", expires_in: 3600}, transport.baseUrl);
  const client = new AutogradeClient(transport, tokens), signal = new AbortController().signal;
  const scope = {baseUrl: transport.baseUrl, generation: tokens.getSessionGeneration(), assignmentId: "a1", rootPath: "/work", scheme: "file", authority: ""};
  assert.deepEqual(await client.getLearningCheckpointBundle(scope, checkpoint, signal), archive);
  assert.equal(transport.calls[0]?.endpoint, "/v1/assignments/a1/learning-checkpoints/cp_one/bundle");
  assert.equal(transport.calls[0]?.options?.maxResponseBytes, 25 * 1024 * 1024);
  await assert.rejects(client.getLearningCheckpointBundle(scope, {...checkpoint, archive_sha256: "a".repeat(64)}, signal), /지문/);
  await assert.rejects(client.getLearningCheckpointBundle(scope, {...checkpoint, assignment_id: "other"}, signal));
  assert.equal(transport.calls.length, 2);
});

test("checkpoint semantic dedup validates the received digest while retaining stored digest for restore", async () => {
  const archive = Buffer.from("new compression"), digest = sha256Hex(archive), oldDigest = "a".repeat(64);
  const checkpoint = {checkpoint_id: "cp_same_content", assignment_id: "a1", archive_sha256: oldDigest,
    created_at: "2026-10-26T10:00:00Z", file_count: 1, source_bytes: 12, version: 1};
  const transport = new FakeTransport([{checkpoint, received_archive_sha256: digest, replayed: true}]);
  const tokens = new TokenManager(transport); await tokens.storeSession({access_token: "access", refresh_token: "refresh", expires_in: 3600}, transport.baseUrl);
  const client = new AutogradeClient(transport, tokens), controller = new AbortController();
  const scope = {baseUrl: transport.baseUrl, generation: tokens.getSessionGeneration(), assignmentId: "a1", rootPath: "/work", scheme: "file", authority: ""};
  const saved = await client.saveLearningCheckpoint(scope, archive, digest, controller.signal);
  assert.equal(saved.archive_sha256, oldDigest); assert.equal(saved.received_archive_sha256, digest);
  controller.abort(); await assert.rejects(client.getLearningCheckpoints(scope, controller.signal), /취소/);
  assert.equal(transport.calls.length, 1);
});

test("remote learning APIs use authenticated scoped endpoints and exact checkpoint IDs", async () => {
  const session = {id:"space_one",status:"ready",checkpoint_id:"cp_1",created_at:"2026-10-28T10:00:00Z",expires_at:"2026-10-28T11:00:00Z"};
  const output = {exit_code:0,stdout:"hello",stderr:""};
  const transport = new FakeTransport([{enabled:true,provider:"remote_linux",languages:["c","cpp"],session:null}, {session},
    {session:{...session,checkpoint_id:"cp_2"},checkpoint_id:"cp_2",status:"completed",compile:output,run:output},
    {status:"closed",checkpoint_id:"cp_3",cleanup_verified:true}]);
  const tokens = new TokenManager(transport); await tokens.storeSession({access_token:"access",refresh_token:"refresh",expires_in:3600},transport.baseUrl);
  const client = new AutogradeClient(transport,tokens), scope = {baseUrl:transport.baseUrl,generation:tokens.getSessionGeneration(),assignmentId:"a1",rootPath:"/private/student",scheme:"file",authority:""};
  const signal = new AbortController().signal;
  assert.equal((await client.getLearningRuntime(scope,signal)).enabled,true);
  assert.equal((await client.createLearningRuntime(scope,"cp_1",signal)).id,"space_one");
  assert.equal((await client.runLearningRuntime(scope,"space_one","cp_2",{language:"cpp",sources:["main.cpp"],stdin:"42\n"},signal)).checkpoint_id,"cp_2");
  assert.equal((await client.closeLearningRuntime(scope,"space_one","cp_3",signal)).cleanup_verified,true);
  assert.deepEqual(transport.calls.map(call=>call.endpoint),[
    "/v1/assignments/a1/learning-runtime","/v1/assignments/a1/learning-runtime/sessions",
    "/v1/assignments/a1/learning-runtime/sessions/space_one/run","/v1/assignments/a1/learning-runtime/sessions/space_one/close"]);
  assert.ok(transport.calls.every(call=>call.bearerToken==="access"&&call.options?.expectedBaseUrl===transport.baseUrl&&call.options?.signal===signal));
  assert.equal(transport.calls[2]?.options?.timeoutMs,90_000);
  assert.deepEqual(JSON.parse(String(transport.calls[2]?.init.body)),{checkpoint_id:"cp_2",language:"cpp",sources:["main.cpp"],stdin:"42\n"});
  assert.ok(transport.calls.every(call=>!String(call.init.body).includes("/private")));
  await tokens.clear();await assert.rejects(client.getLearningRuntime(scope,signal),/세션이 변경/);assert.equal(transport.calls.length,4);
});

test("remote API rejects wrong result versions, unverified cleanup and invalid requests before dispatch", async()=>{
  const session = {id:"space_one",status:"ready",checkpoint_id:"cp_old",created_at:"2026-10-28T10:00:00Z",expires_at:"2026-10-28T11:00:00Z"};
  const transport = new FakeTransport([{session},{status:"closed",checkpoint_id:"cp_1",cleanup_verified:false}]);
  const tokens = new TokenManager(transport);await tokens.storeSession({access_token:"access",refresh_token:"refresh",expires_in:3600},transport.baseUrl);
  const client = new AutogradeClient(transport,tokens), scope = {baseUrl:transport.baseUrl,generation:tokens.getSessionGeneration(),assignmentId:"a1",rootPath:"/private/student",scheme:"file",authority:""};
  const signal = new AbortController().signal;
  await assert.rejects(client.createLearningRuntime(scope,"cp_1",signal),/일치/);
  await assert.rejects(client.closeLearningRuntime(scope,"space_one","cp_1",signal),/삭제/);
  await assert.rejects(client.runLearningRuntime(scope,"space_one","cp_1",{language:"cpp",sources:["../../secret.cpp"],stdin:""},signal));
  await assert.rejects(client.closeLearningRuntime(scope,"../other","cp_1",signal));
  assert.equal(transport.calls.length,2);
});
