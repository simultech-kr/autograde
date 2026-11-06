import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readFile, readdir, rm, writeFile } from "node:fs/promises";
import * as path from "node:path";
import { tmpdir } from "node:os";
import { AutogradeClient } from "../api";
import { createSubmissionBundle } from "../bundle";
import { contentCanonicalJson, contentSha256, type ContentManifest, type ContentContext } from "../contentSync";
import type { StudentMaterial } from "../studentMaterials";
import { EXPORT_PRIVACY_TITLE, EXPORT_PRIVACY_CONTINUE } from "../exportPrivacy";

interface Loader { _load(name: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as Loader, original = loader._load;
interface Pick {category?: string; format?: string}
const messages: string[] = [];
const pickerHints: string[] = [];
const privacyNotices: {modal?: boolean; detail?: string}[] = [];
let privacyDecision: string | undefined = EXPORT_PRIVACY_CONTINUE, afterPrivacy = () => {};
let trusted = true, directory = "", format = "folder", selectedCategories = ["current"], picks = 0, dialogCount = 0;
let afterPick = () => {}, afterDirectory = () => {}, decision: string | undefined = "저장하고 내보내기";
let documents: {isDirty: boolean; uri: {scheme: string; fsPath: string; authority?: string}; save(): Promise<boolean>}[] = [];
let remoteName: string | undefined, directoryScheme = "file", directoryAuthority = "";
let folders: {uri: {scheme: string; fsPath: string; authority?: string}}[] = [];
const stub = {
  CancellationTokenSource: class { public token = {isCancellationRequested: false}; public cancel() {this.token.isCancellationRequested = true;} public dispose() {} },
  env: {get remoteName() {return remoteName;}},
  workspace: {get isTrusted() {return trusted;}, get workspaceFolders() {return folders;}, get textDocuments() {return documents;}},
  ProgressLocation: {Notification: 1},
  window: {
    showInformationMessage: async (message: string) => {messages.push(message);},
    showWarningMessage: async (message: string, options?: {modal?: boolean; detail?: string}) => {
      if (message === EXPORT_PRIVACY_TITLE) {privacyNotices.push(options ?? {}); afterPrivacy(); return privacyDecision;}
      messages.push(message); return options?.modal ? decision : undefined;
    },
    showQuickPick: async (items: Pick[], options: {canPickMany: boolean; placeHolder?: string}) => {
      if (options.placeHolder) pickerHints.push(options.placeHolder);
      picks++; afterPick();
      if (!options.canPickMany) return items.find(item => item.format === format);
      if (items.some(item => item.category)) return items.filter(item => selectedCategories.includes(item.category ?? ""));
      return items;
    },
    showOpenDialog: async () => {dialogCount++; afterDirectory(); return [{scheme: directoryScheme, authority: directoryAuthority, fsPath: directory}];},
    withProgress: async (_options: unknown, task: (progress: unknown, cancellation: unknown) => Promise<unknown>) => task({report() {}},
      {isCancellationRequested: false, onCancellationRequested: () => ({dispose() {}})}),
  },
};
loader._load = function(name, parent, main) {return name === "vscode" ? stub : original.call(this, name, parent, main);};
const {LearningExportView} = require("../learningExportView") as typeof import("../learningExportView");
loader._load = original;

const context: ContentContext = {schema: "autograde.content.context.v1", course_key: "come2201", subject_scope: "a".repeat(64),
  limits: {max_file_bytes: 26214400, max_release_bytes: 104857600, max_files: 1000, max_manifest_bytes: 524288}};
const notes = Buffer.from("course notes"), pdf = Buffer.from("%PDF-1.7\nfeedback\n%%EOF");
const raw = {schema: "autograde.content.manifest.v1", course_key: context.course_key, subject_scope: context.subject_scope,
  collection_id: `cc_${"1".repeat(32)}`, release_id: `cr_${"2".repeat(32)}`, generation: 1, policy_revision: 1,
  title: "강의자료", change_note: "업데이트", files: [{path: "notes.md", sha256: contentSha256(notes), size_bytes: notes.length, media_type: "text/markdown"}]};
const digest = contentSha256(contentCanonicalJson(raw)), manifest = {...raw, manifest_id: `cm_${digest}`, manifest_sha256: digest} as ContentManifest;
const material: StudentMaterial = {id: "mat_test", courseKey: "come2201", title: "개인 피드백", instructions: "",
  format: "pdf", sizeBytes: pdf.length, sha256: contentSha256(pdf), fileRevision: 1, edition: 1,
  availability: "open", opensAt: "2026-01-01T00:00:00Z", closesAt: "2027-01-01T00:00:00Z", updated: false};

async function fixture(cleanupMode?: "on_exit" | "next_start") {
  messages.length = 0; trusted = true; selectedCategories = ["current"]; format = "folder"; picks = 0; dialogCount = 0;
  pickerHints.length = 0;
  afterPick = () => {}; afterDirectory = () => {}; documents = []; decision = "저장하고 내보내기";
  privacyNotices.length = 0; privacyDecision = EXPORT_PRIVACY_CONTINUE; afterPrivacy = () => {};
  remoteName = undefined; directoryScheme = "file"; directoryAuthority = ""; folders = [];
  const root = await mkdtemp(path.join(tmpdir(), "autograde-export-view-")), session = path.join(root, "session"), currentCode = path.join(session, "workspace"), cache = path.join(session, "materials"), output = path.join(root, "personal");
  await mkdir(currentCode, {recursive: true}); await mkdir(output); directory = output;
  await writeFile(path.join(currentCode, "main.cpp"), "current code");
  const submitted = path.join(root, "submitted"); await mkdir(submitted); await writeFile(path.join(submitted, "main.cpp"), "previous submitted code");
  const bundle = await createSubmissionBundle(submitted);
  const assignment = {id: "assignment-1", title: "실습", deliveryMode: "bundle"};
  const version = {id: "bsub_previous", assignmentId: assignment.id, state: "published", receivedAt: "2026-10-01T00:00:00Z", sourceDigest: bundle.sha256, sourceSize: bundle.archive.length};
  const state = {base: "https://test.invalid", generation: 1, loggedIn: true, authFailure: false, workspace: currentCode as string | undefined, reads: 0, histories: 0, verifies: 0, unsupported: false, afterRead: () => {}, cacheValid: true, workspaceValid: true, cacheChecks: 0, workspaceChecks: 0};
  const client = {
    getBaseUrl: () => state.base, tokens: {getSessionGeneration: () => state.generation, hasSession: async () => {if (state.authFailure) throw new Error("offline"); return state.loggedIn;}},
    getSubmissionHistory: async () => {state.histories++; return {submissions: [version], hasMore: false};},
    getSubmissionSource: async () => {state.reads++; state.afterRead(); return bundle.archive;},
    getStudentMaterials: async () => ({courseKey: "come2201", materials: [material]}),
    getStudentMaterialFile: async () => {state.reads++; state.afterRead(); return pdf;},
  } as unknown as AutogradeClient;
  const view = new LearningExportView(client, {currentWorkspace: () => state.workspace ? {rootPath: state.workspace, title: "실습 코드"} : undefined,
    assignments: () => [assignment], cacheRoot: () => cache,
    validateCacheRoot: async () => {state.cacheChecks++; if (!state.cacheValid) throw new Error("student cache changed");},
    validateWorkspace: async () => {state.workspaceChecks++; if (!state.workspaceValid) throw new Error("student workspace changed");},
    excludedDestinations: () => [session], cleanupMode});
  Object.assign((view as unknown as {content: object}).content, {
    context: async () => context,
    catalog: async () => ({...context, collections: state.unsupported ? [] : [manifest]}),
    manifest: async () => manifest,
    object: async () => {state.reads++; state.afterRead(); return notes;},
    verify: async () => {state.verifies++;},
  });
  return {root, session, currentCode, cache, output, state, view, close: async () => {view.dispose(); await rm(root, {recursive: true, force: true});}};
}

test("offline current-code export requires no server login and never submits or switches workspace", async () => {
  const f = await fixture();
  try {
    f.state.loggedIn = false; await f.view.show();
    const outputs = await readdir(f.output); assert.equal(outputs.length, 1);
    assert.equal(await readFile(path.join(f.output, outputs[0]!, "current-code/main.cpp"), "utf8"), "current code");
    assert.equal(f.state.reads, 0); assert.equal(f.state.histories, 0); assert.equal(f.state.workspace, f.currentCode);
    assert.match(messages.join(" "), /내보내기 완료/); assert.match(messages.join(" "), /공식 제출은 별도/);
    assert.equal(privacyNotices.length, 1); assert.equal(privacyNotices[0]!.modal, true);
    assert.match(privacyNotices[0]!.detail!, /학번.*이름.*점수/);
    assert.match(privacyNotices[0]!.detail!, /삭제되지 않으며.*다른 사람이/);
  } finally {await f.close();}
});

test("shared export explains the actual cleanup time and keeps independent output", async () => {
  const f = await fixture("next_start");
  try {
    await f.view.show();
    assert.match(pickerHints.join(" "), /코드·자료는 다음 IDE 실행 시 정리/);
    assert.match(pickerHints.join(" "), /내보낸 파일은 자동 정리 후에도 보존/);
    assert.doesNotMatch(pickerHints.join(" "), /완전히 종료하면.*코드·자료/);
    assert.equal((await readdir(f.output)).length, 1);
  } finally {await f.close();}
});

test("export privacy cancellation or an unrelated answer creates no copies or downloads", async () => {
  for (const choice of [undefined, "취소", "저장하고 내보내기", "yes"]) {
    const f = await fixture(); let saves = 0;
    try {
      privacyDecision = choice; selectedCategories = ["current", "submissions", "content", "personal"];
      documents = [{isDirty: true, uri: {scheme: "file", fsPath: path.join(f.currentCode, "main.cpp")}, save: async () => {saves++; return true;}}];
      await f.view.show();
      assert.equal(privacyNotices.length, 1); assert.deepEqual(await readdir(f.output), []);
      assert.equal(f.state.reads, 0); assert.equal(saves, 0);
      assert.equal((await readdir(f.session)).includes("materials"), false);
    } finally {await f.close();}
  }
});

test("privacy confirmation is not remembered and cannot authorize a changed student", async () => {
  const f = await fixture();
  try {
    await f.view.show(); assert.equal((await readdir(f.output)).length, 1);
    privacyDecision = undefined; await f.view.show();
    assert.equal(privacyNotices.length, 2); assert.equal((await readdir(f.output)).length, 1);
    privacyDecision = EXPORT_PRIVACY_CONTINUE; afterPrivacy = () => {f.state.generation++;};
    await f.view.show(); assert.equal((await readdir(f.output)).length, 1);
  } finally {await f.close();}
});

test("one export combines current code, prior submission, Git materials and personal PDF without linking restored code", async () => {
  const f = await fixture();
  try {
    selectedCategories = ["current", "submissions", "content", "personal"]; await f.view.show();
    const outputs = await readdir(f.output); assert.equal(outputs.length, 1, messages.join(" "));
    const exported = path.join(f.output, outputs[0]!);
    assert.equal(await readFile(path.join(exported, "current-code/main.cpp"), "utf8"), "current code");
    assert.equal(await readFile(path.join(exported, "submissions/assignment-1/bsub_previous/main.cpp"), "utf8"), "previous submitted code");
    assert.equal(await readFile(path.join(exported, `course-materials/${manifest.collection_id}-v1/notes.md`), "utf8"), "course notes");
    assert.deepEqual(await readFile(path.join(exported, "personal-materials/mat_test/come2201-mat_test-v1.pdf")), pdf);
    assert.equal(f.state.workspace, f.currentCode); assert.equal(f.state.reads, 3); assert.ok(f.state.verifies >= 3);
    assert.ok(!(await readdir(f.cache)).some(name => name.startsWith("export-stage-")));
    const info = JSON.parse(await readFile(path.join(exported, "EXPORT-INFO.json"), "utf8")); assert.equal(info.groups.length, 4);
  } finally {await f.close();}
});

test("student can choose ZIP and selected categories only", async () => {
  const f = await fixture();
  try {selectedCategories = ["personal"]; format = "zip"; await f.view.show();
    const outputs = await readdir(f.output); assert.equal(outputs.length, 1); assert.match(outputs[0]!, /\.zip$/);
    assert.equal((await readFile(path.join(f.output, outputs[0]!))).readUInt32LE(0), 0x04034b50); assert.equal(f.state.histories, 0);
  } finally {await f.close();}
});

test("dirty code is saved only within chosen folder with explicit confirmation", async () => {
  const f = await fixture(); let saved = 0, unrelated = 0;
  try {
    documents = [
      {isDirty: true, uri: {scheme: "file", fsPath: path.join(f.currentCode, "main.cpp")}, save: async () => {saved++; await writeFile(path.join(f.currentCode, "main.cpp"), "saved edit"); documents[0]!.isDirty = false; return true;}},
      {isDirty: true, uri: {scheme: "file", fsPath: path.join(f.root, "another.cpp")}, save: async () => {unrelated++; return true;}},
    ];
    await f.view.show(); assert.equal(saved, 1); assert.equal(unrelated, 0);
    const name = (await readdir(f.output))[0]!; assert.equal(await readFile(path.join(f.output, name, "current-code/main.cpp"), "utf8"), "saved edit");
  } finally {await f.close();}
});

test("failed save or cancelled dirty prompt leaves no export", async () => {
  for (const reason of ["failed", "cancelled"]) {
    const f = await fixture();
    try {
      if (reason === "cancelled") decision = undefined;
      documents = [{isDirty: true, uri: {scheme: "file", fsPath: path.join(f.currentCode, "main.cpp")}, save: async () => false}];
      await f.view.show(); assert.deepEqual(await readdir(f.output), []);
    } finally {await f.close();}
  }
});

test("identity, server, workspace change and explicit clear cancel before export", async () => {
  for (const change of ["identity", "server", "workspace", "clear"]) {
    const f = await fixture();
    try {
      afterPick = () => {if (change === "identity") f.state.generation++; else if (change === "server") f.state.base = "https://other.invalid"; else if (change === "workspace") f.state.workspace = undefined; else f.view.clear();};
      await f.view.show(); assert.deepEqual(await readdir(f.output), []); assert.equal(f.state.reads, 0); assert.deepEqual(messages, []);
    } finally {await f.close();}
  }
});

test("managed session destination and untrusted workspace are rejected without writes", async () => {
  for (const cause of ["session", "trust"]) {
    const f = await fixture();
    try {if (cause === "session") directory = f.session; else trusted = false;
      await f.view.show(); assert.deepEqual(await readdir(f.output), []); assert.match(messages.join(" "), cause === "session" ? /학습 공간 밖/ : /신뢰/);
    } finally {await f.close();}
  }
});

test("failed remote selection never silently exports only part of the requested selection", async () => {
  const f = await fixture();
  try {selectedCategories = ["current", "content"]; f.state.unsupported = true; await f.view.show();
    assert.equal(dialogCount, 0); assert.deepEqual(await readdir(f.output), []); assert.match(messages.join(" "), /공개된 강의자료가 없습니다/);
  } finally {await f.close();}
});

test("account change during remote download leaves no export and cleans private staging", async () => {
  const f = await fixture();
  try {selectedCategories = ["submissions"]; f.state.afterRead = () => {f.state.generation++;}; await f.view.show();
    assert.deepEqual(await readdir(f.output), []); assert.deepEqual(await readdir(f.cache), []); assert.deepEqual(messages, []);
  } finally {await f.close();}
});

test("authentication lookup failure still permits current code export without a server read", async () => {
  const f = await fixture();
  try {f.state.authFailure = true; await f.view.show(); assert.equal((await readdir(f.output)).length, 1); assert.equal(f.state.reads, 0);}
  finally {await f.close();}
});

test("untitled edits require explicit exclusion and are never silently reported as exported", async () => {
  for (const allow of [false, true]) {
    const f = await fixture();
    try {
      documents = [{isDirty: true, uri: {scheme: "untitled", fsPath: "Untitled-1"}, save: async () => {throw new Error("must not save arbitrary buffer");}}];
      decision = allow ? "이름 없는 문서 제외하고 계속" : undefined; await f.view.show();
      const names = await readdir(f.output); assert.equal(names.length, allow ? 1 : 0); assert.match(messages.join(" "), /이름 없는 문서 1개/);
      if (allow) {const info = JSON.parse(await readFile(path.join(f.output, names[0]!, "EXPORT-INFO.json"), "utf8")); assert.equal(info.groups[0].excluded_entries, 1);}
    } finally {await f.close();}
  }
});

test("remote-host documents are saved within current authority; other hosts cannot be export destinations", async () => {
  for (const sameHost of [true, false]) {
    const f = await fixture(); let saved = 0;
    try {
      remoteName = "wsl"; directoryScheme = "vscode-remote"; directoryAuthority = sameHost ? "wsl+Ubuntu" : "ssh-remote+other";
      folders = [{uri: {scheme: "vscode-remote", authority: "wsl+Ubuntu", fsPath: f.currentCode}}];
      documents = [{isDirty: true, uri: {scheme: "vscode-remote", authority: "wsl+Ubuntu", fsPath: path.join(f.currentCode, "main.cpp")},
        save: async () => {saved++; await writeFile(path.join(f.currentCode, "main.cpp"), "WSL edit"); documents[0]!.isDirty = false; return true;}}];
      await f.view.show(); assert.equal(saved, sameHost ? 1 : 0); assert.equal((await readdir(f.output)).length, sameHost ? 1 : 0);
      if (!sameHost) assert.match(messages.join(" "), /같은 실행 환경/);
    } finally {await f.close();}
  }
});

test("successful save return is insufficient when current or newly added scoped documents remain dirty", async () => {
  for (const change of ["still-dirty", "new-document"]) {
    const f = await fixture();
    try {
      documents = [{isDirty: true, uri: {scheme: "file", fsPath: path.join(f.currentCode, "main.cpp")}, save: async () => {
        if (change === "new-document") {
          documents[0]!.isDirty = false;
          documents.push({isDirty: true, uri: {scheme: "file", fsPath: path.join(f.currentCode, "new.cpp")}, save: async () => true});
        }
        return true;
      }}];
      await f.view.show(); assert.deepEqual(await readdir(f.output), []); assert.match(messages.join(" "), /미저장 변경이 남아/);
    } finally {await f.close();}
  }
});

test("explicit disk-only choice preserves dirty buffers and records their exclusion in the export manifest", async () => {
  const f = await fixture(); let saves = 0;
  try {
    decision = "디스크에 저장된 내용만";
    documents = [{isDirty: true, uri: {scheme: "file", fsPath: path.join(f.currentCode, "main.cpp")}, save: async () => {saves++; return true;}}];
    await f.view.show(); const names = await readdir(f.output); assert.equal(names.length, 1); assert.equal(saves, 0); assert.equal(documents[0]!.isDirty, true);
    assert.equal(await readFile(path.join(f.output, names[0]!, "current-code/main.cpp"), "utf8"), "current code");
    const info = JSON.parse(await readFile(path.join(f.output, names[0]!, "EXPORT-INFO.json"), "utf8")); assert.equal(info.groups[0].excluded_entries, 1);
  } finally {await f.close();}
});

test("editing current code while remote items download cancels the combined export", async () => {
  const f = await fixture();
  try {
    selectedCategories = ["current", "personal"];
    f.state.afterRead = () => {documents.push({isDirty: true, uri: {scheme: "file", fsPath: path.join(f.currentCode, "main.cpp")}, save: async () => true});};
    await f.view.show(); assert.deepEqual(await readdir(f.output), []); assert.match(messages.join(" "), /미저장 변경이 남아/);
  } finally {await f.close();}
});


for (const category of ["submissions", "content"]) {
  test(`export checks the bound student cache before staging ${category}`, async () => {
    const f = await fixture();
    try {
      selectedCategories = [category]; f.state.cacheValid = false;
      await f.view.show();
      assert.equal(f.state.reads, 0); assert.ok(f.state.cacheChecks > 0);
      assert.deepEqual(await readdir(f.output), []);
      assert.match(messages.join(" "), /student cache changed/);
    } finally {await f.close();}
  });
}

test("export rechecks the student cache after a submission download before extracting it", async () => {
  const f = await fixture();
  try {
    selectedCategories = ["submissions"]; f.state.afterRead = () => {f.state.cacheValid = false;};
    await f.view.show();
    assert.equal(f.state.reads, 1); assert.ok(f.state.cacheChecks >= 3);
    assert.deepEqual(await readdir(f.output), []);
    assert.match(messages.join(" "), /student cache changed/);
    const stage = (await readdir(f.cache)).find(name => name.startsWith("export-stage-"))!;
    assert.deepEqual(await readdir(path.join(f.cache, stage)), []);
  } finally {await f.close();}
});

test("export refuses a changed student workspace before saving dirty documents or collecting code", async () => {
  const f = await fixture(); let saves = 0;
  try {
    f.state.workspaceValid = false;
    documents = [{isDirty: true, uri: {scheme: "file", fsPath: path.join(f.currentCode, "main.cpp")}, save: async () => {saves++; return true;}}];
    await f.view.show();
    assert.equal(saves, 0); assert.deepEqual(await readdir(f.output), []);
    assert.match(messages.join(" "), /student workspace changed/);
  } finally {await f.close();}
});
