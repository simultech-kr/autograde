import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import test from "node:test";
import type * as vscode from "vscode";

interface ModuleLoader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
interface TestUri { fsPath: string; scheme?: string; authority?: string }
let folders: { uri: TestUri }[] = [];
let commands: string[] = [];
let notices: string[] = [];
let previews: string[] = [];
let previewUris: TestUri[] = [];
let warnings: string[] = [];
let failExplorer = false;
let failDocument = false;
let failPreview = false;
let onCommand: ((command: string, uri?: TestUri) => void | Promise<void>) | undefined;
let onOpenDocument: (() => void) | undefined;
function reset(): void {
  folders = []; commands = []; notices = []; previews = []; previewUris = []; warnings = [];
  failExplorer = false; failDocument = false; failPreview = false; onCommand = undefined; onOpenDocument = undefined;
}
const stub = {
  Uri: { joinPath: (uri: { fsPath: string }, child: string) => ({ ...uri, fsPath: path.join(uri.fsPath, child) }) },
  workspace: { get workspaceFolders() { return folders; }, openTextDocument: async (uri: TestUri) => {
    onOpenDocument?.();
    if (failDocument) throw new Error("private raw filesystem error");
    return { uri };
  } },
  commands: { executeCommand: async (command: string, uri?: TestUri) => {
    commands.push(command); await onCommand?.(command, uri);
    if (failExplorer && command === "revealInExplorer") throw new Error("explorer failed");
  } },
  window: { showInformationMessage: async (message: string) => { notices.push(message); },
    showWarningMessage: async (message: string) => { warnings.push(message); },
    showTextDocument: async (document: { uri: TestUri }) => {
      if (failPreview) throw new Error("private raw IDE error");
      previews.push(document.uri.fsPath); previewUris.push(document.uri);
    } },
};
loader._load = function(name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const { revealAssignmentFolder } = require("../assignmentFolderView") as typeof import("../assignmentFolderView");
loader._load = original;

test("Explorer open failure propagates for diagnostics while existing code remains untouched", async () => {
  reset();
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
  reset();
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

test("equal filesystem paths from another remote authority are never revealed in the current Explorer", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-view-"));
  try {
    await writeFile(path.join(root, "main.cpp"), "// edited");
    for (const workspaceUri of [
      { fsPath: root, scheme: "file", authority: "" },
      { fsPath: root, scheme: "vscode-remote", authority: "ssh-remote+other" },
    ]) {
      reset();
      folders = [{ uri: workspaceUri }];
      await revealAssignmentFolder({ fsPath: root, scheme: "vscode-remote", authority: "ssh-remote+course" } as vscode.Uri, root);
      assert.deepEqual(commands, ["workbench.view.explorer"]);
      assert.match(notices[0]!, /작업 영역은 바뀌지 않았습니다/);
      assert.equal(previewUris[0]?.scheme, "vscode-remote");
      assert.equal(previewUris[0]?.authority, "ssh-remote+course");
    }
    reset();
    const uri = { fsPath: root, scheme: "vscode-remote", authority: "wsl+Ubuntu" };
    folders = [{ uri }];
    await revealAssignmentFolder(uri as vscode.Uri, root);
    assert.deepEqual(commands, ["workbench.view.explorer", "revealInExplorer", "revealInExplorer"]);
    assert.equal(previewUris[0]?.authority, "wsl+Ubuntu");
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("deleted or mismatched target paths stop before changing the UI", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-view-"));
  try {
    for (const [uriPath, targetPath] of [[root, path.join(root, "missing")], [path.join(root, "missing"), path.join(root, "missing")]]) {
      reset();
      await assert.rejects(revealAssignmentFolder({ fsPath: uriPath! } as vscode.Uri, targetPath!), { code: "folder_unavailable" });
      assert.deepEqual(commands, []);
      assert.deepEqual(previews, []);
    }
    reset();
    await writeFile(path.join(root, "main.cpp"), "// edited");
    folders = [{ uri: { fsPath: root } }];
    onCommand = async command => { if (command === "workbench.view.explorer") await rm(root, { recursive: true }); };
    await assert.rejects(revealAssignmentFolder({ fsPath: root } as vscode.Uri, root), { code: "folder_unavailable" });
    assert.deepEqual(previews, []);
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("preview errors leave the selected answer intact and show fixed manual-open guidance", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-view-"));
  try {
    await writeFile(path.join(root, "main.cpp"), "// edited");
    for (const failure of ["document", "editor", "source-reveal"]) {
      reset();
      folders = [{ uri: { fsPath: root } }];
      failDocument = failure === "document";
      failPreview = failure === "editor";
      onCommand = (_command, uri) => { if (failure === "source-reveal" && uri?.fsPath.endsWith("main.cpp")) throw new Error("private raw reveal error"); };
      await revealAssignmentFolder({ fsPath: root } as vscode.Uri, root);
      assert.equal(warnings.length, 1);
      assert.match(warnings[0]!, /파일은 보존됩니다.*직접 여세요/);
      assert.doesNotMatch(warnings[0]!, /private raw/);
      assert.equal(await readFile(path.join(root, "main.cpp"), "utf8"), "// edited");
      assert.ok(!commands.includes("vscode.openFolder"));
    }
  } finally { await rm(root, { recursive: true, force: true }); }
});

test("stale sessions do not open or warn about a previous student's source after async UI boundaries", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-view-"));
  try {
    await writeFile(path.join(root, "main.cpp"), "// edited");
    for (const boundary of ["initial", "explorer", "folder", "source", "document", "document-error"]) {
      reset();
      let current = boundary !== "initial";
      folders = [{ uri: { fsPath: root } }];
      onCommand = (command, uri) => {
        if ((boundary === "explorer" && command === "workbench.view.explorer") ||
          (boundary === "folder" && uri?.fsPath === root) ||
          (boundary === "source" && uri?.fsPath.endsWith("main.cpp"))) current = false;
      };
      onOpenDocument = () => { if (boundary.startsWith("document")) current = false; };
      failDocument = boundary === "document-error";
      await revealAssignmentFolder({ fsPath: root } as vscode.Uri, root, () => current);
      assert.deepEqual(previews, [], boundary);
      assert.deepEqual(warnings, [], boundary);
      assert.deepEqual(notices, [], boundary);
      if (boundary === "initial") assert.deepEqual(commands, []);
    }
    assert.equal(await readFile(path.join(root, "main.cpp"), "utf8"), "// edited");
  } finally { await rm(root, { recursive: true, force: true }); }
});
