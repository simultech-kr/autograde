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
