"use client";

import { useEffect, useRef, useState } from "react";
import { AppShell, PageHeading } from "../../components/InstructorUI";
import { ApiError } from "../../lib/api/client";
import { classroom, classroomBootstrap, classroomDetail, classroomEntry, classroomURL, type CommandRequest } from "../../lib/api/classroom";
import { ContractError, type Bootstrap } from "../../lib/contracts/instructor";
import { parseClassroom, type ClassroomDetail, type ClassroomSnapshot, type ClassroomStudent } from "../../lib/contracts/classroom";
import { activityLabel, connectionLabels, displayTime, stableStudents, submissionLabel, submissionScore, needsAttention, type AttentionFilter } from "./presentation";
import { availableModules } from "../registry";
import { selectedCourse } from "../navigation";
import { ClassroomControls } from "./ClassroomControls";
import styles from "./classroom.module.css";

type Stream = "connecting" | "live" | "reconnecting" | "paused" | "denied";
type Failure = { message: string; terminal: boolean };
const failure = (error: unknown): Failure => ({ message: error instanceof ApiError || error instanceof ContractError ? error.message : "서버에 연결하지 못했습니다. 마지막 확인 상태를 표시합니다.", terminal: error instanceof ContractError || error instanceof ApiError && [401, 403, 404].includes(error.status) });

export function StudentCard({ student, onOpen }: { student: ClassroomStudent; onOpen: () => void }) {
  return <button className={`${styles.student} ${styles[student.connection.state]}`} aria-haspopup="dialog" onClick={onOpen} data-student={student.student_key}>
    <span className={styles.studentKey}>{student.student_key}</span>
    {student.student_name && <span className={styles.studentName}>{student.student_name}</span>}
    <span className={styles.connection}><span className={styles.dot} aria-hidden="true" />{connectionLabels[student.connection.state]}</span>
    <span className={styles.metric}><span aria-hidden="true">↗</span><span>{activityLabel(student.activity?.kind)}<small>{student.activity && displayTime(student.activity.received_at)}</small></span></span>
    <span className={styles.metric}><span aria-hidden="true">{student.submission.submitted ? "✓" : "○"}</span>{submissionLabel(student)}</span>
  </button>;
}

