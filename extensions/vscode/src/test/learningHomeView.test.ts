import assert from "node:assert/strict";
import test from "node:test";
import type { LearningHomeState } from "../learningHome";
import type { LearningHomeCallbacks } from "../learningHomeView";

interface ModuleLoader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
type MessageHandler = (message: unknown) => Promise<void>;
interface FakePanel {
  visible: boolean;
  webview: {html: string; postMessage(message: unknown): Promise<boolean>; onDidReceiveMessage(handler: MessageHandler): {dispose(): void}};
  onDidDispose(handler: () => void): {dispose(): void};
  onDidChangeViewState(handler: (event: {webviewPanel: FakePanel}) => void): {dispose(): void};
  reveal(): void; dispose(): void;
}
const calls: unknown[][] = [];
const info: string[] = [];
const errors: string[] = [];
const posted: Record<string, unknown>[] = [];
let panel: FakePanel;
let handler: MessageHandler;
let changeView: (event: {webviewPanel: FakePanel}) => void;
let close: (() => void) | undefined;
let listenersDisposed = 0;
let panelCount = 0;
let reveals = 0;
let options: Record<string, unknown> = {};
let execute: () => Promise<unknown> = async () => undefined;
const stub = {
  ViewColumn: {One: 1},
  commands: {executeCommand: async (...args: unknown[]) => {calls.push(args); return execute();}},
  window: {
    showInformationMessage: (text: string) => {info.push(text);},
    showErrorMessage: (text: string) => {errors.push(text);},
    createWebviewPanel: (_type: string, _title: string, _column: number, givenOptions: Record<string, unknown>) => {
      panelCount++; options = givenOptions;
      panel = {
        visible: true,
        webview: {html: "", postMessage: async message => {posted.push(message as Record<string, unknown>); return true;},
          onDidReceiveMessage: callback => { handler = callback; return {dispose: () => {listenersDisposed++;}}; }},
        onDidDispose: callback => {close = callback; return {dispose: () => {listenersDisposed++; close = undefined;}};},
        onDidChangeViewState: callback => {changeView = callback; return {dispose: () => {listenersDisposed++;}};},
        reveal: () => {reveals++;}, dispose: () => {close?.();},
      };
      return panel;
    },
  },
};
loader._load = function(name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const {LearningHomeView} = require("../learningHomeView") as typeof import("../learningHomeView");
loader._load = original;
const ready: LearningHomeState = {authenticated: true, trusted: true, hasWorkspace: true, serviceBaseUrl: "https://grade.example.test", sessionGeneration: 1};
const nonce = (): string => /data-nonce="([^"]+)"/.exec(panel.webview.html)![1]!;
const action = (name: string) => ({type: "learningAction", action: name, nonce: nonce()});
const form = (name: "claim" | "server" | "portal", value: string) => ({type: "learningForm", form: name, nonce: nonce(), value,
  ...(name === "claim" ? {expectedServer: ready.serviceBaseUrl} : {})});
const noCallbacks: LearningHomeCallbacks = {claim: async () => undefined, configureServer: async () => undefined, configurePortal: async () => undefined};

test("home is singleton, denies command URIs and local resources, and disposes all listeners", async () => {
  const startCount = panelCount;
  const startDisposed = listenersDisposed;
  const view = new LearningHomeView(() => ready);
  view.show(); view.show();
  assert.equal(panelCount, startCount + 1); assert.ok(reveals > 0);
  assert.equal(options.enableCommandUris, false); assert.deepEqual(options.localResourceRoots, []);
  assert.equal(options.retainContextWhenHidden, false);
  await handler(action("result")); assert.deepEqual(calls.at(-1), ["autograde.viewLatestResult"]);
  const stale = action("result");
  view.dispose(); view.dispose();
  assert.equal(panel.webview.html, ""); assert.equal(listenersDisposed, startDisposed + 3);
  const count = calls.length; await handler(stale); view.show();
  assert.equal(calls.length, count); assert.equal(panelCount, startCount + 1);
});

test("closing releases resources and reopening creates a fresh panel", () => {
  const view = new LearningHomeView(() => ready);
  const count = panelCount; const disposed = listenersDisposed;
  view.show(); panel.dispose(); assert.equal(listenersDisposed, disposed + 3);
  view.show(); assert.equal(panelCount, count + 2); view.dispose();
});

