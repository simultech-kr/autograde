import assert from "node:assert/strict";
import test from "node:test";
import {mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile} from "node:fs/promises";
import * as path from "node:path";
import {tmpdir} from "node:os";
import type {AutogradeClient} from "../api";
import {createSubmissionBundle, readWorkspaceMarker, writeWorkspaceMarker} from "../bundle";
import type {LearningCheckpointScope} from "../learningCheckpoint";

interface Loader {_load(name: string, parent: unknown, main: boolean): unknown}
const loader = require("node:module") as Loader, original = loader._load;
interface Uri {scheme: string; authority: string; fsPath: string}
const uri = (fsPath: string): Uri => ({scheme: "file", authority: "", fsPath});
let trusted = true, parent = "", afterPick = () => {}, afterDialog = () => {};
const messages: string[] = [];
let documents: {isDirty: boolean; uri: Uri; save(): Promise<boolean>}[] = [];
const stub = {
  StatusBarAlignment: {Left: 1},
  Uri: {file: uri, joinPath: (base: Uri, ...parts: string[]) => ({...base, fsPath: path.join(base.fsPath, ...parts)})},
  workspace: {get isTrusted() {return trusted;}, get textDocuments() {return documents;}},
  window: {
    createStatusBarItem: () => ({show() {}, hide() {}, dispose() {}, text: "", tooltip: "", command: ""}),
    showInformationMessage: async (message: string) => {messages.push(message);},
    showWarningMessage: async (message: string) => {messages.push(message);},
    showQuickPick: async (items: unknown[]) => {afterPick(); return items[0];},
    showOpenDialog: async () => {afterDialog(); return [uri(parent)];},
  },
};
loader._load = function(name, parent, main) {return name === "vscode" ? stub : original.call(this, name, parent, main);};
const {LearningCheckpointView} = require("../learningCheckpointView") as typeof import("../learningCheckpointView");
loader._load = original;

async function fixture() {
  trusted = true; messages.length = 0; documents = []; afterPick = () => {}; afterDialog = () => {};
  const root = await mkdtemp(path.join(tmpdir(), "autograde-checkpoint-")), workspace = path.join(root, "workspace"), current = path.join(workspace, "assignment");
  await mkdir(current, {recursive: true}); await writeFile(path.join(current, "main.cpp"), "current");
  await writeWorkspaceMarker(current, {schemaVersion: 1, serviceBaseUrl: "https://school.test", assignmentId: "a1"});
  const stored = path.join(root, "stored"); await mkdir(stored); await writeFile(path.join(stored, "main.cpp"), "server code");
  const bundle = await createSubmissionBundle(stored);
  const checkpoint = {checkpoint_id: "cp_one", assignment_id: "a1", archive_sha256: bundle.sha256,
    created_at: "2026-10-26T10:00:00Z", file_count: 1, source_bytes: 11, version: 1};
  parent = workspace;
  const state = {scope: {baseUrl: "https://school.test", generation: 1, assignmentId: "a1", rootPath: current, scheme: "file", authority: ""} as LearningCheckpointScope | undefined,
    uploads: 0, reads: 0, connected: "", busy: false, afterRead: () => {}, failSave: false};
  const client = {saveLearningCheckpoint: async (_scope: LearningCheckpointScope, _bytes: Uint8Array, hash: string) => {
    state.uploads++; if (state.failSave) throw new Error("offline"); return {...checkpoint, archive_sha256: hash};
  }, getLearningCheckpoints: async () => [checkpoint], getLearningCheckpointBundle: async () => {state.reads++; state.afterRead(); return bundle.archive;}} as unknown as AutogradeClient;
  const view = new LearningCheckpointView(client, {
    currentScope: () => state.scope, isBusy: () => state.busy,
    connect: async (folder, _scope, isCurrent) => {
      assert.equal(isCurrent(), true); state.connected = folder.fsPath;
      state.scope = {...state.scope!, rootPath: folder.fsPath};
      assert.equal(isCurrent(), true, "Intentional folder switch must permit IDE reveal");
    }, onStatus: () => {},
  });
  return {root, workspace, current, state, view, close: async () => {view.dispose(); await rm(root, {recursive: true, force: true});}};
}

