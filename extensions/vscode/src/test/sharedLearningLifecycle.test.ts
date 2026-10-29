import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { lstat, mkdir, mkdtemp, readFile, realpath, rm, writeFile } from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import test from "node:test";
import type { ExtensionContext } from "vscode";
import * as actualApi from "../api";
import * as actualDiscovery from "../serverDiscovery";
import { SHARED_AUTH_USED, SHARED_SESSION_MARKER } from "../sharedLearningSession";
import type { Assignment } from "../types";

interface Loader { _load(name: string, parent: {filename?: string}, main: boolean): unknown }
const loader = require("node:module") as Loader, originalLoad = loader._load;
const noOp = () => {}, disposable = () => ({dispose: noOp});
const commands = new Map<string, (...args: unknown[]) => unknown>();
const ui = {choice: undefined as string | undefined, quits: 0, exports: 0, signins: 0, claims: 0,
  choices: [] as (string | undefined)[],
  revoked: 0, warnings: [] as string[], errors: [] as string[], workspace: "", exportRoot: "",
  failRevoke: false, assignments: [] as Assignment[], inspected: "", serverOrigin: "https://test.invalid", homeForms: [] as unknown[],
  configuration: {} as Record<string, {defaultValue?: unknown; globalValue?: unknown; workspaceValue?: unknown}>,
  configurationReads: [] as string[], scannedProfiles: [] as actualDiscovery.TrustedDiscoveryServer[][]};