test("messages cannot bypass auth/trust, inject arguments or reuse an old document", async () => {
  let state = ready;
  const view = new LearningHomeView(() => state); view.show();
  const count = calls.length;
  await handler({...action("submit"), arguments: ["evil"]});
  await handler(action("vscode.openFolder"));
  const stale = action("result"); view.refresh(); await handler(stale);
  state = {...ready, authenticated: false}; view.refresh(); await handler(action("submit"));
  state = {...ready, trusted: false}; view.refresh(); await handler(action("claim"));
  assert.equal(calls.length, count); assert.match(info.at(-1)!, /신뢰/);
  assert.match(panel.webview.html, /제한 모드/); view.dispose();
});

test("ordinary pending commands suppress duplicates and never disclose command errors", async () => {
  const view = new LearningHomeView(() => ready); view.show();
  let finish!: () => void;
  execute = () => new Promise(resolve => {finish = () => resolve(undefined);});
  const count = calls.length;
  const pending = handler(action("connection")); await handler(action("connection"));
  assert.equal(calls.length, count + 1); assert.match(panel.webview.html, /작업 진행 중/);
  finish(); await pending; assert.doesNotMatch(panel.webview.html, /작업 진행 중/);
  execute = async () => {throw new Error("private-token-value");}; await handler(action("connection"));
  assert.doesNotMatch(errors.at(-1)!, /private-token-value/); assert.match(errors.at(-1)!, /연결 상태/);
  execute = async () => undefined; view.dispose();
});

test("claim and address actions focus inline fields without invoking commands or quick input", async () => {
  const view = new LearningHomeView(() => ready, noCallbacks); view.show("claim");
  assert.match(panel.webview.html, /data-initial-form="claim"/);
  const count = calls.length;
  for (const [name, field] of [["claim", "claim"], ["configure", "server"], ["portal", "portal"]]) {
    await handler(action(name!)); assert.deepEqual(posted.at(-1), {type: "learningFocus", form: field, nonce: nonce()});
  }
  assert.equal(calls.length, count);
  view.show("server"); assert.match(panel.webview.html, /data-initial-form="server"/);
  view.dispose();
});

test("input edits defer noncritical refresh and focus switches, and apply it when cleared", async () => {
  let state = ready;
  const view = new LearningHomeView(() => state, noCallbacks); view.show();
  await handler({type: "learningEdit", nonce: nonce(), dirty: true});
  const html = panel.webview.html;
  state = {...state, checkpointStatus: "새 서버 저장 상태"}; view.refresh();
  changeView({webviewPanel: panel});
  view.show("server");
  assert.equal(panel.webview.html, html); assert.equal(posted.at(-1)?.form, "server");
  await handler({type: "learningEdit", nonce: nonce(), dirty: false});
  assert.match(panel.webview.html, /새 서버 저장 상태/); assert.notEqual(panel.webview.html, html);
  view.dispose();
});

test("auth generation, trust, destinations and shared-session changes always replace an edited document", async () => {
  for (const patch of [{authenticated: false}, {trusted: false}, {serviceBaseUrl: "https://new.example.test"},
    {studentPortalBaseUrl: "https://portal.example.test"}, {sessionGeneration: 2}, {sharedSession: true}, {sessionNeedsRestart: true}]) {
    let state = ready;
    const view = new LearningHomeView(() => state, noCallbacks); view.show();
    const oldNonce = nonce(); await handler({type: "learningEdit", nonce: oldNonce, dirty: true});
    state = {...state, ...patch}; view.refresh();
    assert.notEqual(nonce(), oldNonce);
    assert.doesNotMatch(panel.webview.html, /id="claim-code"[^>]* value=/);
    view.dispose();
  }
});

test("hide immediately empties the document and rejects its code when shown again", async () => {
  let received = 0;
  const view = new LearningHomeView(() => ready, {...noCallbacks, claim: async () => {received++;}}); view.show();
  const stale = form("claim", "AK1-TEST-CODE-1234");
  panel.visible = false; changeView({webviewPanel: panel}); assert.equal(panel.webview.html, "");
  await handler(stale); panel.visible = true; changeView({webviewPanel: panel}); await handler(stale);
  assert.equal(received, 0); assert.notEqual(nonce(), stale.nonce); view.dispose();
});

