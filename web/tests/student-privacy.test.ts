import test from "node:test";
import assert from "node:assert/strict";
import {parsePrivacy} from "../src/features/student/privacy.ts";
import {allowedStudentPath} from "../src/features/student/client.ts";

const record = () => ({ok: true, result: {student_key: "20260001", course_key: "come2201", decision: "unanswered", revision: 0,
  research_allowed: false, updated_at: null, history: [], policy: Object.fromEntries(["version", "title", "purpose", "items", "retention", "choice", "withdrawal", "scope", "contact"].map(key => [key, `synthetic ${key}`]))}});

test("consent response must belong to the current student and course", () => {
  assert.equal(parsePrivacy(record(), "come2201", "20260001").research_allowed, false);
  assert.throws(() => parsePrivacy(record(), "come3105", "20260001"));
  assert.throws(() => parsePrivacy(record(), "come2201", "20260002"));
  assert.equal(allowedStudentPath("/student/api/course/come2201/privacy"), true);
  for (const path of ["/student/api/course/come2201/privacy?student_key=20260002", "https://other.test/student/api/course/come2201/privacy", "/student/api/course/../privacy"])
    assert.equal(allowedStudentPath(path), false);
});

test("unknown or inconsistent consent cannot become permission to use research data", () => {
  for (const fields of [{decision: "yes"}, {revision: -1}, {revision: 1.1}, {research_allowed: true}, {decision: "granted", research_allowed: false}, {history: [null]}, {updated_at: "not-a-date"}]) {
    const payload = record(); Object.assign(payload.result, fields);
    assert.throws(() => parsePrivacy(payload, "come2201", "20260001"));
  }
  const payload = record(); payload.result.policy.items = "x".repeat(2001);
  assert.throws(() => parsePrivacy(payload, "come2201", "20260001"));
});
