import test from "node:test";
import assert from "node:assert/strict";
import {scoreRows, scoreRowState} from "../src/features/student/scoreboard.ts";
import type {CourseData, Receipt} from "../src/features/student/model.ts";

const row: Receipt = {assignment_id: "a1", assignment_key: "p1", title: "Practice", submission_id: "s2", received_at: "2026-11-01T01:00:00Z", state: "queued", code_version: "c".repeat(64), score: null, max_score: null, published_at: null, version: 2, previous_best_id: "s1", previous_best_score: 9, previous_best_max_score: 10};
const overview = (): CourseData["overview"] => ({assignments: [
  {assignment_id: "a1", assignment_key: "p1", title: "Practice", opens_at: null, due_at: null, accepted_at: null},
  {assignment_id: "a2", assignment_key: "p2", title: "Next practice", opens_at: null, due_at: null, accepted_at: null},
], latest: [{...row}], history: [{...row}], latest_truncated: false, assignments_truncated: false, history_truncated: false, limit: 100});

test("student score rows distinguish missing, queued, private and public results", () => {
  const data = overview(), rows = scoreRows(data);
  assert.equal(scoreRowState(rows[0]).label, "채점 대기");
  assert.equal(scoreRowState(rows[0]).scored, false);
  assert.equal(rows[0].receipt?.previous_best_score, 9);
  assert.equal(scoreRowState(rows[1]).label, "공식 제출 전");
  data.latest[0] = {...row, state: "graded", score: 3, max_score: 10};
  assert.equal(scoreRowState(scoreRows(data)[0]).scored, false);
  data.latest[0] = {...data.latest[0], state: "published", published_at: "2026-11-01T01:02:00Z"};
  assert.equal(scoreRowState(scoreRows(data)[0]).scored, true);
  assert.equal(scoreRows(data)[0].receipt?.score, 3);
});

test("bounded data never labels an unlisted receipt as non submission", () => {
  const data = overview(); data.latest_truncated = true;
  const rows = scoreRows(data);
  assert.equal(rows[0].missingIsUnknown, false);
  assert.equal(rows[1].missingIsUnknown, true);
  assert.equal(scoreRowState(rows[1]).label, "제출 기록 추가 확인 필요");
});

test("latest receipts outside the bounded assignment page remain visible once", () => {
  const data = overview(); data.assignments = data.assignments.slice(1);
  const rows = scoreRows(data);
  assert.deepEqual(rows.map(item => item.assignment_id), ["a2", "a1"]);
  assert.equal(rows[1].receipt?.submission_id, "s2");
  assert.deepEqual(scoreRows({...data, assignments: [], latest: []}), []);
});