let homeInputs: {claim(code: string, expectedServer: string): Promise<void>; configureServer(address: string): Promise<void>};
class Tokens {
  public loggedIn = false; public generation = 0;
  public hasSession = async () => this.loggedIn;
  public getSessionGeneration = () => this.generation;
  public clear = async () => {this.loggedIn = false; this.generation++;};
}
let tokens: Tokens;
class Client {
  constructor(public transport: {getBaseUrl(): string}, public tokens: Tokens) { }
  public getBaseUrl = () => this.transport.getBaseUrl();
  public getAssignments = async () => ui.assignments;
  public revokeCurrentSession = async () => {ui.revoked++; if (ui.failRevoke) throw new Error("offline"); await this.tokens.clear();};
}
class View {
  public clear = noOp; public dispose = noOp; public reconcile = noOp; public show = async () => {}; public refresh = noOp;
  public cancel = noOp; public start = noOp; public select = noOp;
}
class Emitter {
  public event = () => disposable(); public fire = noOp; public dispose = noOp;
}
const uri = (fsPath: string) => ({scheme: "file", authority: "", fsPath, path: fsPath, toString: () => `file://${fsPath}`});
const vscode = {
  CancellationError: class extends Error {},
  env: {appName: "Autograde IDE", remoteName: undefined},
  Uri: {file: uri, joinPath: (base: {fsPath: string}, ...parts: string[]) => uri(path.join(base.fsPath, ...parts)), parse: uri},
  EventEmitter: Emitter, TreeItem: class {}, ThemeIcon: class {}, MarkdownString: class {appendText() {return this;} appendMarkdown() {return this;}},
  TreeItemCollapsibleState: {None: 0, Collapsed: 1}, StatusBarAlignment: {Left: 1}, ProgressLocation: {Notification: 1},
  commands: {registerCommand: (id: string, callback: (...args: unknown[]) => unknown) => {commands.set(id, callback); return disposable();},
    executeCommand: async (id: string, ...args: unknown[]) => {if (id === "workbench.action.quit") {ui.quits++; return;} return commands.get(id)?.(...args);}},
  workspace: {isTrusted: true, workspaceFile: undefined,
    get workspaceFolders() {return [{uri: uri(ui.workspace), name: "공용 실습", index: 0}];},
    getConfiguration: () => ({
      get: (name: string, fallback: unknown) => {
        const value = ui.configuration[name];
        return value ? value.workspaceValue ?? value.globalValue ?? value.defaultValue ?? fallback : typeof fallback === "string" ? "https://test.invalid" : fallback;
      },
      inspect: (name: string) => {ui.configurationReads.push(name); return {key: `autograde.${name}`, ...ui.configuration[name]};},
    }),
    onDidChangeTextDocument: disposable, onDidSaveTextDocument: disposable, onDidChangeWorkspaceFolders: disposable, onDidGrantWorkspaceTrust: disposable,
    onDidChangeConfiguration: disposable, registerTextDocumentContentProvider: disposable},
  languages: {createDiagnosticCollection: () => ({dispose: noOp, clear: noOp})},
  window: {activeTextEditor: undefined, tabGroups: {all: []},
    createTreeView: () => ({dispose: noOp, onDidChangeSelection: disposable}),
    createOutputChannel: () => ({dispose: noOp, clear: noOp, hide: noOp}),
    createStatusBarItem: () => ({dispose: noOp, show: noOp, hide: noOp}),
    showInformationMessage: async () => undefined,
    showWarningMessage: async (message: string) => {ui.warnings.push(message); return ui.choices.length ? ui.choices.shift() : ui.choice;},
    showErrorMessage: async (message: string) => {ui.errors.push(message); return undefined;},
    withProgress: async (_options: unknown, task: (progress: unknown, cancellation: unknown) => Promise<unknown>) =>
      task({report: noOp}, {isCancellationRequested: false, onCancellationRequested: disposable})},
};
const api = {...actualApi,
  HttpTransport: class {public getBaseUrl = () => ui.serverOrigin;},
  TokenManager: class extends Tokens {constructor() {super(); tokens = this;}}, AutogradeClient: Client,
};
const replacements: Record<string, unknown> = {
  "./api": api,
  "./serverDiscovery": {...actualDiscovery, ServerDiscoveryController: class extends actualDiscovery.ServerDiscoveryController {
    constructor(options: ConstructorParameters<typeof actualDiscovery.ServerDiscoveryController>[0]) {
      // Activation exercises real trust/configuration logic without sending multicast from the test runner.
      super({...options, scan: async profiles => {ui.scannedProfiles.push([...profiles]); return [];}});
    }
  }},
  "./auth": {AuthenticationController: class {
    constructor(private client: Client, _version: string, private changed: (value: boolean) => void) {}
    async signIn() {ui.signins++; this.client.tokens.loggedIn = true; this.client.tokens.generation++; this.changed(true);}
    async signOut() {await this.client.tokens.clear(); this.changed(false); return true;}
  }},
  "./assignmentClaim": {AssignmentClaimController: class {
    constructor(private client: Client, _version: string, private redeemed: (id?: string) => Promise<void>) {}
    async redeem() {ui.claims++; this.client.tokens.loggedIn = true; this.client.tokens.generation++; await this.redeemed(ui.assignments[0]?.id); return true;}
  }},
  "./git": {GitPreflightError: class extends Error {}, cloneRepository: async (_url: string, target: string) => {await mkdir(target);},
    inspectRepository: async (root: string) => {ui.inspected = root; throw new Error("synthetic inspection stop");}},
  "./connectionMonitor": {CHECK_SERVER_CONNECTION_COMMAND: "autograde.checkServerConnection", ServerConnectionMonitor: View},
  "./assignmentDocumentView": {ASSIGNMENT_DOCUMENT_SCHEME: "autograde-assignment", AssignmentDocumentView: View},
  "./submissionResultMonitor": {SubmissionResultMonitor: View},
  "./examModeView": {ExamModeView: View}, "./studentMaterialsView": {StudentMaterialsView: View},
  "./contentSyncView": {CONTENT_SYNC_COMMAND: "autograde.syncContent", ContentSyncView: View},
  "./learningHomeView": {LearningHomeView: class extends View {
    constructor(_state: unknown, callbacks: typeof homeInputs) {super(); homeInputs = callbacks;}
    override show = async (form?: unknown) => {ui.homeForms.push(form);};
  }},
  "./classroomActivity": {ClassroomActivityReporter: class extends View {getCommandContext() {return undefined;}}, classroomEnvironment: () => undefined},
  "./classroomCommands": {ClassroomCommands: View, CLASSROOM_COMMAND_LABELS: {}},
  "./learningExportView": {LEARNING_EXPORT_COMMAND: "autograde.exportLearning", LearningExportView: class extends View {
    constructor(_client: unknown, private options: {currentWorkspace(): {rootPath: string} | undefined}) {super();}
    override show = async () => {ui.exports++; ui.exportRoot = this.options.currentWorkspace()?.rootPath ?? "";};
  }},
};
loader._load = function(name, parent, main) {
  if (name === "vscode") return vscode;
  if (parent.filename?.endsWith(`${path.sep}extension.js`) && name in replacements) return replacements[name];
  return originalLoad.call(this, name, parent, main);
};
const extension = require("../extension") as typeof import("../extension");
loader._load = originalLoad;