test("autosave writes only matching assignment dirty documents and refuses failed saves", async () => {
  const f = await fixture();
  try {
    let otherSaves = 0;
    const own = {uri: uri(path.join(f.current, "main.cpp")), isDirty: true, async save() {await writeFile(this.uri.fsPath, "edited"); this.isDirty = false; return true;}};
    documents = [own, {uri: uri(path.join(f.root, "unrelated.cpp")), isDirty: true, async save() {otherSaves++; return true;}},
      {uri: {...uri(path.join(f.current, "remote.cpp")), scheme: "vscode-remote", authority: "different"}, isDirty: true, async save() {otherSaves++; return true;}}];
    assert.equal(await f.view.saveNow(false), true); assert.equal(f.state.uploads, 1); assert.equal(otherSaves, 0);
    assert.equal(await readFile(own.uri.fsPath, "utf8"), "edited");
    own.isDirty = true; own.save = async () => false;
    assert.equal(await f.view.saveNow(false), false); assert.equal(f.state.uploads, 1);
  } finally {await f.close();}
});

test("linked dirty file cannot autosave outside assignment and untrusted work never uploads", async () => {
  const f = await fixture();
  try {
    const external = path.join(f.root, "outside.cpp"), linked = path.join(f.current, "linked.cpp");
    await writeFile(external, "private"); await symlink(external, linked); let saves = 0;
    documents = [{uri: uri(linked), isDirty: true, async save() {saves++; return true;}}];
    assert.equal(await f.view.saveNow(false), false); assert.equal(saves, 0); assert.equal(f.state.uploads, 0);
    trusted = false; assert.equal(await f.view.saveNow(false), false); assert.equal(f.state.uploads, 0);
    assert.equal(await readFile(external, "utf8"), "private");
  } finally {await f.close();}
});

test("restore creates and opens a new managed folder, preserving local edits and assignment marker", async () => {
  const f = await fixture();
  try {
    await f.view.restore(); assert.notEqual(f.state.connected, f.current);
    assert.equal(await readFile(path.join(f.current, "main.cpp"), "utf8"), "current");
    assert.equal(await readFile(path.join(f.state.connected, "main.cpp"), "utf8"), "server code");
    assert.equal((await readWorkspaceMarker(f.state.connected))?.assignmentId, "a1");
    assert.match(messages.join(" "), /새 과제 폴더/);
  } finally {await f.close();}
});

test("restoring refuses a folder within current code and cancels after auth changes during download", async () => {
  const f = await fixture();
  try {
    parent = f.current; await assert.rejects(f.view.restore(), /과제 폴더 밖/); assert.equal(f.state.reads, 0);
    parent = f.workspace; f.state.afterRead = () => {f.state.scope = {...f.state.scope!, generation: 2};};
    await assert.rejects(f.view.restore(), /인증이 변경/);
    assert.deepEqual(await readdir(f.workspace), ["assignment"]); assert.equal(f.state.connected, "");
  } finally {await f.close();}
});

test("restore stops if trust or assignment changes while choosing a snapshot", async () => {
  const f = await fixture();
  try {
    afterPick = () => {trusted = false;}; await assert.rejects(f.view.restore(), /인증이 변경/); assert.equal(f.state.reads, 0);
    trusted = true; afterPick = () => {f.state.scope = {...f.state.scope!, assignmentId: "other"};};
    await assert.rejects(f.view.restore(), /인증이 변경/); assert.equal(f.state.reads, 0);
  } finally {await f.close();}
});

test("bounded exit save fails honestly when offline; busy assignment operations do not save", async () => {
  const f = await fixture();
  try {
    f.state.busy = true; assert.equal(await f.view.saveNow(false), false); assert.equal(f.state.uploads, 0);
    f.state.busy = false; f.state.failSave = true; assert.equal(await f.view.flushBeforeExit(50), false);
    assert.equal(f.view.autosaver.getStatus().savedAt, undefined); assert.equal(f.state.uploads, 1);
  } finally {await f.close();}
});

test("execution receives the exact acknowledged capture and refuses stale scopes or failed document saves", async()=>{
  const f=await fixture();
  try {
    const own={uri:uri(path.join(f.current,"main.cpp")),isDirty:true,async save(){await writeFile(this.uri.fsPath,"new exact code");this.isDirty=false;return true;}};
    documents=[own];
    const saved=await f.view.saveForExecution(f.state.scope!);
    assert.deepEqual(saved.filePaths,["main.cpp"]);
    assert.equal(saved.checkpoint.archive_sha256,(await createSubmissionBundle(f.current)).sha256);
    assert.equal(f.state.uploads,1);
    own.isDirty=true;own.save=async()=>false;
    await assert.rejects(f.view.saveForExecution(f.state.scope!),/현재 코드의 서버 저장/);
    assert.equal(f.state.uploads,1);
    const old=f.state.scope!;f.state.scope={...old,generation:old.generation+1};
    await assert.rejects(f.view.saveForExecution(old),/과제가 변경/);
  } finally {await f.close();}
});
