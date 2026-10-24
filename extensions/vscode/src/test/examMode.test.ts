import assert from "node:assert/strict";
import test from "node:test";
import { ExamClient, ExamError, ExamSession, examAgentLabel, normalizeExamOrigin, parseExamStatus, type ExamScope, type ExamStatus } from "../examMode";

const scope: ExamScope = {generation: 1, assignmentId: "assignment-1", autogradeOrigin: "https://grade.example"};
const token = "example-ide-token-never-autograde-1234567890123";
function status(): ExamStatus { return {
  exam: {id: "exam-1", title: "중간 시험", status: "active"}, seat_id: "A01", assignment_id: scope.assignmentId, autograde_origin: scope.autogradeOrigin,
  server_time: 1000, expires_at: 2000,
  agent: {online: true, mode: "enforced", backend: "windows-wfp-hyperv", state: "installed", last_seen: 998, lease_expires_at: 1100, verified: false},
  permissions: {can_release: false},
}; }
function json(result: unknown): Response { return new Response(JSON.stringify({ok: true, result}), {headers: {"Content-Type": "application/json"}}); }
function deferred<T>() { let resolve!: (value: T) => void; let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => {resolve = yes; reject = no;}); return {promise, resolve, reject}; }

test("exam addresses accept only HTTPS or exact loopback HTTP origins", () => {
  for (const value of ["https://exam.example:20001", "https://exam.example", "http://localhost:20100", "http://127.0.0.1:20100", "http://[::1]:20100"]) assert.equal(normalizeExamOrigin(value + "/"), value);
  for (const value of ["", "exam.example", "https:exam.example", "http://10.0.0.1:20100", "http://localhost.evil", "https://user:pass@example.test", "https://example.test/path", "https://example.test/../", "https://example.test/%2e%2e", "https://example.test/?x=1", "https://example.test/#token", "file:///tmp/test", "https://example.test\\evil", "https://exam.example\r\nx"]) assert.throws(() => normalizeExamOrigin(value), ExamError, value);
});

test("connect/status/disconnect have distinct credentials, fixed Origin, no cookies and no redirects", async () => {
  const requests: {url: string; init: RequestInit}[] = [];
  const client = new ExamClient("https://exam.example", async (url, init = {}) => {
    requests.push({url: String(url), init});
    return json(String(url).endsWith("connect") && !String(url).endsWith("disconnect") ? {token, expires_at: 2000, status: status()}
      : String(url).endsWith("disconnect") ? {disconnected: true} : status());
  });
  assert.equal((await client.connect("12345678", scope)).token, token);
  await client.status(token, scope); await client.disconnect(token);
  assert.deepEqual(JSON.parse(String(requests[0]!.init.body)), {code: "12345678", assignment_id: "assignment-1", autograde_origin: "https://grade.example"});
  assert.equal(new Headers(requests[0]!.init.headers).get("Authorization"), null);
  assert.equal(new Headers(requests[1]!.init.headers).get("Authorization"), `Bearer ${token}`);
  assert.equal(requests[2]!.init.body, "{}");
  for (const request of requests) {
    assert.equal(new Headers(request.init.headers).get("Origin"), "https://exam.example");
    assert.equal(request.init.credentials, "omit"); assert.equal(request.init.redirect, "error"); assert.equal(request.init.cache, "no-store");
    assert.doesNotMatch(request.url, /token|12345678/);
  }
});

for (const code of ["123456", " 12345678", "123456789", "abcdefgh", "１２３４５６７８"]) {
  test(`invalid code never reaches the network (${code})`, async () => {
    let calls = 0; const client = new ExamClient("https://exam.example", async () => {calls++; return json({});});
    await assert.rejects(client.connect(code, scope), /8자리/); assert.equal(calls, 0);
  });
}

test("status accepts numeric UTC timestamps and null simulation backend but rejects malformed authority", () => {
  assert.equal(parseExamStatus(status(), scope).agent.verified, false);
  assert.equal(parseExamStatus({...status(), agent: {...status().agent, mode: "simulation", backend: null, lease_expires_at: null}}, scope).agent.backend, null);
  for (const patch of [{assignment_id: "other"}, {autograde_origin: "https://evil.example"}, {server_time: "2026-01-01"},
    {expires_at: Infinity}, {expires_at: 999}, {expires_at: 99999999}, {agent: {...status().agent, verified: true}},
    {agent: {...status().agent, online: "true"}}, {agent: {...status().agent, mode: "root"}},
    {agent: {...status().agent, last_seen: "now"}}, {permissions: {can_release: true}}, {exam: {id: "x", title: "x\nsecret", status: "active"}}]) {
    assert.throws(() => parseExamStatus({...status(), ...patch}, scope));
  }
  const parsed = parseExamStatus({...status(), token: "secret", agent: {...status().agent, token: "secret"}}, scope);
  assert.doesNotMatch(JSON.stringify(parsed), /secret|token/);
});

