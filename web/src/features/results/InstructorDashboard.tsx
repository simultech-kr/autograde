"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError, bootstrap, entryURL, results, terminalError } from "../../lib/api/client";
import { ContractError, type Bootstrap, type Results } from "../../lib/contracts/instructor";
import { availableModules } from "../registry";
import { defaultQuery, filterKey, formatTime, pageURL, parseQuery, pollDelay, statusNames, statuses, type ResultsQuery, type Sort, type Status } from "./query";
import { ResultsTable, SummaryCards } from "./ResultsView";
import styles from "./dashboard.module.css";

type Failure = { message: string; requestId: string; code: string; terminal: boolean };
function failure(error: unknown): Failure {
  if (error instanceof ApiError) return { message: error.status === 401 ? "교수자 인증이 필요합니다. 인증 후 이 화면으로 돌아옵니다." : error.code === "next_ui_disabled" ? "새 현황 화면이 중단되었거나 이 수업에는 아직 허용되지 않았습니다. 기존 교수자 화면을 사용해 주세요." : error.message,
    requestId: error.requestId, code: error.code, terminal: terminalError(error) };
  if (error instanceof ContractError) return { message: error.message, requestId: "", code: "contract_mismatch", terminal: true };
  return { message: "서버에 연결하지 못했습니다. 마지막 확인 시각을 확인하고 연결 상태를 점검해 주세요.", requestId: "", code: "network_error", terminal: false };
}

