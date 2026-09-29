import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { diagnosticText, feedbackLocation, resultHtml, summarizeResult } from "../resultSummary";
import type { GradeResult } from "../types";

const full: GradeResult = { state: "published", score: 10, maxScore: 10, rubric: [], diagnostics: [] };
test("current result and prior public best are separate, including pending current submission", () => {
  const best = {submissionId:"bsub_old", receivedAt:"2026-09-17T00:00:00Z", score:8, maxScore:10};
  const current = {...full, score:3, previousBest:best};
  assert.equal(summarizeResult(current).score, "3 / 10점");
  const html = resultHtml({id:"a", title:"Lab"}, current, "bsub_new", "이번 제출");
  assert.match(html, /3 \/ 10점/); assert.match(html, /8 \/ 10점/); assert.match(html, /이번 제출 이전 최고점/);
  assert.equal(summarizeResult({...current, state:"queued"}).score, "점수 미공개");
});
test("current corrections precede the collapsed previous-best score with separate receipt details", () => {
  const html = resultHtml({id: "a", title: "Lab"}, { ...full, score: 3,
    rubric: [{name: "output", score: 0, maxScore: 7, feedback: "현재 제출 출력 확인", hint: "줄바꿈 수정"}],
    previousBest: {submissionId: "bsub_old", receivedAt: "2026-09-17T00:00:00Z", score: 8, maxScore: 10},
  }, "bsub_new", "이번 제출");
  assert.ok(html.indexOf("줄바꿈 수정") < html.indexOf('class="card previous-best"'));
  assert.match(html, /<details class="card previous-best" aria-label="이전 최고점"><summary>이번 제출 이전 최고점: 8 \/ 10점<\/summary><p>2026-09-17T00:00:00Z · 접수번호 bsub_old<\/p><\/details>/);
  assert.match(html, /이번 제출 · 접수번호 bsub_new/);
  assert.match(html, /body\{box-sizing:border-box;width:100%/);
  assert.match(html, /@media\(max-width:600px\)\{body\{padding:16px\}\}/);
});
test("new bundle receipt replaces result panel and latest lookup refreshes server identity", () => {
  const source = readFileSync(resolve(__dirname, "../../src/extension.ts"), "utf8");
  const submit = source.split("async function submitCurrentBundle")[1]?.split("async function viewSubmissionHistory")[0];
  assert.ok(submit);
  assert.match(submit, /const resultScope = resultUi.monitor.begin\(\);[\s\S]*clearResultPanel\(\)/);
  assert.match(submit, /watchSubmissionResult\(resultUi, resultScope, studentState, client, assignment, submission, "이번 제출"\)/);
  const latest = source.split("async function viewLatestResult")[1];
  assert.ok(latest);
  assert.match(latest, /await client.getAssignments\(\)/);
  assert.match(latest, /fresh\?\.latestSubmission\?\.id/);
});
test("result panel is read-only and clears at authentication and address boundaries", () => {
  const panel = readFileSync(resolve(__dirname, "../../src/resultPanel.ts"), "utf8");
  const extension = readFileSync(resolve(__dirname, "../../src/extension.ts"), "utf8");
  assert.match(panel, /enableScripts: false/);
  assert.match(panel, /localResourceRoots: \[\]/);
  assert.match(extension, /\{ dispose: clearResultPanel \}/);
  assert.match(extension, /if \(!authenticated\) \{ resultMonitor.cancel\(\); clearResultPanel\(\); clearDownloadDiagnostic\(\); documentView.clear\(\); \}/);
  for (const start of ["new AuthenticationController", "new AssignmentClaimController", "const clearSessionUiForAddressChange"]) {
    assert.match(extension.slice(extension.indexOf(start), extension.indexOf(start) + 280), /clearResultPanel\(\)/);
  }
});
test("result refresh and pause controls use only fixed allowlisted commands and escaped notices", () => {
  const html = resultHtml({id: "a", title: "Lab"}, {...full, state: "queued"}, "new", "이번 제출",
    {canRefresh: true, canPause: true, notice: '<a href="command:evil">bad</a>'});
  assert.deepEqual([...html.matchAll(/href="command:([^"]+)"/g)].map(match => match[1]),
    ["autograde.refreshDisplayedResult", "autograde.pauseDisplayedResult"]);
  assert.match(html, /&lt;a href=&quot;command:evil&quot;&gt;/);
  assert.match(html, /role="status" aria-live="polite"/);
  assert.doesNotMatch(resultHtml({id: "a", title: "Lab"}, full, "old", "과거 제출 기록"), /href="command:/);
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

test("failed and partial criteria precede blocked criteria and successes with distinct verdict counts", () => {
  const grade: GradeResult = { ...full, score: 1, rubric: [
    { name: "compile", score: 1, maxScore: 1, status: "passed" },
    { name: "case_2", score: 0, maxScore: 3, status: "blocked" },
    { name: "case_1", score: 0, maxScore: 3, status: "failed", feedback: "출력 불일치\n공백이 다릅니다.", hint: "공백을 확인하세요.", path: "src/main.cpp", line: 8, column: 2 },
    { name: "style", score: 1, maxScore: 3, status: "partial" },
  ] };
  const view = summarizeResult(grade);
  assert.deepEqual(view.items.map(item => item.status), ["failed", "partial", "blocked", "passed"]);
  assert.equal(view.items[2]?.needsWork, false);
  assert.match(view.summary, /1개 충족 · 2개 수정 필요 · 1개 선행 단계 해결 후 재검사/);
  const html = resultHtml({ id: "a", title: "Lab" }, grade, "new", "이번 제출");
  assert.match(html, /<details class="card criterion needs-work" open>/);
  assert.match(html, /<details class="card criterion blocked" open>/);
  assert.match(html, /<details class="card criterion "><summary>|<details class="card criterion ">\n<summary>/);
  assert.match(html, /확인된 현상<\/h3><p class="feedback">출력 불일치\n공백이 다릅니다\./);
  assert.match(html, /수정 가이드<\/h3><p class="feedback">공백을 확인하세요\./);
  assert.match(html, /src\/main.cpp:8:2/);
  assert.match(html, /\.feedback\{white-space:pre-wrap\}/);
});

test("a passed status cannot hide a lower score, and blocked criteria cannot imply completion", () => {
  for (const criterion of [
    { name: "test", score: 0, maxScore: 2, status: "passed" as const },
    { name: "test", score: 2, maxScore: 2, status: "failed" as const },
    { name: "test", score: 2, maxScore: 2, status: "blocked" as const },
  ]) {
    const view = summarizeResult({ ...full, rubric: [criterion] });
    assert.equal(view.headline, "결과 확인이 필요합니다");
    assert.notEqual(view.items[0]?.status, "passed");
  }
  assert.equal(summarizeResult({ ...full, rubric: [{name: "test", score: -1, maxScore: 2, status: "passed"}] }).items[0]?.status, "unknown");
});

test("criterion score/status discrepancies are explicit even when the overall score is below full", () => {
  for (const criterion of [
    { name: "test", score: 2, maxScore: 2, status: "failed" as const },
    { name: "test", score: 0, maxScore: 2, status: "passed" as const },
    { name: "test", score: 2, maxScore: 2, status: "partial" as const },
    { name: "test", score: 1, maxScore: 2, status: "blocked" as const },
  ]) {
    const grade = { ...full, score: 5, rubric: [criterion] };
    const view = summarizeResult(grade);
    assert.equal(view.items[0]?.scoreStatusMismatch, true);
    assert.equal(view.headline, "결과 확인이 필요합니다");
    assert.match(view.next, /점수와 항목 판정이 서로 맞지 않습니다/);
    const html = resultHtml({id: "a", title: "Lab"}, grade, "new", "이번 제출");
    assert.match(html, /점수·판정 확인 필요/);
    assert.match(html, /이 항목의 점수와 판정이 서로 맞지 않습니다/);
  }
});

test("an explicitly passed unscored compile stage remains a valid compact success", () => {
  const grade: GradeResult = { ...full, rubric: [{ name: "compile", status: "passed", feedback: "컴파일에 성공했습니다." }] };
  const view = summarizeResult(grade);
  assert.equal(view.items[0]?.status, "passed");
  assert.equal(view.items[0]?.scoreStatusMismatch, false);
  assert.equal(view.headline, "자동채점 총점 기준 충족");
  const html = resultHtml({id: "a", title: "Lab"}, grade, "new", "이번 제출");
  assert.match(html, /<details class="card criterion ">\n<summary><strong>충족 · 컴파일/);
  assert.doesNotMatch(html, /점수·판정 확인 필요/);
});

test("missing feedback and score-only projections do not invent corrections or locations", () => {
  const html = resultHtml({ id: "a", title: "Lab" }, { ...full, score: 0,
    rubric: [{ name: "compile", score: 0, maxScore: 2 }], diagnostics: [{ message: "일반 피드백\n두 번째 줄" }],
  }, "new", "이번 제출");
  assert.match(html, /공개된 상세 피드백이 없습니다/);
  assert.match(html, /공개된 개별 수정 가이드가 없습니다/);
  assert.match(html, /일반 피드백\n두 번째 줄/);
  assert.doesNotMatch(html, /undefined|소스 위치:/);
  const scoreOnly = resultHtml({ id: "a", title: "Lab" }, { ...full, score: 5 }, "new", "이번 제출");
  assert.match(scoreOnly, /항목별 판정은 제공되지 않았습니다/);
  assert.doesNotMatch(scoreOnly, /항목별 결과와 수정 가이드|확인된 현상|추가 피드백/);
});

test("public corrective text and locations remain inert and invalid coordinates are omitted", () => {
  const html = resultHtml({ id: "a", title: "Lab" }, { ...full, rubric: [{
    name: "compile", feedback: "<script>bad()</script>\nline 2", hint: '<a href="command:evil">fix</a>',
    path: "src/<bad>.cpp", line: 2, column: 3,
  }] }, "new", "이번 제출");
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /&lt;a href=&quot;command:evil&quot;&gt;/);
  assert.match(html, /src\/&lt;bad&gt;.cpp:2:3/);
  assert.doesNotMatch(html, /<script>|<a href="command:evil"/);
  assert.equal(feedbackLocation({path: "../private.cpp", line: 2}), undefined);
  assert.equal(feedbackLocation({path: "main.cpp", line: 0, column: 3}), "main.cpp");
  assert.equal(feedbackLocation({path: "main.cpp", line: 2, column: 1.5}), "main.cpp:2");
  assert.equal(feedbackLocation({path: "main.cpp", line: 10_000_001, column: 3}), "main.cpp");
  assert.equal(feedbackLocation({path: "main.cpp", line: 2, column: 10_000_001}), "main.cpp:2");
  assert.equal(feedbackLocation({path: "main.cpp", line: 10_000_000, column: 10_000_000}), "main.cpp:10000000:10000000");
  assert.equal(feedbackLocation({path: "src/\u202emain.cpp", line: 2}), undefined);
  assert.equal(feedbackLocation({path: "x".repeat(513), line: 2}), undefined);
  assert.equal(diagnosticText({message: "일반 진단"}), "일반 진단");
});

test("all unpublished states hide corrective fields while retaining only the previous public best", () => {
  for (const state of ["received", "accepted", "queued", "running", "graded", "result_pending", "infra_failed", "assessment_failed", "failed", "rejected", "unknown"]) {
    const html = resultHtml({ id: "a", title: "Lab" }, { ...full, state,
      rubric: [{ name: "secret-name", feedback: "secret-feedback", hint: "secret-hint", path: "secret.cpp", line: 9, status: "failed" }],
      diagnostics: [{ message: "secret-general-diagnostic" }],
      previousBest: { submissionId: "old", receivedAt: "2026-09-17T00:00:00Z", score: 8, maxScore: 10 },
    }, "new", "이번 제출");
    assert.doesNotMatch(html, /secret-|secret.cpp|10 \/ 10점|확인된 현상/);
    assert.match(html, /이번 제출 이전 최고점/); assert.match(html, /8 \/ 10점/);
  }
  const extension = readFileSync(resolve(__dirname, "../../src/extension.ts"), "utf8");
  const render = extension.split("function renderResult")[1]!.split("function publishDiagnostics")[0]!;
  assert.match(render, /result.state === "published" && result.score !== undefined/);
  assert.match(render, /const view = summarizeResult\(result\)/);
  assert.match(render, /result.state === "published" && result.diagnostics.length/);
  const publish = extension.split("function publishDiagnostics")[1]!.split("function diagnosticSeverity")[0]!;
  assert.match(publish, /if \(!item.path\) continue;/);
});
