import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import { ApiError, AutogradeClient, HttpTransport, type SessionTokenStore } from "../api";
import { contentCanonicalJson, contentSha256 } from "../contentSync";

interface Loader { _load(name: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as Loader, original = loader._load;
const messages: string[] = [];
let trusted = true, pickCount = 0, action = "sync", directory = "", dialogCount = 0;
let afterPick = () => {};
const stub = {
  CancellationTokenSource: class { public token = {isCancellationRequested: false}; public cancel() {this.token.isCancellationRequested = true;} public dispose() {} },
  workspace: {get isTrusted() {return trusted;}, workspaceFolders: []}, ProgressLocation: {Notification: 1},
  window: {
    showInformationMessage: async (message: string) => {messages.push(message);},
    showWarningMessage: async (message: string) => {messages.push(message);},
    showQuickPick: async (items: {action?: string}[]) => {pickCount++; afterPick(); return items.find(item => item.action === action) ?? items[0];},
    showOpenDialog: async () => {dialogCount++; return [{scheme: "file", fsPath: directory}];},
    withProgress: async (_options: unknown, task: (progress: unknown, cancellation: unknown) => Promise<unknown>) => task({report() {}},
      {isCancellationRequested: false, onCancellationRequested: () => ({dispose() {}})}),
  },
};
loader._load = function(name, parent, main) {return name === "vscode" ? stub : original.call(this, name, parent, main);};
const {ContentSyncView} = require("../contentSyncView") as typeof import("../contentSyncView");
loader._load = original;

const context = {schema: "autograde.content.context.v1", course_key: "come2201", subject_scope: "a".repeat(64),
  limits: {max_file_bytes: 26214400, max_release_bytes: 104857600, max_files: 1000, max_manifest_bytes: 524288}};
const file = Buffer.from("read the notes");
const raw = {schema: "autograde.content.manifest.v1", course_key: context.course_key, subject_scope: context.subject_scope,
  collection_id: `cc_${"1".repeat(32)}`, release_id: `cr_${"2".repeat(32)}`, generation: 1, policy_revision: 1,
  title: "강의자료", change_note: "업데이트", files: [{path: "readme.md", sha256: contentSha256(file), size_bytes: file.length, media_type: "text/markdown"}]};
const digest = contentSha256(contentCanonicalJson(raw)), manifest = {...raw, manifest_id: `cm_${digest}`, manifest_sha256: digest};
class Tokens implements SessionTokenStore {
  public generation = 1; public loggedIn = true; public expired = false;
  public getSessionGeneration() {return this.generation;}
  public async hasSession() {return this.loggedIn;}
  public async getAccessToken(force = false) {
    if (force && this.expired) {await this.clear(); throw new ApiError("expired", 401, "invalid_refresh_token");}
    return "memory-token";
  }
  public async storeSession() {this.generation++; this.loggedIn = true;}
  public async clear() {this.generation++; this.loggedIn = false;}
}
async function fixture() {
  messages.length = 0; trusted = true; pickCount = 0; action = "sync"; directory = ""; dialogCount = 0; afterPick = () => {};
  const root = await mkdtemp(path.join(tmpdir(), "autograde-content-view-"));
  const tokens = new Tokens();
  const state = {reads: 0, requests: 0, empty: false, unsupported: false, verifyConflict: false, afterObject: () => {},
    afterCatalog: () => {}, base: "https://test.invalid", collections: [manifest], manifestRequests: [] as string[]};
  const transport = new HttpTransport(() => state.base, async (input) => {
    state.requests++; const endpoint = new URL(String(input)).pathname;
    if (tokens.expired) return new Response(JSON.stringify({error: {code: "invalid_token"}}), {status: 401, headers: {"Content-Type": "application/json"}});
    if (state.unsupported) return new Response("{}", {status: 404, headers: {"Content-Type": "application/json"}});
    let payload: unknown;
    if (endpoint.endsWith("/context")) payload = context;
    else if (endpoint.endsWith("/collections")) {state.afterCatalog(); payload = {...context, collections: state.empty ? [] : state.collections};}
    else if (endpoint.endsWith("/manifest")) {state.manifestRequests.push(endpoint); payload = manifest;}
    else if (endpoint.endsWith("/verify")) {
      if (state.verifyConflict) return new Response("{}", {status: 409, headers: {"Content-Type": "application/json"}});
      payload = {verified: true, manifest_id: manifest.manifest_id};
    } else if (endpoint.includes("/objects/")) {
      state.reads++; state.afterObject(); return new Response(file, {headers: {"Content-Type": "application/octet-stream"}});
    } else throw new Error("unexpected endpoint");
    return new Response(JSON.stringify(payload), {headers: {"Content-Type": "application/json"}});
  });
  const client = new AutogradeClient(transport, tokens);
  let authenticationCleared = 0;
  const view = new ContentSyncView(client, {cacheRoot: () => path.join(root, "materials"), onAuthenticationRequired: () => {authenticationCleared++;}});
  return {root, tokens, state, client, view, cleared: () => authenticationCleared, close: async () => {view.dispose(); await rm(root, {recursive: true, force: true});}};
}

test("content UI requires login, explains unsupported server and empty course without filesystem writes", async () => {
  for (const situation of ["login", "unsupported", "empty"]) {
    const f = await fixture();
    try {
      if (situation === "login") f.tokens.loggedIn = false;
      if (situation === "unsupported") f.state.unsupported = true;
      if (situation === "empty") f.state.empty = true;
      await f.view.show();
      assert.equal(pickCount, 0); assert.equal(dialogCount, 0); assert.equal(f.state.reads, 0); assert.deepEqual(await readdir(f.root), []);
      assert.match(messages.join(" "), situation === "login" ? /인증/ : situation === "unsupported" ? /지원하지 않습니다/ : /강의자료가 없습니다/);
    } finally {await f.close();}
  }
});

test("content UI stages once, repeats with cache reuse, then exports without new object requests", async () => {
  const f = await fixture();
  try {
    assert.equal(await f.view.show(), "synced"); assert.equal(f.state.reads, 1); assert.equal(dialogCount, 0);
    assert.match(messages.join(" "), /캐시에 보관/);
    await f.view.show(); assert.equal(f.state.reads, 1); assert.match(messages.join(" "), /변경 파일 0개 수신, 1개 재사용/);
    action = "export"; directory = path.join(f.root, "exports"); await mkdir(directory);
    await f.view.show(); assert.equal(f.state.reads, 1); assert.equal(dialogCount, 1);
    assert.equal((await readdir(directory)).length, 1); assert.match(messages.join(" "), /자동으로 열거나 실행하지 않습니다/);
  } finally {await f.close();}
});

test("instructor-request guard can expire during a picker without staging or falsely completing", async () => {
  const f = await fixture(); let valid = true;
  try {
    afterPick = () => {valid = false;};
    const result = await f.view.show(() => {if (!valid) throw new Error("expired request");});
    assert.equal(result, "failed"); assert.equal(f.state.reads, 0); assert.deepEqual(await readdir(f.root), []);
    const controller = new AbortController(); controller.abort();
    assert.equal(await f.view.show(undefined, controller.signal), "cancelled");
  } finally {await f.close();}
});

test("restricted workspaces can see metadata but cannot create a cache or export", async () => {
  const f = await fixture();
  try {
    trusted = false; await f.view.show();
    assert.equal(f.state.reads, 0); assert.deepEqual(await readdir(f.root), []); assert.match(messages.join(" "), /제한 모드/);
  } finally {await f.close();}
});

test("account change or explicit clear during pick silently cancels old student's operation", async () => {
  for (const change of ["identity", "clear"]) {
    const f = await fixture();
    try {
      afterPick = () => {if (change === "identity") f.tokens.generation++; else f.view.clear();};
      await f.view.show(); assert.equal(f.state.reads, 0); assert.deepEqual(messages, []); assert.deepEqual(await readdir(f.root), []);
    } finally {await f.close();}
  }
});

test("trust loss during download and final 409 do not claim success", async () => {
  for (const situation of ["trust", "conflict"]) {
    const f = await fixture();
    try {
      if (situation === "trust") f.state.afterObject = () => {trusted = false;};
      else f.state.verifyConflict = true;
      await f.view.show();
      assert.match(messages.join(" "), situation === "trust" ? /제한 모드/ : /이전 캐시/);
      assert.doesNotMatch(messages.join(" "), /캐시에 보관했습니다/);
    } finally {await f.close();}
  }
});

test("expired refresh resets stale UI and tells student to reconnect", async () => {
  const f = await fixture();
  try {
    f.tokens.expired = true; await f.view.show();
    assert.equal(f.cleared(), 1); assert.equal(f.state.reads, 0); assert.match(messages.join(" "), /새 수령 코드/);
  } finally {await f.close();}
});

test("shared content sync stays temporary while explicit export must be outside the entire session", async () => {
  const f = await fixture();
  const session = path.join(f.root, "session"), cache = path.join(session, "materials");
  const view = new ContentSyncView(f.client, {cacheRoot: () => cache, excludedExportDirectories: () => [session]});
  try {
    assert.equal(await view.show(), "synced"); assert.equal(f.state.reads, 1);
    assert.match(messages.join(" "), /임시로 받은 자료는 실습 종료 시 삭제/);
    action = "export"; directory = cache; messages.length = 0;
    assert.equal(await view.show(), "failed");
    assert.match(messages.join(" "), /학습 공간 밖에 저장/);
    assert.equal((await readdir(cache)).some(name => name.startsWith("cc_")), false);
    directory = path.join(f.root, "take-home"); await mkdir(directory); messages.length = 0;
    assert.equal(await view.show(), "synced");
    assert.equal(f.state.reads, 1); assert.equal(dialogCount, 2);
    const exported = (await readdir(directory))[0]!;
    assert.deepEqual(await readFile(path.join(directory, exported, "readme.md")), file);
    assert.match(messages.join(" "), /내보낸 파일은 실습 종료 후에도 보존/);
  } finally {view.dispose(); await f.close();}
});

test("shared content export rejects a symlink ancestor alias into the managed session before downloading", async t => {
  if (process.platform === "win32") return t.skip("POSIX symlink test");
  const f = await fixture(), session = path.join(f.root, "session"), alias = path.join(f.root, "alias");
  const view = new ContentSyncView(f.client, {cacheRoot: () => path.join(session, "materials"), excludedExportDirectories: () => [session]});
  try {
    await mkdir(path.join(session, "target"), {recursive: true}); await symlink(session, alias);
    action = "export"; directory = path.join(alias, "target");
    assert.equal(await view.show(), "failed"); assert.equal(f.state.reads, 0);
    assert.deepEqual(await readdir(path.join(session, "target")), []);
    assert.match(messages.join(" "), /학습 공간 밖에 저장/);
  } finally {view.dispose(); await f.close();}
});

test("content export revalidates exclusions after receipt instead of retaining files in a newly managed destination", async () => {
  const f = await fixture(), session = path.join(f.root, "session");
  let excluded = [session];
  const view = new ContentSyncView(f.client, {cacheRoot: () => path.join(session, "materials"), excludedExportDirectories: () => excluded});
  try {
    action = "export"; directory = path.join(f.root, "later-managed"); await mkdir(directory);
    f.state.afterObject = () => {excluded = [session, directory];};
    assert.equal(await view.show(), "failed"); assert.equal(f.state.reads, 1);
    assert.deepEqual(await readdir(directory), []);
    assert.doesNotMatch(messages.join(" "), /저장 위치:/);
  } finally {view.dispose(); await f.close();}
});


test("targeted course download reloads exact collection and skips only the collection picker", async () => {
  const f = await fixture();
  try {
    f.state.collections = [{...manifest, collection_id: `cc_${"3".repeat(32)}`, release_id: `cr_${"4".repeat(32)}`}, manifest];
    assert.equal(await f.view.show(undefined, undefined, manifest.collection_id), "synced");
    assert.equal(pickCount, 1); assert.equal(f.state.reads, 1); assert.equal(dialogCount, 0);
    assert.deepEqual(f.state.manifestRequests, [`/v1/content/releases/${manifest.release_id}/manifest`]);
    f.state.collections = f.state.collections.slice(0, 1);
    assert.equal(await f.view.show(undefined, undefined, manifest.collection_id), "unavailable");
    assert.equal(pickCount, 1); assert.equal(f.state.reads, 1);
    assert.equal(f.state.manifestRequests.length, 1);
    assert.match(messages.join(" "), /철회.*새로고침/);
  } finally {await f.close();}
});

test("targeted course download never uses an address or a missing item as a release", async () => {
  for (const id of ["https://other.invalid/manifest", "../manifest", `cc_${"f".repeat(32)}`]) {
    const f = await fixture();
    try {
      assert.notEqual(await f.view.show(undefined, undefined, id), "synced");
      assert.equal(f.state.reads, 0); assert.equal(pickCount, 0); assert.equal(dialogCount, 0);
      assert.equal(f.state.manifestRequests.length, 0); assert.deepEqual(await readdir(f.root), []);
      if (!id.startsWith("cc_")) assert.equal(f.state.requests, 0);
    } finally {await f.close();}
  }
});

test("targeted course download preserves trust, cancellation and catalog-session boundaries", async () => {
  for (const situation of ["untrusted", "account", "server", "aborted"]) {
    const f = await fixture();
    try {
      if (situation === "untrusted") trusted = false;
      if (situation === "account") f.state.afterCatalog = () => {f.tokens.generation++;};
      if (situation === "server") f.state.afterCatalog = () => {f.state.base = "https://other.invalid";};
      const controller = new AbortController(); if (situation === "aborted") controller.abort();
      assert.notEqual(await f.view.show(undefined, controller.signal, manifest.collection_id), "synced");
      assert.equal(f.state.reads, 0); assert.deepEqual(await readdir(f.root), []);
      if (situation === "account" || situation === "server") {
        assert.equal(f.state.manifestRequests.length, 0); assert.deepEqual(messages, []);
      }
    } finally {await f.close();}
  }
});
