import assert from "node:assert/strict";
import test from "node:test";
import { publicSubmissionScore, receiptProgress, submissionLabel, submissionTime } from "../learningJourney";
import { normalizeAssignments, normalizeSubmission } from "../helpers";
import { learningNextStep, renderLearningHome } from "../learningHome";
import { studentPortalUrl, configuredStudentPortalBase } from "../studentPortalLink";

const ready = {authenticated: true, trusted: true, hasWorkspace: true, hasAssignmentFolder: true};
const assignment = {id: "b_a", title: "Iterator <practice>", courseLabel: "COME2201", courseKey: "come2201"};

test("receipt states and public scores cannot turn an ungraded resubmission into a completed score", () => {
  for (const state of ["received", "accepted", "queued", "running", "graded", "rejected", "infra_failed", "unknown"]) {
    assert.equal(publicSubmissionScore({state, score: 100, maxScore: 100}), undefined);
  }
  assert.equal(publicSubmissionScore({state: "published", score: 80, maxScore: 100}), "80 / 100점");
  for (const [score, maxScore] of [[NaN, 100], [10, 0], [-1, 10], [11, 10], [Infinity, Infinity]]) {
    assert.equal(publicSubmissionScore({state: "published", score, maxScore}), undefined);
  }
  assert.equal(submissionLabel("received"), "제출 접수");
  assert.equal(submissionLabel("accepted"), "제출 접수");
  assert.equal(submissionLabel("queued"), "채점 대기");
  assert.equal(submissionLabel("published"), "결과 공개");
  assert.equal(submissionLabel(), "공식 제출 전");
  assert.equal(submissionLabel("infra_failed"), "처리 확인 필요");
  assert.deepEqual(receiptProgress("rejected").filter(item => item.current || item.complete), []);
  assert.equal(receiptProgress("queued").find(item => item.current)?.label, "채점 대기");
});

test("home uses connected assignment, retains previous best separately and explains next action", () => {
  assert.equal(learningNextStep({...ready, authenticated: false}).action, "claim");
  assert.equal(learningNextStep(ready).action, "downloads");
  assert.equal(learningNextStep({...ready, assignment, hasAssignmentFolder: false}).action, "open");
  assert.equal(learningNextStep({...ready, assignment}).action, "submit");
  assert.equal(learningNextStep({...ready, authenticated: false, sharedSession: true, sessionNeedsRestart: true}).action, "endSession");
  const pending = {...assignment, latestSubmission: {id: "bsub_new", state: "queued", score: 100, maxScore: 100,
    receivedAt: "2026-10-29T04:50:00Z", previousBest: {submissionId: "bsub_old", receivedAt: "2026-10-28T04:00:00Z", score: 95, maxScore: 100}}};
  assert.equal(learningNextStep({...ready, assignment: pending}).action, "result");
  const html = renderLearningHome({...ready, assignment: pending}, "nonce");
  assert.match(html, /Iterator &lt;practice&gt;/);
  assert.doesNotMatch(html, /100 \/ 100점/);
  assert.match(html, /이번 제출 점수<\/dt><dd>아직 공개되지 않음/);
  assert.match(html, /이전 제출 중 최고점<\/dt><dd>95 \/ 100점/);
  assert.match(html, /접수번호 <code>bsub_new/);
  assert.match(html, /13:50:00/);
  assert.match(html, /과제·자료·제출 도구/);
  assert.match(html, /자동 저장은 공식 제출이나 채점을 시작하지 않습니다/);
});

test("student portal uses an explicit web origin and never converts an API port or accepts credentials", () => {
  assert.equal(configuredStudentPortalBase({globalValue: "https://school.example", workspaceValue: "https://attacker.example"}), "https://school.example");
  assert.equal(configuredStudentPortalBase({workspaceValue: "https://attacker.example", workspaceFolderValue: "https://other.example"}), "");
  assert.equal(studentPortalUrl("https://school.example:20010/", "come2201"), "https://school.example:20010/app/student?course_key=come2201");
  assert.equal(studentPortalUrl("https://school.example:20000"), "https://school.example:20000/app/student");
  assert.equal(studentPortalUrl("http://127.0.0.1:18183"), "http://127.0.0.1:18183/app/student");
  assert.equal(studentPortalUrl("https://school.example", "bad&token=secret"), "https://school.example/app/student");
  for (const origin of ["https://user:password@school.example", "https://school.example/student", "https://school.example?token=secret", "https://school.example#secret", "file:///tmp/student", "javascript:alert(1)", "http://192.168.0.2:20010", "http://school.example", ""]) {
    assert.throws(() => studentPortalUrl(origin));
  }
});

test("receipt time is server-provided and validated rather than guessed from the client clock", () => {
  assert.equal(normalizeSubmission({submission_id: "s", state: "queued", received_at: "invalid"})?.receivedAt, undefined);
  assert.equal(normalizeSubmission({submission_id: "s", state: "queued", received_at: "2026-10-29T04:50:00Z"})?.receivedAt, "2026-10-29T04:50:00Z");
  assert.match(submissionTime(undefined), /서버 제공 시각 없음/);
  assert.match(submissionTime("2026-10-29T04:50:00Z"), /13:50:00/);
  assert.equal(normalizeAssignments([{assignment_id:"a",title:"Lab",course_key:"come2201"}])[0]?.courseKey, "come2201");
});
