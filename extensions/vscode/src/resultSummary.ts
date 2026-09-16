import type { Assignment, GradeResult } from "./types";

const labels: Record<string, string> = {
  received: "접수됨 · 서버 검증 대기", accepted: "채점 대기", queued: "채점 대기", running: "채점 중",
  graded: "채점 완료 · 결과 공개 대기", published: "결과 공개됨", rejected: "제출 검증 거절",
  infra_failed: "채점 환경 오류", assessment_failed: "채점 작업 실패", failed: "채점 작업 실패",
};
const valid = (score?: number, max?: number): boolean => Number.isFinite(score) && Number.isFinite(max)
  && max! > 0 && score! >= 0 && score! <= max!;
const escape = (text: unknown): string => String(text ?? "").replace(/[&<>"']/g, char =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]!);
const criterionTitles: Record<string, string> = { compile: "컴파일", execution: "프로그램 실행", output: "출력 결과", correctness: "정확성" };
const title = (key: string): string => criterionTitles[key]
  ?? (key.startsWith("case_") ? `테스트 ${key.slice(5)}` : key);

export function summarizeResult(result: GradeResult) {
  const published = result.state === "published";
  const items = published ? result.rubric.map(item => ({ ...item, title: title(item.name),
    comparable: valid(item.score, item.maxScore),
    needsWork: valid(item.score, item.maxScore) && item.score! < item.maxScore!,
  })).sort((a, b) => Number(b.needsWork) - Number(a.needsWork)) : [];
  const scored = items.filter(item => item.comparable);
  const full = valid(result.score, result.maxScore) && result.score === result.maxScore;
  const inconsistent = full && items.some(item => item.needsWork);
  const known = published && valid(result.score, result.maxScore) && !inconsistent;
  const headline = !published ? labels[result.state] ?? "상태 확인 필요" : !known ? "결과 확인이 필요합니다" :
    full ? "자동채점 총점 기준 충족" : "수정이 필요합니다";
  const next = !published ? (result.state === "graded" ? "교수자가 결과를 공개한 뒤 다시 확인하세요. 다시 제출할 필요는 없습니다." :
    ["rejected", "infra_failed", "assessment_failed", "failed"].includes(result.state) ? "제출 안내를 확인하고 접수번호와 함께 교수자에게 문의하세요." :
    "접수된 제출은 서버에서 처리됩니다. 잠시 뒤 최신 결과를 확인하세요.") : !known ?
    "점수 또는 항목별 결과가 충분하지 않습니다. 교수자에게 확인하세요." : full ?
    "자동채점은 만점입니다. 보고서·설계 설명 등 별도 요구사항과 제출한 코드가 최종본인지 확인하세요." :
    "수정 필요 항목과 공개된 피드백을 확인하고 코드 수정 → 모두 저장 → 다시 제출하세요. 상세 피드백이 없다면 교수자에게 문의하세요.";
  return { headline, next, items, percent: published && valid(result.score, result.maxScore) ? result.score! / result.maxScore! * 100 : undefined,
    score: published ? valid(result.score, result.maxScore) ? `${result.score} / ${result.maxScore}점` : "점수 정보 확인 필요" : "점수 미공개",
    summary: !published ? "아직 완성 여부를 판단할 수 없습니다." : scored.length ?
      `공개된 채점 항목 ${scored.length}개 중 ${scored.filter(item => !item.needsWork).length}개 충족 · ${scored.filter(item => item.needsWork).length}개 수정 필요` :
      "항목별 판정은 제공되지 않았습니다. 총점만으로 표시하며 교수자의 최종 평가를 대신하지 않습니다.",
  };
}

export function resultHtml(assignment: Assignment, result: GradeResult, receipt: string, context: string): string {
  const view = summarizeResult(result);
  const diagnostics = result.state === "published" ? result.diagnostics : [];
  const diagnostic = (item: GradeResult["diagnostics"][number]) => `${item.path}${item.line ? `:${item.line}` : ""} ${item.message}`;
  return `<!doctype html><html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Autograde 채점 결과</title><style>
body{font:var(--vscode-font-size)/1.6 var(--vscode-font-family);color:var(--vscode-editor-foreground);background:var(--vscode-editor-background);padding:24px;max-width:850px;margin:auto}
h1{font-size:26px;margin:8px 0}.score{font-size:36px;font-weight:700;margin:8px 0}.card{border:1px solid var(--vscode-panel-border,var(--vscode-editor-foreground));padding:18px;margin:16px 0;border-radius:8px}
.hint{color:var(--vscode-descriptionForeground)}h2{font-size:18px}h3{font-size:16px}p,pre{overflow-wrap:anywhere}pre{white-space:pre-wrap}progress{width:100%;height:12px;accent-color:var(--vscode-progressBar-background)}
summary{cursor:pointer;padding:10px 0}:focus-visible{outline:2px solid var(--vscode-focusBorder)}.needs-work{border-left:4px solid var(--vscode-editorWarning-foreground)}
</style></head><body><p>${escape(assignment.courseLabel)} · ${escape(assignment.title)}</p><p class="hint">${escape(context)} · 접수번호 ${escape(receipt)}</p>
<section class="card" aria-label="채점 결과 요약"><h1>${escape(view.headline)}</h1><p class="score">${escape(view.score)}</p>
${view.percent === undefined ? "" : `<progress max="100" value="${view.percent}" aria-label="자동채점 총점 달성률"></progress>`}
<p>${escape(view.summary)}</p><h2>다음 할 일</h2><p>${escape(view.next)}</p></section>
${view.items.map(item => `<section class="card ${item.needsWork ? "needs-work" : ""}"><h3>${item.comparable ? item.needsWork ? "수정 필요" : "충족" : "참고 · 판정 없음"} · ${escape(item.title)}</h3>
<p>${item.comparable ? `${item.score} / ${item.maxScore}점` : "배점 정보 없음"}</p><p>${escape(item.feedback ?? "공개된 상세 피드백이 없습니다.")}</p></section>`).join("")}
${diagnostics.length ? `<section class="card"><h2>추가 피드백 (${diagnostics.length}건)</h2>${diagnostics.slice(0, 5).map(item => `<p>${escape(diagnostic(item))}</p>`).join("")}</section>` : ""}
<details><summary>제출 식별값·전체 진단 (복사 가능)</summary><pre>${escape(`접수번호: ${receipt}\n소스: ${result.sourceDigest ?? result.headSha ?? "제공되지 않음"}\n` + diagnostics.map(diagnostic).join("\n"))}</pre></details>
<p class="hint">이 화면은 조회 시점의 결과입니다. 코드를 수정한 뒤에는 저장·재제출하고 새 접수번호의 결과를 확인하세요.</p></body></html>`;
}
