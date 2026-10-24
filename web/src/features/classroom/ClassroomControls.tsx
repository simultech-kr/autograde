"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError } from "../../lib/api/client";
import { createClassroomCommand, type CommandRequest } from "../../lib/api/classroom";
import { commandKinds, type ClassroomCommand, type CommandKind } from "../../lib/contracts/classroom";
import { commandLabels, commandStatusLabel, commandResultLabel, checkLabel, displayTime } from "./presentation";
import styles from "./classroom.module.css";

type Props = {
  course: string; student: string; assignment: string; csrf: string; allowed: boolean;
  supported: boolean; actions: CommandKind[]; commands: ClassroomCommand[];
  historyStale: boolean;
  intents: Map<string, CommandRequest>; onCreated: () => void; onDenied: (error: ApiError) => void;
};

/** The form is stable across SSE detail refreshes. An uncertain POST retains its
 * idempotency key in this page's memory even when the popup is closed/reopened. */
export function ClassroomControls(props: Props) {
  const scope = `${props.course}/${props.student}/${props.assignment}`;
  const prior = props.intents.get(scope);
  const [kind, setKind] = useState<CommandKind>(prior?.kind || "notice");
  const [message, setMessage] = useState(prior?.message || "");
  const [intent, setIntent] = useState<CommandRequest | null>(prior || null);
  const [busy, setBusy] = useState(false), [feedback, setFeedback] = useState(prior ? "이전 전송 결과가 확인되지 않았습니다. 같은 요청으로 다시 확인하세요." : "");
  const alive = useRef(true), flight = useRef<AbortController | null>(null);
  const permission = useRef(props.allowed && props.supported);
  permission.current = props.allowed && props.supported;
  useEffect(() => { alive.current = true; return () => { alive.current = false; flight.current?.abort(); }; }, []);
  async function send(event: React.FormEvent) {
    event.preventDefault();
    if (flight.current || !permission.current || !props.actions.includes(kind) || (kind !== "notice" && !props.assignment)) return;
    const requestKey = "req_" + [...crypto.getRandomValues(new Uint8Array(16))].map(n => n.toString(16).padStart(2, "0")).join("");
    const payload = intent || { csrf: props.csrf, kind, message: message.trim(), ...(props.assignment ? { assignment_id: props.assignment } : {}), request_key: requestKey };
    props.intents.set(scope, payload); setIntent(payload); setBusy(true); setFeedback("서버 접수 확인 중…");
    const controller = new AbortController(); flight.current = controller;
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await createClassroomCommand(props.course, props.student, { ...payload, csrf: props.csrf }, controller.signal);
      if (props.intents.get(scope)?.request_key === payload.request_key) props.intents.delete(scope);
      if (!alive.current || !permission.current) return;
      setIntent(null); setMessage(""); setFeedback(response.duplicate ? "기존 요청을 확인했습니다. 새 요청을 중복 생성하지 않았습니다. 아래 이력에서 상태를 확인하세요." : "요청을 접수했습니다. IDE 수신과 학생 확인 결과를 아래 이력에서 확인하세요."); props.onCreated();
    } catch (error) {
      if (!alive.current) return;
      if (error instanceof ApiError && [401, 403, 404].includes(error.status)) { props.onDenied(error); return; }
      if (error instanceof ApiError && error.status >= 400 && error.status < 500 && ![408, 409, 429].includes(error.status)) {
        props.intents.delete(scope); setIntent(null); setFeedback(error.message);
      } else setFeedback("전송 결과를 확인하지 못했습니다. 입력을 유지한 채 ‘같은 요청 다시 확인’을 눌러 중복 없이 확인하세요.");
    } finally { clearTimeout(timeout); if (flight.current === controller) flight.current = null; if (alive.current) setBusy(false); }
  }
  return <section data-testid="classroom-controls">
    <h3>학생 IDE에 요청</h3>
    <p>모든 요청은 학생이 확인한 뒤 실행합니다. 원격 명령 실행·PC 잠금·외부 앱 차단은 제공하지 않습니다.</p>
    <p className={styles.footnote}>현재 인증한 과제만 처리합니다. 다른 과제 요청은 학생이 해당 과제 수령 코드로 다시 연결해야 수신할 수 있습니다.</p>
    {!props.allowed || !props.supported ? <p className={styles.notice}>현재 IDE 요청 권한 또는 서버 기능을 확인할 수 없습니다.</p> : <form onSubmit={send} className={styles.commandForm}>
      <label>요청 기능<select aria-label="요청 기능" value={kind} disabled={busy || !!intent} onChange={event => setKind(event.target.value as CommandKind)}>{commandKinds.filter(k => props.actions.includes(k)).map(k => <option key={k} value={k}>{commandLabels[k]}</option>)}</select></label>
      <p className={styles.footnote}>대상 과제: {props.assignment || "미선택 · 공지만 가능"} · 요청 유효 시간 10분</p>
      <label>학생 안내<textarea aria-label="학생 안내" rows={3} maxLength={1000} value={message} disabled={busy || !!intent} onChange={event => setMessage(event.target.value)} placeholder="학생이 요청을 이해하는 데 필요한 안내를 입력하세요." required={kind === "notice"} /></label>
      {kind === "save_submit" && <p className={styles.notice}>학생 확인 후 현재 과제 코드를 저장·제출합니다. 이미 접수된 제출은 취소되지 않습니다.</p>}
      {kind !== "notice" && !props.assignment && <p role="alert">팝업을 닫고 공식 제출 확인 과제를 먼저 선택하세요.</p>}
      <button type="submit" disabled={busy || !props.actions.includes(kind) || (kind !== "notice" && !props.assignment)}>{busy ? "접수 확인 중…" : intent ? "같은 요청 다시 확인" : "학생에게 요청"}</button>
    </form>}
    {feedback && <p role="status" className={styles.notice}>{feedback}</p>}
    <h3>IDE 요청 이력</h3>
    {props.historyStale && <p role="status" className={styles.notice}>마지막 확인한 요청 이력입니다. 최신 학생 확인·처리 결과를 다시 조회하고 있습니다.</p>}
    <p className={styles.footnote}>요청됨 → IDE 수신 → 학생 수락 → 처리 결과. 수신은 학생 확인이나 실행 완료를 뜻하지 않습니다.</p>
    <ol className={styles.commandHistory}>{props.commands.map(command => <li key={command.command_id} data-command-id={command.command_id}>
      <div className={styles.commandHeading}><strong>{commandLabels[command.kind]}</strong><span>{commandStatusLabel(command)}</span></div>
      {command.message && <p className={styles.commandMessage}>{command.message}</p>}
      <p className={styles.footnote}>과제: {command.assignment_id || "수업 공지"} · 요청자: {command.created_by}</p>
      <dl className={styles.facts}><dt>요청</dt><dd>{displayTime(command.created_at)}</dd><dt>IDE 수신</dt><dd>{displayTime(command.received_at)}</dd><dt>학생 확인</dt><dd>{command.accepted_at ? `수락 · ${displayTime(command.accepted_at)}` : command.status === "declined" ? `거절 · ${displayTime(command.finished_at)}` : "미확인"}</dd><dt>처리 결과</dt><dd>{commandResultLabel(command) || "결과 대기"}{command.finished_at && <small> · {displayTime(command.finished_at)}</small>}</dd><dt>만료</dt><dd>{displayTime(command.expires_at)}</dd></dl>
      {command.result?.submission_id && <p>공식 제출 ID: <code>{command.result.submission_id}</code></p>}
      {command.result?.checks && <ul>{Object.entries(command.result.checks).map(([key, value]) => <li key={key}>{checkLabel(key, value)}</li>)}</ul>}
    </li>)}</ol>
    {!props.commands.length && <p>이 학생에게 보낸 IDE 요청이 없습니다.</p>}
  </section>;
}
