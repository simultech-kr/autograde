import assert from "node:assert/strict";
import test from "node:test";
import { SubmissionResultMonitor } from "../submissionResultMonitor";
import type { GradeResult } from "../types";

interface ModuleLoader { _load(request: string, parent: unknown, isMain: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
const created: FakePanel[] = [];
let options: unknown;
class FakePanel {
  public webview = { html: "" };
  public reveals = 0;
  public disposed = false;
  private close?: () => void;
  public onDidDispose(callback: () => void) { this.close = callback; }
  public reveal() { this.reveals++; }
  public dispose() { if (!this.disposed) { this.disposed = true; this.close?.(); } }
}
const stub = { ViewColumn: { Beside: 2 }, window: {
  createWebviewPanel: (_type: string, _title: string, _column: number, value: unknown) => {
    options = value;
    const panel = new FakePanel(); created.push(panel); return panel;
  },
} };
loader._load = function (name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const { clearResultPanel, showResultPanel, refreshDisplayedResult, pauseDisplayedResult } = require("../resultPanel") as typeof import("../resultPanel");
loader._load = original;
const result: GradeResult = {state: "queued", rubric: [], diagnostics: []};

test("poll updates reuse the panel without stealing focus and permit only result commands", async () => {
  clearResultPanel(); created.length = 0;
  let refreshes = 0; let pauses = 0;
  const actions = {refresh: async () => { refreshes++; }, pause: () => { pauses++; }};
  showResultPanel({id: "a", title: "Lab"}, result, "receipt", "이번 제출", actions);
  showResultPanel({id: "a", title: "Lab"}, result, "receipt", "이번 제출", {...actions, reveal: false});
  assert.equal(created.length, 1); assert.equal(created[0]?.reveals, 1);
  assert.deepEqual(options, {enableScripts: false,
    enableCommandUris: ["autograde.refreshDisplayedResult", "autograde.pauseDisplayedResult"],
    localResourceRoots: [], retainContextWhenHidden: false});
  await refreshDisplayedResult(); pauseDisplayedResult();
  assert.equal(refreshes, 1); assert.equal(pauses, 1);
  clearResultPanel();
  await refreshDisplayedResult(); pauseDisplayedResult();
  assert.equal(refreshes, 1); assert.equal(pauses, 1);
});

test("closing a panel clears its actions once and cannot restore old student callbacks", async () => {
  clearResultPanel();
  let closes = 0;
  showResultPanel({id: "a", title: "Lab"}, result, "receipt", "이번 제출", {
    refresh: async () => assert.fail("closed panel must have no refresh handler"),
    onClose: () => { closes++; },
  });
  created.at(-1)!.dispose();
  clearResultPanel(); await refreshDisplayedResult();
  assert.equal(closes, 1);
});

test("reserving a new result scope before clearing the old panel does not cancel the new receipt", () => {
  clearResultPanel();
  const monitor = new SubmissionResultMonitor({getSubmission: async () => { throw new Error("unused"); },
    getResult: async () => result});
  const oldScope = monitor.begin();
  showResultPanel({id: "a", title: "Lab"}, result, "old", "이전 제출", {
    onClose: () => { if (oldScope.isCurrent()) monitor.cancel(); },
  });
  const newScope = monitor.begin();
  clearResultPanel();
  assert.equal(oldScope.isCurrent(), false);
  assert.equal(newScope.isCurrent(), true);
  monitor.cancel();
});

test("historical results remove active polling actions without retaining their callbacks", async () => {
  clearResultPanel();
  showResultPanel({id: "a", title: "Lab"}, result, "new", "이번 제출", {
    refresh: async () => assert.fail("history must not refresh a different submission"),
  });
  showResultPanel({id: "a", title: "Lab"}, result, "old", "과거 제출 기록");
  await refreshDisplayedResult();
  assert.doesNotMatch(created.at(-1)!.webview.html, /href="command:/);
  clearResultPanel();
});
