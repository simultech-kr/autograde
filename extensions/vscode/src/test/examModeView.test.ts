import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import * as path from "node:path";
import test from "node:test";
import type {ExamScope, ExamViewState} from "../examMode";

interface ModuleLoader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
const fetchOriginal = globalThis.fetch;
type Handler = (value: unknown) => Promise<void>;
let handler: Handler;
let panel: {webview: {html: string; postMessage(value: unknown): Promise<boolean>; onDidReceiveMessage(value: Handler): {dispose(): void}};
  reveal(): void; dispose(): void; onDidDispose(value: () => void): {dispose(): void}};
let html = "";
let globalValue: string | undefined = "https://exam.example";
let options: Record<string, unknown>;
let inputs: string[] = [];
let prompt: () => Promise<string | undefined> = async () => inputs.shift();
const errors: string[] = [];
const writes: {key: string; value: unknown; target: number}[] = [];
const requests: {url: string; init: RequestInit}[] = [];
const stub = {
  ViewColumn: {One: 1}, StatusBarAlignment: {Left: 1}, ConfigurationTarget: {Global: 1},
  workspace: {getConfiguration: () => ({inspect: () => ({globalValue, workspaceValue: "https://malicious-workspace.example"}),
    update: async (key: string, value: string, target: number) => {writes.push({key, value, target}); globalValue = value;}})},
  window: {
    showInputBox: async () => prompt(), showErrorMessage: (value: string) => {errors.push(value);},
    createStatusBarItem: () => ({text: "", tooltip: "", show() {}, hide() {}, dispose() {}}),
    createWebviewPanel: (_type: string, _title: string, _column: number, value: Record<string, unknown>) => {
      options = value; let closed: (() => void) | undefined;
      panel = {webview: {html: "", postMessage: async message => {html = (message as {html: string}).html; return true;},
        onDidReceiveMessage: value => {handler = value; return {dispose() {}};}},
      reveal() {}, dispose() {closed?.();}, onDidDispose: value => {closed = value; return {dispose() {}};}};
      return panel;
    },
  },
};
loader._load = function(name, parent, main) {return name === "vscode" ? stub : original.call(this, name, parent, main);};
const {ExamModeView, examAction, renderExamMode} = require("../examModeView") as typeof import("../examModeView");
loader._load = original;
const scope: ExamScope = {generation: 1, assignmentId: "assignment-1", autogradeOrigin: "https://grade.example"};
const connected: ExamViewState = {phase: "connected", origin: "https://exam.example", fresh: true, checkedAt: 1000000, leaseRemainingSeconds: 99,
  status: {exam: {id: "exam", title: "시험 <script>evil</script>", status: "active"}, seat_id: "A01", assignment_id: "assignment-1", autograde_origin: "https://grade.example", server_time: 1000, expires_at: 2000,
    agent: {online: true, mode: "enforced", backend: "windows-wfp-hyperv", state: "installed", lease_expires_at: 1100, last_seen: 999, verified: false}, permissions: {can_release: false}}};

test("exam actions reject extra fields, arbitrary URLs/commands and release requests", () => {
  for (const action of ["connect", "refresh", "disconnect", "configure"]) assert.equal(examAction({type: "examAction", action}), action);
  for (const message of [null, [], "connect", {type: "examAction", action: "release"}, {type: "examAction", action: "connect", token: "evil"},
    {type: "examAction", action: "__proto__"}, {type: "examAction", action: "vscode.openFolder"}, {type: "examAction", action: "connect", url: "https://evil"}]) assert.equal(examAction(message), undefined);
});

