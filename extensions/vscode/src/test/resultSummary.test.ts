import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { resultHtml, summarizeResult } from "../resultSummary";
import type { GradeResult } from "../types";

const full: GradeResult = { state: "published", score: 10, maxScore: 10, rubric: [], diagnostics: [] };
test("result panel is read-only and clears at authentication and address boundaries", () => {
  const panel = readFileSync(resolve(__dirname, "../../src/resultPanel.ts"), "utf8");
  const extension = readFileSync(resolve(__dirname, "../../src/extension.ts"), "utf8");
  assert.match(panel, /enableScripts: false/);
  assert.match(panel, /localResourceRoots: \[\]/);
  assert.match(extension, /\{ dispose: clearResultPanel \}/);
  assert.match(extension, /if \(!authenticated\) \{ clearResultPanel\(\); clearDownloadDiagnostic\(\); \}/);
  for (const start of ["new AuthenticationController", "new AssignmentClaimController", "const clearSessionUiForAddressChange"]) {
    assert.match(extension.slice(extension.indexOf(start), extension.indexOf(start) + 280), /clearResultPanel\(\)/);
  }
});
test("full score is scoped to automatic grading, not final course completion", () => {
  const result = summarizeResult(full);
  assert.equal(result.headline, "자동채점 총점 기준 충족");
  assert.match(result.next, /별도 요구사항/);
  assert.match(result.summary, /항목별 판정은 제공되지/);
});
test("failed criteria sort first and retain feedback", () => {
  const view = summarizeResult({ ...full, score: 2, rubric: [
    { name: "compile", score: 2, maxScore: 2 }, { name: "output", score: 0, maxScore: 8, feedback: "줄바꿈 확인" },
  ] });
  assert.equal(view.headline, "수정이 필요합니다");
  assert.equal(view.items[0]?.title, "출력 결과");
  assert.equal(view.items[0]?.feedback, "줄바꿈 확인");
  assert.equal(view.percent, 20);
  assert.match(view.summary, /1개 충족 · 1개 수정 필요/);
});
test("pending, unpublished and infra failures never show supplied provisional scores or feedback", () => {
  for (const state of ["queued", "running", "graded", "infra_failed", "assessment_failed", "rejected", "unknown"]) {
    const grade = { ...full, state, rubric: [{ name: "secret-test", score: 10, maxScore: 10 }], diagnostics: [{ path: "secret.cpp", message: "secret-feedback" }] };
    const view = summarizeResult(grade);
    assert.equal(view.score, "점수 미공개");
    assert.equal(view.percent, undefined);
    const html = resultHtml({ id: "a", title: "Lab" }, grade, "sub_1", "최신 제출");
    assert.doesNotMatch(html, /secret-test|secret-feedback|secret.cpp|10 \/ 10/);
  }
});
test("invalid score, zero max and inconsistent criteria cannot imply completion", () => {
  for (const score of [undefined, NaN, Infinity, -1, 11]) assert.equal(summarizeResult({ ...full, score }).headline, "결과 확인이 필요합니다");
  assert.equal(summarizeResult({ ...full, score: 0, maxScore: 0 }).percent, undefined);
  assert.equal(summarizeResult({ ...full, rubric: [{ name: "compile", score: 0, maxScore: 2 }] }).headline, "결과 확인이 필요합니다");
  assert.equal(summarizeResult({ ...full, score: 0 }).headline, "수정이 필요합니다");
});
test("server text is inert escaped HTML, and historical receipt is explicit", () => {
  const html = resultHtml({ id: "a", title: "<img src=x onerror=alert(1)>" }, {
    ...full, rubric: [{ name: "<script>evil</script>", feedback: "<iframe src='https://evil'>" }],
    diagnostics: [{ path: "<bad>", message: "<svg onload=alert(1)>" }],
  }, "bsub_old", "과거 제출 기록");
  assert.doesNotMatch(html, /<script>|<iframe|<img|<svg/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /default-src 'none'/);
  assert.match(html, /과거 제출 기록 · 접수번호 bsub_old/);
  assert.match(html, /var\(--vscode-editor-background\)/);
});
