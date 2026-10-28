import test from "node:test";
import assert from "node:assert/strict";
import { parseBootstrap, parseCourse, parseClaim, coursePath, courseLink, courseMeta, submissionState, scoreText, remainingSeconds, RequestVersion, StudentContractError } from "../src/features/student/model.ts";
import { allowedStudentPath, studentRequest, StudentApiError } from "../src/features/student/client.ts";

const key = "20260001", course = "come2201";
const row = { assignment_id: "assignment_01", submission_id: "submission_04", assignment_key: "week09", title: "실습", received_at: "2026-10-29T01:01:00Z", state: "queued", code_version: "a".repeat(64), score: null, max_score: null, published_at: null, version: 4, previous_best_id: "submission_02", previous_best_score: 95, previous_best_max_score: 100, previous_best_at: "2026-10-28T01:00:00Z" };
const bootstrap = { ok: true, result: { authenticated: true, csrf: "synthetic_csrf", expires_at: "2026-10-29T01:10:00Z", student: { student_key: key, verified: true }, api_base_url: "https://example.test:20000", courses: [{ course_key: course, code: "COME2201", name: "소프트웨어 설계", year: 2026, semester: "2", section: "01", term: "2026학년도 2학기", status: "matched", request_status: "matched", enrolled: true, eligible: true, can_withdraw: false }] } };
const courseResponse = { ok: true, result: { student_key: key, course: { course_key: course, code: "COME2201", name: "소프트웨어 설계", section: "01" }, csrf: "synthetic_csrf", server_time: "2026-10-29T01:01:00Z", api_base_url: "https://example.test:20000", links: { assignments: "/courses/come2201/assignments", materials: "/courses/come2201/materials" }, overview: { assignments: [{ assignment_id: "assignment_01", assignment_key: "week09", title: "실습", opens_at: null, due_at: "2026-11-01T01:00:00Z", accepted_at: "2026-10-29T01:00:00Z" }], latest: [row], history: [row], history_truncated: false, latest_truncated: false, assignments_truncated: false, limit: 100 } } };

test("missing course metadata is explicit and never creates empty academic labels", () => {
  assert.equal(courseMeta({ term: "", section: "" }), "학기·분반 정보 미등록");
  assert.equal(courseMeta({ year: 2026, section: "" }), "2026학년도");
  assert.equal(courseMeta({ section: "02" }), "02분반");
  assert.equal(courseMeta({ year: 2026, semester: "2", section: "01" }), "2026학년도 2학기 · 01분반");
});

