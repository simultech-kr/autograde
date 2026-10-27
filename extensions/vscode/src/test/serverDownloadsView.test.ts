import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, AutogradeClient, HttpTransport, type SessionTokenStore } from "../api";
interface Loader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as Loader, original = loader._load;
let trusted = true;
interface Panel {
  webview: {html: string; onDidReceiveMessage(handler: (message: unknown) => Promise<void>): {dispose(): void}};
  onDidDispose(handler: () => void): {dispose(): void}; reveal(): void; dispose(): void;
}
let panel: Panel, message: (value: unknown) => Promise<void>, close: (() => void) | undefined, options: Record<string, unknown>, creations = 0, reveals = 0;
const stub = {ViewColumn: {One: 1}, workspace: {get isTrusted() {return trusted;}}, window: {
  createWebviewPanel: (_kind: string, _title: string, _column: number, opts: Record<string, unknown>) => {
    creations++; options = opts;
    panel = {webview: {html: "", onDidReceiveMessage: handler => {message = handler; return {dispose() {}};}},
      onDidDispose: handler => {close = handler; return {dispose() {close = undefined;}};}, reveal: () => {reveals++;}, dispose: () => {close?.();}};
    return panel;
  },
}};
loader._load = function(name, parent, main) {return name === "vscode" ? stub : original.call(this, name, parent, main);};
const {ServerDownloadsView} = require("../serverDownloadsView") as typeof import("../serverDownloadsView"); loader._load = original;
const context = {schema: "autograde.content.context.v1", course_key: "come2201", subject_scope: "a".repeat(64), limits: {max_file_bytes: 26214400, max_release_bytes: 104857600, max_files: 1000, max_manifest_bytes: 524288}};
const collection = {collection_id: "cc_" + "1".repeat(32), release_id: "cr_" + "2".repeat(32), generation: 2, policy_revision: 1, title: "교수자 강의자료", change_note: "둘째 주 변경"};
const material = {material_id: "mat_test", course_key: "come2201", title: "개별 PDF", instructions: "읽으세요", format: "pdf", size_bytes: 100, sha256: "b".repeat(64), file_revision: 1, edition: 1, availability: "open", opens_at: "2026-01-01T00:00:00Z", closes_at: "2027-01-01T00:00:00Z", file_updated: false};
class Tokens implements SessionTokenStore {
  generation = 1; loggedIn = true; expired = false;
  getSessionGeneration() {return this.generation;} async hasSession() {return this.loggedIn;}
  async getAccessToken(force = false) {if (force && this.expired) {await this.clear(); throw new ApiError("expired", 401, "invalid_refresh_token");} return "fixture-token";} async clear() {this.generation++; this.loggedIn = false;} async storeSession() {this.generation++; this.loggedIn = true;}
}
function deferred<T>() {let resolve!: (value: T) => void; const promise = new Promise<T>(done => {resolve = done;}); return {promise, resolve};}
async function settle() {for (let i = 0; i < 12; i++) await new Promise<void>(resolve => setImmediate(resolve));}
function action(row = 0) {
  const keys = [...panel.webview.html.matchAll(/data-row-key="([^"]+)"/g)].map(match => match[1]!);
  return {type: "serverDownloads", action: "download", revision: Number(/const revision=(\d+)/.exec(panel.webview.html)![1]), rowKey: keys[row]!};
}
function command(name: string) {return {type: "serverDownloads", action: name, revision: Number(/const revision=(\d+)/.exec(panel.webview.html)![1])};}
function fixture() {
  trusted = true; const tokens = new Tokens(), calls: string[][] = [], errors: unknown[] = [];
  const state = {origin: "https://class.invalid", count: 0, materialFailure: false, cancelConnect: false, materialClosed: false, authUi: undefined as boolean | undefined, resetCount: 0, hold: undefined as Promise<void> | undefined,
    outcome: "saved" as "saved" | "cancelled" | "failed" | "unavailable", downloadWait: undefined as Promise<void> | undefined,
    downloadError: undefined as Error | undefined};
  const transport = new HttpTransport(() => state.origin, async input => {
    state.count++; if (state.hold) await state.hold;
    if (tokens.expired) return new Response(JSON.stringify({error: {code: "invalid_token"}}), {status: 401, headers: {"Content-Type": "application/json"}});
    const url = new URL(String(input)); let result: unknown;
    if (url.pathname === "/v1/accepted-assignments") result = {assignments: [{assignment_id: "assignment_one", title: "실습 과제", status: "ready", ready: true, delivery_mode: "bundle", starter_url: "/v1/assignments/assignment_one/starter"}]};
    else if (url.pathname === "/v1/materials") {
      if (state.materialFailure) return new Response(JSON.stringify({error: {code: "not_found"}}), {status: 404, headers: {"Content-Type": "application/json"}});
      result = {course_key: "come2201", materials: [{...material, availability: state.materialClosed ? "closed" : "open"}], next_cursor: null};
    } else if (url.pathname === "/v1/content/context") result = context;
    else if (url.pathname === "/v1/content/collections") result = {...context, collections: [collection]};
    else throw new ApiError("unexpected fixture route", 500);
    return new Response(JSON.stringify(result), {headers: {"Content-Type": "application/json"}});
  });
  const client = new AutogradeClient(transport, tokens);
  const view = new ServerDownloadsView(client, {authenticated: () => state.authUi ?? tokens.loggedIn,
    onAuthenticationRequired: () => {state.resetCount++; state.authUi = false;},
    downloadAssignment: async id => {calls.push(["assignment", id]); await state.downloadWait; if (state.downloadError) throw state.downloadError;},
    downloadMaterial: async id => {calls.push(["material", id]); await state.downloadWait; return state.outcome;},
    downloadContent: async id => {calls.push(["content", id]); return "synced";},
    connect: async () => {calls.push(["connect"]); if (!state.cancelConnect) {tokens.loggedIn = true; tokens.generation++;}},
    onActionError: error => {errors.push(error);},
  });
  return {view, client, state, tokens, calls, errors};
}

