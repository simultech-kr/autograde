import assert from "node:assert/strict";
import * as path from "node:path";
import test from "node:test";
import { saveAssignmentDocuments, type SubmissionDocument } from "../submissionPreflight";

function document(file: string, saveResult: boolean | Error = true) {
  let dirty = true;
  let saves = 0;
  return {
    uri: { scheme: "file", fsPath: path.resolve(file) },
    get isDirty() { return dirty; },
    get saves() { return saves; },
    async save() {
      saves += 1;
      if (saveResult instanceof Error) throw saveResult;
      if (saveResult) dirty = false;
      return saveResult;
    },
  };
}

test("save confirmation includes only edited files inside the submitted assignment", async () => {
  const inside = document("course/problem01/main.cpp");
  const nested = document("course/problem01/include/item.hpp");
  const other = document("course/problem02/main.cpp");
  const prefixSibling = document("course/problem01-copy/main.cpp");
  const untitled = { ...document("course/problem01/untitled.cpp"), uri: { scheme: "untitled", fsPath: "draft" } };
  let shown: readonly string[] = [];
  assert.equal(await saveAssignmentDocuments("course/problem01", () => [inside, nested, other, prefixSibling, untitled],
    async files => { shown = files; return true; }, () => true), true);
  assert.deepEqual(shown, ["main.cpp", path.join("include", "item.hpp")]);
  assert.equal(inside.saves, 1); assert.equal(nested.saves, 1);
  assert.equal(other.saves, 0); assert.equal(prefixSibling.saves, 0); assert.equal(untitled.saves, 0);
});

test("cancel prevents saving and prevents continuation to bundle creation", async () => {
  const dirty = document("course/problem01/main.cpp");
  const proceed = await saveAssignmentDocuments("course/problem01", () => [dirty], async () => false, () => true);
  assert.equal(proceed, false); assert.equal(dirty.saves, 0);
});

test("WSL and remote files are saved only in the submitting workspace's URI authority", async () => {
  const current = document("course/problem01/main.cpp");
  const otherHost = document("course/problem01/main.cpp");
  const local = document("course/problem01/main.cpp");
  const remoteCurrent = { ...current, uri: { ...current.uri, scheme: "vscode-remote", authority: "wsl+Ubuntu" },
    get isDirty() { return current.isDirty; } };
  const remoteOther = { ...otherHost, uri: { ...otherHost.uri, scheme: "vscode-remote", authority: "ssh-remote+other" } };
  assert.equal(await saveAssignmentDocuments("course/problem01", () => [remoteCurrent, remoteOther, local],
    async files => { assert.deepEqual(files, ["main.cpp"]); return true; }, () => true,
    { scheme: "vscode-remote", authority: "wsl+Ubuntu" }), true);
  assert.equal(current.saves, 1); assert.equal(otherHost.saves, 0); assert.equal(local.saves, 0);
});

test("a declined or failed save blocks submission with a filename and recovery instruction", async () => {
  for (const outcome of [false, new Error("disk full")]) {
    const dirty = document("course/problem01/main.cpp", outcome);
    await assert.rejects(saveAssignmentDocuments("course/problem01", () => [dirty], async () => true, () => true),
      /main.cpp.*저장하지 못해 제출을 중단/);
  }
});

test("documents that remain dirty after a save cannot be submitted", async () => {
  const dirty: SubmissionDocument = { uri: { scheme: "file", fsPath: path.resolve("course/problem01/main.cpp") },
    isDirty: true, save: async () => true };
  await assert.rejects(saveAssignmentDocuments("course/problem01", () => [dirty], async () => true, () => true), /제출을 중단/);
});

test("an edit created while saving another document is detected before bundle creation", async () => {
  const newEdit = document("course/problem01/new.cpp");
  const first = document("course/problem01/main.cpp");
  let reads = 0;
  await assert.rejects(saveAssignmentDocuments("course/problem01", () => ++reads === 1 ? [first] : [first, newEdit],
    async () => true, () => true), /저장되지 않은 변경/);
});

test("authentication changes during confirmation or saving stop the old student's submission", async () => {
  const dirty = document("course/problem01/main.cpp");
  let current = true;
  assert.equal(await saveAssignmentDocuments("course/problem01", () => [dirty],
    async () => { current = false; return true; }, () => current), false);
  assert.equal(dirty.saves, 0);
  current = true;
  const duringSave: SubmissionDocument = { uri: dirty.uri, get isDirty() { return current; },
    save: async () => { current = false; return true; } };
  assert.equal(await saveAssignmentDocuments("course/problem01", () => [duringSave], async () => true, () => current), false);
});
