import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, readdir, realpath, rename, rm, symlink } from "node:fs/promises";
import * as path from "node:path";
import { tmpdir } from "node:os";
import { ApiError, AutogradeClient, HttpTransport, TokenManager } from "../api";
import type { StudentMaterial } from "../studentMaterials";

interface Loader { _load(name: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as Loader, original = loader._load;
const pdf = Buffer.from("%PDF-1.7\nSynthetic material\n%%EOF");
const material: StudentMaterial = {id: "mat_test", courseKey: "come2201", title: "자료", instructions: "설명",
  format: "pdf", sizeBytes: pdf.length, sha256: createHash("sha256").update(pdf).digest("hex"), fileRevision: 1, edition: 1,
  availability: "open", opensAt: "2026-10-23T00:00:00Z", closesAt: "2026-12-23T00:00:00Z", updated: false};
const messages: string[] = [];
let pickCount = 0, dialogCount = 0, directory = "", trusted = true;
let workspaceFolders: {uri: {scheme: string; fsPath: string}}[] = [];
let afterPick = () => {};
let afterDirectory = () => {};
let receivedDefault: unknown;
const stub = {
  CancellationTokenSource: class {
    public token = {isCancellationRequested: false};
    public cancel() { this.token.isCancellationRequested = true; }
    public dispose() {}
  },
  workspace: {get isTrusted() {return trusted;}, get workspaceFolders() {return workspaceFolders;}},
  ProgressLocation: {Notification: 1},
  window: {
    showInformationMessage: async (message: string) => {messages.push(message);},
    showWarningMessage: async (message: string) => {messages.push(message);},
    showQuickPick: async (items: unknown[]) => { pickCount++; afterPick(); return items[0]; },
    showOpenDialog: async (options: {defaultUri?: unknown}) => {dialogCount++; receivedDefault = options.defaultUri;
      afterDirectory(); return [{scheme: "file", fsPath: directory}];},
    withProgress: async (_options: unknown, task: (progress: object, token: unknown) => Promise<unknown>) => task({}, {
      isCancellationRequested: false, onCancellationRequested: () => ({dispose() {}}),
    }),
  },
};
loader._load = function(name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const {StudentMaterialsView} = require("../studentMaterialsView") as typeof import("../studentMaterialsView");
loader._load = original;

function fixture() {
  messages.length = 0; pickCount = 0; dialogCount = 0; trusted = true; workspaceFolders = [];
  afterPick = () => {}; afterDirectory = () => {}; receivedDefault = undefined;
  const state = {generation: 1, base: "https://test.invalid", loggedIn: true, reads: 0, lists: 0,
    items: [material], failure: undefined as Error | undefined};
  const client = {getBaseUrl: () => state.base,
    tokens: {getSessionGeneration: () => state.generation, hasSession: async () => state.loggedIn},
    getStudentMaterials: async () => {state.lists++; if (state.failure) throw state.failure; return {courseKey: "come2201", materials: state.items};},
    getStudentMaterialFile: async () => {state.reads++; return pdf;},
  } as unknown as AutogradeClient;
  return {state, client};
}

test("materials require login and distinguish unsupported server or empty personal list", async () => {
  for (const situation of ["login", "unsupported", "empty"]) {
    const {state, client} = fixture();
    if (situation === "login") state.loggedIn = false;
    if (situation === "unsupported") state.failure = new ApiError("missing", 404, "materials_unsupported");
    if (situation === "empty") state.items = [];
    const view = new StudentMaterialsView(client);
    await view.show(); view.dispose();
    assert.equal(dialogCount, 0); assert.equal(state.reads, 0); assert.equal(pickCount, 0);
    assert.match(messages.join(" "), situation === "login" ? /로그인/ : situation === "unsupported" ? /지원하지/ : /배포된 자료가 없습니다/);
  }
});

test("period and trust restrictions explain why a listed material cannot be downloaded", async () => {
  for (const situation of ["scheduled", "closed", "untrusted"]) {
    const {state, client} = fixture();
    if (situation === "untrusted") trusted = false;
    else state.items = [{...material, availability: situation as "scheduled" | "closed"}];
    const view = new StudentMaterialsView(client);
    await view.show(); view.dispose();
    assert.equal(pickCount, 1); assert.equal(dialogCount, 0); assert.equal(state.reads, 0);
    assert.match(messages.join(" "), situation === "scheduled" ? /시작 전/ : situation === "closed" ? /종료/ : /제한 모드/);
  }
});

test("session/server changes across dialogs stop downloads without stale notifications", async () => {
  for (const boundary of ["pick", "directory"]) for (const change of ["session", "server", "clear"]) {
    const {state, client} = fixture();
    const view = new StudentMaterialsView(client);
    const changeState = () => { if (change === "session") state.generation++; else if (change === "server") state.base = "https://other.invalid"; else view.clear(); };
    if (boundary === "pick") afterPick = changeState; else afterDirectory = changeState;
    await view.show(); view.dispose();
    assert.equal(state.reads, 0); assert.deepEqual(messages, []);
  }
});

test("per-student directory is only suggested and download does not open or extract the file", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-material-view-"));
  try {
    const {state, client} = fixture(); directory = root;
    const hint = {scheme: "file", fsPath: root} as import("vscode").Uri;
    let saved = "";
    const view = new StudentMaterialsView(client, {defaultDirectory: async () => hint,
      onDidSave: (_material, filename) => {saved = filename;}});
    await view.show();
    assert.equal(dialogCount, 1); assert.equal(state.reads, 1); assert.equal(receivedDefault, hint);
    assert.deepEqual(await readFile(saved), pdf);
    assert.match(messages.join(" "), /자동 압축 해제하지/);
    assert.equal((await readdir(root)).length, 1);
    await view.show();
    assert.match(messages.join(" "), /덮어쓰지/);
    assert.deepEqual(await readFile(saved), pdf);
    view.dispose();
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("assignment folder and trust changes after receipt cannot leave a downloaded file", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-material-view-"));
  try {
    const {state, client} = fixture(); directory = root;
    await mkdir(path.join(root, "nested"));
    workspaceFolders = [{uri: {scheme: "file", fsPath: root}}];
    directory = path.join(root, "nested");
    const view = new StudentMaterialsView(client);
    await view.show();
    assert.equal(state.reads, 0); assert.match(messages.join(" "), /별도 자료 폴더/);
    workspaceFolders = []; messages.length = 0;
    client.getStudentMaterialFile = async () => {trusted = false; return pdf;};
    await view.show();
    assert.deepEqual(await readdir(directory), []);
    view.dispose();
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("a real rejected token refresh clears authentication but still explains re-login and resets UI", async () => {
  for (const expiredBeforeRequest of [false, true]) {
    fixture();
    const urls: string[] = [];
    const transport = new HttpTransport(() => "https://test.invalid", async (url) => {
      urls.push(String(url));
      const refresh = String(url).endsWith("/v1/tokens/refresh");
      return new Response(JSON.stringify({error: {code: refresh ? "invalid_grant" : "invalid_token"}}),
        {status: refresh ? 400 : 401, headers: {"Content-Type": "application/json"}});
    });
    const tokens = new TokenManager(transport);
    await tokens.storeSession({access_token: "a", refresh_token: "r", expires_in: expiredBeforeRequest ? 1 : 3600}, transport.getBaseUrl());
    const identity = tokens.getSessionGeneration();
    let resets = 0;
    const view = new StudentMaterialsView(new AutogradeClient(transport, tokens), {onAuthenticationRequired: async () => {
      resets++; view.clear();
    }});
    await view.show();
    assert.equal(await tokens.hasSession(), false);
    assert.notEqual(tokens.getSessionGeneration(), identity);
    assert.equal(resets, 1); assert.match(messages.join(" "), /로그인/);
    assert.equal(dialogCount, 0); assert.equal(pickCount, 0);
    assert.equal(urls.filter(url => url.endsWith("/v1/tokens/refresh")).length, 1);
    view.dispose();
  }
});

test("late authorization errors never reset or notify a replacement or explicitly logged-out session", async () => {
  for (const transition of ["new-session", "logout", "server", "login-during-check"]) {
    const {state, client} = fixture();
    let resets = 0;
    const view = new StudentMaterialsView(client, {onAuthenticationRequired: () => {resets++;}});
    client.getStudentMaterials = async () => {
      state.generation++;
      state.loggedIn = transition === "new-session";
      if (transition === "logout") view.clear();
      if (transition === "server") state.base = "https://other.invalid";
      if (transition === "login-during-check") client.tokens.hasSession = async () => {
        state.generation++; state.loggedIn = true;
        return false; // delayed false from the invalidated identity
      };
      throw new ApiError("old rejected session", 401, "invalid_token");
    };
    await view.show();
    assert.equal(resets, 0); assert.deepEqual(messages, []);
    assert.equal(pickCount, 0); assert.equal(dialogCount, 0);
    view.dispose();
  }
});

test("shared ordinary material receipt uses only the managed temporary folder and never asks for an external destination", async () => {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), "autograde-shared-material-view-")));
  try {
    const {state, client} = fixture();
    const managed = path.join(root, "session", "materials"), outside = path.join(root, "usb");
    await mkdir(managed, {recursive: true}); await mkdir(outside); directory = outside;
    let saved = "", suggestions = 0;
    const view = new StudentMaterialsView(client, {managedDirectory: () => managed,
      defaultDirectory: async () => {suggestions++; return {scheme: "file", fsPath: outside} as import("vscode").Uri;},
      onDidSave: (_material, file) => {saved = file;}});
    await view.show();
    assert.equal(state.reads, 1); assert.equal(dialogCount, 0); assert.equal(suggestions, 0);
    assert.equal(path.dirname(saved), managed); assert.deepEqual(await readFile(saved), pdf);
    assert.deepEqual(await readdir(outside), []);
    assert.match(messages.join(" "), /실습 종료 시 삭제/); assert.match(messages.join(" "), /내 학습내용 내보내기/);
    messages.length = 0; await view.show();
    assert.match(messages.join(" "), /이미 임시로 받았습니다/);
    assert.doesNotMatch(messages.join(" "), /다른 폴더를 선택/);
    view.dispose();
  } finally {await rm(root, {recursive: true, force: true});}
});

test("shared material destination changes or symlink substitution cannot write outside the session", async () => {
  const root = await realpath(await mkdtemp(path.join(tmpdir(), "autograde-shared-material-view-")));
  try {
    for (const kind of ["scope", "symlink"]) {
      if (kind === "symlink" && process.platform === "win32") continue;
      const {client} = fixture();
      const managed = path.join(root, `${kind}-materials`), outside = path.join(root, `${kind}-external`);
      await mkdir(managed); await mkdir(outside); let selected = managed;
      const view = new StudentMaterialsView(client, {managedDirectory: () => selected});
      client.getStudentMaterialFile = async () => {
        if (kind === "scope") selected = outside;
        else {await rename(managed, `${managed}-original`); await symlink(outside, managed);}
        return pdf;
      };
      await view.show();
      assert.equal(dialogCount, 0); assert.deepEqual(await readdir(outside), []);
      assert.doesNotMatch(messages.join(" "), /자료를 저장했습니다/);
      view.dispose();
    }
  } finally {await rm(root, {recursive: true, force: true});}
});