async function fixture(used = false) {
  commands.clear(); Object.assign(ui, {choice: undefined, choices: [], quits: 0, exports: 0, signins: 0, claims: 0, revoked: 0, warnings: [], errors: [], failRevoke: false, assignments: [], inspected: "", serverOrigin: "https://test.invalid", homeForms: [],
    configuration: {"discovery.enabled": {defaultValue: false}}, configurationReads: [], scannedProfiles: []});
  const parent = await mkdtemp(path.join(await realpath(os.tmpdir()), "ag-lifecycle-test-"));
  const id = randomBytes(12).toString("hex"), root = path.join(parent, `e-${id}`), workspace = path.join(root, "workspace");
  await mkdir(root, {mode: 0o700}); await mkdir(workspace, {mode: 0o700}); await mkdir(path.join(root, "materials"), {mode: 0o700});
  const stat = await lstat(root);
  await writeFile(path.join(root, SHARED_SESSION_MARKER), JSON.stringify({schema: 1, sessionId: id, root, rootDev: stat.dev,
    rootIno: stat.ino, launcherPid: process.pid, phase: "running"}), {mode: 0o600});
  if (used) await writeFile(path.join(root, SHARED_AUTH_USED), JSON.stringify({schema: 1, sessionId: id, authenticated: true}), {mode: 0o600});
  const settings = {AUTOGRADE_LEARNING_LIFETIME: "shared", AUTOGRADE_SESSION_ROOT: root, AUTOGRADE_SESSION_ID: id,
    AUTOGRADE_LEARNER_WORKSPACE: workspace, AUTOGRADE_LEARNER_MATERIALS: path.join(root, "materials"), AUTOGRADE_DISCOVERY_PROFILE_PATH: ""};
  const old = Object.fromEntries(Object.keys(settings).map(key => [key, process.env[key]])); Object.assign(process.env, settings); ui.workspace = workspace;
  const context = {subscriptions: [] as {dispose(): void}[], secrets: {delete: async () => {}},
    globalState: {get: () => undefined, update: async () => {}}, extension: {packageJSON: {version: "test"}}, globalStorageUri: uri(path.join(root, "storage"))} as unknown as ExtensionContext;
  const dispose = async () => {for (const item of context.subscriptions) item.dispose(); for (const [key, value] of Object.entries(old)) {
    if (value === undefined) delete process.env[key]; else process.env[key] = value;
  } await rm(parent, {recursive: true, force: true});};
  try {await extension.activate(context); for (let i = 0; i < 20; i++) await Promise.resolve();}
  catch (error) {await dispose(); throw error;}
  return {root, workspace, dispose, command: async (id: string) => {await commands.get(id)?.(); for (let i = 0; i < 20; i++) await Promise.resolve();}};
}

test("actual activation keeps cancel and export-before-end separate from quitting or deleting local code", async () => {
  const f = await fixture(true);
  try {
    const code = path.join(f.workspace, "main.cpp"); await writeFile(code, "student code");
    await f.command("autograde.endLearningSession"); assert.equal(ui.quits, 0); assert.equal(ui.exports, 0);
    ui.choice = "먼저 내보내기"; await f.command("autograde.endLearningSession");
    assert.equal(ui.exports, 1); assert.equal(ui.exportRoot, f.workspace); assert.equal(ui.quits, 0);
    assert.equal(await readFile(code, "utf8"), "student code");
  } finally {await f.dispose();}
});

test("actual claim success locks reauthentication and account/address replacement for this shared session", async () => {
  const f = await fixture();
  try {
    await f.command("autograde.redeemAssignmentClaim"); assert.equal(ui.claims, 0);
    assert.ok(ui.homeForms.includes("claim"));
    await homeInputs.claim("AK1-2345-6789-ABCD", ui.serverOrigin); assert.equal(ui.claims, 1);
    assert.deepEqual(ui.errors, []);
    assert.equal(JSON.parse(await readFile(path.join(f.root, SHARED_AUTH_USED), "utf8")).authenticated, true);
    await f.command("autograde.redeemAssignmentClaim"); await f.command("autograde.signIn"); await f.command("autograde.configureServiceAddress");
    await assert.rejects(homeInputs.configureServer("https://other.invalid"), /새 IDE 세션/);
    assert.equal(ui.claims, 1); assert.equal(ui.signins, 0); assert.match(ui.errors.join(" "), /새 IDE 세션/);
  } finally {await f.dispose();}
});

