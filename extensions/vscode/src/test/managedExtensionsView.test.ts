import assert from "node:assert/strict";
import test, { type TestContext } from "node:test";
import { mkdtemp, mkdir, writeFile, readFile, readdir, realpath, rm, rename, symlink, lstat } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import type * as vscode from "vscode";
import { AssignmentOperationGate } from "../assignmentOperation";
import { SharedLearningSession, SHARED_SESSION_MARKER } from "../sharedLearningSession";
import { ExtensionInstallError, type ExtensionRelease } from "../managedExtensions";
import type { LearningEnvironment } from "../learningEnvironment";

interface Loader {_load(name: string, parent: unknown, main: boolean): unknown}
const loader = require("node:module") as Loader, original = loader._load;
class FileUri {readonly scheme = "file"; constructor(public readonly fsPath: string) {}}
const state = {trusted: true, remote: undefined as string | undefined, picked: true,
  extensions: new Map<string, {extensionPath: string; packageJSON: {version: string}}>(),
  calls: [] as {name: string; argument: unknown}[], checkedToken: false,
  execute: async (_name: string, _argument: unknown) => {},
};
const stub = {
  version: "1.143.0", Uri: {file: (file: string) => new FileUri(file)},
  env: {get remoteName() {return state.remote;}}, workspace: {get isTrusted() {return state.trusted;}},
  extensions: {getExtension: (id: string) => state.extensions.get(id)},
  CancellationTokenSource: class {token = {isCancellationRequested: false}; cancel() {this.token.isCancellationRequested = true;} dispose() {}},
  commands: {executeCommand: async (name: string, argument: unknown) => {state.calls.push({name, argument}); await state.execute(name, argument);}},
  window: {
    showQuickPick: async (items: {approved: boolean}[], _options: unknown, token: {isCancellationRequested: boolean}) => {
      state.checkedToken = token.isCancellationRequested; return token.isCancellationRequested ? undefined : items.find(item => item.approved === state.picked);
    },
    showInformationMessage: async () => {},
  },
};
loader._load = function(name, parent, main) {return name === "vscode" ? stub : original.call(this, name, parent, main);};
const {ManagedExtensionsInstaller} = require("../managedExtensionsView") as typeof import("../managedExtensionsView");
loader._load = original;
const release: ExtensionRelease = {release_id: `er_${"a".repeat(32)}`, extension_id: "teacher.cpp-lab", version: "1.0.0",
  filename: "cpp-lab.vsix", target_platform: "universal", vscode_engine: "^1.92.0", sha256: "a".repeat(64), size_bytes: 4};
const bytes = Buffer.from("VSIX");

async function fixture(t: TestContext) {
  state.trusted = true; state.remote = undefined; state.extensions.clear(); state.calls.length = 0;
  state.picked = true; state.checkedToken = false; state.execute = async () => {};
  const temporary = await mkdtemp(path.join(tmpdir(), "autograde-managed-extension-"));
  const base = await realpath(temporary), id = "a".repeat(24), root = path.join(base, `e-${id}`);
  await mkdir(root, {mode: 0o700});
  for (const name of ["workspace", "materials", "r", "r/extensions"]) await mkdir(path.join(root, name), {mode: 0o700});
  const info = await lstat(root);
  await writeFile(path.join(root, SHARED_SESSION_MARKER), JSON.stringify({schema: 1, sessionId: id, root,
    rootDev: info.dev, rootIno: info.ino, launcherPid: process.pid, phase: "running"}), {mode: 0o600});
  const session = (await SharedLearningSession.fromEnvironment("Autograde IDE", {
    AUTOGRADE_LEARNING_LIFETIME: "shared", AUTOGRADE_SESSION_ROOT: root, AUTOGRADE_SESSION_ID: id,
    AUTOGRADE_LEARNER_WORKSPACE: path.join(root, "workspace"), AUTOGRADE_LEARNER_MATERIALS: path.join(root, "materials"),
  }))!;
  const own = {id: "autograde.autograde-vscode", packageJSON: {version: "0.5.13"}, extensionPath: "/app/extensions/autograde"} as vscode.Extension<unknown>;
  const operations = new AssignmentOperationGate();
  const environment: LearningEnvironment = {policy: "standard"};
  const make = (env = environment, selected: SharedLearningSession | undefined = session) => new ManagedExtensionsInstaller(selected, env, operations, own);
  t.after(() => rm(temporary, {recursive: true, force: true}));
  return {base, root, session, operations, make, installer: make()};
}