for (const failure of ["redirect", "html", "envelope", "invalid-json", "oversize-length", "oversize-stream", "http-error"] as const) {
  test(`transport fails closed without disclosing server body: ${failure}`, async () => {
    const client = new ExamClient("https://exam.example", async () => {
      if (failure === "redirect") return Object.defineProperty(json(status()), "url", {value: "https://evil.example/api/ide/status"});
      if (failure === "html") return new Response("<h1>private-secret</h1>", {headers: {"Content-Type": "text/html"}});
      if (failure === "envelope") return new Response('{"ok":false,"error":"private-secret"}', {headers: {"Content-Type": "application/json"}});
      if (failure === "invalid-json") return new Response("private-secret", {headers: {"Content-Type": "application/json"}});
      if (failure === "oversize-length") return new Response("private-secret", {headers: {"Content-Type": "application/json", "Content-Length": "999999"}});
      if (failure === "oversize-stream") return new Response("x".repeat(65537), {headers: {"Content-Type": "application/json"}});
      return new Response("private-secret", {status: 403});
    });
    await assert.rejects(client.status(token, scope), error => error instanceof ExamError && !error.message.includes("private-secret"));
  });
}

test("timeout bounds fetch and streaming response even when mock transport ignores cancellation", async () => {
  const blocked = new ExamClient("https://exam.example", () => new Promise<Response>(() => undefined), 10);
  await assert.rejects(blocked.status(token, scope), /초과/);
  let cancelled = false;
  const streaming = new ExamClient("https://exam.example", async () => new Response(new ReadableStream({cancel() {cancelled = true;}}), {headers: {"Content-Type": "application/json"}}), 10);
  await assert.rejects(streaming.status(token, scope), /초과/); assert.equal(cancelled, true);
});

test("rejected HTTP or non-JSON headers cancel an unread stream", async () => {
  for (const headers of [false, true]) {
    let cancelled = false;
    const client = new ExamClient("https://exam.example", async () => new Response(new ReadableStream({cancel() {cancelled = true;}}),
      headers ? {headers: {"Content-Type": "text/html"}} : {status: 403}));
    await assert.rejects(client.status(token, scope)); assert.equal(cancelled, true);
  }
});

test("already cancelled request never fetches and pending abort terminates", async () => {
  let calls = 0; const cancelled = new AbortController(); cancelled.abort();
  const client = new ExamClient("https://exam.example", async () => {calls++; return json(status());});
  await assert.rejects(client.status(token, scope, cancelled.signal)); assert.equal(calls, 0);
  const controller = new AbortController();
  const pending = new ExamClient("https://exam.example", () => new Promise<Response>(() => undefined)).status(token, scope, controller.signal);
  controller.abort(); await assert.rejects(pending, /취소/);
});

test("connection rejects wrong expiry and header-injection tokens", async () => {
  for (const result of [{token, expires_at: 1999, status: status()}, {token: "bad\r\ntoken", expires_at: 2000, status: status()}]) {
    await assert.rejects(new ExamClient("https://exam.example", async () => json(result)).connect("12345678", scope));
  }
});

class FakeClient extends ExamClient {
  public revoked: string[] = []; public signals: (AbortSignal | undefined)[] = [];
  public onConnect: () => Promise<{token: string; status: ExamStatus}> = async () => ({token, status: status()});
  public onStatus: () => Promise<ExamStatus> = async () => status();
  public constructor() {super("https://exam.example");}
  public override async connect(_code: string, _scope: ExamScope, signal?: AbortSignal) { this.signals.push(signal); return this.onConnect(); }
  public override async status(_token: string, _scope: ExamScope, signal?: AbortSignal) {this.signals.push(signal); return this.onStatus();}
  public override async disconnect(value: string) {this.revoked.push(value);}
}

test("connected label is only unverified report, and stale/offline/recovery states cannot claim restriction", async () => {
  let now = 0; const client = new FakeClient(); const session = new ExamSession(() => scope, undefined, () => now);
  try {
    await session.connect(client, "12345678");
    assert.match(examAgentLabel(session.snapshot()), /설치 보고.*미검증/);
    assert.equal(session.snapshot().leaseRemainingSeconds, 100);
    assert.doesNotMatch(JSON.stringify(session.snapshot()), /example-ide-token/);
    now = 16000; assert.match(examAgentLabel(session.snapshot()), /상태 확인 불가/);
    for (const [patch, label] of [[{online: false}, /오프라인/], [{mode: "simulation"}, /모의/], [{state: "recovery_required"}, /복구 필요/], [{state: "released"}, /해제 보고/], [{state: "surprise"}, /미확인/]] as const) {
      client.onStatus = async () => ({...status(), agent: {...status().agent, ...patch}});
      await session.refresh(); assert.match(examAgentLabel(session.snapshot()), label);
    }
    now = 200000; client.onStatus = async () => ({...status(), server_time: 1200});
    await session.refresh(); assert.match(examAgentLabel(session.snapshot()), /기한 경과/);
  } finally {session.dispose();}
});

