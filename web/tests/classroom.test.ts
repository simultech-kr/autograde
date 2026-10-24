import test from "node:test";
import assert from "node:assert/strict";
import { parseClassroom, parseClassroomDetail, parseClassroomCommand } from "../src/lib/contracts/classroom.ts";
import { classroomURL, createClassroomCommand } from "../src/lib/api/classroom.ts";
import { connectionLabels, stableStudents, submissionLabel, submissionScore, commandStatusLabel, commandResultLabel, checkLabel } from "../src/features/classroom/presentation.ts";

const attempt = { submission_id: "sub-new", received_at: "2026-10-25T10:00:00Z", state: "received", score: null, max_score: null, code_version: "a".repeat(64), delivery_mode: "bundle" };
const student = { student_key: "TEST-001", student_name: "Test <script>", connection: { state: "online", last_seen_at: "2026-10-25T10:00:00Z", environment: { os: "macos", remote_name: "none", ide: "vscode" } }, activity: { kind: "file_saved", received_at: "2026-10-25T10:00:00Z", assignment_id: "a1" }, submission: { state: "received", submitted: true, latest: attempt, previous_best: { submission_id: "sub-old", received_at: "2026-10-24T10:00:00Z", score: 90, max_score: 100 } } };
const meta = { contract_version: "1", request_id: "request-1", course_key: "come2201", version: "a".repeat(64), server_time: "2026-10-25T10:00:00Z", selected_assignment_id: "a1" };
const fixture = () => ({ ...meta, summary: { total: 1, online: 1, stale: 0, offline: 0, never_connected: 0, submitted: 1, pending: 1 }, students: [structuredClone(student)], assignments: [{ assignment_id: "a1", title: "Observer", assignment_key: "week1", release_id: "r1", delivery_mode: "bundle" }] });

test("snapshot contract distinguishes IDE report, official submission and previous best", () => {
  const data = parseClassroom(fixture()), row = data.students[0];
  assert.equal(connectionLabels[row.connection.state], "IDE 연결");
  assert.equal(submissionLabel(row), "공식 제출 접수");
  assert.equal(submissionScore(row.submission.latest), "접수 완료 · 점수 미확정");
  assert.equal(row.submission.previous_best?.score, 90);
  assert.equal(row.student_name, "Test <script>");
});
test("current zero grade remains separate from previous best and stale pending scores are hidden", () => {
  assert.equal(submissionScore({ ...attempt, state: "published", score: 0, max_score: 100 }), "0 / 100점");
  assert.equal(submissionScore({ ...attempt, state: "running", score: 100, max_score: 100 }), "접수 완료 · 점수 미확정");
  assert.equal(submissionScore({ ...attempt, state: "infra_failed", score: 0, max_score: 100 }), "처리 오류 · 이력 확인");
});
test("unselected assignment is never falsely labeled unsubmitted", () => {
  const row = parseClassroom(fixture()).students[0];
  row.submission = { state: "assignment_required", submitted: false, latest: null, previous_best: null };
  assert.equal(submissionLabel(row), "과제 선택 필요");
  row.submission.state = "not_assigned";
  assert.equal(submissionLabel(row), "배정 대상 아님");
});
test("snapshot contract rejects duplicate students, invalid states, counts and marks", () => {
  const duplicate = fixture(); duplicate.students.push(duplicate.students[0]); duplicate.summary.total = 2;
  assert.throws(() => parseClassroom(duplicate));
  const invalidState = fixture(); invalidState.students[0].connection.state = "focused";
  assert.throws(() => parseClassroom(invalidState));
  const count = fixture(); count.summary.total = 2; assert.throws(() => parseClassroom(count));
  const invalidScore = fixture() as any; invalidScore.students[0].submission.latest.score = 101; invalidScore.students[0].submission.latest.max_score = 100;
  assert.throws(() => parseClassroom(invalidScore));
});
test("detail contract preserves unverified storage and only allowlisted IDE request actions", () => {
  const value = parseClassroomDetail({ ...meta, student: { ...student, activities: [student.activity], submissions: [attempt], code_storage: { state: "receipt_recorded", verified: false, message: "Not a backup" }, analysis: { workspace_opened: 1, file_saved: 2, submission_started: 1, submission_finished: 1, window: "recent100", note: "IDE reports only" }, controls: { supported: true, actions: ["notice"], reason: "Student confirmation required" }, commands: [] } });
  assert.equal(value.student.code_storage.verified, false);
  assert.equal(value.student.controls.supported, true);
  assert.deepEqual(value.student.controls.actions, ["notice"]);
  assert.deepEqual(value.student.commands, []);
});

