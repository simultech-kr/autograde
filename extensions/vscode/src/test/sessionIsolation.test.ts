import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, AutogradeClient, TokenManager, type ApiTransport } from "../api";

const baseUrl = "https://grade.example.edu";
const oldTokens = { access_token: "old-access", refresh_token: "old-refresh", expires_in: 3600 };
const newTokens = { access_token: "new-access", refresh_token: "new-refresh", expires_in: 3600 };
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
const identityChanged = (error: unknown) => error instanceof ApiError && error.code === "login_required";

for (const kind of ["bundle", "starter", "result"] as const) {
  test(`late old ${kind} 401 never refreshes or replays under a replacement student's session`, async () => {
    const pending = deferred<unknown>();
    const started = deferred<void>();
    const calls: string[] = [];
    const request = async <T>(endpoint: string): Promise<T> => { calls.push(endpoint); started.resolve(); return await pending.promise as T; };
    const transport: ApiTransport = { getBaseUrl: () => baseUrl, request,
      requestBytes: endpoint => request<Uint8Array>(endpoint) };
    const tokens = new TokenManager(transport);
    await tokens.storeSession(oldTokens, baseUrl);
    const client = new AutogradeClient(transport, tokens);
    const operation = kind === "bundle" ? client.submitBundle("a", Buffer.from("old student's source"), "key")
      : kind === "starter" ? client.getStarter("a") : client.getResult("receipt");
    const rejected = assert.rejects(operation, identityChanged);
    await started.promise;
    await tokens.storeSession(newTokens, baseUrl);
    pending.reject(new ApiError("old session revoked", 401, "token_revoked"));
    await rejected;
    assert.equal(calls.length, 1);
    assert.equal(await tokens.getAccessToken(), "new-access");
  });
}

for (const kind of ["bundle", "git"] as const) {
  test(`${kind} outer network retry cannot replay old work after a login switch during backoff`, async () => {
    let calls = 0;
    const transport: ApiTransport = { getBaseUrl: () => baseUrl,
      request: async () => { calls += 1; throw new ApiError("temporary", 503); },
      requestBytes: async () => { throw new Error("unexpected bytes request"); } };
    const tokens = new TokenManager(transport);
    await tokens.storeSession(oldTokens, baseUrl);
    const client = new AutogradeClient(transport, tokens, async () => tokens.storeSession(newTokens, baseUrl));
    await assert.rejects(kind === "bundle" ? client.submitBundle("a", Buffer.from("old work"), "key")
      : client.submit("a", 42, "a".repeat(40), "key"), identityChanged);
    assert.equal(calls, 1);
    assert.equal(await tokens.getAccessToken(), "new-access");
  });
}

test("late successful old response is discarded and normal same-student 401 refresh still works", async () => {
  const pending = deferred<unknown>();
  const started = deferred<void>();
  const transport: ApiTransport = { getBaseUrl: () => baseUrl,
    request: async <T>() => { started.resolve(); return await pending.promise as T; },
    requestBytes: async () => Buffer.alloc(0) };
  const tokens = new TokenManager(transport);
  await tokens.storeSession(oldTokens, baseUrl);
  const rejected = assert.rejects(new AutogradeClient(transport, tokens).getAssignments(), identityChanged);
  await started.promise;
  await tokens.storeSession(newTokens, baseUrl);
  pending.resolve({ assignments: [] });
  await rejected;

  const calls: string[] = [];
  let count = 0;
  transport.request = async <T>(endpoint: string) => {
    calls.push(endpoint);
    if (count++ === 0) throw new ApiError("expired access", 401);
    return (endpoint === "/v1/tokens/refresh" ? newTokens : { assignments: [] }) as T;
  };
  const identity = tokens.getSessionGeneration();
  assert.deepEqual(await new AutogradeClient(transport, tokens).getAssignments(), []);
  assert.equal(tokens.getSessionGeneration(), identity);
  assert.deepEqual(calls, ["/v1/accepted-assignments", "/v1/tokens/refresh", "/v1/accepted-assignments"]);
});

test("a rejected refresh from an old session cannot clear a newly installed login", async () => {
  const pending = deferred<unknown>();
  const started = deferred<void>();
  const transport: ApiTransport = { getBaseUrl: () => baseUrl,
    request: async <T>() => { started.resolve(); return await pending.promise as T; },
    requestBytes: async () => Buffer.alloc(0) };
  const tokens = new TokenManager(transport);
  await tokens.storeSession({ ...oldTokens, expires_in: 1 }, baseUrl);
  const rejected = assert.rejects(tokens.getAccessToken());
  await started.promise;
  await tokens.storeSession(newTokens, baseUrl);
  pending.reject(new ApiError("old refresh revoked", 401, "invalid_grant"));
  await rejected;
  assert.equal(await tokens.hasSession(), true);
  assert.equal(await tokens.getAccessToken(), "new-access");
});
