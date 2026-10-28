import { useState } from "react";
import s from "./portal-preview.module.css";

const steps = ["제출 접수", "채점 대기", "채점 중", "결과 공개"];
export function SubmissionPreview() {
  const [state, setState] = useState("published");
  const index = state === "received" ? 0 : state === "queued" ? 1 : state === "running" ? 2 : state === "published" ? 3 : -1;
  return <><label className={s.smallSelect}>제출 상태 체험<select value={state} onChange={event => setState(event.target.value)}><option value="published">결과 공개</option><option value="received">재제출 접수</option><option value="queued">채점 대기</option><option value="running">채점 중</option><option value="failed">처리 확인 필요</option></select></label>
    <ol className={s.submissionSteps} aria-label="현재 제출 처리 단계">{steps.map((step, i) => <li key={step} data-current={i === index}>{step}</li>)}</ol>
    <p className={s.muted}>이번 제출 · 버전 {state === "published" ? 3 : 4} · 접수번호 DEMO-{state === "published" ? "003" : "004"}</p>
    <div className={s.scoreComparison} aria-live="polite"><div><span>이번 제출 점수</span><strong>{state === "published" ? <>80 <small>/ 100</small></> : <span className={s.pendingScore}>점수 미확정</span>}</strong><p>{state === "published" ? "10.23 13:42 · 보완 필요" : state === "failed" ? "처리 확인 필요" : steps[index]}</p></div><div><span>이전 제출 중 최고점</span><strong>95 <small>/ 100</small></strong><p>현재 제출 제외</p></div></div>
    <div className={s.hint}><div><strong>{state === "published" ? "다음 수정 사항" : state === "failed" ? "교수자에게 확인 요청" : "이번 제출을 처리하고 있습니다."}</strong><p>{state === "published" ? "파일을 열 수 없는 경우의 오류 처리를 확인하세요. 표준 오류 출력과 종료 코드를 함께 점검하세요." : state === "failed" ? "접수번호와 오류 상태를 교수자에게 알려 주세요. 이전 최고점은 이번 제출의 결과가 아닙니다." : "다시 제출하지 않아도 됩니다. 채점 후 공개된 점수를 확인하세요. 이전 점수로 현재 결과를 대신 표시하지 않습니다."}</p></div></div>
  </>;
}
