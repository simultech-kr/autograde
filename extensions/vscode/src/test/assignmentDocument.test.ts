import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test from "node:test";
import { AssignmentDocumentState, documentText, parseAssignmentDocument, parseDocumentMetadata } from "../assignmentDocument";
import { normalizeAssignments } from "../helpers";

const content = "# 문제 설명\r\n<script>untrusted()</script>\n[run](command:workbench.action.closeWindow)\n";
const raw = {assignment_id: "a", revision: 1, content,
  sha256: "sha256:" + createHash("sha256").update(content).digest("hex"),
  updated_at: "2026-09-22T04:00:00Z", change_note: "예제 보완", history: []};

test("optional metadata degrades gracefully and document SHA verifies exact UTF-8 content", () => {
  assert.equal(parseDocumentMetadata(null), undefined);
  assert.equal(parseDocumentMetadata({...raw, revision: -1}), undefined);
  const assignment = normalizeAssignments({assignments: [{assignment_id: "a", document: raw}]})[0]!;
  assert.equal(assignment.document?.revision, 1);
  assert.equal(normalizeAssignments({assignments: [{assignment_id: "old", document: null}]})[0]?.document, undefined);
  const document = parseAssignmentDocument({document: raw}, "a")!;
  assert.equal(document.content, content);
  assert.ok(documentText("Assignment", document).includes(content));
  assert.equal(parseAssignmentDocument({document: null}, "a"), undefined);
  assert.equal(parseAssignmentDocument({document: {...raw, revision: 0}}, "a")?.revision, 0);
  assert.throws(() => parseAssignmentDocument({document: raw}, "other"), /응답 형식/);
  assert.throws(() => parseAssignmentDocument({document: {...raw, content: content.trim()}}, "a"), /SHA-256/);
  assert.throws(() => parseAssignmentDocument({document: {...raw, content: "a".repeat(20001)}}, "a"), /응답 형식/);
  assert.throws(() => parseAssignmentDocument({document: {...raw, history: [null]}}, "a"), /변경 이력/);
});

test("read badges remain session-only and late documents cannot acknowledge another selection", () => {
  const state = new AssignmentDocumentState();
  const document = parseAssignmentDocument({document: raw}, "a")!;
  const assignment = {id: "a", title: "A", document};
  assert.deepEqual(state.summary({id: "legacy", title: "Legacy"}), {revision: 0, unread: false});
  assert.deepEqual(state.summary(assignment), {revision: 1, unread: true});
  const stale = state.begin("a");
  state.select("b");
  assert.equal(state.markRead(document, stale), false);
  assert.equal(state.markRead(document, state.begin("a")), true);
  assert.deepEqual(state.summary(assignment), {revision: 1, unread: false});
  assert.deepEqual(state.summary({...assignment, document: {...document, revision: 2}}), {revision: 2, unread: true});
  const pending = state.begin("a");
  state.clear();
  assert.equal(pending.isCurrent(), false);
  assert.deepEqual(state.summary(assignment), {revision: 1, unread: true});
});

test("change notes use the server's 500 Unicode-character limit, including supplementary characters", () => {
  for (const character of ["가", "😀"]) {
    assert.equal(parseDocumentMetadata({...raw, change_note: character.repeat(500)})?.changeNote, character.repeat(500));
    assert.equal(parseDocumentMetadata({...raw, change_note: character.repeat(501)}), undefined);
    assert.throws(() => parseAssignmentDocument({document: {...raw, change_note: character.repeat(501)}}, "a"), /응답 형식/);
    assert.throws(() => parseAssignmentDocument({document: {...raw, history: [{...raw, change_note: character.repeat(501)}]}}, "a"), /변경 이력/);
  }
});

test("description history rejects duplicate or future revisions while accepting distinct current and earlier entries", () => {
  const current = {...raw, revision: 2};
  const document = parseAssignmentDocument({document: {...current, history: [current, raw, {...raw, revision: 0}]}}, "a")!;
  assert.deepEqual(document.history.map(item => item.revision), [2, 1, 0]);
  assert.throws(() => parseAssignmentDocument({document: {...current, history: [raw, {...raw, change_note: "same revision"}]}}, "a"), /변경 이력/);
  assert.throws(() => parseAssignmentDocument({document: {...raw, history: [current]}}, "a"), /변경 이력/);
});
