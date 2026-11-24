"use client";
import {useEffect, useRef, useState} from "react";
import {scoreText, timeText, validScore, type CourseData, type Receipt} from "./model";
import {scoreRows, scoreRowState} from "./scoreboard";
import s from "./student.module.css";
import w from "./workbench.module.css";
import {useStudentListHistory} from "./useStudentListHistory";
import {StudentGradebook} from "./StudentGradebook.tsx";
import {studentRequest} from "./client.ts";
import {criterionVerdict, feedbackPath, parseSubmissionFeedback, type SubmissionFeedback} from "./workbench.ts";

export function StudentScores({data, navigationScope}: {data: CourseData; navigationScope?: string}) {
  const {option: view, choose} = useStudentListHistory(navigationScope, "scores/" + data.course.course_key, ["practice", "gradebook"]);
  function setView(next: "practice" | "gradebook") {
    if (next === "gradebook") {
      const url = new URL(window.location.href);
      if (url.searchParams.has("assignment")) {
        url.searchParams.delete("assignment");
        window.history.replaceState(window.history.state, "", url);
      }
    }
    choose(next);
  }
  const rows = scoreRows(data.overview).sort((a,b)=>(b.receipt ? Date.parse(b.receipt.received_at) : 0) - (a.receipt ? Date.parse(a.receipt.received_at) : 0));
  const published = rows.filter(row => scoreRowState(row, Date.parse(data.server_time)).scored).length;
  return <><div className={w.resultSwitch} aria-label="성적 종류"><button aria-pressed={view === "practice"} onClick={()=>setView("practice")}>실습 제출 결과</button><button aria-pressed={view === "gradebook"} onClick={()=>setView("gradebook")}>시험·출석 성적</button></div>
    {view === "gradebook" ? <StudentGradebook publication={data.gradebook} /> : <section aria-labelledby="student-score-heading">
    <div className={s.sectionHeading}><div><h2 id="student-score-heading">최신 제출부터 확인하세요</h2><p className={s.muted}>이번 제출 점수 공개 {published}개 · 조회 중인 과제 {rows.length}개</p></div></div>
    {!rows.length && <p className={s.empty}>점수를 확인할 공개 과제가 없습니다.</p>}
    <div className={w.results}>{rows.map((row,index) => {
      const state = scoreRowState(row, Date.parse(data.server_time)), receipt = row.receipt;
      return <article className={w.result} key={row.assignment_id} data-score-assignment={row.assignment_id} data-newest={index === 0 && !!receipt} aria-labelledby={`score-title-${row.assignment_id}`}>
        <div className={w.resultHeading}><div><p className={s.eyebrow}>{index === 0 && receipt ? "가장 최근 제출 · " : ""}{row.assignment_key}</p><h3 id={`score-title-${row.assignment_id}`}>{row.title}</h3>{receipt && <p className={w.caption}>{timeText(receipt.received_at)} KST 제출 · 버전 {receipt.version}</p>}</div><span className={s.badge} data-tone={state.tone}>{state.label}</span></div>
        <dl className={w.scorePair}><div><dt>이번 제출 점수</dt><dd>{state.scored && receipt ? scoreText(receipt.score, receipt.max_score) : receipt ? "점수 미확정" : "—"}</dd></div>
          <div><dt>이전 제출 중 최고점</dt><dd>{receipt?.previous_best_id ? scoreText(receipt.previous_best_score, receipt.previous_best_max_score) : "—"}</dd><small>{receipt?.previous_best_at ? timeText(receipt.previous_best_at) + " KST" : "공개된 이전 점수 없음"}</small></div></dl>
        {!state.scored && <p>{state.hint}</p>}
        {state.scored && receipt && (receipt.feedback_policy === "score_only"
          ? <p className={w.resultNotice}><strong>총점만 공개</strong> · 이 제출의 평가 항목과 수정 힌트는 공개되지 않습니다.</p>
          : <FeedbackDetails key={`${receipt.submission_id}:${receipt.published_at}:${receipt.score}:${receipt.max_score}`} data={data} receipt={receipt} />)}
        {receipt && <p className={w.caption}>이전 최고점은 이번 제출을 제외한 기록입니다.{data.overview.history.some(item => item.submission_id === receipt.submission_id) && <> <a href={`#history-${receipt.submission_id}`}>이 제출의 코드 버전·접수 이력</a></>}</p>}
      </article>;
    })}</div>
    {(data.overview.assignments_truncated || data.overview.latest_truncated) && <p className={s.muted}>일부 과제만 표시합니다. 과제별 전체 제출 이력은 IDE에서 확인하세요.</p>}
  </section>}</>;
}