test("panel is theme-aware, narrow responsive, CSP-protected and distinguishes unverified/offline/simulation", () => {
  const rendered = renderExamMode(connected, "test-nonce", true, false, scope);
  assert.match(rendered, /default-src 'none'; style-src 'nonce-test-nonce'; script-src 'nonce-test-nonce'/);
  assert.match(rendered, /var\(--vscode-editor-background\)/); assert.match(rendered, /var\(--vscode-focusBorder\)/);
  assert.match(rendered, /@media\(max-width:420px\)/); assert.match(rendered, /overflow-wrap:anywhere/);
  assert.match(rendered, /시험 &lt;script&gt;evil&lt;\/script&gt;/); assert.doesNotMatch(rendered, /<script>evil/);
  assert.match(rendered, /채점 서버: https:\/\/grade.example · 현재 과제 ID: assignment-1/);
  assert.match(rendered, /제한 설치 보고 · 실제 차단 미검증/);
  assert.match(rendered, /\(진행 중\)/);
  assert.match(rendered, /종료는 시험 종료나 통신 제한 해제가 아닙니다/);
  assert.match(rendered, /화면 공유 기능은 포함하지 않습니다/);
  assert.doesNotMatch(rendered, /localStorage|setState|fetch\(|command:|<iframe|<form|<input|data-action="release"/);
  assert.match(renderExamMode({...connected, fresh: false}, "n", true, false), /상태 확인 불가/);
  assert.match(renderExamMode({...connected, status: {...connected.status!, agent: {...connected.status!.agent, mode: "simulation"}}}, "n", true, false), /모의 모드 · 실제 통신 차단 아님/);
  assert.match(renderExamMode({phase: "disconnected", fresh: false}, "n", false, false), /data-action="connect" disabled/);
});

test("UI uses user-only origin, no persisted token, no automatic connect, and disconnect never releases Agent", async () => {
  requests.length = 0; writes.length = 0; globalValue = "https://exam.example"; inputs = ["12345678"];
  globalThis.fetch = async (url, init = {}) => {
    requests.push({url: String(url), init});
    const result = String(url).endsWith("disconnect") ? {disconnected: true} : {token: "independent-exam-token-12345678901234567890123", expires_at: 2000, status: connected.status};
    return new Response(JSON.stringify({ok: true, result}), {headers: {"Content-Type": "application/json"}});
  };
  const view = new ExamModeView(() => scope);
  try {
    view.show(); assert.equal(requests.length, 0); assert.equal(options.enableCommandUris, false); assert.deepEqual(options.localResourceRoots, []);
    assert.match(panel.webview.html, /시험 연결/);
    await handler({type: "examAction", action: "connect", token: "injected"}); assert.equal(requests.length, 0);
    await handler({type: "examAction", action: "connect"});
    assert.equal(requests.length, 1); assert.equal(requests[0]!.url, "https://exam.example/api/ide/connect");
    assert.equal(writes.length, 0); assert.doesNotMatch(html, /independent-exam-token|12345678|malicious-workspace/);
    await handler({type: "examAction", action: "disconnect"}); await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests[1]!.url, "https://exam.example/api/ide/disconnect"); assert.equal(requests.length, 2);
    assert.match(html, /시험 미연결/);
    view.dispose(); await handler({type: "examAction", action: "connect"}); assert.equal(requests.length, 2);
  } finally {view.dispose(); globalThis.fetch = fetchOriginal;}
});

test("missing user setting ignores workspace address and prompts explicit origin; saves only that origin", async () => {
  globalValue = undefined; writes.length = 0; requests.length = 0; inputs = ["https://exam.example:20100", "12345678"];
  globalThis.fetch = async (url, init = {}) => {requests.push({url: String(url), init}); return new Response("", {status: 403});};
  const view = new ExamModeView(() => scope);
  try {
    view.show(); await handler({type: "examAction", action: "connect"});
    assert.deepEqual(writes, [{key: "examControlBaseUrl", value: "https://exam.example:20100", target: 1}]);
    assert.equal(requests[0]!.url, "https://exam.example:20100/api/ide/connect");
  } finally {view.dispose(); globalThis.fetch = fetchOriginal;}
});

test("logout/configuration change while code input is open prevents stale submission", async () => {
  globalValue = "https://exam.example"; requests.length = 0;
  let resolve!: (value: string) => void; prompt = () => new Promise<string>(yes => {resolve = yes;});
  globalThis.fetch = async (url, init = {}) => {requests.push({url: String(url), init}); return new Response("", {status: 403});};
  const view = new ExamModeView(() => scope);
  try {
    view.show(); const pending = handler({type: "examAction", action: "connect"}); view.clear(); resolve("12345678"); await pending;
    assert.equal(requests.length, 0);
  } finally {view.dispose(); globalThis.fetch = fetchOriginal; prompt = async () => inputs.shift();}
});

test("exam command is in learning home/sidebar, version/lock agree, and lifecycle hooks clear memory sessions", async () => {
  const root = path.join(__dirname, "../..");
  const manifest = JSON.parse(await readFile(path.join(root, "package.json"), "utf8"));
  const lock = JSON.parse(await readFile(path.join(root, "package-lock.json"), "utf8"));
  assert.equal(manifest.version, lock.version); assert.equal(manifest.version, lock.packages[""].version);
  assert.deepEqual(manifest.extensionKind, ["workspace"]);
  assert.ok(manifest.contributes.commands.some((entry: {command: string}) => entry.command === "autograde.openExamMode"));
  assert.ok(manifest.contributes.menus["view/title"].some((entry: {command: string}) => entry.command === "autograde.openExamMode"));
  const source = await readFile(path.join(root, "src/extension.ts"), "utf8");
  assert.match(source, /if \(!authenticated\).*examMode\.clear\(\)/);
  assert.match(source, /onSelected = id => .*examMode\.reconcile\(\)/);
  assert.match(source, /autograde\.examControlBaseUrl.*examMode\.clear\(\)/);
  const client = await readFile(path.join(root, "src/examMode.ts"), "utf8");
  assert.doesNotMatch(client, /SecretStorage|globalState|workspaceState|writeFile|execFile|spawn\(|powershell|\/api\/.*release/);
});