test("transient errors never show a previous installed response as current, and recovery updates it", async () => {
  const client = new FakeClient(); const session = new ExamSession(() => scope);
  try {
    await session.connect(client, "12345678");
    client.onStatus = async () => {throw new Error("secret-token");}; await session.refresh();
    assert.equal(session.snapshot().fresh, false); assert.match(examAgentLabel(session.snapshot()), /확인 불가/);
    assert.doesNotMatch(JSON.stringify(session.snapshot()), /secret-token/);
    client.onStatus = async () => status(); await session.refresh(); assert.equal(session.snapshot().fresh, true);
  } finally {session.dispose();}
});

for (const change of ["logout", "login", "assignment", "origin"] as const) {
  test(`changing ${change} aborts a pending poll, drops memory token and ignores a late success`, async () => {
    let current: ExamScope | undefined = scope;
    const client = new FakeClient(); const session = new ExamSession(() => current);
    try {
      await session.connect(client, "12345678"); const pending = deferred<ExamStatus>(); client.onStatus = () => pending.promise;
      const refresh = session.refresh();
      current = change === "logout" ? undefined : {...scope, ...(change === "login" ? {generation: 2} : change === "assignment" ? {assignmentId: "other"} : {autogradeOrigin: "https://other.example"})};
      session.reconcile(); assert.equal(client.signals.at(-1)?.aborted, true);
      pending.resolve(status()); await refresh;
      assert.equal(session.snapshot().phase, "disconnected"); assert.equal(session.snapshot().status, undefined);
      assert.deepEqual(client.revoked, [token]);
    } finally {session.dispose();}
  });
}

test("late connect completion after clear is discarded and best-effort revoked", async () => {
  const client = new FakeClient(); const session = new ExamSession(() => scope); const pending = deferred<{token: string; status: ExamStatus}>();
  client.onConnect = () => pending.promise;
  const connected = session.connect(client, "12345678"); session.clear();
  pending.resolve({token, status: status()}); await connected;
  assert.equal(session.snapshot().phase, "disconnected"); assert.deepEqual(client.revoked, [token]); session.dispose();
});

test("late response detects context switch even without event reconciliation", async () => {
  let current = scope; const client = new FakeClient(); const session = new ExamSession(() => current); const pending = deferred<{token: string; status: ExamStatus}>();
  client.onConnect = () => pending.promise; const connected = session.connect(client, "12345678"); current = {...scope, generation: 3};
  pending.resolve({token, status: status()}); await connected;
  assert.equal(session.snapshot().phase, "disconnected"); assert.deepEqual(client.revoked, [token]); session.dispose();
});

test("late failed old poll cannot clear a replacement connection", async () => {
  const old = new FakeClient(); const replacement = new FakeClient(); const session = new ExamSession(() => scope);
  await session.connect(old, "12345678"); const pending = deferred<ExamStatus>(); old.onStatus = () => pending.promise;
  const refresh = session.refresh(); await session.connect(replacement, "87654321"); pending.reject(new ExamError("old expired", 401)); await refresh;
  assert.equal(session.snapshot().phase, "connected"); assert.deepEqual(replacement.revoked, []); session.dispose();
});

test("unauthorized or changed seat/exam clears the session without a release command", async () => {
  for (const kind of ["401", "seat", "exam"] as const) {
    const client = new FakeClient(); const session = new ExamSession(() => scope);
    await session.connect(client, "12345678");
    client.onStatus = async () => { if (kind === "401") throw new ExamError("expired", 401);
      return {...status(), ...(kind === "seat" ? {seat_id: "other"} : {exam: {...status().exam, id: "other"}})}; };
    await session.refresh(); assert.equal(session.snapshot().phase, "disconnected"); assert.deepEqual(client.revoked, [token]); session.dispose();
  }
});

test("monotonic lease expiry is not extended by polls or local wall-clock change", async () => {
  let now = 0; let wall = 3000; const client = new FakeClient(); const session = new ExamSession(() => scope, undefined, () => now, () => wall);
  await session.connect(client, "12345678"); now = 900000; wall = 0; await session.refresh();
  now = 1000001; assert.equal(session.snapshot().phase, "disconnected"); assert.deepEqual(client.revoked, [token]); session.dispose();
});

test("response latency is subtracted and overlapping status polls are suppressed", async () => {
  let now = 0; const client = new FakeClient(); const session = new ExamSession(() => scope, undefined, () => now);
  client.onConnect = async () => {now = 3000; return {token, status: status()};};
  await session.connect(client, "12345678"); assert.equal(session.snapshot().leaseRemainingSeconds, 97);
  const pending = deferred<ExamStatus>(); let calls = 0; client.onStatus = () => {calls++; return pending.promise;};
  const first = session.refresh(); await session.refresh(); assert.equal(calls, 1); pending.resolve(status()); await first; session.dispose();
});