function FeedbackDetails({data, receipt}: {data:CourseData; receipt:Receipt}) {
  const [expanded, setExpanded] = useState(false), [loading, setLoading] = useState(false), [error, setError] = useState("");
  const [feedback, setFeedback] = useState<SubmissionFeedback | null>(null), controller = useRef<AbortController | null>(null);
  const context = `${data.student_key}:${data.csrf}:${data.course.course_key}:${receipt.submission_id}`;
  useEffect(()=>{setExpanded(false);setFeedback(null);setError("");setLoading(false);return ()=>controller.current?.abort();},[context]);
  async function read() {
    if (loading) return;
    setExpanded(true);
    if (feedback) return;
    const request = new AbortController(); controller.current?.abort(); controller.current = request; setLoading(true); setError("");
    try {
      const raw = await studentRequest(feedbackPath(data.course.course_key,receipt.submission_id),undefined,request.signal);
      const parsed = parseSubmissionFeedback(raw,data.course.course_key,data.student_key,receipt.submission_id,receipt.assignment_id);
      if (parsed.score !== receipt.score || parsed.max_score !== receipt.max_score || Date.parse(parsed.published_at) !== Date.parse(receipt.published_at!)) throw new Error("채점 결과가 갱신되었습니다. 수업 정보를 새로고침한 뒤 상세 결과를 다시 확인하세요.");
      if (!request.signal.aborted) setFeedback(parsed);
    } catch (failure) {if (!request.signal.aborted) setError(failure instanceof Error ? failure.message : "상세 결과를 확인하지 못했습니다. 다시 시도하세요.");}
    finally {if (!request.signal.aborted) setLoading(false);}
  }
  const fullScore = validScore(receipt.score,receipt.max_score) && receipt.score === receipt.max_score;
  const items = feedback?.criteria.map(item=>({...item,verdict:criterionVerdict(item)})).sort((a,b)=>a.verdict.rank-b.verdict.rank) || [];
  const needsWork = items.filter(item=>item.verdict.needsWork).length, passed = items.filter(item=>item.verdict.status === "passed").length, blocked = items.filter(item=>item.verdict.status === "blocked").length;
  const inconsistent = items.some(item=>item.verdict.mismatch) || fullScore && (needsWork > 0 || blocked > 0);
  const id = `feedback-${receipt.submission_id}`;
  return <div>
    <p className={w.caption}>{fullScore ? "자동채점 총점은 만점입니다. 세부 평가와 과제의 추가 요구사항을 함께 확인하세요." : "공개된 평가 항목에서 수정할 부분과 힌트를 확인하세요."}</p>
    <div className={w.resultActions}><button className={s.primary} aria-expanded={expanded} aria-controls={id} disabled={loading} onClick={()=>expanded ? setExpanded(false) : void read()}>{loading ? "평가 항목 확인 중…" : expanded ? "평가 항목 접기" : "평가 항목·수정 힌트 보기"}</button></div>
    <div id={id} hidden={!expanded} className={w.feedback}>
      {loading && <p role="status">이번 제출의 공개 결과를 확인하고 있습니다.</p>}
      {error && <div className={s.error} role="alert"><p>{error}</p><button className={s.secondary} onClick={()=>void read()}>상세 결과 다시 확인</button></div>}
      {feedback && <>
        {inconsistent && <p className={s.warning}>점수와 항목 판정이 서로 맞지 않습니다. 아래 피드백을 확인하고 교수자에게 문의하세요.</p>}
        {!feedback.feedback_available || !items.length && !feedback.diagnostics.length ? <div className={w.resultNotice}><strong>항목별 피드백이 공개되지 않았습니다</strong><p>공개된 총점만 표시합니다. 어떤 부분을 수정해야 하는지 담당 교수자에게 문의하세요.</p></div> : <>
          <div className={w.feedbackStatus} aria-label="평가 항목 요약"><span>충족 {passed}</span><span>수정 필요 {needsWork}</span>{blocked > 0 && <span>재검사 필요 {blocked}</span>}</div>
          <h4>평가 항목과 수정 안내</h4>
          {items.map(item=><details className={w.criterion} key={item.key} data-status={item.verdict.status} open={item.verdict.needsWork || item.verdict.status === "blocked"}>
            <summary><span>{item.title}<small>{item.verdict.label}</small></span>{validScore(item.score,item.max_score) && <span>{scoreText(item.score,item.max_score)}</span>}</summary>
            <div className={w.criterionBody}>{item.location && <p className={s.muted}>확인할 코드 <code>{item.location}</code></p>}<h5>확인된 현상</h5><p className={w.feedbackText}>{item.feedback || "공개된 상세 설명이 없습니다."}</p><h5>수정 힌트</h5><p className={w.feedbackText}>{item.hint || (item.verdict.status === "passed" ? "이 항목은 충족했습니다. 다른 요구사항을 확인하세요." : "별도 힌트가 없습니다. 과제 설명과 이 항목의 피드백을 함께 확인하세요.")}</p></div>
          </details>)}
          {feedback.diagnostics.length > 0 && <details><summary>추가 피드백 {feedback.diagnostics.length}개</summary>{feedback.diagnostics.map((item,index)=><div className={w.diagnostic} key={index}>{item.location && <code>{item.location}</code>}<p className={w.feedbackText}>{item.message}</p></div>)}</details>}
          <p className={w.feedbackFooter}>수정할 항목을 확인한 뒤 IDE에서 코드 수정 → 저장 → 공식 제출 순서로 진행하세요.</p>
        </>}
      </>}
    </div>
  </div>;
}