test("adapter installs a local URI from disposable staging and removes the VSIX after success", async t => {
  const f = await fixture(t); let staged = "";
  state.execute = async (name, argument) => {
    assert.equal(name, "workbench.extensions.installExtension"); assert.ok(argument instanceof FileUri);
    staged = argument.fsPath; assert.equal(argument.scheme, "file");
    assert.ok(staged.startsWith(path.join(f.root, "r", "extension-install-")));
    assert.deepEqual(await readFile(staged), bytes);
    assert.equal((await lstat(staged)).mode & 0o777, 0o600);
  };
  await f.installer.install(release, bytes, () => {});
  assert.equal(state.calls.length, 1); await assert.rejects(lstat(staged), {code: "ENOENT"});
  assert.deepEqual(await readdir(path.join(f.root, "r")), ["extensions"]);
});

test("failed built-in installer command removes staged bytes without deleting student code", async t => {
  const f = await fixture(t); await writeFile(path.join(f.session.workspace, "answer.cpp"), "student answer");
  state.execute = async () => {throw new Error("installer rejected VSIX");};
  await assert.rejects(f.installer.install(release, bytes, () => {}), /installer rejected/);
  assert.deepEqual(await readdir(path.join(f.root, "r")), ["extensions"]);
  assert.equal(await readFile(path.join(f.session.workspace, "answer.cpp"), "utf8"), "student answer");
});

test("guard cancellation after staging prevents the command and cleans temporary bytes", async t => {
  const f = await fixture(t); let guards = 0;
  await assert.rejects(f.installer.install(release, bytes, () => {if (++guards === 3) throw new ExtensionInstallError("scope_changed");}), {code: "scope_changed"});
  assert.equal(state.calls.length, 0); assert.deepEqual(await readdir(path.join(f.root, "r")), ["extensions"]);
});

test("shared session, trust and execution-policy restrictions block installation", async t => {
  const f = await fixture(t);
  assert.equal(f.make({policy: "unmanaged"}).restriction(), "unsupported_client");
  assert.equal(f.make({policy: "restricted"}).restriction(), "restricted_policy");
  state.trusted = false;
  await assert.rejects(f.installer.install(release, bytes, () => {}), {code: "untrusted_workspace"});
  state.trusted = true; state.remote = "wsl";
  await assert.rejects(f.installer.install(release, bytes, () => {}), {code: "unsupported_client"});
  // Native WSLg has no VS Code Remote host name and follows the Linux process target.
  state.remote = undefined; assert.equal(f.installer.restriction(), undefined);
  assert.equal(state.calls.length, 0);
});

test("replaced launcher-owned root fails closed instead of staging into a replacement directory", async t => {
  const f = await fixture(t);
  await rename(f.root, path.join(f.base, "previous-session"));
  await mkdir(f.root, {mode: 0o700});
  await assert.rejects(f.installer.install(release, bytes, () => {}));
  assert.equal(state.calls.length, 0);
});

test("runtime symlink never stages a VSIX outside the session", async t => {
  const f = await fixture(t), outside = path.join(f.base, "outside");
  await mkdir(outside); await rm(path.join(f.root, "r"), {recursive: true});
  await symlink(outside, path.join(f.root, "r"));
  await assert.rejects(f.installer.install(release, bytes, () => {}), {code: "package_invalid"});
  assert.deepEqual(await readdir(outside), []); assert.equal(state.calls.length, 0);
});

test("built-in/outside paths are protected while disposable user extensions are eligible", async t => {
  const f = await fixture(t);
  assert.equal(f.installer.isBuiltin("autograde.autograde-vscode"), true);
  assert.equal(f.installer.isBuiltin("autograde.autograde-ide-profile"), true);
  state.extensions.set("teacher.cpp-lab", {extensionPath: path.join(f.root, "r/extensions/teacher.cpp-lab-1.0.0"), packageJSON: {version: "1.0.0"}});
  assert.equal(f.installer.isBuiltin("teacher.cpp-lab"), false);
  assert.equal(f.installer.installedVersion("teacher.cpp-lab"), "1.0.0");
  state.extensions.set("teacher.cpp-lab", {extensionPath: path.join(f.root, "r/extensions-other/plugin"), packageJSON: {version: "1.0.0"}});
  assert.equal(f.installer.isBuiltin("teacher.cpp-lab"), true);
  assert.equal(f.installer.installedVersion("autograde.autograde-vscode"), "0.5.13");
});

test("cancelled confirmation is not an install approval", async t => {
  const f = await fixture(t), controller = new AbortController(); controller.abort();
  assert.equal(await f.installer.confirm(release, controller.signal), false); assert.equal(state.checkedToken, true);
  assert.equal(state.calls.length, 0);
});
