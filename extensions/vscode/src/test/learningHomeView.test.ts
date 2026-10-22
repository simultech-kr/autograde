import assert from "node:assert/strict";
import test from "node:test";
import type { LearningHomeState } from "../learningHome";

interface ModuleLoader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
type MessageHandler = (message: unknown) => Promise<void>;
interface FakePanel {
  webview: {html: string; onDidReceiveMessage(handler: MessageHandler): {dispose(): void}};
  onDidDispose(handler: () => void): {dispose(): void};
  reveal(): void; dispose(): void;
}
const calls: unknown[][] = [];
const info: string[] = [];
const errors: string[] = [];
let panel: FakePanel;
let handler: MessageHandler;
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
        webview: {html: "", onDidReceiveMessage: callback => { handler = callback; return {dispose: () => {listenersDisposed++;}}; }},
        onDidDispose: callback => {close = callback; return {dispose: () => {listenersDisposed++; close = undefined;}};},
        reveal: () => {reveals++;}, dispose: () => {close?.();},
      };
      return panel;
    },
  },
};
loader._load = function(name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const {LearningHomeView} = require("../learningHomeView") as typeof import("../learningHomeView");
loader._load = original;
const ready: LearningHomeState = {authenticated: true, trusted: true, hasWorkspace: true};

test("home is a singleton, disables command URIs and local resources, and disposes listeners", async () => {
  const startCount = panelCount;
  const startDisposed = listenersDisposed;
  const view = new LearningHomeView(() => ready);
  view.show(); view.show();
  assert.equal(panelCount, startCount + 1);
  assert.ok(reveals > 0);
  assert.equal(options.enableCommandUris, false);
  assert.deepEqual(options.localResourceRoots, []);
  assert.equal(options.retainContextWhenHidden, false);
  await handler({type: "learningAction", action: "result"});
  assert.deepEqual(calls.at(-1), ["autograde.viewLatestResult"]);
  view.dispose(); view.dispose();
  assert.equal(listenersDisposed, startDisposed + 2);
  const count = calls.length;
  await handler({type: "learningAction", action: "result"});
  view.show();
  assert.equal(calls.length, count);
  assert.equal(panelCount, startCount + 1);
});

test("closing releases resources and manually reopening creates a fresh panel", () => {
  const view = new LearningHomeView(() => ready);
  const count = panelCount;
  const disposed = listenersDisposed;
  view.show(); panel.dispose();
  assert.equal(listenersDisposed, disposed + 2);
  view.show();
  assert.equal(panelCount, count + 2);
  view.dispose();
});

test("messages cannot bypass current auth/trust or inject command arguments", async () => {
  let state = ready;
  const view = new LearningHomeView(() => state);
  view.show();
  const count = calls.length;
  for (const message of [{type: "learningAction", action: "submit", arguments: ["evil"]},
    {type: "learningAction", action: "vscode.openFolder"}]) await handler(message);
  state = {...ready, authenticated: false};
  await handler({type: "learningAction", action: "submit"});
  state = {...ready, trusted: false};
  await handler({type: "learningAction", action: "claim"});
  assert.equal(calls.length, count);
  assert.match(info.at(-1)!, /信頼|신뢰/);
  view.refresh();
  assert.match(panel.webview.html, /제한 모드/);
  view.dispose();
});

test("duplicate actions are suppressed during a pending command and command errors do not disclose details", async () => {
  const view = new LearningHomeView(() => ready);
  view.show();
  let finish!: () => void;
  execute = () => new Promise(resolve => {finish = () => resolve(undefined);});
  const count = calls.length;
  const pending = handler({type: "learningAction", action: "configure"});
  await handler({type: "learningAction", action: "configure"});
  assert.equal(calls.length, count + 1);
  assert.match(panel.webview.html, /작업 진행 중/);
  finish(); await pending;
  assert.doesNotMatch(panel.webview.html, /작업 진행 중/);
  execute = async () => {throw new Error("private-token-value");};
  await handler({type: "learningAction", action: "configure"});
  assert.doesNotMatch(errors.at(-1)!, /private-token-value/);
  assert.match(errors.at(-1)!, /연결 상태/);
  execute = async () => undefined;
  view.dispose();
});