test("actual reload blocks authentication but allows own code export and full IDE shutdown while offline", async () => {
  const f = await fixture(true);
  try {
    await f.command("autograde.redeemAssignmentClaim"); await f.command("autograde.signIn");
    assert.equal(ui.claims, 0); assert.equal(ui.signins, 0);
    await f.command("autograde.exportLearning"); assert.equal(ui.exportRoot, f.workspace);
    tokens.loggedIn = true; ui.failRevoke = true; ui.choice = "실습 종료";
    ui.choices = ["실습 종료", "미저장 변경 폐기 선택", "미저장 변경을 폐기하고 종료"];
    await f.command("autograde.signOut");
    assert.equal(ui.revoked, 1); assert.equal(tokens.loggedIn, false); assert.equal(ui.quits, 1);
    // The actual process supervisor, not extension deactivation, owns deletion.
    extension.deactivate(); assert.equal((await lstat(f.workspace)).isDirectory(), true);
  } finally {await f.dispose();}
});

test("shared Git claim and submit use the selected child repository, not the outer disposable workspace", async () => {
  const f = await fixture();
  try {
    ui.assignments = [{id: "git-1", title: "Git 과제", repository: {ready: true, name: "student-repo", cloneUrl: "https://github.com/example/student-repo.git"}}];
    await f.command("autograde.redeemAssignmentClaim");
    await homeInputs.claim("AK1-2345-6789-ABCD", ui.serverOrigin); assert.deepEqual(ui.errors, []);
    const repository = path.join(f.workspace, "student-repo"); assert.equal((await lstat(repository)).isDirectory(), true);
    await f.command("autograde.submitCurrentCommit");
    assert.equal(ui.inspected, repository); assert.match(ui.errors.join(" "), /synthetic inspection stop/);
  } finally {await f.dispose();}
});

test("inline claim rejects a stale rendered server before any authentication", async () => {
  const f = await fixture();
  try {
    await assert.rejects(homeInputs.claim("AK1-2345-6789-ABCD", "https://previous.invalid"), /서버나 로그인이 변경/);
    assert.equal(ui.claims, 0); assert.equal(tokens.loggedIn, false);
  } finally {await f.dispose();}
});

test("inline claim rechecks origin and identity after asynchronous workspace preparation", async () => {
  for (const change of ["origin", "identity"]) {
    const f = await fixture();
    try {
      const pending = homeInputs.claim("AK1-2345-6789-ABCD", ui.serverOrigin);
      if (change === "origin") ui.serverOrigin = "https://other.invalid";
      else tokens.generation++;
      await assert.rejects(pending, /서버나 로그인이 변경/);
      assert.equal(ui.claims, 0); assert.equal(tokens.loggedIn, false);
    } finally {await f.dispose();}
  }
});

test("a downloaded workspace cannot enable automatic discovery over a disabled machine setting", async () => {
  const f = await fixture();
  try {
    ui.configuration["discovery.enabled"] = {defaultValue: false, workspaceValue: true};
    assert.equal(vscode.workspace.getConfiguration().get("discovery.enabled", false), true);
    await f.command("autograde.discoverServers");
    assert.deepEqual(ui.scannedProfiles, []);
    assert.match(ui.errors.join(" "), /자동 검색을 비활성화/);
    assert.ok(ui.configurationReads.includes("discovery.enabled"));
  } finally {await f.dispose();}
});

test("discovery consumes installed/global institution trust and ignores a workspace's endpoint override", async () => {
  const f = await fixture();
  try {
    const trusted = {institutionId: "school", displayName: "학교", apiUrl: "https://api.example.test", portalUrl: "https://portal.example.test"};
    const untrusted = {...trusted, apiUrl: "https://evil.example.test", portalUrl: "https://evil-portal.example.test"};
    ui.configuration["discovery.enabled"] = {defaultValue: false, globalValue: true};
    ui.configuration["discovery.trustedServers"] = {defaultValue: [], globalValue: [trusted], workspaceValue: [untrusted]};
    await f.command("autograde.discoverServers");
    assert.deepEqual(ui.scannedProfiles, [[trusted]]);
    assert.ok(ui.configurationReads.includes("discovery.trustedServers"));
  } finally {await f.dispose();}
});
