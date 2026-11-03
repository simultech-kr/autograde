"use client";
import {useCallback, useEffect, useRef, useState, type ReactNode} from "react";
import {studentRequest} from "./client";
import {coursePath, timeText, StudentContractError} from "./model";
import {parsePrivacy, parsePrivacyOverview, type PrivacyCourse, type PrivacyData, type PrivacyOverview} from "./privacy";
import s from "./student.module.css";
import p from "./privacy.module.css";

const labels = {granted: "동의함", declined: "동의하지 않음", unanswered: "아직 선택하지 않음"};
const reminderKey = (row: PrivacyCourse) => `${row.course_key}:${row.policy.version}`;
type Props = {
  studentKey: string; sessionKey: string; visible: boolean; settings: boolean;
  preferredCourse?: string; onOpenSettings: () => void;
  onMutationStart?: () => void; onMutationEnd?: () => void;
};

function PolicyDetails({policy}: {policy: PrivacyData["policy"]}) {
  return <details className={p.policy}><summary>어떤 정보를, 언제까지 활용하나요?</summary><dl>
    {([["purpose", "활용 목적"], ["items", "활용하는 정보"], ["retention", "보관과 파기"], ["choice", "선택할 권리"], ["withdrawal", "변경과 철회"], ["scope", "관리 범위"], ["contact", "문의"]] as const).map(([key, label]) => <div key={key}><dt>{label}</dt><dd>{policy[key]}</dd></div>)}
  </dl><p className={s.muted}>안내 버전 {policy.version}</p></details>;
}

function Explanation() {
  return <><p className={p.intro}>어떤 실습이 어렵고, 어떤 설명이 도움이 되었는지 살펴 더 좋은 수업을 만들고자 합니다.</p>
    <div className={p.benefits}><div><span aria-hidden="true">01</span><strong>실습 난이도 조정</strong><p>어려웠던 부분을 살펴 학습에 필요한 설명과 연습을 보완합니다.</p></div><div><span aria-hidden="true">02</span><strong>수업 개선과 교육 연구</strong><p>과제 제출·점수·수업 활동 기록을 활용하고, 수업 보관 후에는 동의 학생의 집계 통계만 남깁니다.</p></div></div>
    <p className={p.choiceHint}>동의 여부는 자유롭게 선택하세요. 어느 쪽을 선택해도 과제 이용과 성적에는 영향이 없으며, 수업 보관 전까지 변경할 수 있습니다.</p></>;
}

function ConsentDialog({children, onClose}: {children: ReactNode; onClose: () => void}) {
  const element = useRef<HTMLDivElement>(null), close = useRef(onClose); close.current = onClose;
  useEffect(() => {
    const prior = document.activeElement instanceof HTMLElement ? document.activeElement : null, dialog = element.current!;
    dialog.querySelector<HTMLElement>("[data-dialog-heading]")?.focus();
    const focusables = () => Array.from(dialog.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],select:not(:disabled),summary,input:not(:disabled),[tabindex="0"]')).filter(node => !node.closest("[hidden]") && !node.closest("details:not([open]) :not(summary)"));
    function keyboard(event: KeyboardEvent) {
      if (event.key === "Escape") {event.preventDefault(); event.stopPropagation(); close.current();}
      if (event.key !== "Tab") return;
      const nodes = focusables(), first = nodes[0], last = nodes[nodes.length - 1];
      if (!first) {event.preventDefault(); dialog.focus(); return;}
      if (event.shiftKey && (document.activeElement === first || !nodes.includes(document.activeElement as HTMLElement))) {event.preventDefault(); last.focus();}
      else if (!event.shiftKey && (document.activeElement === last || !nodes.includes(document.activeElement as HTMLElement))) {event.preventDefault(); first.focus();}
    }
    function contain(event: FocusEvent) {if (event.target && !dialog.contains(event.target as Node)) dialog.querySelector<HTMLElement>("[data-dialog-heading]")?.focus();}
    document.addEventListener("keydown", keyboard, true); document.addEventListener("focusin", contain);
    return () => {
      document.removeEventListener("keydown", keyboard, true); document.removeEventListener("focusin", contain);
      if (prior?.isConnected && !prior.closest("[hidden],[inert]")) prior.focus();
    };
  }, []);
  return <div className={p.backdrop}><div ref={element} className={p.dialog} role="dialog" aria-modal="true" aria-labelledby="research-dialog-heading" aria-describedby="research-dialog-description" tabIndex={-1}>{children}</div></div>;
}

