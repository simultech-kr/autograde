import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as path from "node:path";
import test from "node:test";
import { SubmissionResultMonitor } from "../submissionResultMonitor";
import type { GradeResult } from "../types";
import type { ResultDiagnosticTarget } from "../resultDiagnostics";

class FakeUri {
  public constructor(public readonly value: string) {}
  public get scheme() { return new URL(this.value).protocol.slice(0, -1); }
  public get authority() { return new URL(this.value).host; }
  public get fsPath() { return decodeURIComponent(new URL(this.value).pathname); }
  public toString() { return this.value; }
}
class FakeRange {
  public constructor(public readonly startLine: number, public readonly startColumn: number,
    public readonly endLine: number, public readonly endColumn: number) {}
}
let whileBuilding: (() => void) | undefined;
class FakeDiagnostic {
  public source?: string;
  public constructor(public readonly range: FakeRange, public readonly message: string, public readonly severity: number) {
    whileBuilding?.();
  }
}
type Entry = [FakeUri, FakeDiagnostic[]];
function collection() {
  const calls: {cleared: number; entries: Entry[]; writes: number} = {cleared: 0, entries: [], writes: 0};
  return {calls, api: {
    clear: () => {calls.cleared++; calls.entries = [];},
    set: (entries: Entry[]) => {calls.entries = entries; calls.writes++;},
  } as unknown as import("vscode").DiagnosticCollection};
}
const uri = (value: string) => new FakeUri(value) as unknown as import("vscode").Uri;
interface ModuleLoader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
const stub = {
  Uri: {
    parse: (value: string) => new FakeUri(value),
    joinPath: (root: FakeUri, ...parts: string[]) => {
      const value = new URL(root.value);
      value.pathname = path.posix.join(decodeURIComponent(value.pathname), ...parts);
      return new FakeUri(value.toString());
    },
  },
  Diagnostic: FakeDiagnostic, Range: FakeRange,
  DiagnosticSeverity: {Error: 0, Warning: 1, Information: 2, Hint: 3},
};
loader._load = function(name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const {createResultDiagnosticTarget, publishResultDiagnostics, rememberResultDiagnosticTarget} = require("../resultDiagnostics") as typeof import("../resultDiagnostics");
loader._load = original;

const result: GradeResult = {state: "published", sourceDigest: "sha256:source", score: 0, maxScore: 100,
  rubric: [], diagnostics: [{path: "main.cpp", line: 4, column: 7, message: "expected semicolon", severity: "error"}]};

test("bundle diagnostics resolve against submitted assignment root, not its course workspace or focused file", () => {
  const target = createResultDiagnosticTarget(uri("file:///course/hello-world"));
  assert.ok(Object.isFrozen(target));
  const output = collection();
  publishResultDiagnostics(output.api, result, target, () => true);
  assert.equal(output.calls.entries[0]?.[0].fsPath, "/course/hello-world/main.cpp");
  assert.notEqual(output.calls.entries[0]?.[0].fsPath, "/course/main.cpp");
  const diagnostic = output.calls.entries[0]?.[1][0];
  assert.equal(diagnostic?.message, "expected semicolon");
  assert.equal(diagnostic?.source, "Autograde");
  assert.equal(diagnostic?.severity, 0);
  assert.deepEqual(diagnostic?.range, new FakeRange(3, 6, 3, 7));
});

test("captured URI is immutable even if the caller's holder is later changed", () => {
  let selected = uri("file:///course/submitted-a");
  const target = createResultDiagnosticTarget(selected);
  selected = uri("file:///course/editing-b");
  const output = collection();
  publishResultDiagnostics(output.api, result, target, () => true);
  assert.equal(output.calls.entries[0]?.[0].fsPath, "/course/submitted-a/main.cpp");
  assert.notEqual(output.calls.entries[0]?.[0].toString(), selected.toString());
});

test("WSL and SSH diagnostic URIs keep their original scheme and authority", () => {
  for (const authority of ["wsl+Ubuntu", "ssh-remote+lab-server"]) {
    const root = uri(`vscode-remote://${authority}/home/student/course/hello`);
    const output = collection();
    publishResultDiagnostics(output.api, {...result, diagnostics: [{...result.diagnostics[0]!, path: "src/main.cpp"}]},
      createResultDiagnosticTarget(root), () => true);
    assert.equal(output.calls.entries[0]?.[0].scheme, "vscode-remote");
    assert.equal(output.calls.entries[0]?.[0].authority, authority);
    assert.equal(output.calls.entries[0]?.[0].fsPath, "/home/student/course/hello/src/main.cpp");
  }
});

test("git assignment subdirectories remain supported while unsafe server paths cannot escape the bound root", () => {
  const output = collection();
  publishResultDiagnostics(output.api, {...result, diagnostics: [
    ...result.diagnostics, ...["../other.cpp", "/etc/passwd", "C:\\secret.txt", "command:evil", "src/../../outside.cpp"].map(value => ({path: value, message: "unsafe"})),
  ]}, createResultDiagnosticTarget(uri("file:///repository"), "labs/week01"), () => true);
  assert.equal(output.calls.entries.length, 1);
  assert.equal(output.calls.entries[0]?.[0].fsPath, "/repository/labs/week01/main.cpp");
  publishResultDiagnostics(output.api, result, createResultDiagnosticTarget(uri("file:///repository"), "../outside"), () => true);
  assert.equal(output.calls.entries.length, 0);
});

test("unpublished, unknown local bindings and unsupported URI schemes never create file diagnostics", () => {
  const output = collection();
  const target = createResultDiagnosticTarget(uri("file:///course/hello"));
  for (const state of ["queued", "running", "graded", "result_pending", "infra_failed"]) {
    publishResultDiagnostics(output.api, {...result, state}, target, () => true);
  }
  publishResultDiagnostics(output.api, result, undefined, () => true);
  assert.equal(output.calls.writes, 0);
  assert.equal(output.calls.cleared, 6);
  assert.equal(createResultDiagnosticTarget(uri("https://example.invalid/code")), undefined);
  assert.equal(createResultDiagnosticTarget(uri("untitled:///main.cpp")), undefined);
});

test("stale result scopes cannot clear or overwrite newer diagnostics, including scope changes while rendering", () => {
  const output = collection();
  const target = createResultDiagnosticTarget(uri("file:///course/hello"));
  publishResultDiagnostics(output.api, result, target, () => true);
  const before = {cleared: output.calls.cleared, writes: output.calls.writes, entries: output.calls.entries};
  publishResultDiagnostics(output.api, {...result, state: "queued"}, target, () => false);
  publishResultDiagnostics(output.api, result, target, () => false);
  let current = true;
  whileBuilding = () => {current = false;};
  try { publishResultDiagnostics(output.api, result, target, () => current); }
  finally { whileBuilding = undefined; }
  assert.deepEqual(output.calls, before);
});

test("receipt bindings are independent, bounded and can be discarded at session boundaries", () => {
  const bindings = new Map<string, ResultDiagnosticTarget>();
  const first = createResultDiagnosticTarget(uri("file:///course/first"))!;
  const second = createResultDiagnosticTarget(uri("file:///course/second"))!;
  rememberResultDiagnosticTarget(bindings, "one", first);
  rememberResultDiagnosticTarget(bindings, "two", second);
  assert.equal(bindings.get("one"), first);
  assert.equal(bindings.get("two"), second);
  rememberResultDiagnosticTarget(bindings, "unknown", undefined);
  assert.equal(bindings.has("unknown"), false);
  for (let index = 0; index < 100; index++) rememberResultDiagnosticTarget(bindings, `receipt-${index}`, second);
  assert.equal(bindings.size, 100);
  assert.equal(bindings.has("one"), false);
  assert.equal(bindings.has("two"), false);
  bindings.clear();
  assert.equal(bindings.size, 0);
});

test("late grading from an old receipt cannot replace the new receipt's bound file diagnostics", async t => {
  let finishOld!: (value: GradeResult) => void;
  const old = new Promise<GradeResult>(resolve => {finishOld = resolve;});
  let oldRequested = false;
  const monitor = new SubmissionResultMonitor({
    getSubmission: async id => ({id, state: "published", sourceDigest: "sha256:source"}),
    getResult: async id => { if (id === "old") {oldRequested = true; return old;} return result; },
  });
  t.after(() => monitor.cancel());
  const output = collection();
  const start = (id: string, folder: string) => {
    const scope = monitor.begin();
    const target = createResultDiagnosticTarget(uri(`file://${folder}`));
    monitor.start(scope, {submission: {id, state: "queued", sourceDigest: "sha256:source"}, isSessionCurrent: () => true,
      onAuthenticationRequired: () => assert.fail("unexpected auth failure"),
      onUpdate: update => publishResultDiagnostics(output.api, update.result, target, () => scope.isCurrent())});
  };
  start("old", "/course/old");
  for (let index = 0; index < 20 && !oldRequested; index++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(oldRequested, true);
  start("new", "/course/new");
  for (let index = 0; index < 20 && output.calls.writes === 0; index++) await new Promise(resolve => setImmediate(resolve));
  assert.equal(output.calls.entries[0]?.[0].fsPath, "/course/new/main.cpp");
  const writes = output.calls.writes;
  finishOld({...result, diagnostics: [{path: "old.cpp", message: "old feedback"}]});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(output.calls.writes, writes);
  assert.equal(output.calls.entries[0]?.[0].fsPath, "/course/new/main.cpp");
});

test("extension binds the precise submitted URI and reuses receipt binding without consulting active workspace", () => {
  const source = readFileSync(path.resolve(__dirname, "../../src/extension.ts"), "utf8");
  const submit = source.split("async function submitCurrentBundle")[1]!.split("async function viewSubmissionHistory")[0]!;
  assert.match(submit, /vscode\.Uri\.joinPath\(submissionUri, path\.relative\(submissionUri\.fsPath, assignmentRoot\)\)/);
  assert.match(submit, /rememberResultDiagnosticTarget\(resultUi\.diagnosticTargets, submission\.id, diagnosticTarget\)/);
  const watch = source.split("function watchSubmissionResult")[1]!.split("function renderResult")[0]!;
  assert.match(watch, /const diagnosticTarget = ui\.diagnosticTargets\.get\(submission\.id\)/);
  assert.match(watch, /publishResultDiagnostics\(ui\.diagnostics, result, diagnosticTarget, isCurrent\)/);
  assert.doesNotMatch(watch, /selectWorkspaceFolder|activeTextEditor/);
  assert.ok((source.match(/diagnosticTargets\.clear\(\)/g) ?? []).length >= 3, "logout, normal sign-in and claim sign-in clear local bindings");
});
