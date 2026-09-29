import { positiveResultPosition, safeResultPath } from "./helpers";
import type { Assignment, GradeResult, ResultDiagnostic, RubricItem } from "./types";

const labels: Record<string, string> = {
  received: "접수됨 · 서버 검증 대기", accepted: "채점 대기", queued: "채점 대기", running: "채점 중",
  graded: "채점 완료 · 결과 공개 대기", published: "결과 공개됨", rejected: "제출 검증 거절",
  result_pending: "채점 완료 · 결과 준비 중",
  infra_failed: "채점 환경 오류", assessment_failed: "채점 작업 실패", failed: "채점 작업 실패",
};
const valid = (score?: number, max?: number): boolean => Number.isFinite(score) && Number.isFinite(max)
  && max! > 0 && score! >= 0 && score! <= max!;
const escape = (text: unknown): string => String(text ?? "").replace(/[&<>"']/g, char =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
const criterionTitles: Record<string, string> = { compile: "컴파일", execution: "프로그램 실행", output: "출력 결과", correctness: "정확성" };
const title = (key: string): string => criterionTitles[key]
  ?? (key.startsWith("case_") ? `테스트 ${key.slice(5)}` : key);

const statusLabels = { passed: "충족", partial: "부분 충족 · 수정 필요", failed: "수정 필요",
  blocked: "선행 단계 해결 후 재검사", unknown: "참고 · 판정 없음" };

function criterionStatus(item: RubricItem): keyof typeof statusLabels {
  if (item.status === "blocked") return "blocked";
  // A declared pass must never conceal a lower score. Explicit failures also
  // remain visible when the score and status disagree.
  if (item.status === "failed" || item.status === "partial") return item.status;
  if (valid(item.score, item.maxScore)) return item.score === item.maxScore ? "passed" : item.score! > 0 ? "partial" : "failed";
  if (item.score === undefined && item.maxScore === undefined && item.status === "passed") return "passed";
  return "unknown";
}

function scoreStatusMismatch(item: RubricItem): boolean {
  if (!valid(item.score, item.maxScore)) return false;
  switch (item.status) {
    case "passed": return item.score !== item.maxScore;
    case "partial": return item.score === 0 || item.score === item.maxScore;
    case "failed":
    case "blocked": return item.score !== 0;
    default: return false;
  }
}

export function feedbackLocation(item: { readonly path?: string; readonly line?: number; readonly column?: number }): string | undefined {
  const path = safeResultPath(item.path);
  if (!path) return undefined;
  const line = positiveResultPosition(item.line);
  const column = line ? positiveResultPosition(item.column) : undefined;
  return `${path}${line ? `:${line}${column ? `:${column}` : ""}` : ""}`;
}

export function diagnosticText(item: ResultDiagnostic): string {
  const location = feedbackLocation(item);
  return `${location ? `${location} — ` : ""}${item.message}`;
}

export function summarizeResult(result: GradeResult) {
  const published = result.state === "published";
  const items = published ? result.rubric.map(item => {
    const status = criterionStatus(item);
    const mismatch = scoreStatusMismatch(item);
    return { ...item, title: title(item.name), status, scoreStatusMismatch: mismatch,
      label: `${statusLabels[status]}${mismatch ? " · 점수·판정 확인 필요" : ""}`,
      comparable: valid(item.score, item.maxScore), needsWork: status === "failed" || status === "partial" };
  }).sort((a, b) => {
    const rank = { failed: 0, partial: 0, blocked: 1, unknown: 2, passed: 3 };
    return rank[a.status] - rank[b.status];
  }) : [];
  const passed = items.filter(item => item.status === "passed").length;
  const needsWork = items.filter(item => item.needsWork).length;
  const blocked = items.filter(item => item.status === "blocked").length;
  const unknown = items.filter(item => item.status === "unknown").length;
  const full = valid(result.score, result.maxScore) && result.score === result.maxScore;
  const inconsistent = items.some(item => item.scoreStatusMismatch) || (full && (needsWork > 0 || blocked > 0));
  const known = published && valid(result.score, result.maxScore) && !inconsistent;
  const headline = !published ? labels[result.state] ?? "상태 확인 필요" : !known ? "결과 확인이 필요합니다" :
    full ? "자동채점 총점 기준 충족" : "수정이 필요합니다";
  const next = !published ? (result.state === "graded" ? "교수자가 결과를 공개한 뒤 다시 확인하세요. 다시 제출할 필요는 없습니다." :
    ["rejected", "infra_failed", "assessment_failed", "failed"].includes(result.state) ? "제출 안내를 확인하고 접수번호와 함께 교수자에게 문의하세요." :
    "접수된 제출은 서버에서 처리됩니다. 잠시 뒤 최신 결과를 확인하세요.") : inconsistent ?
    "점수와 항목 판정이 서로 맞지 않습니다. 공개된 피드백을 참고하고 교수자에게 확인하세요." : !known ?
    "점수 또는 항목별 결과가 충분하지 않습니다. 교수자에게 확인하세요." : full ?
    "자동채점은 만점입니다. 보고서·설계 설명 등 별도 요구사항과 제출한 코드가 최종본인지 확인하세요." :
    "수정 필요 항목과 공개된 피드백을 확인하고 코드 수정 → 모두 저장 → 다시 제출하세요. 상세 피드백이 없다면 교수자에게 문의하세요.";
  return { headline, next, items, percent: published && valid(result.score, result.maxScore) ? result.score! / result.maxScore! * 100 : undefined,
    score: published ? valid(result.score, result.maxScore) ? `${result.score} / ${result.maxScore}점` : "점수 정보 확인 필요" : "점수 미공개",
    summary: !published ? "아직 완성 여부를 판단할 수 없습니다." : items.length ?
      `공개된 채점 항목 ${items.length}개 중 ${passed}개 충족 · ${needsWork}개 수정 필요${blocked ? ` · ${blocked}개 선행 단계 해결 후 재검사` : ""}${unknown ? ` · ${unknown}개 판정 없음` : ""}` :
      "항목별 판정은 제공되지 않았습니다. 총점만으로 표시하며 교수자의 최종 평가를 대신하지 않습니다.",
  };
}

export function resultHtml(assignment: Assignment, result: GradeResult, receipt: string, context: string,
  controls: { readonly canRefresh?: boolean; readonly canPause?: boolean; readonly notice?: string } = {},
): string {
  const view = summarizeResult(result);
  const diagnostics = result.state === "published" ? result.diagnostics : [];
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Autograde 채점 결과</title><style>
body{box-sizing:border-box;width:100%;font:var(--vscode-font-size)/1.6 var(--vscode-font-family);color:var(--vscode-editor-foreground);background:var(--vscode-editor-background);padding:24px;max-width:850px;margin:auto}
h1{font-size:26px;margin:8px 0}.score{font-size:36px;font-weight:700;margin:8px 0}.card{border:1px solid var(--vscode-panel-border,var(--vscode-editor-foreground));padding:18px;margin:16px 0;border-radius:8px}
.hint{color:var(--vscode-descriptionForeground)}h2{font-size:18px}h3{font-size:16px}p,pre{overflow-wrap:anywhere}pre{white-space:pre-wrap}.feedback{white-space:pre-wrap}progress{width:100%;height:12px;accent-color:var(--vscode-progressBar-background)}
summary{cursor:pointer;padding:10px 0;overflow-wrap:anywhere}:focus-visible{outline:2px solid var(--vscode-focusBorder)}.needs-work{border-left:4px solid var(--vscode-editorWarning-foreground)}.blocked{border-left:4px solid var(--vscode-descriptionForeground)}.criterion-score{display:block;margin-left:18px;font-weight:normal}.criterion.card,.previous-best.card{padding:8px 18px}.criterion h3{margin-bottom:4px}.criterion p{margin-top:4px}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0}.action{display:inline-block;padding:8px 12px;background:var(--vscode-button-background);color:var(--vscode-button-foreground);border-radius:4px;text-decoration:none}.action.secondary{background:var(--vscode-button-secondaryBackground);color:var(--vscode-button-secondaryForeground)}
@media(max-width:600px){body{padding:16px}}
</style></head><body><p>${escape(assignment.courseLabel)} · ${escape(assignment.title)}</p><p class="hint">${escape(context)} · 접수번호 ${escape(receipt)}</p>
${controls.notice ? `<p role="status" aria-live="polite">${escape(controls.notice)}</p>` : ""}
${controls.canRefresh || controls.canPause ? `<div class="actions">${controls.canRefresh ? '<a class="action" href="command:autograde.refreshDisplayedResult">결과 다시 확인</a>' : ""}${controls.canPause ? '<a class="action secondary" href="command:autograde.pauseDisplayedResult">자동 확인 중지</a>' : ""}</div>` : ""}
<section class="card" aria-label="채점 결과 요약"><h1>${escape(view.headline)}</h1><p class="score">${escape(view.score)}</p>
${view.percent === undefined ? "" : `<progress max="100" value="${view.percent}" aria-label="자동채점 총점 달성률"></progress>`}
<p>${escape(view.summary)}</p><h2>다음 할 일</h2><p>${escape(view.next)}</p></section>
${view.items.length ? '<h2>항목별 결과와 수정 가이드</h2>' : ""}
${view.items.map(item => `<details class="card criterion ${item.needsWork ? "needs-work" : item.status === "blocked" ? "blocked" : ""}"${item.status === "passed" ? "" : " open"}>
<summary><strong>${escape(item.label)} · ${escape(item.title)}</strong><span class="criterion-score">${item.comparable ? `${item.score} / ${item.maxScore}점` : "배점 정보 없음"}</span></summary>
${item.scoreStatusMismatch ? '<p class="hint">이 항목의 점수와 판정이 서로 맞지 않습니다. 교수자에게 확인하세요.</p>' : ""}
${item.status === "blocked" ? '<p class="hint">이 항목에는 아직 독립적인 통과·실패 판정이 없습니다. 아래 공개된 안내를 확인한 뒤 다시 제출하세요.</p>' : ""}
${feedbackLocation(item) ? `<p>소스 위치: <code>${escape(feedbackLocation(item))}</code></p>` : ""}
<h3>확인된 현상</h3><p class="feedback">${escape(item.feedback?.trim() ? item.feedback : "공개된 상세 피드백이 없습니다.")}</p>
<h3>수정 가이드</h3><p class="feedback">${escape(item.hint?.trim() ? item.hint : "공개된 개별 수정 가이드가 없습니다. 과제 설명과 공개된 피드백을 확인하세요.")}</p></details>`).join("")}
${result.previousBest ? `<details class="card previous-best" aria-label="이전 최고점"><summary>이번 제출 이전 최고점: ${escape(result.previousBest.score)} / ${escape(result.previousBest.maxScore)}점</summary><p>${escape(result.previousBest.receivedAt)} · 접수번호 ${escape(result.previousBest.submissionId)}</p></details>` : '<p class="hint">이전 공개 최고점 정보 없음</p>'}
${diagnostics.length ? `<section class="card"><h2>추가 피드백 (${diagnostics.length}건)</h2>${diagnostics.slice(0, 5).map(item => `<p class="feedback">${escape(diagnosticText(item))}</p>`).join("")}${diagnostics.length > 5 ? '<p class="hint">나머지 피드백은 아래 전체 진단에서 확인하세요.</p>' : ""}</section>` : ""}
<details><summary>제출 식별값·전체 진단 (복사 가능)</summary><pre>${escape(`접수번호: ${receipt}\n소스: ${result.sourceDigest ?? result.headSha ?? "제공되지 않음"}\n` + diagnostics.map(diagnosticText).join("\n"))}</pre></details>
<p class="hint">이 화면은 조회 시점의 결과입니다. 코드를 수정한 뒤에는 저장·재제출하고 새 접수번호의 결과를 확인하세요.</p></body></html>`;
}
