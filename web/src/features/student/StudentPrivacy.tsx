"use client";
import {useCallback, useEffect, useRef, useState} from "react";
import {studentRequest} from "./client";
import {coursePath, timeText} from "./model";
import {parsePrivacy, type PrivacyData} from "./privacy";
import s from "./student.module.css";

const labels = {granted: "동의함", declined: "동의하지 않음 · 연구 활용 제외", unanswered: "아직 선택하지 않음 · 연구 활용 제외"};
export function StudentPrivacy({courseKey, studentKey, csrf}: {courseKey: string; studentKey: string; csrf: string}) {
  const [data, setData] = useState<PrivacyData | null>(null), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const request = useRef<AbortController | null>(null), version = useRef(0), mounted = useRef(false);
  const endpoint = coursePath(courseKey) + "/privacy";
  const load = useCallback(async () => {
    const current = ++version.current; request.current?.abort(); const controller = new AbortController(); request.current = controller;
    setData(null); setBusy(true); setError(""); setNotice("");
    try {
      const next = parsePrivacy(await studentRequest(endpoint, undefined, controller.signal), courseKey, studentKey);
      if (mounted.current && current === version.current) setData(next);
    } catch (error) {
      if (mounted.current && current === version.current && !controller.signal.aborted) setError(error instanceof Error ? error.message : "동의 상태를 확인하지 못했습니다.");
    } finally { if (mounted.current && current === version.current) setBusy(false); }
  }, [endpoint, courseKey, studentKey]);
  useEffect(() => {
    mounted.current = true; void load();
    return () => {mounted.current = false; ++version.current; request.current?.abort();};
  }, [load]);
  async function choose(decision: "granted" | "declined") {
    if (!data || busy) return;
    setBusy(true); setError(""); setNotice("");
    const current = ++version.current; request.current?.abort(); const controller = new AbortController(); request.current = controller;
    try {
      const next = parsePrivacy(await studentRequest(endpoint, {csrf, decision, policy_version: data.policy.version, revision: String(data.revision)}, controller.signal), courseKey, studentKey);
      if (mounted.current && current === version.current) {setData(next); setNotice("선택이 저장되었습니다.");}
    } catch (error) {
      if (mounted.current && current === version.current && !controller.signal.aborted) {
        setError(error instanceof Error ? error.message : "저장 결과를 확인하지 못했습니다. 다시 확인하세요.");
        // A withdrawal in another tab or a lost response must be read before
        // another choice. Never retry or overwrite consent automatically.
        setData(null);
      }
    } finally {if (mounted.current && current === version.current) setBusy(false);}
  }
  return <section className={s.panel} aria-label="개인정보 활용 동의" id="student-privacy">
    <div className={s.sectionHeading}><h2>개인정보 활용 · 내 선택</h2><button className={s.quiet} disabled={busy} onClick={() => void load()}>동의 상태 새로고침</button></div>
    <p>연구·수업 개선용 활용은 선택입니다. 동의하지 않아도 과제·점수를 이용할 수 있습니다.</p>
    {error && <p className={s.error} role="alert">{error}</p>}{notice && <p role="status">{notice}</p>}
    {busy && <p role="status">처리 결과를 확인하고 있습니다…</p>}
    {data && <><p><strong>{labels[data.decision]}</strong>{data.updated_at && <span className={s.muted}> · {timeText(data.updated_at)} KST</span>}</p>
      <details open={data.decision === "unanswered"}><summary>활용 목적·항목·보관 기간·철회 안내</summary><dl>
        {([ ["purpose", "활용 목적"], ["items", "활용 항목"], ["retention", "보관·파기"], ["choice", "선택권"], ["withdrawal", "동의 변경·철회"], ["scope", "관리 범위"], ["contact", "문의"] ] as const).map(([key, label]) => <div key={key}><dt><strong>{label}</strong></dt><dd>{data.policy[key]}</dd></div>)}
      </dl><p className={s.muted}>안내 버전 {data.policy.version}</p></details>
      <div className={s.actions}><button className={s.secondary} disabled={busy} onClick={() => void choose("declined")}>동의하지 않음 · 철회</button><button className={s.primary} disabled={busy} onClick={() => void choose("granted")}>동의하고 저장</button></div>
      {data.history.length > 0 && <details><summary>내 동의 변경 이력 · 최근 20건</summary><ul>{data.history.map((row, index) => <li key={index}>{timeText(row.created_at)} KST · {labels[row.decision]} · {row.policy_version}</li>)}</ul></details>}
    </>}
    <p className={s.muted}><a href={`/courses/${courseKey}/privacy`}>별도 동의 화면 열기</a> · <a href="/student/privacy">개인정보 활용 안내</a></p>
  </section>;
}
