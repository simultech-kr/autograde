import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import ts from "typescript";
import type { ResultRow } from "../src/lib/contracts/instructor.ts";
import { assignmentDownloadsURL, submissionDownloadURL } from "../src/features/results/downloads.ts";
import { sampleResults } from "./fixtures.ts";

// Node's native TypeScript runner does not transform JSX or CSS modules. Load
// only these repository presentation modules in memory; no app, network,
// browser globals, generated files, or new test dependency is needed.
const sourceRoot = fileURLToPath(new URL("../src/", import.meta.url));
const allowed = ["features/results/ResultsView.tsx", "components/InstructorUI.tsx", "features/results/query.ts", "features/results/downloads.ts"]
  .map(file => resolve(sourceRoot, file));
const require = createRequire(import.meta.url);
const cache = new Map<string, { exports: unknown }>();
function load(file: string): unknown {
  assert.ok(allowed.includes(file), `Unexpected presentation dependency: ${file}`);
  if (cache.has(file)) return cache.get(file)!.exports;
  const module = { exports: {} };
  cache.set(file, module);
  const compiled = ts.transpileModule(readFileSync(file, "utf8"), {
    fileName: file,
    compilerOptions: { jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true },
  }).outputText;
  const localRequire = (name: string): unknown => {
    if (name.endsWith(".module.css")) return { __esModule: true, default: new Proxy({}, { get: (_target, key) => String(key) }) };
    if (name.startsWith(".")) {
      const target = allowed.find(candidate => candidate === resolve(dirname(file), `${name}.ts`) || candidate === resolve(dirname(file), `${name}.tsx`));
      assert.ok(target, `Unexpected local dependency: ${name}`);
      return load(target);
    }
    assert.equal(name, "react/jsx-runtime");
    return require(name);
  };
  new Function("require", "module", "exports", compiled)(localRequire, module, module.exports);
  return module.exports;
}
const { AssignmentDownloadsLink, ResultsTable, SummaryCards, resultPresentation, refreshPresentation } = load(allowed[0]) as typeof import("../src/features/results/ResultsView");
const row = (patch: Partial<ResultRow> = {}): ResultRow => ({ ...sampleResults.rows[0], ...patch });
const render = (value: ResultRow) => renderToStaticMarkup(createElement(ResultsTable, { rows: [value] }));
const renderCourse = (value: ResultRow, courseKey = sampleResults.course_key) =>
  renderToStaticMarkup(createElement(ResultsTable, { rows: [value], courseKey }));
const cell = (html: string, testId: string) => {
  const match = html.match(new RegExp(`data-testid="${testId}"[^>]*>([\\s\\S]*?)</td>`));
  assert.ok(match, `Missing ${testId} cell`);
  return match[1];
};

test("summary separates student headcount from five latest-state metrics and explains overlapping totals", () => {
  const html = renderToStaticMarkup(createElement(SummaryCards, { summary: sampleResults.summary }));
  assert.equal((html.match(/class="metric"/g) || []).length, 5);
  assert.match(html, /제출한 학생/);
  assert.match(html, /3<small>명/);
  assert.match(html, /수강생 4명 중 · 중복 제외/);
  assert.match(html, /접수·채점 중/);
  assert.match(html, /아직 점수가 확정되지 않은 제출/);
  assert.match(html, /완료와 중복 가능/);
  assert.match(html, /만점 미달 포함/);
});

test("zero and partial scores remain current results, not a successful full-score badge or previous best", () => {
  for (const score of [0, 45]) {
    // The presentation stays safe even if a server omits the attention flag.
    const value = row({ score, attention: false });
    const html = render(value);
    assert.equal(resultPresentation(value).tone, "warning");
    assert.match(html, /data-tone="warning"/);
    assert.match(html, /채점 완료 · 보완 필요/);
    assert.match(cell(html, "current-score"), new RegExp(`<strong>${score}</strong>`));
    assert.doesNotMatch(cell(html, "current-score"), /<strong>100<\/strong>/);
    assert.match(cell(html, "previous-best"), /<strong>100<\/strong>/);
    assert.match(html, /최신 제출 점수/);
    assert.match(html, /현재 제출 제외/);
  }
});

