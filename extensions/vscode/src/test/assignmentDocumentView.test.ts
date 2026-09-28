import assert from "node:assert/strict";
import test from "node:test";
import { AssignmentDocumentState } from "../assignmentDocument";
import type { AssignmentDocument } from "../types";

interface FakeUri { toString(): string }
interface FakeDocument { uri: FakeUri }
interface ModuleLoader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
const shown: FakeDocument[] = [];
const changed: FakeUri[] = [];
const languages: string[] = [];
let open: (uri: FakeUri) => Promise<FakeDocument> = async uri => ({uri});
let notices = 0;
const stub = {
  EventEmitter: class { public event = () => {}; public fire(uri: FakeUri) { changed.push(uri); } public dispose() {} },
  Uri: {from: (parts: {scheme: string; path: string}) => ({toString: () => `${parts.scheme}:${parts.path}`})},
  workspace: {openTextDocument: (uri: FakeUri) => open(uri)},
  languages: {setTextDocumentLanguage: async (document: FakeDocument, language: string) => { languages.push(language); return document; }},
  window: {showTextDocument: async (document: FakeDocument) => {shown.push(document);},
    showInformationMessage: () => {notices++;}},
  ViewColumn: {Beside: 2},
};
loader._load = function(name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const {AssignmentDocumentView} = require("../assignmentDocumentView") as typeof import("../assignmentDocumentView");
loader._load = original;
const assignment = {id: "a", title: "A"};
const document: AssignmentDocument = {assignmentId: "a", revision: 1, content: "<script>unsafe()</script>\n[run](command:evil)",
  sha256: "sha256:" + "a".repeat(64), updatedAt: "2026-09-22T04:00:00Z", changeNote: "수정", history: []};
const uriOf = (item: FakeDocument) => item.uri as import("vscode").Uri;

test("description opens as virtual plaintext, marks read only after display, and clears on logout", async () => {
  shown.length = 0; changed.length = 0; languages.length = 0;
  const state = new AssignmentDocumentState();
  let acknowledgements = 0;
  const view = new AssignmentDocumentView(state, async () => document, () => true, () => {acknowledgements++;});
  await view.show(assignment);
  assert.equal(shown.length, 1);
  assert.deepEqual(languages, ["plaintext"]);
  assert.match(shown[0]!.uri.toString(), /^autograde-assignment-document:/);
  assert.ok(view.provideTextDocumentContent(uriOf(shown[0]!)).includes(document.content));
  assert.equal(acknowledgements, 1);
  view.clear();
  assert.match(view.provideTextDocumentContent(uriOf(shown[0]!)), /표시가 종료/);
  assert.equal(changed.length, 1);
  assert.equal(state.summary({...assignment, document}).unread, true);
  view.dispose();
});

test("changed selection, logout and no longer accepted work suppress delayed responses", async () => {
  for (const boundary of ["selection", "logout", "acceptance"]) {
    shown.length = 0;
    let finish!: (value: AssignmentDocument) => void;
    let accepted = true;
    const view = new AssignmentDocumentView(new AssignmentDocumentState(), () => new Promise(resolve => {finish = resolve;}),
      () => accepted, () => assert.fail("stale description must not be marked read"));
    const pending = view.show(assignment);
    if (boundary === "selection") view.select("b");
    else if (boundary === "logout") view.clear();
    else accepted = false;
    finish(document);
    await pending;
    assert.equal(shown.length, 0);
    view.dispose();
  }
});

test("an old editor-open completion cannot erase the newer selected description", async () => {
  shown.length = 0;
  let finish!: (value: FakeDocument) => void;
  let delayed!: FakeUri;
  let calls = 0;
  open = async uri => ++calls === 1 ? new Promise(resolve => {finish = resolve; delayed = uri;}) : {uri};
  const view = new AssignmentDocumentView(new AssignmentDocumentState(), async id => ({...document, assignmentId: id}), () => true, () => {});
  const first = view.show(assignment);
  await new Promise(resolve => setImmediate(resolve));
  await view.show({id: "b", title: "B"});
  finish({uri: delayed});
  await first;
  assert.equal(shown.length, 1);
  assert.match(view.provideTextDocumentContent(uriOf(shown[0]!)), /^B —/);
  view.dispose(); open = async uri => ({uri});
});

test("unsupported endpoint shows an explanation without acknowledgement or opening an editor", async () => {
  shown.length = 0; notices = 0;
  const view = new AssignmentDocumentView(new AssignmentDocumentState(), async () => undefined, () => true,
    () => assert.fail("unsupported descriptions cannot be acknowledged"));
  await view.show(assignment);
  assert.equal(shown.length, 0); assert.equal(notices, 1);
  view.dispose();
});

test("refresh removing an accepted assignment clears its open description", async () => {
  shown.length = 0;
  let accepted = true;
  const view = new AssignmentDocumentView(new AssignmentDocumentState(), async () => document, () => accepted, () => {});
  await view.show(assignment);
  accepted = false;
  view.reconcileAccepted();
  assert.match(view.provideTextDocumentContent(uriOf(shown[0]!)), /표시가 종료/);
  view.dispose();
});