export function ClassroomDashboard() {
  const [identity, setIdentity] = useState<Bootstrap | null>(null), [course, setCourse] = useState(""), [assignment, setAssignment] = useState("");
  const [data, setData] = useState<ClassroomSnapshot | null>(null), [error, setError] = useState<Failure | null>(null), [stream, setStream] = useState<Stream>("connecting");
  const [filter, setFilter] = useState("all"), [search, setSearch] = useState(""), [selected, setSelected] = useState<string | null>(null);
  const [attention, setAttention] = useState<AttentionFilter>("all");
  const [detail, setDetail] = useState<ClassroomDetail | null>(null), [detailError, setDetailError] = useState(""), [tab, setTab] = useState("overview");
  const [boot, setBoot] = useState(0), [refresh, setRefresh] = useState(0), [lastSuccess, setLastSuccess] = useState("");
  const [detailRefresh, setDetailRefresh] = useState(0);
  const commandIntents = useRef(new Map<string, CommandRequest>());
  const dialog = useRef<HTMLDialogElement>(null), lastButton = useRef<HTMLButtonElement | null>(null);
  const validData = data?.course_key === course && data.selected_assignment_id === (assignment || null) ? data : null;
  const selectedCard = validData?.students.find(s => s.student_key === selected);
  const scopedDetail = detail?.course_key === course && detail.selected_assignment_id === (assignment || null) && detail.student.student_key === selected ? detail.student : null;
  const validDetail = scopedDetail && JSON.stringify(scopedDetail.submission) === JSON.stringify(selectedCard?.submission) ? scopedDetail : null;

  useEffect(() => {
    const restore = (event: PageTransitionEvent) => { if (event.persisted) { setData(null); setDetail(null); setSelected(null); setIdentity(null); setBoot(n => n + 1); } };
    window.addEventListener("pageshow", restore); return () => window.removeEventListener("pageshow", restore);
  }, []);
  useEffect(() => {
    const controller = new AbortController(); let alive = true;
    setError(null); setIdentity(null); setData(null); setSelected(null); commandIntents.current.clear();
    const timeout = setTimeout(() => controller.abort(), 15000);
    const params = new URLSearchParams(window.location.search);
    classroomBootstrap(controller.signal).then(value => {
      if (!alive) return;
      const requested = params.get("course_key") || "";
      if (requested && !value.courses.some(c => c.course_key === requested)) throw new ApiError(404, "course_not_available", "이 수업에 접근할 수 없습니다.");
      if (!availableModules(value.capabilities).some(module => module.id === "classroom")) throw new ApiError(403, "classroom_disabled", "교실 조회 권한이 없습니다.");
      setCourse(requested || value.courses[0]?.course_key || ""); setAssignment(params.get("assignment_id") || ""); setIdentity(value);
    }).catch(error => { if (alive) setError(failure(error)); }).finally(() => clearTimeout(timeout));
    return () => { alive = false; clearTimeout(timeout); controller.abort(); };
  }, [boot]);

  useEffect(() => {
    if (!identity || !course) return;
    let alive = true, source: EventSource | null = null, controller: AbortController | null = null, timer: ReturnType<typeof setTimeout> | undefined;
    let watchdog: ReturnType<typeof setTimeout> | undefined, attempts = 0, stopped = false, generation = 0;
    const params = new URLSearchParams({ course_key: course, ...(assignment ? { assignment_id: assignment } : {}) });
    window.history.replaceState(null, "", `/app/classroom?${params}`);
    const available = () => navigator.onLine !== false && !document.hidden;
    const clear = () => { generation++; source?.close(); source = null; controller?.abort(); clearTimeout(timer); clearTimeout(watchdog); };
    const terminal = (err: unknown) => { stopped = true; clear(); setData(null); setDetail(null); setSelected(null); setIdentity(null); setStream("denied"); setError(failure(err)); };
    const apply = (value: ClassroomSnapshot) => {
      if (value.course_key !== course || value.selected_assignment_id !== (assignment || null)) throw new ContractError();
      setData(old => old?.course_key === course && old.selected_assignment_id === (assignment || null) && old.version === value.version ? old : value);
      setSelected(current => current && value.students.some(student => student.student_key === current) ? current : null);
      setLastSuccess(value.server_time); setError(null);
    };
    const retry = () => {
      clear();
      if (!alive || stopped) return;
      setStream(available() ? "reconnecting" : "paused");
      clearTimeout(timer); if (available()) timer = setTimeout(connect, Math.min(30000, 1000 * 2 ** Math.min(attempts++, 5)));
    };
    const pulse = () => { clearTimeout(watchdog); watchdog = setTimeout(retry, 45000); };
    const connect = async () => {
      if (!alive || stopped || !available()) { if (alive && !stopped) setStream("paused"); return; }
      clear(); const token = generation; controller = new AbortController(); const current = controller;
      const timeout = setTimeout(() => current.abort(), 15000);
      try {
        const response = await classroom(course, assignment, current.signal);
        if (!alive || token !== generation) return;
        apply(response);
        if (!window.EventSource) { setStream("reconnecting"); timer = setTimeout(connect, 5000); return; }
        source = new EventSource(classroomURL(course, assignment, "/events"));
        source.addEventListener("snapshot", event => {
          if (!alive || token !== generation) return;
          try { apply(parseClassroom(JSON.parse((event as MessageEvent).data))); attempts = 0; setStream("live"); pulse(); }
          catch (error) { terminal(error); }
        });
        source.addEventListener("heartbeat", () => { if (alive && token === generation) { setStream("live"); pulse(); } });
        source.addEventListener("access_revoked", () => { if (alive && token === generation) terminal(new ApiError(403, "access_revoked", "교수자 세션 또는 수업 권한이 변경되었습니다. 다시 인증해 주세요.")); });
        source.onerror = () => { if (alive && token === generation) retry(); }; pulse();
      } catch (err) {
        if (!alive || token !== generation) return;
        const next = failure(err); if (next.terminal) terminal(err); else { setError(next); retry(); }
      } finally { clearTimeout(timeout); }
    };
    const visibility = () => { clear(); if (!stopped && alive) { if (available()) void connect(); else setStream("paused"); } };
    document.addEventListener("visibilitychange", visibility); window.addEventListener("online", visibility); window.addEventListener("offline", visibility);
    setStream("connecting"); setError(null); void connect();
    return () => { alive = false; clear(); document.removeEventListener("visibilitychange", visibility); window.removeEventListener("online", visibility); window.removeEventListener("offline", visibility); };
  }, [identity, course, assignment, refresh]);

  useEffect(() => {
    if (!selected || !validData || stream === "denied") { setDetail(null); return; }
    const controller = new AbortController(); let alive = true;
    const timeout = setTimeout(() => controller.abort(), 15000);
    setDetailError("");
    classroomDetail(course, assignment, selected, controller.signal).then(value => {
      if (!alive) return;
      if (value.course_key !== course || value.selected_assignment_id !== (assignment || null) || value.student.student_key !== selected) throw new ContractError();
      setDetail(value);
    }).catch(err => { if (alive) {
      const next = failure(err); setDetailError(next.message);
      if (next.terminal) { setDetail(null); setSelected(null); setData(null); setError(next); setIdentity(null); }
    } }).finally(() => clearTimeout(timeout));
    return () => { alive = false; controller.abort(); clearTimeout(timeout); };
  }, [course, assignment, selected, validData?.version, stream === "denied", detailRefresh]);

  useEffect(() => {
    if (selected && dialog.current && !dialog.current.open) dialog.current.showModal();
    if (!selected && dialog.current?.open) { dialog.current.close(); lastButton.current?.focus(); }
  }, [selected]);
  const open = (student: string) => { lastButton.current = document.activeElement instanceof HTMLButtonElement ? document.activeElement : null; setTab("overview"); setDetail(null); setDetailError(""); setSelected(student); };
  const clearSelection = () => { setSelected(null); setDetail(null); setData(null); setLastSuccess(""); setAttention("all"); };
  const attentionCount = validData?.students.filter(s => needsAttention(s, "attention", validData.server_time)).length || 0;
  const students = stableStudents(validData?.students || []).filter(s => (!search || `${s.student_key} ${s.student_name}`.includes(search)) && needsAttention(s, attention, validData?.server_time || "") && (!assignment || filter === "all" || (filter === "submitted" ? s.submission.submitted : !s.submission.submitted && s.submission.state !== "not_assigned")));
  return <AppShell testId="classroom-dashboard" course={selectedCourse(identity?.courses, course)} section="classroom" resultsEnabled={identity?.capabilities.includes("results.read")}>
    <PageHeading eyebrow="수업 운영 / 실습 교실" title="학생 활동" description="IDE 연결·수업 활동·공식 제출을 확인하고 학생에게 IDE 작업을 요청합니다." label="실시간 · 학생 확인 후 실행" />
    {error && <div role="alert" className={styles.notice}><p>{error.message}</p>{error.terminal || !identity ? <a href={classroomEntry(course)}>교수자 인증</a> : <button onClick={() => setRefresh(n => n + 1)}>다시 연결</button>}{!identity && !error.terminal && <button onClick={() => setBoot(n => n + 1)}>다시 조회</button>}</div>}
    {!identity && !error && <p role="status">교수자 접근 권한 확인 중… <a href={classroomEntry(course)}>교수자 인증</a></p>}
    {identity && <>
      <section className={styles.filters} aria-label="수업과 과제 선택">
        <label>교과목 · 분반<select aria-label="교과목 · 분반" value={course} onChange={e => { clearSelection(); setCourse(e.target.value); setAssignment(""); }}><option value="" disabled>수업 선택</option>{identity.courses.map(c => <option key={c.course_key} value={c.course_key}>{c.name || c.code || c.course_key}{c.section ? ` · ${c.section}` : ""}</option>)}</select></label>
        <label>공식 제출 확인 과제<select aria-label="공식 제출 확인 과제" value={assignment} onChange={e => { clearSelection(); setAssignment(e.target.value); }}><option value="">과제 선택 · 전체 수업 활동</option>{validData?.assignments.map(a => <option key={a.assignment_id} value={a.assignment_id}>{a.title || a.assignment_key}</option>)}</select></label>
        <button onClick={() => setRefresh(n => n + 1)}>다시 연결</button>
      </section>
      {!identity.courses.length && <p>조회할 수 있는 수업이 없습니다.</p>}
      <div className={styles.live} role="status" data-stream={stream}>{stream === "live" ? "● 실시간 수신" : stream === "paused" ? "수신 일시 중단" : stream === "denied" ? "접근 종료" : "실시간 연결 확인 중"}<span>마지막 확인: {displayTime(lastSuccess)}</span></div>
      {stream !== "live" && validData && <p className={styles.notice}>브라우저의 수신이 중단되었거나 재연결 중입니다. 카드는 마지막 확인 상태이며, 학생 IDE의 연결 종료를 뜻하지 않습니다.</p>}
      {validData && <>
        <div className={styles.summary}><span>수강생 <strong>{validData.summary.total}명</strong></span><span>IDE 연결 <strong>{validData.summary.online}명</strong></span><span>신호 지연 <strong>{validData.summary.stale}명</strong></span><span>공식 제출 <strong>{assignment ? `${validData.summary.submitted}명` : "과제 선택"}</strong></span></div>
        <div className={styles.filters}><label>학생 검색<input aria-label="학생 검색" value={search} onChange={e => setSearch(e.target.value)} placeholder="학번 또는 이름" /></label><label>제출 상태<select aria-label="제출 상태" value={filter} onChange={e => setFilter(e.target.value)} disabled={!assignment}><option value="all">전체</option><option value="submitted">제출</option><option value="unsubmitted">미제출</option></select></label>
          <label>조치 필요<select aria-label="조치 필요" value={attention} onChange={e => setAttention(e.target.value as AttentionFilter)}><option value="all">전체 학생</option><option value="attention">조치 필요 {attentionCount}명</option><option value="connection">IDE 신호 지연·연결 끊김</option><option value="never_connected">연결 기록 없음</option><option value="grading_error" disabled={!assignment}>제출·채점 오류</option><option value="grading_wait" disabled={!assignment}>채점 접수 후 10분 이상 대기</option><option value="command">IDE 요청 실패·만료</option></select></label><span role="status">{students.length}명 · 학번순 고정</span></div>
        {attention !== "all" && <p className={styles.footnote}>마지막 서버 확인 상태를 기준으로 분류합니다. 요청 실패·만료는 최근 20개 요청 중 같은 작업·과제의 마지막 상태이며, 과제 선택 시 채점 상태도 확인합니다. 학생 카드에서 원인과 처리 결과를 확인하세요.</p>}
        <div className={styles.grid} aria-label="학생별 현재 상태">{students.map(s => <StudentCard key={s.student_key} student={s} onOpen={() => open(s.student_key)} />)}</div>
        {!students.length && <p className={styles.notice}>표시할 학생이 없습니다. 수강 명단 또는 검색 조건을 확인하세요.</p>}
        <p className={styles.footnote}>카드 선택 → 코드·제출 / 활동·분석 / IDE 요청. 접속 여부나 저장 횟수만으로 출석·집중도·성실도를 판단하지 않습니다.</p>
      </>}
    </>}
    <dialog className={styles.dialog} ref={dialog} onCancel={() => setSelected(null)} onClose={() => setSelected(null)} aria-labelledby="classroom-student-title">
      <header className={styles.dialogHeader}><div><h2 id="classroom-student-title">{selected} {selectedCard?.student_name}</h2><p>{selectedCard && connectionLabels[selectedCard.connection.state]} · 마지막 신호 {displayTime(selectedCard?.connection.last_seen_at)}</p></div><button onClick={() => setSelected(null)} aria-label="학생 상세 닫기">닫기</button></header>
      <nav className={styles.tabs} aria-label="학생 상세 메뉴">{[["overview", "코드·제출"], ["activity", "활동·분석"], ["control", "IDE 요청"]].map(([key, text]) => <button key={key} aria-pressed={tab === key} onClick={() => setTab(key)}>{text}</button>)}</nav>
      {stream !== "live" && <p className={styles.notice}>실시간 수신 확인 중 · 마지막 확인 기록</p>}
      {detailError && <p role="alert">{detailError}</p>}
      {!validDetail && !detailError && <p role="status">상세 기록을 확인하고 있습니다…</p>}
      {validDetail && tab === "overview" && <>
        <h3>서버 코드 보관</h3><p><strong>{validDetail.code_storage.state === "receipt_recorded" ? "서버 제출 접수 기록 있음" : "서버 제출 보관 기록 없음"}</strong></p><p>{validDetail.code_storage.message}</p><p className={styles.footnote}>제출 접수 기록과 현재 PC의 모든 코드 보관은 다릅니다. 서버가 검증하지 않은 보관 완료 상태는 표시하지 않습니다.</p>
        <h3>공식 제출</h3><dl className={styles.facts}><dt>제출 상태</dt><dd>{submissionLabel(validDetail)}</dd><dt>최신 제출</dt><dd>{displayTime(validDetail.submission.latest?.received_at)}</dd><dt>최신 결과</dt><dd data-testid="classroom-current-score">{submissionScore(validDetail.submission.latest)}</dd><dt>이전 최고점</dt><dd data-testid="classroom-previous-best">{validDetail.submission.previous_best ? `${validDetail.submission.previous_best.score} / ${validDetail.submission.previous_best.max_score}점 · 최신 제출 제외` : "이전 채점 기록 없음"}</dd></dl>
        <h3>제출 시각 → 코드 버전 → 점수</h3><div className={styles.history}>{validDetail.submissions.length ? validDetail.submissions.map(s => <article key={s.submission_id}><strong>{displayTime(s.received_at)}</strong><span>{s.submission_id === validDetail.submission.latest?.submission_id && "최신 · "}{submissionScore(s)}</span><code>{s.code_version || "코드 버전 기록 없음"}</code></article>) : <p>공식 제출 이력이 없습니다.</p>}</div>
        <p>서버 보관 ≠ 공식 제출 ≠ 채점 완료</p>
      </>}
      {validDetail && tab === "activity" && <>
        <h3>관측된 수업 활동</h3><dl className={styles.facts}><dt>과제 열기</dt><dd>{validDetail.analysis.workspace_opened}회</dd><dt>파일 저장</dt><dd>{validDetail.analysis.file_saved}회</dd><dt>제출 시도</dt><dd>{validDetail.analysis.submission_started}회</dd><dt>제출 응답 확인</dt><dd>{validDetail.analysis.submission_finished}회 · 공식 접수와 별도</dd></dl><p>{validDetail.analysis.note}</p>
        <ol className={styles.history}>{validDetail.activities.map((a, i) => <li key={`${a.received_at}-${i}`}><span>{activityLabel(a.kind)}</span><time>{displayTime(a.received_at)}</time></li>)}</ol><p className={styles.footnote}>IDE가 보고한 수업 활동만 표시합니다. 다른 앱·키 입력·웹 검색·화면은 수집하지 않습니다.</p>
      </>}
      {selected && identity && tab === "control" && <ClassroomControls key={`${course}/${selected}/${assignment}`} course={course} student={selected} assignment={assignment} csrf={identity.csrf} allowed={identity.capabilities.includes("classroom.control") && stream !== "denied"} supported={!!scopedDetail?.controls.supported} actions={scopedDetail?.controls.actions || []} commands={scopedDetail?.commands || []} historyStale={!!detailError || !scopedDetail || scopedDetail.commands_version !== selectedCard?.commands_version} intents={commandIntents.current} onCreated={() => setDetailRefresh(n => n + 1)} onDenied={err => { commandIntents.current.clear(); setSelected(null); setDetail(null); setData(null); setError(failure(err)); setIdentity(null); }} />}
    </dialog>
  </AppShell>;
}