test("full score is successful only for completed grades without another attention flag", () => {
  for (const state of ["graded", "published"]) {
    const value = row({ state, score: 100, attention: false });
    assert.equal(resultPresentation(value).tone, "success");
    assert.match(cell(render(value), "current-score"), /만점/);
    assert.equal(resultPresentation({ ...value, attention: true }).tone, "warning");
  }
});

test("fractional marks retain their precision rather than becoming a rounded zero", () => {
  const html = render(row({ score: 0.0001, max_score: 0.1234, previous_best: null }));
  assert.match(cell(html, "current-score"), /<strong>0\.0001<\/strong>/);
  assert.match(cell(html, "current-score"), /0\.1234점/);
});

test("pending stages do not display a current or stale grade, while previous best stays separate", () => {
  for (const state of ["received", "accepted", "queued", "running"]) {
    const value = row({ state, score: 75, max_score: 100, status_label: "채점 대기" });
    const html = render(value);
    assert.equal(resultPresentation(value).tone, "info");
    assert.equal(resultPresentation(value).scored, false);
    assert.match(cell(html, "current-score"), /점수 미확정/);
    assert.doesNotMatch(cell(html, "current-score"), /<strong>/);
    assert.match(cell(html, "previous-best"), /<strong>100<\/strong>/);
  }
});

test("processing failures are errors rather than a zero score or partial-score warning", () => {
  for (const state of ["rejected", "infra_failed", "assessment_failed", "failed"]) {
    const value = row({ state, score: null, max_score: null, status_label: "처리 오류" });
    const html = render(value);
    assert.equal(resultPresentation(value).tone, "error");
    assert.match(html, /data-tone="error"/);
    assert.match(cell(html, "current-score"), /오류 · 이력 확인/);
    assert.doesNotMatch(cell(html, "current-score"), /<strong>0<\/strong>/);
  }
});

test("unknown, unsubmitted, and incomplete grades never imply a confirmed score", () => {
  assert.deepEqual(resultPresentation(row({ submission_id: null })), { tone: "neutral", label: "미제출", scored: false, note: "제출 없음" });
  for (const value of [row({ state: "unexpected", score: 100 }), row({ score: null, max_score: null })]) {
    assert.equal(resultPresentation(value).tone, "warning");
    assert.equal(resultPresentation(value).scored, false);
    assert.match(cell(render(value), "current-score"), /결과 확인 필요/);
  }
});

test("row context, time semantics, mobile labels and scoped review link stay available", () => {
  const value = row({ detail_url: "/courses/come2201/instructor/submissions/sample-submission-2" });
  const html = render(value);
  assert.match(html, /1행 = 학생 1명 × 과제 1개/);
  assert.match(html, /role="rowheader" data-label="학생"/);
  assert.match(html, /data-label="최신 제출 점수"/);
  assert.match(html, /data-label="이전 최고점 \(현재 제출 제외\)"/);
  assert.match(html, /dateTime="2026-10-10T04:48:00Z"/i);
  assert.match(html, /href="\/courses\/come2201\/instructor\/submissions\/sample-submission-2"/);
  assert.match(html, /이력·코드 확인/);
  assert.match(html, /누적 제출 2회/);
});

test("submission downloads target the displayed receipt, independently of history, previous best or grade state", () => {
  for (const state of ["received", "queued", "failed", "published"]) {
    const value = row({ state, submission_id: "bsub_displayed-2", detail_url: "/courses/come2201/instructor/students/SAMPLE-001/submissions?assignment_id=observer" });
    const html = renderCourse(value);
    assert.match(html, /href="\/courses\/come2201\/instructor\/submissions\/bsub_displayed-2\/download\.zip" download=""/);
    assert.match(html, /제출 ZIP 받기/);
    assert.match(html, /SAMPLE-001, Week04-Problem01, 현재 표시된 제출/);
    assert.doesNotMatch(html, /sample-submission-1\/download|latest\/download/);
    assert.match(html, /이력·코드 확인/);
  }
  assert.match(renderCourse(row({ submission_id: "bsub_displayed-2", detail_url: null })), /data-testid="submission-download"/);
  assert.doesNotMatch(renderCourse(row({ submission_id: null })), /data-testid="submission-download"/);
  assert.doesNotMatch(render(row()), /data-testid="submission-download"/); // Synthetic preview.
});

