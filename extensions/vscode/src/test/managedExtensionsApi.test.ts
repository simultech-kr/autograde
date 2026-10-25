import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { ApiError, AutogradeClient, HttpTransport, RequestCancelledError, type SessionTokenStore } from "../api";
import { ManagedExtensionsApi } from "../managedExtensionsApi";
import type { ExtensionRelease } from "../managedExtensions";

const payload = Buffer.from("VSIX fixture");
const release: ExtensionRelease = {release_id: `er_${"a".repeat(32)}`, extension_id: "teacher.cpp-lab", version: "1.0.0",
  filename: "cpp-lab.vsix", target_platform: "universal", vscode_engine: "^1.92.0",
  sha256: createHash("sha256").update(payload).digest("hex"), size_bytes: payload.length};
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {status, headers: {"Content-Type": "application/json"}});
const signal = () => new AbortController().signal;

function fixture() {
  const state = {base: "https://school.invalid:20000", generation: 1, tokens: [] as boolean[],
    calls: [] as {url: string; init: RequestInit}[], responses: [] as (() => Promise<Response>)[],
    afterToken: () => {}, afterResponse: () => {}};
  const tokens: SessionTokenStore = {
    getSessionGeneration: () => state.generation,
    hasSession: async () => true, storeSession: async () => {}, clear: async () => {state.generation++;},
    getAccessToken: async force => {state.tokens.push(!!force); state.afterToken(); return force ? "new-token" : "access-token";},
  };
  const transport = new HttpTransport(() => state.base, async (url, init) => {
    state.calls.push({url: String(url), init: init ?? {}});
    const response = await (state.responses.shift()?.() ?? Promise.resolve(json({releases: [release]})));
    state.afterResponse(); return response;
  });
  return {state, api: new ManagedExtensionsApi(new AutogradeClient(transport, tokens)),
    scope: {baseUrl: state.base, generation: 1, assignmentId: "a1", clientSessionId: "test-client-0001"}};
}

test("managed extension API uses same-origin fixed paths, memory token and redirect:error", async () => {
  const {state, api, scope} = fixture();
  state.responses.push(async () => json({releases: [release]}),
    async () => new Response(payload, {headers: {"Content-Type": "application/octet-stream"}}),
    async () => json({accepted: true, status: "installed"}));
  assert.deepEqual(await api.catalog(scope, signal()), [release]);
  assert.deepEqual(Buffer.from(await api.download(release, scope, signal())), payload);
  await api.report(release, {status: "installed", result_code: "version_verified", installed_version: "1.0.0"}, scope, signal());
  assert.deepEqual(state.calls.map(call => call.url), [
    `${scope.baseUrl}/v1/ide/extensions`, `${scope.baseUrl}/v1/ide/extensions/${release.release_id}/package`,
    `${scope.baseUrl}/v1/ide/extensions/${release.release_id}/status`,
  ]);
  assert.ok(state.calls.every(call => call.init.redirect === "error" && new Headers(call.init.headers).get("Authorization") === "Bearer access-token"));
  const report = JSON.parse(String(state.calls[2]!.init.body));
  assert.deepEqual(report, {client_session_id: scope.clientSessionId, status: "installed", result_code: "version_verified",
    installed_version: "1.0.0", platform: process.platform, architecture: process.arch});
  assert.equal(state.calls[2]!.init.method, "POST");
});

test("401 refresh retries the same package URL once with the replacement token", async () => {
  const {state, api, scope} = fixture();
  state.responses.push(async () => json({error: {code: "expired", message: "expired"}}, 401),
    async () => new Response(payload, {headers: {"Content-Type": "application/octet-stream"}}));
  // Error body must fit this fixture release's bounded response allowance.
  await api.download({...release, size_bytes: 1024}, scope, signal());
  assert.deepEqual(state.tokens, [false, true]);
  assert.equal(state.calls.length, 2); assert.equal(state.calls[0]!.url, state.calls[1]!.url);
  assert.equal(new Headers(state.calls[1]!.init.headers).get("Authorization"), "Bearer new-token");
});

for (const phase of ["before", "token", "response"] as const) {
  for (const boundary of ["generation", "origin"] as const) {
    test(`catalog ${boundary} boundary during ${phase} rejects stale credentials/results`, async () => {
      const {state, api, scope} = fixture();
      const change = () => {if (boundary === "generation") state.generation++; else state.base = "https://other.invalid";};
      if (phase === "before") change(); else if (phase === "token") state.afterToken = change; else state.afterResponse = change;
      await assert.rejects(api.catalog(scope, signal()), RequestCancelledError);
      assert.equal(state.calls.length, phase === "response" ? 1 : 0);
      assert.ok(state.calls.every(call => call.url.startsWith(scope.baseUrl)));
    });
  }
}

test("an aborted request is rejected before reading a credential", async () => {
  const {state, api, scope} = fixture(), controller = new AbortController(); controller.abort();
  await assert.rejects(api.catalog(scope, controller.signal), RequestCancelledError);
  assert.deepEqual(state.tokens, []); assert.deepEqual(state.calls, []);
});

test("package reads reject oversized streams and HTML bodies through actual HttpTransport", async () => {
  for (const reason of ["stream", "length", "type"] as const) {
    const {state, api, scope} = fixture();
    state.responses.push(async () => new Response(reason === "type" ? "<html>login</html>" : Buffer.alloc(payload.length + 1), {
      headers: {"Content-Type": reason === "type" ? "text/html" : "application/octet-stream",
        ...(reason === "length" ? {"Content-Length": String(payload.length + 1)} : {})},
    }));
    await assert.rejects(api.download(release, scope, signal()), (error: unknown) => error instanceof ApiError
      && error.code === (reason === "type" ? "invalid_response" : "response_too_large"));
  }
});

test("state report requires accepted:true and the matching acknowledged status", async () => {
  for (const body of [{accepted: false, status: "installed"}, {accepted: true, status: "offered"}, {}, []]) {
    const {state, api, scope} = fixture(); state.responses.push(async () => json(body));
    await assert.rejects(api.report(release, {status: "installed", result_code: "version_verified"}, scope, signal()),
      (error: unknown) => error instanceof ApiError && error.code === "invalid_extension_response");
  }
});

test("malformed release paths never become authenticated requests", async () => {
  const {state, api, scope} = fixture();
  assert.throws(() => api.download({...release, release_id: "../../private"}, scope, signal()), /Invalid release/);
  await assert.rejects(api.report({...release, release_id: "https://outside.invalid"}, {status: "offered", result_code: "approval_required"}, scope, signal()), /Invalid release/);
  assert.deepEqual(state.calls, []);
});