test("claim callback receives displayed server directly; input reference clears before async work and retries are suppressed", async () => {
  let received: string[] = []; let finish!: () => void;
  const view = new LearningHomeView(() => ready, {...noCallbacks, claim: (code, server) => {
    received = [code, server]; return new Promise(resolve => {finish = resolve;});
  }}); view.show();
  const count = calls.length; const request = form("claim", "AK1-TEST-CODE-1234");
  const pending = handler(request);
  assert.deepEqual(received, ["AK1-TEST-CODE-1234", ready.serviceBaseUrl]); assert.equal(request.value, "");
  assert.doesNotMatch(panel.webview.html, /AK1-TEST-CODE-1234/);
  await handler(form("claim", "SECOND-CODE")); assert.deepEqual(received, ["AK1-TEST-CODE-1234", ready.serviceBaseUrl]);
  finish(); await pending; assert.equal(calls.length, count); view.dispose();
});

test("form schemas, origin mismatch, stale nonce and shared session cannot invoke callbacks", async () => {
  let received = 0; let state = ready;
  const view = new LearningHomeView(() => state, {claim: async () => {received++;}, configureServer: async () => {received++;}, configurePortal: async () => {received++;}}); view.show();
  const valid = form("claim", "123456");
  for (const request of [{...valid, nonce: "stale"}, {...valid, expectedServer: "https://evil.example"}, {...valid, command: "evil"},
    {...valid, value: "a".repeat(129)}, {...valid, value: "code\n"}]) await handler(request);
  state = {...ready, sharedSession: true}; view.refresh();
  await handler(form("claim", "123456")); await handler(form("server", "https://new.example")); await handler(form("portal", "https://new.example"));
  assert.equal(received, 0); view.dispose();
});

test("address callbacks are separate, and resolved callbacks do not invent successful authentication", async () => {
  const values: string[][] = [];
  const view = new LearningHomeView(() => ({...ready, authenticated: false}), {...noCallbacks,
    configureServer: async value => {values.push(["server", value]);}, configurePortal: async value => {values.push(["portal", value]);}});
  view.show(); await handler(form("server", "127.0.0.1:20000")); await handler(form("portal", "https://portal.example.test"));
  await handler(form("claim", "123456"));
  assert.deepEqual(values, [["server", "127.0.0.1:20000"], ["portal", "https://portal.example.test"]]);
  assert.match(panel.webview.html, /수령 코드 연결 필요/); assert.doesNotMatch(panel.webview.html, /수령 코드 연결됨/); view.dispose();
});

test("inline failures survive periodic refresh, reveal no raw secrets and distinguish cancellation", async () => {
  let fail!: (reason: Error) => void;
  const view = new LearningHomeView(() => ready, {...noCallbacks, claim: () => new Promise((_resolve, reject) => {fail = reject;})}); view.show();
  const errorCount = errors.length;
  const pending = handler(form("claim", "AK1-PRIVATE-VALUE")); view.refresh();
  fail(new Error("server leaked AK1-PRIVATE-VALUE")); await pending;
  assert.match(panel.webview.html, /연결하지 못했습니다/); assert.doesNotMatch(panel.webview.html, /AK1-PRIVATE-VALUE/);
  const cancelled = handler(form("claim", "123456")); const error = new Error("secret"); error.name = "OperationCancelledError";
  fail(error); await cancelled;
  assert.match(panel.webview.html, /취소했습니다. 다시 입력해 주세요/); assert.doesNotMatch(panel.webview.html, /secret/);
  assert.equal(errors.length, errorCount); view.dispose();
});

const discovered = {id: "school-1", displayName: "학교 · 실습실", apiUrl: "https://other.example.test", portalUrl: "https://portal.example.test"};
const discoverySelection = (id = discovered.id) => ({type: "learningDiscoverySelect", nonce: nonce(), id});