test("download routes reject cross-course paths, traversal, encoded separators and IDs outside the instructor routes", () => {
  for (const course of ["", "../other", "come2201/instructor/other", "//example.test", "come2201%2fother", "come2201\n", "c".repeat(97)]) {
    assert.equal(submissionDownloadURL(course, "bsub_2"), null);
    assert.equal(assignmentDownloadsURL(course, "basn_1"), null);
    assert.doesNotMatch(renderCourse(row(), course), /data-testid="submission-download"/);
  }
  for (const id of ["", ".", "..", "../other", "x/y", "x\\y", "x%2fother", "x%252fother", "x?next=other", "x#other", "https://example.test", "x\n", "x\r", "x\0", "bsub_A2._~-"]) {
    assert.equal(submissionDownloadURL("come2201", id), null);
    assert.equal(assignmentDownloadsURL("come2201", id), null);
    assert.doesNotMatch(renderCourse(row({ submission_id: id })), /data-testid="submission-download"/);
  }
  for (const id of ["sample-submission-2", "bsub_"]) assert.equal(submissionDownloadURL("come2201", id), null);
  assert.equal(submissionDownloadURL("course_01-a", "bsub_A2_-"), "/courses/course_01-a/instructor/submissions/bsub_A2_-/download.zip");
});

test("a known selected assignment has one scoped navigation link, including before any submission", () => {
  const assignment = { ...sampleResults.assignments[0], submitted: 0 };
  const html = renderToStaticMarkup(createElement(AssignmentDownloadsLink, { courseKey: "come2201", assignment }));
  assert.match(html, /href="\/courses\/come2201\/instructor\/assignments\/observer\/downloads"/);
  assert.match(html, /등록 파일·제출물 다운로드/);
  assert.match(html, /Observer · Decorator 패턴 구현/);
  assert.doesNotMatch(html, / download=/);
  assert.equal(renderToStaticMarkup(createElement(AssignmentDownloadsLink, { courseKey: "come2201" })), "");
  assert.equal(renderToStaticMarkup(createElement(AssignmentDownloadsLink, { courseKey: "come2201", assignment: { ...assignment, assignment_id: "../other" } })), "");
});

test("untrusted row text remains escaped and empty filtered results remain explicit", () => {
  const html = render(row({ student_name: "<script>name</script>", title: "<img src=x>" }));
  assert.doesNotMatch(html, /<script>|<img/);
  assert.match(html, /&lt;script&gt;/);
  const empty = renderToStaticMarkup(createElement(ResultsTable, { rows: [] }));
  assert.match(empty, /표시할 결과가 없습니다/);
  assert.doesNotMatch(empty, /<table/);
});

const fresh = { changed: false, paused: false, automatic: true, failed: false, loading: false, lastSuccess: "2026-10-10T04:50:00Z" };
test("refresh labels describe polling mode, not an unverified persistent connection", () => {
  assert.deepEqual(refreshPresentation(fresh), { tone: "info", label: "자동 확인 켜짐" });
  assert.deepEqual(refreshPresentation({ ...fresh, lastSuccess: "", loading: true }), { tone: "info", label: "첫 결과 확인 중" });
  assert.equal(refreshPresentation({ ...fresh, lastSuccess: "" }).label, "서버 응답 확인 대기");
  assert.equal(refreshPresentation({ ...fresh, paused: true }).label, "자동 확인 일시 정지");
  assert.equal(refreshPresentation({ ...fresh, automatic: false }).label, "수동 확인 모드");
});

test("stale results and failures stay visible even with manual mode or paused polling", () => {
  assert.deepEqual(refreshPresentation({ ...fresh, failed: true, automatic: false }), { tone: "error", label: "조회 실패 · 마지막 확인 결과 표시" });
  assert.equal(refreshPresentation({ ...fresh, failed: true, paused: true }).tone, "error");
  assert.equal(refreshPresentation({ ...fresh, failed: true, lastSuccess: "" }).label, "조회 실패 · 결과 확인 필요");
  assert.deepEqual(refreshPresentation({ ...fresh, changed: true }), { tone: "warning", label: "새 결과 반영 필요" });
});