const command = () => ({ command_id: "cmd_001", kind: "notice", assignment_id: "a1", message: "가상 <script> 공지", status: "queued", created_by: "Teacher", created_at: "2026-10-25T10:00:00Z", expires_at: "2026-10-25T10:10:00Z", received_at: null, accepted_at: null, finished_at: null, result_code: null, result: null });
test("IDE receipt, student acceptance, completion and uncertain expiry remain distinct", () => {
  const queued = parseClassroomCommand(command());
  assert.match(commandStatusLabel(queued), /IDE 수신 대기/);
  assert.match(commandStatusLabel({ ...queued, status: "received" }), /학생 확인 대기/);
  assert.match(commandStatusLabel({ ...queued, status: "accepted" }), /학생 수락/);
  const expired = { ...queued, status: "expired" as const, accepted_at: queued.created_at, result_code: "expired" };
  assert.match(commandStatusLabel(expired), /처리 결과 미확인/);
  assert.match(commandResultLabel(expired), /취소되었다는 뜻은 아닙니다/);
  assert.match(commandResultLabel({ ...queued, result_code: "submission_received" }), /채점 완료와는 다릅니다/);
  assert.equal(checkLabel("compiler", "not_checked"), "컴파일러: 점검하지 않음");
});
test("command parsing rejects unknown actions, status and raw environment data", () => {
  assert.throws(() => parseClassroomCommand({ ...command(), kind: "shell" }));
  assert.throws(() => parseClassroomCommand({ ...command(), status: "done" }));
  assert.throws(() => parseClassroomCommand({ ...command(), result: { path: "/private" } }));
  assert.throws(() => parseClassroomCommand({ ...command(), result: { checks: { compiler: "/usr/bin/clang" } } }));
  const checked = parseClassroomCommand({ ...command(), result: { checks: { compiler: "available", os: "macos" } } });
  assert.equal(checked.result?.checks?.os, "macos");
});
test("command create preserves the caller's idempotency key and uses authenticated same-origin POST", async () => {
  const original = globalThis.fetch, controller = new AbortController();
  const payload = { csrf: "csrf", kind: "notice" as const, assignment_id: "a1", message: "Test", request_key: "request-0001" };
  let calls = 0;
  globalThis.fetch = async (input, init) => {
    calls++;
    assert.equal(input, "/instructor/api/v1/courses/come2201/classroom/students/TEST-001/commands");
    assert.equal(init?.method, "POST"); assert.equal(init?.credentials, "same-origin"); assert.equal(init?.redirect, "error"); assert.equal(init?.signal, controller.signal);
    assert.deepEqual(JSON.parse(init?.body as string), payload);
    return Response.json({ contract_version: "1", command: command(), duplicate: calls > 1 }, { status: calls === 1 ? 201 : 200 });
  };
  try {
    assert.equal((await createClassroomCommand("come2201", "TEST-001", payload, controller.signal)).duplicate, false);
    assert.equal((await createClassroomCommand("come2201", "TEST-001", payload, controller.signal)).duplicate, true);
  } finally { globalThis.fetch = original; }
});
test("command create never accepts a response for another action", async () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () => Response.json({ contract_version: "1", command: { ...command(), kind: "save_submit" }, duplicate: false });
  try { await assert.rejects(() => createClassroomCommand("come2201", "TEST-001", { csrf: "csrf", kind: "notice", assignment_id: "a1", message: "Test", request_key: "request-0001" }, new AbortController().signal)); }
  finally { globalThis.fetch = original; }
});
test("card order and same-origin endpoints are stable", () => {
  const row = parseClassroom(fixture()).students[0];
  assert.deepEqual(stableStudents([{ ...row, student_key: "TEST-020" }, row]).map(s => s.student_key), ["TEST-001", "TEST-020"]);
  assert.equal(classroomURL("come2201", "abc def", "/events"), "/instructor/api/v1/courses/come2201/classroom/events?assignment_id=abc+def");
});