test("panel loads independent categories and routes opaque keys to only the validated listed item", async () => {
  const f = fixture(); f.view.show(); await settle();
  assert.match(panel.webview.html, /실습 과제/); assert.match(panel.webview.html, /개별 PDF/); assert.match(panel.webview.html, /교수자 강의자료/);
  assert.equal(options.enableCommandUris, false); assert.deepEqual(options.localResourceRoots, []); assert.equal(options.retainContextWhenHidden, false);
  assert.doesNotMatch(panel.webview.html, /fixture-token|assignment_one|mat_test|cc_111/);
  await message(action(0)); await message(action(1)); await message(action(2));
  assert.deepEqual(f.calls, [["assignment", "assignment_one"], ["material", "mat_test"], ["content", collection.collection_id]]);
  assert.match(panel.webview.html, /자료 수신을 완료/); f.view.dispose();
});
test("unsupported material category leaves assignment and course content usable", async () => {
  const f = fixture(); f.state.materialFailure = true; f.view.show(); await settle();
  assert.match(panel.webview.html, /서버.*목록 기능을 사용할 수 없습니다/); assert.match(panel.webview.html, /실습 과제/); assert.match(panel.webview.html, /교수자 강의자료/);
  await message(action(1)); assert.deepEqual(f.calls, [["content", collection.collection_id]]); f.view.dispose();
});
test("no login performs no list requests; connecting then loads scope and singleton reveal works", async () => {
  const f = fixture(); f.tokens.loggedIn = false; const before = creations; f.view.show(); f.view.show(); await settle();
  assert.equal(f.state.count, 0); assert.equal(creations, before + 1); assert.ok(reveals > 0);
  await message(command("connect")); await settle(); assert.match(panel.webview.html, /실습 과제/); assert.deepEqual(f.calls, [["connect"]]); f.view.dispose();
});
test("pre-show reconcile does not hide the initial load", async () => {
  const f = fixture(); f.view.reconcile(); assert.equal(f.state.count, 0); f.view.show(); await settle(); assert.match(panel.webview.html, /실습 과제/); f.view.dispose();
});
test("cancelled login stops the progress wording and does not load data", async () => {
  const f = fixture(); f.tokens.loggedIn = false; f.state.cancelConnect = true; f.view.show();
  await message(command("connect")); await settle(); assert.equal(f.state.count, 0);
  assert.match(panel.webview.html, /연결하지 않았습니다/); assert.doesNotMatch(panel.webview.html, /수령 코드 연결을 진행합니다/); f.view.dispose();
});
test("stale messages, injected IDs, revoked trust and unavailable items cannot trigger downloads", async () => {
  const f = fixture(); f.state.materialClosed = true; f.view.show(); await settle(); const stale = action();
  await message(action(1)); assert.equal(f.calls.length, 0); assert.match(panel.webview.html, /배포 기간이 종료되었습니다/);
  await message({...stale, url: "https://evil.invalid"}); await message({...stale, rowKey: "row-" + "f".repeat(32)});
  await message(command("refresh")); await message(stale); assert.equal(f.calls.length, 0);
  trusted = false; await message(action()); assert.equal(f.calls.length, 0); assert.match(panel.webview.html, /제한 모드에서는 목록만/); f.view.dispose();
});
test("logout and origin changes during requests cannot publish prior student's list", async () => {
  const f = fixture(), hold = deferred<void>(); f.state.hold = hold.promise; f.view.show(); await settle();
  await f.tokens.clear(); f.view.reconcile(); hold.resolve(); await settle(); assert.doesNotMatch(panel.webview.html, /실습 과제|개별 PDF/);
  await f.tokens.storeSession(); f.state.hold = undefined; f.view.reconcile(); await settle(); const old = action();
  f.state.origin = "https://other.invalid"; await message(old); await settle(); assert.equal(f.calls.length, 0); f.view.dispose();
});
test("closed panel drops pending lists and refuses old messages when reopened", async () => {
  const f = fixture(), hold = deferred<void>(); f.state.hold = hold.promise; f.view.show(); await settle(); const oldHandler = message;
  panel.dispose(); hold.resolve(); await settle(); f.state.hold = undefined; f.view.show(); await settle();
  await oldHandler({type: "serverDownloads", action: "download", revision: 1, rowKey: "row-" + "a".repeat(32)}); assert.equal(f.calls.length, 0);
  f.view.dispose(); await message(action()); assert.equal(f.calls.length, 0);
});
test("duplicate download is suppressed and cancellation is not reported as success", async () => {
  const f = fixture(), wait = deferred<void>(); f.view.show(); await settle(); f.state.downloadWait = wait.promise; f.state.outcome = "cancelled";
  const request = action(1), pending = message(request); await message(request); assert.equal(f.calls.length, 1);
  wait.resolve(); await pending; assert.match(panel.webview.html, /다운로드를 취소했습니다/); assert.doesNotMatch(panel.webview.html, /자료 수신을 완료/); f.view.dispose();
});
test("late action errors after account switch do not replace new catalogue", async () => {
  const f = fixture(), wait = deferred<void>(); f.view.show(); await settle(); f.state.downloadWait = wait.promise;
  const pending = message(action(1)); await f.tokens.clear(); f.view.reconcile(); await f.tokens.storeSession(); f.view.reconcile(); await settle();
  const currentHtml = panel.webview.html; wait.resolve(); await pending; assert.equal(panel.webview.html, currentHtml); f.view.dispose();
});