test("actual course catalog preserves course and section identity and rejects malformed auth", () => {
  const value = parseBootstrap(bootstrap);
  assert.equal(value.student?.student_key, key); assert.equal(value.courses[0].section, "01");
  assert.throws(() => parseBootstrap({ ...bootstrap, result: { ...bootstrap.result, authenticated: false } }), StudentContractError);
  assert.throws(() => parseBootstrap({ ...bootstrap, result: { ...bootstrap.result, expires_at: null } }), StudentContractError);
  assert.throws(() => parseBootstrap({ ...bootstrap, result: { ...bootstrap.result, courses: [...bootstrap.result.courses, ...bootstrap.result.courses] } }), StudentContractError);
});
test("course API rejects a different student, course and cross-course download link", () => {
  assert.equal(parseCourse(courseResponse, course, key).overview.latest[0].previous_best_score, 95);
  assert.throws(() => parseCourse(courseResponse, course, "20260002"), StudentContractError);
  assert.throws(() => parseCourse(courseResponse, "come3105", key), StudentContractError);
  const links = { ...courseResponse.result.links, materials: "/courses/come3105/materials" };
  assert.throws(() => parseCourse({ ...courseResponse, result: { ...courseResponse.result, links } }, course, key), StudentContractError);
});
test("claim response must belong to the expected current account and assignment", () => {
  const response = { ok: true, result: { student_key: key, claim: { assignment_id: "assignment_01", title: "실습", code: "SYNTHETIC-NOT-USABLE", expires_at: "2026-10-29T01:10:00Z", api_url: "https://example.test:20000" } } };
  assert.equal(parseClaim(response, "assignment_01", key).assignment_id, "assignment_01");
  assert.throws(() => parseClaim(response, "assignment_02", key), StudentContractError);
  assert.throws(() => parseClaim(response, "assignment_01", "20260002"), StudentContractError);
});
test("queued resubmission and processing errors never present the previous grade as current", () => {
  for (const state of ["received", "accepted", "queued", "running", "graded", "rejected", "infra_failed", "assessment_failed", "unexpected"] ) {
    const result = submissionState({ state, score: 95, max_score: 100, published_at: "2026-10-29T01:01:00Z" });
    assert.equal(result.scored, false, state);
  }
  assert.equal(submissionState({ state: "published", score: 80, max_score: 100, published_at: "2026-10-29T01:01:00Z" }).scored, true);
  assert.equal(submissionState({ state: "published", score: 80, max_score: 100, published_at: null }).scored, false);
  assert.equal(submissionState({ state: "published", score: 101, max_score: 100, published_at: "2026-10-29T01:01:00Z" }).scored, false);
  assert.equal(scoreText(null, null), "—"); assert.match(scoreText(9.999999, 10), /9\.999999/);
});
test("request generations reject late previous-account responses and expiry reaches zero", () => {
  const versions = new RequestVersion(), old = versions.next(); const next = versions.next();
  assert.equal(versions.current(old), false); assert.equal(versions.current(next), true);
  assert.equal(remainingSeconds("2026-10-29T01:10:00Z", Date.parse("2026-10-29T01:09:01Z")), 59);
  assert.equal(remainingSeconds("2026-10-29T01:10:00Z", Date.parse("2026-10-29T01:11:00Z")), 0);
});
test("student routes and download links cannot escape the same origin or expected scope", () => {
  assert.equal(coursePath(course), "/student/api/course/come2201");
  for (const path of ["https://remote.test/student/api/bootstrap", "//remote.test", "/student/api/bootstrap?token=x", "/student/api/course/../claims", "/student/api/course/a%2fb", "/student/api/bootstrap#x", "/instructor/api/v1/bootstrap"]) assert.equal(allowedStudentPath(path), false, path);
  for (const path of ["//remote.test", "javascript:alert(1)", "/courses/other/materials", "/courses/come2201/materials?token=x"]) assert.throws(() => courseLink(course, path, "materials"), StudentContractError);
});
test("student requests send bounded same-origin form data without bearer authorization", async () => {
  let called = false;
  const fetcher: typeof fetch = async (url, options) => {
    called = true; assert.equal(url, "/student/api/enroll"); assert.equal(options?.credentials, "same-origin"); assert.equal(options?.redirect, "error"); assert.equal(options?.cache, "no-store");
    assert.equal(new Headers(options?.headers).has("Authorization"), false);
    const body = new URLSearchParams(String(options?.body)); assert.equal(body.get("csrf"), "synthetic_csrf"); assert.equal(body.get("course_keys"), '["come2201"]');
    return Response.json(bootstrap);
  };
  await studentRequest("/student/api/enroll", { csrf: "synthetic_csrf", course_keys: JSON.stringify([course]) }, undefined, fetcher);
  assert.equal(called, true);
});
test("HTML responses never become success or fixtures; errors never expose network exception details", async () => {
  await assert.rejects(studentRequest("/student/api/bootstrap", undefined, undefined, async () => new Response("<html>sign in</html>")), StudentContractError);
  await assert.rejects(studentRequest("/student/api/bootstrap", undefined, undefined, async () => { throw new Error("secret host diagnostics"); }), error => error instanceof StudentApiError && error.code === "network_error" && !error.message.includes("secret"));
  await assert.rejects(studentRequest("/student/api/bootstrap", undefined, undefined, async () => Response.json({ ok: false, error: { code: "login_required", message: "다시 로그인하세요." } }, { status: 403 })), error => error instanceof StudentApiError && error.status === 403);
});
