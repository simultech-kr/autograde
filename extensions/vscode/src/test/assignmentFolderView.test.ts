import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import test from "node:test";
import type * as vscode from "vscode";

interface ModuleLoader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
let folders: { uri: { fsPath: string } }[] = [];
let commands: string[] = [];
let notices: string[] = [];
let previews: string[] = [];
let failExplorer = false;
const stub = {
  Uri: { joinPath: (uri: { fsPath: string }, child: string) => ({ ...uri, fsPath: path.join(uri.fsPath, child) }) },
  workspace: { get workspaceFolders() { return folders; }, openTextDocument: async (uri: { fsPath: string }) => ({ uri }) },
  commands: { executeCommand: async (command: string) => { commands.push(command); if (failExplorer && command === "revealInExplorer") throw new Error("explorer failed"); } },
  window: { showInformationMessage: async (message: string) => { notices.push(message); }, showWarningMessage: async () => undefined,
    showTextDocument: async (document: { uri: { fsPath: string } }) => { previews.push(document.uri.fsPath); } },
};
loader._load = function(name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const { revealAssignmentFolder } = require("../assignmentFolderView") as typeof import("../assignmentFolderView");
loader._load = original;

test("Explorer open failure propagates for diagnostics while existing code remains untouched", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-view-"));
  try {
    await writeFile(path.join(root, "main.cpp"), "// edited");
    folders = [{ uri: { fsPath: root } }]; commands = []; notices = []; previews = []; failExplorer = true;
    await assert.rejects(revealAssignmentFolder({ fsPath: root } as vscode.Uri, root), /explorer failed/);
    assert.equal(await readFile(path.join(root, "main.cpp"), "utf8"), "// edited");
    assert.deepEqual(previews, []);
    assert.ok(!commands.includes("vscode.openFolder"));
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("outside-workspace attachment previews source and explicitly keeps Explorer/build in original workspace", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-view-"));
  try {
    await writeFile(path.join(root, "main.cpp"), "// edited");
    folders = [{ uri: { fsPath: path.join(root, "different-assignment") } }]; commands = []; notices = []; previews = []; failExplorer = true;
    await revealAssignmentFolder({ fsPath: root, scheme: "vscode-remote", authority: "wsl+Ubuntu" } as vscode.Uri, root);
    assert.deepEqual(previews, [path.join(root, "main.cpp")]);
    assert.match(notices[0]!, /탐색기와 빌드 작업 영역은 바뀌지 않았습니다/);
    assert.deepEqual(commands, ["workbench.view.explorer"]);
  } finally { await rm(root, { recursive: true, force: true }); }
});
