import assert from "node:assert/strict";
import test from "node:test";

interface ModuleLoader { _load(request: string, parent: unknown, isMain: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
let folders: unknown[] | undefined;
let action: string | undefined;
let selection: unknown[] | undefined;
let prompts = 0;
let opened: unknown[][] = [];
let fails = false;
const stub = {
  workspace: { get workspaceFolders() { return folders; } },
  window: {
    showInformationMessage: async () => { prompts += 1; return action; },
    showOpenDialog: async () => selection,
  },
  commands: { executeCommand: async (...args: unknown[]) => { opened.push(args); if (fails) throw new Error("open failed"); } },
};
loader._load = function (name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const { prepareClaimWorkspace } = require("../claimWorkspace") as typeof import("../claimWorkspace");
loader._load = original;
function reset() { folders = undefined; action = undefined; selection = undefined; prompts = 0; opened = []; fails = false; }

test("an existing workspace can proceed to claim-code input without another folder prompt", async () => {
  reset(); folders = [{}];
  assert.equal(await prepareClaimWorkspace(), true);
  assert.equal(prompts, 0); assert.deepEqual(opened, []);
});
test("a blank window opens a selected folder but does not continue to consume a claim code", async () => {
  reset(); action = "수업 폴더 열기"; selection = [{ fsPath: "/course" }];
  assert.equal(await prepareClaimWorkspace(), false);
  assert.deepEqual(opened, [["vscode.openFolder", selection[0], false]]);
});
test("cancelling either preparation prompt or picker never proceeds to authentication", async () => {
  reset(); assert.equal(await prepareClaimWorkspace(), false); assert.deepEqual(opened, []);
  action = "수업 폴더 열기";
  assert.equal(await prepareClaimWorkspace(), false); assert.deepEqual(opened, []);
});
test("folder open failure preserves the unconsumed-code instruction", async () => {
  reset(); action = "수업 폴더 열기"; selection = [{}]; fails = true;
  await assert.rejects(prepareClaimWorkspace(), /코드는 아직 사용하지 않았습니다/);
});
