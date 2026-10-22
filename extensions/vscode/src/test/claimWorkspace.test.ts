import assert from "node:assert/strict";
import test from "node:test";
import type { MementoLike } from "../submissionAttempt";

interface ModuleLoader { _load(request: string, parent: unknown, isMain: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
let folders: { uri: { toString(): string } }[] | undefined;
let workspaceFile: object | undefined;
let trusted = true;
let action: string | undefined;
let selection: { toString(): string }[] | undefined;
let prompts: unknown[][] = [];
let opened: unknown[][] = [];
let fails = false;
let duringPrompt: (() => void) | undefined;
let beforeCommand: (() => void) | undefined;
const stub = {
  workspace: {
    get workspaceFolders() { return folders; },
    get workspaceFile() { return workspaceFile; },
    get isTrusted() { return trusted; },
  },
  window: {
    showInformationMessage: async (...args: unknown[]) => { prompts.push(args); duringPrompt?.(); return action; },
    showOpenDialog: async () => selection,
  },
  commands: {
    executeCommand: async (...args: unknown[]) => {
      beforeCommand?.(); opened.push(args);
      if (fails) throw new Error("open failed");
    },
  },
};
loader._load = function (name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const { prepareClaimWorkspace, consumePendingClaimWorkspace, resumeClaimWorkspace, PENDING_CLAIM_WORKSPACE_KEY,
  PENDING_CLAIM_WORKSPACE_TTL_MS } = require("../claimWorkspace") as typeof import("../claimWorkspace");
loader._load = original;

class State implements MementoLike {
  public value: unknown;
  public writes: unknown[] = [];
  public get<T>(key: string): T | undefined {
    assert.equal(key, PENDING_CLAIM_WORKSPACE_KEY);
    return this.value as T | undefined;
  }
  public async update(key: string, value: unknown): Promise<void> {
    assert.equal(key, PENDING_CLAIM_WORKSPACE_KEY);
    this.writes.push(value); this.value = value;
  }
}
function uri(value = "file:///course") { return { toString: () => value }; }
function reset() {
  folders = undefined; workspaceFile = undefined; trusted = true; action = undefined; selection = undefined;
  prompts = []; opened = []; fails = false; duringPrompt = undefined; beforeCommand = undefined;
}
function pending(state: State, targetUri = "file:///course", createdAt = Date.now()) {
  state.value = { targetUri, createdAt };
}

test("an existing workspace can proceed to claim-code input without another folder prompt", async () => {
  reset(); folders = [{ uri: uri() }]; const state = new State();
  assert.equal(await prepareClaimWorkspace(state), true);
  assert.equal(prompts.length, 0); assert.deepEqual(opened, []); assert.deepEqual(state.writes, []);
});

test("a blank window persists only folder intent before opening and never consumes a claim code", async () => {
  reset(); action = "수업 폴더 열기"; selection = [uri()]; const state = new State();
  beforeCommand = () => {
    const saved = state.value as { targetUri: string; createdAt: number };
    assert.deepEqual(Object.keys(saved).sort(), ["createdAt", "targetUri"]);
    assert.equal(saved.targetUri, "file:///course"); assert.ok(saved.createdAt <= Date.now());
  };
  assert.equal(await prepareClaimWorkspace(state), false);
  assert.deepEqual(opened, [["vscode.openFolder", selection[0], false]]);
});

test("cancelling either preparation prompt or picker neither persists intent nor proceeds to authentication", async () => {
  reset(); const state = new State();
  assert.equal(await prepareClaimWorkspace(state), false);
  action = "수업 폴더 열기";
  assert.equal(await prepareClaimWorkspace(state), false);
  assert.deepEqual(opened, []); assert.deepEqual(state.writes, []);
});

test("folder open failure clears its intent and preserves the unconsumed-code instruction", async () => {
  reset(); action = "수업 폴더 열기"; selection = [uri()]; fails = true; const state = new State();
  await assert.rejects(prepareClaimWorkspace(state), /코드는 아직 사용하지 않았습니다/);
  assert.equal(state.value, undefined); assert.equal(state.writes.length, 2);
});

test("folder open failure does not clear another window's newer intent", async () => {
  reset(); action = "수업 폴더 열기"; selection = [uri()]; fails = true; const state = new State();
  beforeCommand = () => pending(state, "file:///another-course");
  await assert.rejects(prepareClaimWorkspace(state), /코드는 아직 사용하지 않았습니다/);
  assert.equal((state.value as { targetUri: string }).targetUri, "file:///another-course");
});

test("a matching single folder consumes pending intent exactly once", async () => {
  reset(); folders = [{ uri: uri() }]; const state = new State(); pending(state);
  assert.equal(await consumePendingClaimWorkspace(state), true);
  assert.equal(await consumePendingClaimWorkspace(state), false);
  assert.equal(state.value, undefined); assert.deepEqual(state.writes, [undefined]);
});

test("nonmatching, empty, multiroot and saved workspace windows preserve valid pending intent", async () => {
  reset(); const state = new State(); pending(state);
  assert.equal(await consumePendingClaimWorkspace(state), false);
  folders = [{ uri: uri("file:///course-other") }];
  assert.equal(await consumePendingClaimWorkspace(state), false);
  folders = [{ uri: uri() }, { uri: uri("file:///other") }];
  assert.equal(await consumePendingClaimWorkspace(state), false);
  folders = [{ uri: uri() }]; workspaceFile = {};
  assert.equal(await consumePendingClaimWorkspace(state), false);
  assert.deepEqual(state.writes, []);
});

test("expired, future and malformed intents are removed without continuing", async () => {
  reset(); folders = [{ uri: uri() }]; const state = new State(); const now = Date.now();
  for (const value of [
    { targetUri: "file:///course", createdAt: now - PENDING_CLAIM_WORKSPACE_TTL_MS },
    { targetUri: "file:///course", createdAt: now + 1 },
    { targetUri: "file:///course", createdAt: now, code: "never-retain" },
    { targetUri: "https://example.test/course", createdAt: now },
    { targetUri: "file:///course", createdAt: "bad" },
    { targetUri: "file:///course", createdAt: NaN },
    null, [], "invalid",
  ]) {
    state.value = value;
    assert.equal(await consumePendingClaimWorkspace(state, now), false);
    assert.equal(state.value, undefined);
  }
  assert.deepEqual(opened, []);
});

test("a valid remote folder URI is matched without changing its authority", async () => {
  reset(); const state = new State(); const remote = "vscode-remote://wsl+Ubuntu/home/student/course";
  pending(state, remote); folders = [{ uri: uri(remote) }];
  assert.equal(await consumePendingClaimWorkspace(state), true);
});

test("resuming shows the learning home but requires an explicit action before claim input", async () => {
  reset(); const state = new State(); pending(state); folders = [{ uri: uri() }]; let homes = 0;
  assert.equal(await resumeClaimWorkspace(state, () => { homes += 1; }), true);
  assert.equal(homes, 1); assert.equal(prompts.length, 1); assert.deepEqual(opened, []);
  assert.equal(await resumeClaimWorkspace(state, () => { homes += 1; }), false);
  assert.equal(homes, 1); assert.equal(prompts.length, 1);
});

test("explicit continuation dispatches only the existing claim command with no credentials", async () => {
  reset(); const state = new State(); pending(state); folders = [{ uri: uri() }]; action = "수령 코드 입력";
  await resumeClaimWorkspace(state, () => undefined);
  assert.deepEqual(opened, [["autograde.redeemAssignmentClaim"]]); assert.equal(state.value, undefined);
});

test("untrusted continuation offers guidance only and does not expose a claim action", async () => {
  reset(); const state = new State(); pending(state); folders = [{ uri: uri() }]; trusted = false;
  action = "수령 코드 입력"; let homes = 0;
  assert.equal(await resumeClaimWorkspace(state, () => { homes += 1; }), true);
  assert.equal(homes, 1); assert.equal(prompts[0]?.length, 1);
  assert.match(String(prompts[0]?.[0]), /신뢰/); assert.deepEqual(opened, []); assert.equal(state.value, undefined);
});

test("trust and exact folder are rechecked after the continuation prompt", async () => {
  for (const changed of [() => { trusted = false; }, () => { folders = [{ uri: uri("file:///other") }]; }]) {
    reset(); const state = new State(); pending(state); folders = [{ uri: uri() }]; action = "수령 코드 입력";
    duringPrompt = changed;
    await resumeClaimWorkspace(state, () => undefined);
    assert.deepEqual(opened, []);
  }
});

test("normal existing-workspace startup does not open a home or show another prompt", async () => {
  reset(); folders = [{ uri: uri() }]; const state = new State(); let homes = 0;
  assert.equal(await resumeClaimWorkspace(state, () => { homes += 1; }), false);
  assert.equal(homes, 0); assert.deepEqual(prompts, []); assert.deepEqual(opened, []);
});