test("current assignment failure reports its cause without exposing it in the webview or claiming success", async () => {
  const f = fixture(); f.view.show(); await settle();
  const error = new Error("private destination /Users/student/existing-assignment already exists");
  f.state.downloadError = error;
  await message(action());
  assert.deepEqual(f.errors, [error]);
  assert.match(panel.webview.html, /작업을 완료하지 못했습니다/);
  assert.doesNotMatch(panel.webview.html, /private destination|existing-assignment|자료 수신을 완료|과제 다운로드 절차를 마쳤습니다/);
  f.view.dispose();
});

for (const transition of ["logout", "account switch", "panel close"] as const) {
  test(`delayed assignment failure after ${transition} does not report the former action error`, async () => {
    const f = fixture(), wait = deferred<void>(); f.view.show(); await settle();
    f.state.downloadWait = wait.promise; f.state.downloadError = new Error("former student's failure");
    const pending = message(action());
    if (transition === "logout") await f.tokens.clear();
    else if (transition === "account switch") await f.tokens.storeSession();
    else panel.dispose();
    wait.resolve(); await pending; await settle();
    assert.deepEqual(f.errors, []);
    assert.doesNotMatch(panel.webview.html, /former student's failure|작업을 완료하지 못했습니다/);
    f.view.dispose();
  });
}


test("expired refresh clears cached signed-in UI without reloading indefinitely", async () => {
  const f = fixture(); f.tokens.expired = true; f.state.authUi = true;
  f.view.show(); await settle();
  assert.equal(f.state.resetCount, 1); assert.equal(f.tokens.loggedIn, false);
  assert.match(panel.webview.html, /로그인이 만료/); assert.doesNotMatch(panel.webview.html, /data-row-key/);
  const reads = f.state.count; f.view.reconcile(); await settle(); assert.equal(f.state.count, reads); f.view.dispose();
});

test("a replacement student is not signed out by an older catalogue authentication failure", async () => {
  const f = fixture(), hold = deferred<void>();
  const originalGet = f.client.getAssignments.bind(f.client);
  f.client.getAssignments = async () => {await hold.promise; throw new ApiError("old expired", 401, "invalid_refresh_token");};
  f.view.show(); await settle();
  await f.tokens.storeSession(); f.client.getAssignments = originalGet; f.view.reconcile(); await settle();
  hold.resolve(); await settle(); assert.equal(f.state.resetCount, 0); assert.equal(f.tokens.loggedIn, true);
  assert.match(panel.webview.html, /실습 과제/); f.view.dispose();
});