export function StudentPrivacy({studentKey, sessionKey, visible, settings, preferredCourse = "", onOpenSettings, onMutationStart, onMutationEnd}: Props) {
  const [data, setData] = useState<PrivacyOverview | null>(null), [selected, setSelected] = useState(""), [error, setError] = useState(""), [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false), [validated, setValidated] = useState(false), [, setReminderRevision] = useState(0);
  const mounted = useRef(false), generation = useRef(0), request = useRef<{controller: AbortController; mutation: boolean} | null>(null);
  const mutating = useRef(false), refreshPending = useRef(false), dismissed = useRef(new Set<string>()), visibleRef = useRef(visible), preferredRef = useRef(preferredCourse);
  const callbacks = useRef({onMutationStart, onMutationEnd}); callbacks.current = {onMutationStart, onMutationEnd}; visibleRef.current = visible; preferredRef.current = preferredCourse;
  const load = useCallback(async () => {
    if (mutating.current) {refreshPending.current = true; return;}
    const current = ++generation.current, controller = new AbortController(); request.current?.controller.abort(); request.current = {controller, mutation: false};
    setBusy(true); setValidated(false); setData(null); setError(""); setNotice("");
    try {
      const next = parsePrivacyOverview(await studentRequest("/student/api/privacy", undefined, controller.signal), studentKey);
      if (next.csrf !== sessionKey) throw new StudentContractError();
      if (mounted.current && current === generation.current) {
        setData(next); setValidated(visibleRef.current);
        setSelected(old => next.consents.some(row => row.course_key === old) ? old : next.consents.some(row => row.course_key === preferredRef.current) ? preferredRef.current : next.consents.find(row => row.decision === "unanswered")?.course_key || next.consents[0]?.course_key || "");
      }
    } catch (error) {
      if (mounted.current && current === generation.current && !controller.signal.aborted) setError(error instanceof Error ? error.message : "정보 활용 설정을 불러오지 못했습니다. 다시 확인해 주세요.");
    } finally {if (mounted.current && current === generation.current) {request.current = null; setBusy(false);}}
  }, [studentKey, sessionKey]);
  const latestLoad = useRef(load); latestLoad.current = load;

  useEffect(() => {
    mounted.current = true; dismissed.current.clear(); setSelected(""); setData(null); setValidated(false); setError(""); setNotice("");
    return () => {mounted.current = false; ++generation.current; request.current?.controller.abort();};
  }, [studentKey, sessionKey]);
  useEffect(() => {
    if (visible) void load();
    else {
      setValidated(false);
      if (!request.current?.mutation) {++generation.current; request.current?.controller.abort(); request.current = null; setBusy(false); setData(null);}
    }
  }, [visible, load]);
  const previousSettings = useRef(settings);
  useEffect(() => {
    if (settings && !previousSettings.current && visible) void load();
    previousSettings.current = settings;
  }, [settings, visible, load]);

  // Closing records no consent. The server keeps only a session-memory reminder
  // preference so a reload or the classic portal respects the same choice.
  function closeReminder() {
    let changed = false;
    for (const row of data?.consents || []) if (row.decision === "unanswered" && !row.prompt_dismissed && !dismissed.current.has(reminderKey(row))) {
      dismissed.current.add(reminderKey(row)); changed = true;
    }
    setReminderRevision(value => value + 1);
    // Best effort and never automatically retried: dismissal must not block
    // learning or be confused with an opt-in / opt-out database mutation.
    if (changed && data) void studentRequest("/student/api/privacy/dismiss", {csrf: data.csrf, policy_version: data.policy.version}).catch(() => {});
  }
  async function choose(row: PrivacyCourse, decision: "granted" | "declined", fromPrompt = false) {
    if (!data || mutating.current || busy || !validated || !visibleRef.current) return;
    const current = ++generation.current, controller = new AbortController(), snapshot = data;
    request.current?.controller.abort(); request.current = {controller, mutation: true}; mutating.current = true; refreshPending.current = false;
    setBusy(true); setError(""); setNotice(""); callbacks.current.onMutationStart?.();
    try {
      const next = parsePrivacy(await studentRequest(coursePath(row.course_key) + "/privacy", {csrf: snapshot.csrf, decision, policy_version: row.policy.version, revision: String(row.revision)}, controller.signal), row.course_key, studentKey);
      if (mounted.current && current === generation.current) {
        setData({...snapshot, consents: snapshot.consents.map(item => item.course_key === row.course_key ? {...next, course_name: item.course_name, prompt_dismissed: item.prompt_dismissed} : item)});
        setNotice(`${row.course_name}: ${decision === "granted" ? "정보 활용에 동의했습니다." : "정보 활용에 동의하지 않는 것으로 저장했습니다."}`);
        if (fromPrompt) closeReminder();
      }
    } catch (error) {
      if (mounted.current && current === generation.current && !controller.signal.aborted) {
        setData(null); setValidated(false); setError(error instanceof Error ? error.message : "저장 결과를 확인하지 못했습니다. 상태를 새로 확인해 주세요.");
      }
    } finally {
      mutating.current = false;
      if (mounted.current && current === generation.current) {request.current = null; setBusy(false);}
      callbacks.current.onMutationEnd?.();
      if (refreshPending.current && mounted.current && visibleRef.current) {refreshPending.current = false; void latestLoad.current();}
    }
  }
  const current = data?.consents.find(row => row.course_key === selected) || data?.consents[0];
  const eligible = validated ? data?.consents.filter(row => row.decision === "unanswered" && !row.prompt_dismissed && !dismissed.current.has(reminderKey(row))) || [] : [];
  const prompt = !settings ? eligible.find(row => row.course_key === selected) || eligible.find(row => row.course_key === preferredCourse) || eligible[0] : undefined;
  if (!visible) return null;
  const choices = (row: PrivacyCourse, modal = false) => <div className={p.choiceButtons}>
    <button className={s.secondary} disabled={busy || row.decision === "declined"} onClick={() => void choose(row, "declined", modal)}>{row.decision === "granted" ? "동의 철회" : "동의하지 않습니다"}</button>
    <button className={s.secondary} disabled={busy || row.decision === "granted"} onClick={() => void choose(row, "granted", modal)}>동의합니다</button>
  </div>;

  return <>
    {settings && <section className={p.settings} aria-label="정보 활용 설정">
      <div className={p.settingsHeading}><div><p className={s.eyebrow}>내가 정하는 정보 활용</p><h2>더 나은 실습을 위한 선택</h2><p>수업마다 내 선택을 확인하고, 원할 때 바꿀 수 있습니다.</p></div><button className={s.secondary} disabled={busy} onClick={() => void load()}>상태 새로고침</button></div>
      {error && <p className={s.error} role="alert">{error}</p>}{notice && <p className={p.saved} role="status">{notice}</p>}
      {busy && <p role="status">{mutating.current ? "선택을 저장하고 있습니다…" : "내 설정을 불러오고 있습니다…"}</p>}
      {data && !data.consents.length && <div className={s.empty}><h3>연결된 수업이 생기면 여기서 선택할 수 있어요.</h3><p>수강 등록과 명단 연결이 완료된 수업의 설정을 모아서 보여드립니다.</p></div>}
      {data && data.consents.length > 0 && <div className={p.settingsLayout}>
        <div className={p.courseList} aria-label="정보 활용을 설정할 수업">{data.consents.map(row => <button key={row.course_key} className={p.courseChoice} aria-pressed={current?.course_key === row.course_key} onClick={() => {setSelected(row.course_key); setNotice("");}}><span>{row.course_name}</span><small>{row.course_key}</small><span className={p.status} data-choice={row.decision}>{labels[row.decision]}</span></button>)}</div>
        {current && <article className={p.detail}><div className={p.detailHeading}><div><p className={s.eyebrow}>{current.course_key}</p><h3>{current.course_name}</h3></div><span className={p.status} data-choice={current.decision}>{labels[current.decision]}</span></div>
          <Explanation /><PolicyDetails policy={current.policy} />
          <div className={p.decision}><h4>이 수업에서의 내 선택</h4><p>{current.decision === "granted" ? "연구와 수업 개선을 위한 정보 활용에 동의한 상태입니다." : current.decision === "declined" ? "이 수업의 연구용 집계에서 내 정보를 제외합니다." : "아직 선택하지 않았습니다. 선택 전에는 연구용 집계에 포함하지 않습니다."}</p>{choices(current)}{current.updated_at && <p className={s.muted}>마지막 변경 {timeText(current.updated_at)} KST</p>}</div>
          {current.history.length > 0 && <details className={p.policy}><summary>내 선택 변경 이력 · 최근 20건</summary><ol className={p.history}>{current.history.map((row, index) => <li key={index}><strong>{labels[row.decision]}</strong><span>{timeText(row.created_at)} KST · 안내 {row.policy_version}</span></li>)}</ol></details>}
        </article>}
      </div>}
    </section>}
    {!settings && notice && <div className={p.toast} role="status"><p>{notice}</p><div><button className={s.quiet} onClick={onOpenSettings}>정보 활용 설정</button><button className={s.secondary} onClick={() => setNotice("")}>확인</button></div></div>}
    {!settings && error && <div className={p.reminderError} role="status"><span>정보 활용 설정을 확인하지 못했어요. 실습은 계속할 수 있습니다.</span><button className={s.quiet} onClick={onOpenSettings}>설정에서 확인</button></div>}
    {prompt && <ConsentDialog onClose={closeReminder}>
      <div className={p.dialogHeading}><div><p className={s.eyebrow}>선택 안내</p><h2 id="research-dialog-heading" data-dialog-heading tabIndex={-1}>함께 더 나은 수업을 만들어요</h2></div><button className={p.close} aria-label="나중에 선택하고 닫기" onClick={closeReminder}>×</button></div>
      <p id="research-dialog-description" className={p.dialogDescription}>연구·수업 개선을 위한 선택적 정보 활용에 대해 알려드려요.</p>
      <div className={p.promptCourse}>{eligible.length > 1 ? <label>이번에 선택할 수업<select value={prompt.course_key} onChange={event => setSelected(event.target.value)}>{eligible.map(row => <option key={row.course_key} value={row.course_key}>{row.course_name}</option>)}</select></label> : <><span className={s.eyebrow}>선택 대상 수업</span><strong>{prompt.course_name}</strong><small>{prompt.course_key}</small></>}</div>
      <Explanation /><PolicyDetails policy={prompt.policy} />{choices(prompt, true)}
      {busy && <p role="status">선택을 저장하고 있습니다…</p>}
      <div className={p.dialogFooter}><button className={s.quiet} onClick={closeReminder}>나중에 선택</button><button className={s.quiet} onClick={() => {closeReminder(); onOpenSettings();}}>모든 수업의 설정 보기</button></div>
      <p className={s.muted}>나중에 선택해도 실습을 계속할 수 있어요. 상단의 ‘정보 활용 설정’ 탭에서 다시 확인하세요.</p>
    </ConsentDialog>}
  </>;
}