export function InstructorDashboard() {
  const [identity, setIdentity] = useState<Bootstrap | null>(null);
  const [course, setCourse] = useState("");
  const [query, setQuery] = useState<ResultsQuery>(defaultQuery);
  const [studentInput, setStudentInput] = useState("");
  const [data, setData] = useState<{ key: string; value: Results } | null>(null);
  const [error, setError] = useState<Failure | null>(null);
  const [loading, setLoading] = useState(true);
  const [booting, setBooting] = useState(true);
  const [changed, setChanged] = useState(false);
  const [automatic, setAutomatic] = useState(true);
  const [paused, setPaused] = useState(false);
  const [lastSuccess, setLastSuccess] = useState("");
  const [bootEpoch, setBootEpoch] = useState(0);
  const [refreshEpoch, setRefreshEpoch] = useState(0);
  const version = useRef<{ scope: string; value: string } | null>(null);
  const automaticRef = useRef(true);
  const pollControl = useRef<() => void>(() => {});
  const viewKey = JSON.stringify([course, query]);
  const visibleData = data?.key === viewKey ? data.value : null;

  // Reauthenticate after history restoration: do not redisplay a previous user's data.
  useEffect(() => {
    const restore = (event: PageTransitionEvent) => { if (event.persisted) { setIdentity(null); setData(null); setBootEpoch(n => n + 1); } };
    const history = () => { setIdentity(null); setData(null); setBootEpoch(n => n + 1); };
    window.addEventListener("pageshow", restore); window.addEventListener("popstate", history);
    return () => { window.removeEventListener("pageshow", restore); window.removeEventListener("popstate", history); };
  }, []);

  useEffect(() => {
    const controller = new AbortController(); let alive = true;
    let timedOut = false;
    const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 15000);
    setBooting(true); setLoading(true); setError(null); setData(null); setIdentity(null); setLastSuccess("");
    version.current = null;
    const params = new URLSearchParams(window.location.search); const initial = parseQuery(params);
    setQuery(initial); setStudentInput(initial.student); setCourse(params.get("course_key") || "");
    bootstrap(controller.signal).then(value => {
      if (!alive || controller.signal.aborted) return;
      if (!availableModules(value.capabilities).some(m => m.id === "results")) {
        throw new ApiError(404, "next_ui_disabled", "새 현황 화면이 허용되지 않았습니다.");
      }
      const requested = params.get("course_key");
      if (requested && !value.courses.some(c => c.course_key === requested)) {
        throw new ApiError(404, "course_not_available", "요청한 수업에 접근할 수 없습니다. 기존 교수자 화면에서 담당 수업을 확인해 주세요.");
      }
      setIdentity(value); setCourse(requested || value.courses[0]?.course_key || ""); setLoading(false);
    }).catch(err => { if (alive && (!controller.signal.aborted || timedOut)) { setError(failure(err)); setLoading(false); } })
      .finally(() => { clearTimeout(timeout); if (alive) setBooting(false); });
    return () => { alive = false; controller.abort(); clearTimeout(timeout); };
  }, [bootEpoch]);

  useEffect(() => {
    if (!identity || !course) return;
    window.history.replaceState(null, "", pageURL(course, query));
    const scope = `${course}:${filterKey(query)}`;
    if (version.current?.scope !== scope) version.current = null;
    let alive = true; let inFlight = false; let stopped = false; let failures = 0;
    let controller: AbortController | null = null; let timer: ReturnType<typeof setTimeout> | undefined;
    setChanged(false); setError(null); setLoading(true); setData(null); setLastSuccess("");
    const available = () => document.visibilityState !== "hidden" && navigator.onLine;
    const schedule = () => { if (alive && !stopped && automaticRef.current && available()) timer = setTimeout(load, pollDelay(failures)); };
    const load = async () => {
      if (!alive || stopped || inFlight || !available()) { if (alive) { setPaused(!available()); setLoading(false); } return; }
      setPaused(false); inFlight = true; controller = new AbortController(); const current = controller;
      let timedOut = false; const timeout = setTimeout(() => { timedOut = true; current.abort(); }, 15000);
      try {
        const response = await results(course, query, current.signal, version.current?.scope === scope ? version.current.value : undefined);
        if (!alive || current.signal.aborted) return;
        if (response.course_key !== course || response.page !== query.page) throw new ContractError();
        const oldVersion = version.current?.value; version.current = { scope, value: response.version };
        setData(previous => previous?.key === viewKey && oldVersion === response.version ? previous : { key: viewKey, value: response });
        setLastSuccess(response.server_time); setError(null); failures = 0;
      } catch (err) {
        if (!alive || (current.signal.aborted && !timedOut)) return;
        if (err instanceof ApiError && err.code === "result_set_changed") {
          stopped = true; setChanged(true); setError(null);
        } else {
          const next = failure(err); setError(next); failures++;
          if (next.terminal) { stopped = true; setData(null); setIdentity(null); setLastSuccess(""); version.current = null; }
        }
      } finally {
        clearTimeout(timeout); inFlight = false;
        if (alive) { setLoading(false); schedule(); }
      }
    };
    const visibility = () => {
      clearTimeout(timer); setPaused(!available());
      if (!available()) controller?.abort(); else if (automaticRef.current) void load();
    };
    pollControl.current = () => {
      clearTimeout(timer);
      if (automaticRef.current) void load(); else controller?.abort();
    };
    document.addEventListener("visibilitychange", visibility); window.addEventListener("online", visibility); window.addEventListener("offline", visibility);
    void load();
    return () => { alive = false; controller?.abort(); clearTimeout(timer); pollControl.current = () => {}; document.removeEventListener("visibilitychange", visibility); window.removeEventListener("online", visibility); window.removeEventListener("offline", visibility); };
  }, [identity, course, query, refreshEpoch, viewKey]);

  function changeCourse(key: string) {
    version.current = null; setData(null); setLastSuccess(""); setError(null); setCourse(key); setQuery(defaultQuery); setStudentInput("");
  }
  function changeFilters(patch: Partial<ResultsQuery>) { setQuery(q => ({ ...q, ...patch, page: 1 })); }
  function refresh() { version.current = null; setData(null); setChanged(false); setQuery(q => ({ ...q, page: 1 })); setRefreshEpoch(n => n + 1); }
  function search(event: FormEvent<HTMLFormElement>) { event.preventDefault(); changeFilters({ student: studentInput.trim() }); }
  const selectedCourse = identity?.courses.find(c => c.course_key === course);
  const assignments = visibleData?.assignments || [];
  const unknownAssignment = query.assignment && !assignments.some(a => a.assignment_id === query.assignment);

  return <div data-testid="dashboard" className={styles.shell}>
    <header className={styles.header}><a className={styles.brand} href="/app"><span className={styles.mark}>A</span> Autograde <span className={styles.audience}>교수자</span></a>
      <a className={styles.legacy} href="/instructor">기존 관리 화면 ↗</a></header>
    <main id="main" className={styles.main}>
      <div className={styles.heading}><div><p className={styles.eyebrow}>수업 운영 / 제출 현황</p><h1>제출·채점 현황</h1><p className={styles.description}>최신 제출의 상태와 점수를 확인하고, 필요한 학생의 제출 이력을 검토합니다.</p></div>
        <span className={styles.readOnly}>조회 전용 · 파일럿</span></div>

      {error && <section role="alert" data-testid="api-error" className={styles.error}><h2>{error.terminal ? "접근을 확인해 주세요" : "연결을 확인해 주세요"}</h2><p>{error.message}</p>
        {error.requestId && <p className={styles.requestId}>문의용 요청 ID: {error.requestId}</p>}
        {(error.terminal || !identity) && <a data-testid="auth-entry" className={styles.button} href={entryURL(course)}>교수자 인증</a>}
        {!identity && !error.terminal && <button onClick={() => setBootEpoch(n => n + 1)}>다시 연결</button>}
        {identity && !error.terminal && <button onClick={refresh}>다시 조회</button>}
        <a className={styles.inlineLink} href="/instructor">기존 교수자 화면</a></section>}

      {booting && <section className={styles.notice}><p role="status">교수자 접근 권한을 확인하고 있습니다…</p><a data-testid="auth-entry" className={styles.button} href={entryURL(course)}>교수자 인증</a><p className={styles.summaryNote}>처음 접속하거나 인증 창이 표시되지 않으면 교수자 인증을 선택해 주세요.</p></section>}
      {!identity && !booting && !error && <section className={styles.notice}><h2>교수자 인증</h2><p>기존 교수자 계정으로 인증한 후 담당 수업을 확인할 수 있습니다.</p><a data-testid="auth-entry" className={styles.button} href={entryURL(course)}>교수자 인증</a></section>}

      {identity && <>
        <section className={styles.context} aria-label="수업 선택"><label htmlFor="course-select">교과목 · 분반</label>
          <select id="course-select" data-testid="course-select" value={course} onChange={e => changeCourse(e.target.value)} disabled={!identity.courses.length}>
            {!identity.courses.length && <option value="">허용된 수업 없음</option>}
            {identity.courses.map(c => <option key={c.course_key} value={c.course_key}>{c.name || c.code || c.course_key}{c.section ? ` · ${c.section}` : ""} ({c.course_key})</option>)}
          </select><span className={styles.identity}>{identity.principal.display_name || "교수자"} · {identity.principal.mode === "shared" ? "공유 인증" : "개인 인증"}</span>
        </section>
        {!course && <p className={styles.notice}>새 화면에 허용된 담당 수업이 없습니다. 기존 관리 화면에서 수업과 권한을 확인해 주세요.</p>}
        {course && <>
          <div className={styles.courseHeading}><h2>{selectedCourse?.name || selectedCourse?.code || course}</h2><span className={styles.muted}>{selectedCourse?.section} · 모든 시간 KST</span></div>
          {visibleData && <><SummaryCards summary={visibleData.summary} /><p className={styles.summaryNote}>요약 범위: {visibleData.summary.scope === "course" ? "선택 수업 전체" : "선택한 과제"}. 아래 상태·학생 검색과 관계없이 집계합니다. 누적 제출 {visibleData.summary.submission_attempts}회.</p></>}

          <section className={styles.results} aria-label="학생 결과">
            <form data-testid="filter-form" className={styles.filters} onSubmit={search}>
              <label>실습 과제<select data-testid="assignment-select" value={query.assignment} onChange={e => changeFilters({ assignment: e.target.value })}>
                <option value="">전체 과제</option>{unknownAssignment && <option value={query.assignment}>선택한 과제 ({query.assignment})</option>}
                {assignments.map(a => <option key={a.assignment_id} value={a.assignment_id}>{a.title || a.assignment_key}{!a.ready ? " · 비공개" : ""}</option>)}</select></label>
              <label>제출 상태<select data-testid="status-select" value={query.status} onChange={e => changeFilters({ status: e.target.value as Status })}>{statuses.map(s => <option key={s} value={s}>{statusNames[s]}</option>)}</select></label>
              <label>정렬<select data-testid="sort-select" value={query.sort} onChange={e => changeFilters({ sort: e.target.value as Sort })}><option value="recent">최근 제출 우선</option><option value="student">학생 순</option><option value="assignment">과제 순</option></select></label>
              <label className={styles.search}>학생 검색<input data-testid="student-search" type="search" maxLength={100} placeholder="학번 또는 이름" value={studentInput} onChange={e => setStudentInput(e.target.value)} /></label>
              <button data-testid="apply-filters" type="submit" className={styles.secondary}>검색</button>
            </form>
            <div className={styles.liveBar}>
              <p data-testid="last-success" role="status"><span className={styles.connectionDot} />{changed ? "새 결과 대기" : paused ? "자동 확인 일시 정지" : !automatic ? "수동 확인" : error ? "재연결 대기" : "자동 확인 중"}<span className={styles.lastTime}>{lastSuccess ? `마지막 확인 ${formatTime(lastSuccess)}` : "아직 확인되지 않음"}</span></p>
              <div className={styles.liveActions}><label className={styles.toggle}><input data-testid="poll-toggle" type="checkbox" checked={automatic} onChange={e => { automaticRef.current = e.target.checked; setAutomatic(e.target.checked); pollControl.current(); }} />자동 확인</label><button type="button" data-testid="refresh-results" className={styles.secondary} onClick={refresh} disabled={loading}>새로고침</button></div>
            </div>
            {changed && <div role="status" data-testid="update-notice" className={styles.notice}><strong>목록이 변경되었습니다.</strong><span> 읽는 중인 결과를 유지했습니다. 최신 목록은 첫 페이지부터 다시 확인해 주세요.</span><button type="button" onClick={refresh}>최신 결과 반영</button></div>}
            <div aria-busy={loading} className={styles.tableArea}>{loading && !visibleData ? <p role="status" className={styles.loading}>제출 결과를 불러오고 있습니다…</p> : visibleData ? <ResultsTable rows={visibleData.rows} /> : !error && !changed ? <p className={styles.loading}>연결되면 결과를 표시합니다.</p> : null}</div>
            <footer className={styles.pagination}><span>{visibleData ? `조건에 맞는 ${visibleData.total}건 · ${visibleData.page} / ${Math.max(1, Math.ceil(visibleData.total / visibleData.page_size))}페이지` : "목록 확인 대기"}</span><div><button data-testid="previous-page" className={styles.secondary} disabled={loading || changed || query.page <= 1} onClick={() => setQuery(q => ({ ...q, page: q.page - 1 }))}>이전</button><button data-testid="next-page" className={styles.secondary} disabled={loading || changed || !visibleData || query.page * visibleData.page_size >= visibleData.total} onClick={() => setQuery(q => ({ ...q, page: q.page + 1 }))}>다음</button></div></footer>
          </section>
        </>}
      </>}
      <footer className={styles.footnote}>접수·채점 대기·오류는 0점이 아닙니다. 이전 최고점은 현재 제출을 제외한 결과입니다.<br />공용 PC에서는 사용 후 모든 브라우저 창을 종료하세요. 이 화면은 브라우저의 Basic 인증 기록을 삭제할 수 없습니다.</footer>
    </main>
  </div>;
}