test("discovery search and selection invoke inline callbacks without forwarding endpoints or commands", async () => {
  let state: LearningHomeState = {...ready, authenticated: false, discovery: {status: "idle", servers: []}};
  let searches = 0;
  const selections: string[] = [];
  const view = new LearningHomeView(() => state, {...noCallbacks,
    discoverServers: async () => { searches++; state = {...state, discovery: {status: "complete", servers: [discovered]}}; },
    selectDiscoveredServer: async id => {selections.push(id);},
  });
  view.show();
  const count = calls.length;
  await handler(action("discoverServer"));
  assert.equal(searches, 1);
  assert.match(panel.webview.html, /학교 · 실습실/);
  await handler(discoverySelection());
  assert.deepEqual(selections, ["school-1"]);
  assert.equal(calls.length, count);
  assert.match(panel.webview.html, /수령 코드 연결 필요/);
  view.dispose();
});

test("discovery rejects unknown IDs, stale documents, endpoint injection and busy duplicates", async () => {
  let state: LearningHomeState = {...ready, authenticated: false, discovery: {status: "complete", servers: [discovered]}};
  let selections = 0;
  let finish!: () => void;
  const view = new LearningHomeView(() => state, {...noCallbacks, selectDiscoveredServer: async () => {
    selections++; await new Promise<void>(resolve => {finish = resolve;});
  }});
  view.show();
  await handler({...discoverySelection(), apiUrl: "https://evil.example"});
  await handler({...discoverySelection(), nonce: "old"});
  await handler(discoverySelection("missing"));
  assert.equal(selections, 0);
  assert.match(String(posted.at(-1)?.text), /현재 검색 결과에 없습니다/);
  const pending = handler(discoverySelection());
  await handler(discoverySelection());
  assert.equal(selections, 1);
  finish(); await pending;
  state = {...state, discovery: {status: "searching", servers: [discovered]}}; view.refresh();
  await handler(discoverySelection());
  assert.equal(selections, 1);
  state = {...state, discovery: {status: "complete", servers: [discovered, discovered]}}; view.refresh();
  await handler(discoverySelection()); assert.equal(selections, 1);
  view.dispose();
});

test("discovery respects shared session identity and never discards an edited claim or address", async () => {
  let state: LearningHomeState = {...ready, authenticated: false, discovery: {status: "complete", servers: [discovered]}};
  let invoked = 0;
  const view = new LearningHomeView(() => state, {...noCallbacks,
    discoverServers: async () => {invoked++;}, selectDiscoveredServer: async () => {invoked++;}});
  view.show();
  await handler({type: "learningEdit", nonce: nonce(), dirty: true});
  const html = panel.webview.html;
  await handler(action("discoverServer")); await handler(discoverySelection());
  assert.equal(invoked, 0); assert.equal(panel.webview.html, html);
  assert.match(String(posted.at(-1)?.text), /입력 중인 수령 코드나 주소/);
  await handler({type: "learningEdit", nonce: nonce(), dirty: false});
  for (const patch of [{authenticated: true}, {sessionNeedsRestart: true}]) {
    state = {...state, ...patch, sharedSession: true}; view.refresh();
    await handler(action("discoverServer")); await handler(discoverySelection());
    assert.equal(invoked, 0);
  }
  view.dispose();
});

test("discovery failures are readable without exposing low-level response contents", async () => {
  let state: LearningHomeState = {...ready, authenticated: false, discovery: {status: "complete", servers: [discovered]}};
  const view = new LearningHomeView(() => state, {...noCallbacks,
    discoverServers: async () => {throw new Error("private-token-in-response");},
    selectDiscoveredServer: async () => {throw new Error("private-token-in-response");},
  });
  view.show(); await handler(action("discoverServer"));
  assert.match(panel.webview.html, /주변 서버를 검색하지 못했습니다/);
  assert.doesNotMatch(panel.webview.html, /private-token-in-response/);
  await handler(discoverySelection());
  assert.match(panel.webview.html, /서버를 선택하지 못했습니다/);
  assert.doesNotMatch(panel.webview.html, /private-token-in-response/);
  // Background results do not replace an actively edited document; the pending refresh applies after clearing.
  await handler({type: "learningEdit", nonce: nonce(), dirty: true});
  const before = panel.webview.html;
  state = {...state, discovery: {status: "complete", servers: [], message: "새 검색 완료"}}; view.refresh();
  assert.equal(panel.webview.html, before);
  await handler({type: "learningEdit", nonce: nonce(), dirty: false});
  assert.match(panel.webview.html, /새 검색 완료/);
  view.dispose();
});
