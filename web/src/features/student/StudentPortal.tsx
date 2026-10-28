"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { studentRequest, StudentApiError } from "./client";
import { parseBootstrap, parseCourse, parseClaim, coursePath, courseKey, courseMeta, timeText, scoreText, submissionState, stages, remainingSeconds, RequestVersion, StudentContractError,
  type Bootstrap, type CourseData, type Course, type Receipt, type Claim } from "./model";
import s from "./student.module.css";

type View = "home" | "catalog" | "course" | "account";
type Auth = "login" | "register" | "reset";
const errorText = (error: unknown) => error instanceof Error ? error.message : "요청을 처리하지 못했습니다. 다시 시도하세요.";
const courseStatus = (course: Course, verified: boolean) => course.enrolled ? "수강 중" : course.status === "suspended" ? "수강 상태 확인 필요" : course.status === "pending" ? verified ? "명단 확인 대기" : "계정 활성화 대기" : "신청 가능";
const courseHelp = (course: Course, verified: boolean) => course.enrolled ? "과제·자료와 공식 제출 현황을 확인하세요." : course.status === "suspended" ? "수강이 중지되었습니다. 교수자에게 수강 상태 확인을 요청하세요." : course.status === "pending" ? !verified ? "교수자에게 본인 확인을 받고 활성화 코드를 입력하세요." : "교수자 명단에 학번이 있는지 확인을 요청하세요. 명단이 반영되면 자동 연결됩니다." : "수업을 선택해 신청하세요. 활성화된 계정과 교수자 명단을 함께 확인합니다.";

export function StudentPortal() {
  const [boot, setBoot] = useState<Bootstrap | null>(null), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [view, setView] = useState<View>("home"), [auth, setAuth] = useState<Auth>("login"), [selectedCourse, setSelectedCourse] = useState("");
  const [data, setData] = useState<CourseData | null>(null), [courseLoading, setCourseLoading] = useState(false), [courseError, setCourseError] = useState("");
  const [claimBusy, setClaimBusy] = useState(false), [clockOffset, setClockOffset] = useState(0);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [search, setSearch] = useState(""), [selected, setSelected] = useState<string[]>([]);
  const [claim, setClaim] = useState<Claim | null>(null), [now, setNow] = useState(Date.now()), [lastRead, setLastRead] = useState("");
  const [taskFilter, setTaskFilter] = useState("all"), [theme, setTheme] = useState<"light" | "dark">("light");
  const identityVersion = useRef(new RequestVersion()), courseVersion = useRef(new RequestVersion());
  const accountController = useRef<AbortController | null>(null), courseController = useRef<AbortController | null>(null);
  const claimVersion = useRef(new RequestVersion()), claimController = useRef<AbortController | null>(null), currentIdentity = useRef<string | null>(null);
  const actionController = useRef<AbortController | null>(null), disposed = useRef(false), heading = useRef<HTMLHeadingElement>(null);
  const student = boot?.student, currentCourse = boot?.courses.find(item => item.course_key === selectedCourse && item.enrolled);
  const available = (boot?.courses || []).filter(item => `${item.name} ${item.code} ${item.section} ${item.term}`.toLocaleLowerCase("ko-KR").includes(search.trim().toLocaleLowerCase("ko-KR")));
  const enrolled = (boot?.courses || []).filter(item => item.status !== "available" || item.enrolled);
  const serverNow = now + clockOffset;
  const remaining = remainingSeconds(boot?.expires_at || null, serverNow);
  const activeData = data?.course.course_key === selectedCourse && data.student_key === student?.student_key && currentCourse ? data : null;

  function clearPrivate() {
    courseController.current?.abort(); actionController.current?.abort(); claimController.current?.abort(); claimVersion.current.next(); courseVersion.current.next(); setBusy(false); setClaimBusy(false);
    setData(null); setClaim(null); setSelected([]); setSelectedCourse(""); setCourseError(""); setLastRead(""); setView("home");
  }
  function route(next: View, key = "") {
    claimVersion.current.next(); claimController.current?.abort(); setClaimBusy(false); setView(next); setClaim(null); setNotice(""); setError("");
    if (next === "course") setSelectedCourse(key);
    else { courseController.current?.abort(); courseVersion.current.next(); setData(null); setSelectedCourse(""); }
    const params = next === "course" ? `?course_key=${encodeURIComponent(key)}` : next !== "home" ? `?view=${next}` : "";
    window.history.pushState(null, "", `/app/student${params}`);
    requestAnimationFrame(() => heading.current?.focus());
  }
  const applyBootstrap = useCallback((next: Bootstrap) => {
    if (currentIdentity.current !== (next.student?.student_key || null) || !next.authenticated) {
      courseController.current?.abort(); courseVersion.current.next(); claimController.current?.abort(); claimVersion.current.next();
      setData(null); setClaim(null); setSelected([]); setLastRead(""); setClaimBusy(false);
    }
    currentIdentity.current = next.student?.student_key || null;
    setClockOffset(next.server_time ? Date.parse(next.server_time) - Date.now() : 0);
    setNow(Date.now()); setBoot(next);
  }, []);
  const refreshAccount = useCallback(async () => {
    const version = identityVersion.current.next(); accountController.current?.abort(); const controller = new AbortController(); accountController.current = controller;
    setLoading(true); setError("");
    try {
      const next = parseBootstrap(await studentRequest("/student/api/bootstrap", undefined, controller.signal));
      if (!disposed.current && identityVersion.current.current(version)) applyBootstrap(next);
    } catch (error) { if (!controller.signal.aborted && !disposed.current && identityVersion.current.current(version)) { setError(errorText(error)); if (error instanceof StudentApiError && [401, 403].includes(error.status)) { setBoot(null); setData(null); setClaim(null); currentIdentity.current = null; } } }
    finally { if (!disposed.current && identityVersion.current.current(version)) setLoading(false); }
  }, [applyBootstrap]);
  useEffect(() => {
    disposed.current = false;
    setTheme(matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    function readRoute() {
      const params = new URLSearchParams(window.location.search), key = params.get("course_key");
      claimController.current?.abort(); claimVersion.current.next(); setClaimBusy(false); setClaim(null);
      if (key) {
        try { setSelectedCourse(courseKey(key)); setView("course"); } catch { setSelectedCourse(""); setView("home"); }
      } else { setSelectedCourse(""); setView(["catalog", "account"].includes(params.get("view") || "") ? params.get("view") as View : "home"); }
    }
    function revalidate() {
      accountController.current?.abort(); courseController.current?.abort(); actionController.current?.abort(); claimController.current?.abort();
      identityVersion.current.next(); courseVersion.current.next(); claimVersion.current.next(); currentIdentity.current = null;
      setBoot(null); setData(null); setClaim(null); setSelected([]); setBusy(false); setClaimBusy(false); setLastRead("");
      if (document.visibilityState === "visible") void refreshAccount();
    }
    function restored(event: PageTransitionEvent) { if (event.persisted) revalidate(); }
    readRoute(); window.addEventListener("popstate", readRoute); window.addEventListener("focus", revalidate); window.addEventListener("pageshow", restored); document.addEventListener("visibilitychange", revalidate); void refreshAccount();
    return () => { disposed.current = true; identityVersion.current.next(); courseVersion.current.next(); accountController.current?.abort(); courseController.current?.abort(); actionController.current?.abort(); claimController.current?.abort(); claimVersion.current.next(); window.removeEventListener("popstate", readRoute); window.removeEventListener("focus", revalidate); window.removeEventListener("pageshow", restored); document.removeEventListener("visibilitychange", revalidate); };
  }, [refreshAccount]);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => {
    if (boot?.authenticated && remaining === 0) {
      clearPrivate(); setBoot(null); setNotice("로그인 시간이 끝났습니다. 다시 로그인하세요. 작성한 비밀번호와 수령 코드는 화면에서 지웠습니다."); void refreshAccount();
    }
  }, [boot?.authenticated, remaining, refreshAccount]); // Expiry never silently renews authentication.
  useEffect(() => { if (claim && remainingSeconds(claim.expires_at, serverNow) === 0) { setClaim(null); setNotice("수령 코드가 만료되었습니다. 과제에서 새 코드를 발급하세요."); } }, [claim, serverNow]);
  const refreshCourse = useCallback(async (background = false) => {
    if (!selectedCourse || !boot?.authenticated || !boot.courses.some(item => item.course_key === selectedCourse && item.enrolled)) return;
    const version = courseVersion.current.next(); courseController.current?.abort(); const controller = new AbortController(); courseController.current = controller;
    if (!background) setCourseLoading(true);
    try {
      const next = parseCourse(await studentRequest(coursePath(selectedCourse), undefined, controller.signal), selectedCourse, boot.student!.student_key);
      if (!disposed.current && courseVersion.current.current(version)) { setData(next); setCourseError(""); setLastRead(new Date().toISOString()); }
    } catch (error) {
      if (!controller.signal.aborted && !disposed.current && courseVersion.current.current(version)) {
        setCourseError(errorText(error));
        if (error instanceof StudentContractError || error instanceof StudentApiError && [401, 403, 404].includes(error.status)) { setData(null); setClaim(null); setBoot(null); setSelectedCourse(""); setView("home"); currentIdentity.current = null; window.history.replaceState(null, "", "/app/student"); setError(errorText(error)); void refreshAccount(); }
      }
    } finally { if (!disposed.current && courseVersion.current.current(version)) setCourseLoading(false); }
  }, [boot, selectedCourse, refreshAccount]);
  useEffect(() => {
    setData(null); setCourseError(""); setLastRead("");
    if (view === "course") void refreshCourse();
    return () => { courseVersion.current.next(); courseController.current?.abort(); };
  }, [view, selectedCourse, refreshCourse]);
  useEffect(() => {
    if (view !== "course" || !activeData) return;
    const timer = setInterval(() => { if (document.visibilityState === "visible" && !busy) void refreshCourse(true); }, 15000);
    return () => clearInterval(timer);
  }, [view, Boolean(activeData), busy, refreshCourse]);
  async function action(name: string, fields: Record<string, string>, after?: () => void) {
    if (!boot || busy) return;
    const version = identityVersion.current.next(); accountController.current?.abort(); actionController.current?.abort();
    const controller = new AbortController(); actionController.current = controller; setBusy(true); setError(""); setNotice("");
    try {
      const next = parseBootstrap(await studentRequest(`/student/api/${name}`, { csrf: boot.csrf, ...fields }, controller.signal));
      if (disposed.current || !identityVersion.current.current(version)) return;
      applyBootstrap(next); setSelected([]); setClaim(null);
      if (!next.authenticated) { clearPrivate(); setAuth("login"); }
      else if (name === "register") route("catalog");
      else if (name === "login") { if (!selectedCourse) route("home"); }
      else after?.();
      setNotice(next.message || ({ login: "로그인했습니다.", register: "가입했습니다. 수강할 수업을 선택하세요.", enroll: "수강 신청 상태를 확인했습니다.", withdraw: "수강 신청을 취소했습니다.", activate: "계정 활성화 상태를 확인했습니다.", logout: "학생 웹에서 로그아웃했습니다.", password: "비밀번호를 변경했습니다. 새 비밀번호로 로그인하세요.", reset: "비밀번호를 재설정했습니다. 새 비밀번호로 로그인하세요." }[name] || "처리했습니다."));
    } catch (error) { if (!controller.signal.aborted && !disposed.current && identityVersion.current.current(version)) setError(errorText(error)); }
    finally { if (!disposed.current && identityVersion.current.current(version)) { setBusy(false); setLoading(false); } }
  }
  function submitForm(event: FormEvent<HTMLFormElement>, name: string) {
    event.preventDefault(); if (busy) return; const form = event.currentTarget, fields = Object.fromEntries(Array.from(new FormData(form).entries()).map(([key, value]) => [key, String(value)]));
    form.reset(); void action(name, fields);
  }
  async function issueClaim(assignmentId: string) {
    if (!activeData || busy || claimBusy) return;
    const expectedCourse = selectedCourse, version = claimVersion.current.next(); const controller = new AbortController(); claimController.current = controller;
    setClaimBusy(true); setError(""); setClaim(null);
    try {
      const value = parseClaim(await studentRequest(`${coursePath(expectedCourse)}/claims`, { csrf: activeData.csrf, assignment_id: assignmentId }, controller.signal), assignmentId, boot!.student!.student_key);
      if (!disposed.current && claimVersion.current.current(version)) setClaim(value);
    } catch (error) { if (!controller.signal.aborted && !disposed.current && claimVersion.current.current(version)) { setError(errorText(error)); if (error instanceof StudentContractError || error instanceof StudentApiError && [401, 403].includes(error.status)) { setData(null); setClaim(null); setBoot(null); setSelectedCourse(""); setView("home"); currentIdentity.current = null; window.history.replaceState(null, "", "/app/student"); setError(errorText(error)); void refreshAccount(); } } }
    finally { if (!disposed.current && claimVersion.current.current(version)) setClaimBusy(false); }
  }
  const title = !student ? auth === "register" ? "학생 회원가입" : auth === "reset" ? "회원 비밀번호 재설정" : "학생 로그인" : view === "catalog" ? "수강할 수업 선택" : view === "account" ? "내 계정" : view === "course" ? currentCourse?.name || "수업 확인" : "내 수업";
  return <div className={s.portal} data-theme={theme}>
    <header className={s.header}><a href="/app/student" className={s.brand}>autograde<span>학생 포털</span></a><div className={s.headerActions}><button className={s.quiet} onClick={() => setTheme(theme === "light" ? "dark" : "light")}>{theme === "light" ? "어두운 테마" : "밝은 테마"}</button>{student && <><span className={s.identity}>{student.student_key}</span><button className={s.quiet} disabled={busy} onClick={() => void action("logout", {})}>로그아웃</button></>}</div></header>
    {student && <nav className={s.nav} aria-label="학생 메뉴">{([["home", "내 수업"], ["catalog", "수강 선택"], ["account", "내 계정"]] as const).map(([key, label]) => <button key={key} aria-current={view === key ? "page" : undefined} onClick={() => route(key)}>{label}</button>)}<span className={s.session}>{Math.floor(remaining / 60)}분 {String(remaining % 60).padStart(2, "0")}초 후 로그인 종료</span></nav>}
    <main className={`${s.main} ${view === "course" ? s.courseMain : ""}`} id="main" tabIndex={-1}>
      <div className={s.heading}><div><p className={s.eyebrow}>AUTOGRADE · LEARNING PORTAL</p><h1 ref={heading} tabIndex={-1}>{title}</h1><p>{student ? view === "course" && currentCourse ? `${currentCourse.code} · ${courseMeta(currentCourse)}` : "웹에서 수업과 자료를 확인하고, IDE에서 작성·실행·공식 제출합니다." : "학생 계정으로 로그인한 뒤 수강할 수업을 선택하세요."}</p></div>{student && <button className={s.secondary} disabled={busy || loading} onClick={() => view === "course" ? void refreshCourse() : void refreshAccount()}>상태 새로고침</button>}</div>
      {notice && <div className={s.notice} role="status">{notice}<button aria-label="알림 닫기" onClick={() => setNotice("")}>×</button></div>}
      {error && <div className={s.error} role="alert"><p>{error}</p>{!boot && <button className={s.secondary} onClick={() => void refreshAccount()}>다시 연결</button>}{boot && <button className={s.quiet} disabled={busy} onClick={() => void refreshAccount()}>로그인·수강 상태 다시 확인</button>}</div>}
      {student && remaining > 0 && remaining <= 60 && <p className={s.warning} role="status">곧 로그인이 종료됩니다. 종료되면 다시 로그인해야 합니다. 수령 코드는 화면에서 지워집니다.</p>}
      {loading && !boot && <p role="status" className={s.empty}>학생 포털에 연결하고 있습니다…</p>}
      {boot && !student && <div className={s.authLayout}><section className={s.panel}><div className={s.tabs} aria-label="계정 시작">{([["login", "로그인"], ["register", "회원가입"], ["reset", "비밀번호 분실"]] as const).map(([key, label]) => <button key={key} disabled={busy} aria-pressed={auth === key} onClick={() => { setAuth(key); setError(""); }}>{label}</button>)}</div><form key={auth} onSubmit={event => submitForm(event, auth)}><label>학번<input name="student_key" inputMode="numeric" pattern="[0-9]{4,20}" minLength={4} maxLength={20} autoComplete="off" required /></label>{auth === "reset" && <label>관리자가 제공한 재설정 코드<input name="token" autoComplete="off" maxLength={256} required /></label>}<label>{auth === "reset" ? "새 회원 비밀번호" : "회원 비밀번호"}<input name={auth === "reset" ? "new_password" : "password"} type="password" autoComplete="off" minLength={8} maxLength={128} required /></label><p className={s.muted}>8~128자. 공용 PC에 비밀번호를 저장하지 마세요.</p><button className={s.primary} disabled={busy || loading}>{busy ? "처리 중…" : auth === "register" ? "가입하고 수업 선택" : auth === "reset" ? "비밀번호 재설정" : "로그인"}</button></form></section><aside className={s.guide}><h2>가입부터 실습까지</h2><ol><li>학번과 회원 비밀번호로 가입합니다.</li><li>운영 중인 교과목·분반을 선택합니다.</li><li>교수자에게 본인 확인 후 계정을 활성화합니다.</li><li>명단이 연결된 수업에서 수령 코드를 발급해 IDE에 입력합니다.</li></ol><p>비밀번호를 잊었다면 관리자에게 본인 확인을 받은 뒤 일회용 재설정 코드를 요청하세요.</p><a href="/courses">기존 6자리 수업 비밀번호로 계정 연결</a></aside></div>}
      {student && !student.verified && <div className={s.warning}><strong>계정 활성화가 필요합니다.</strong><p>수강할 수업을 신청한 뒤 교수자에게 본인 확인을 받으세요. 명단에 학번이 있어도 활성화 전에는 과제·자료를 열 수 없습니다.</p><button className={s.secondary} onClick={() => route("account")}>활성화 코드 입력</button></div>}
      {student && view === "home" && <><div className={s.sectionHeading}><h2>신청한 수업 <span>{enrolled.length}</span></h2><button className={s.primary} onClick={() => route("catalog")}>수업 추가</button></div>{!enrolled.length && <div className={s.empty}><h2>아직 신청한 수업이 없습니다.</h2><p>수업 목록에서 교과목과 분반을 확인하고 신청하세요.</p><button className={s.primary} onClick={() => route("catalog")}>수강할 수업 선택</button></div>}<div className={s.grid}>{enrolled.map(course => <article className={s.course} key={course.course_key}><span className={s.badge} data-tone={course.enrolled ? "success" : "warning"}>{courseStatus(course, student.verified)}</span><p className={s.eyebrow}>{course.code}</p><h2>{course.name || course.course_key}</h2><p className={s.muted}>{courseMeta(course)}</p><p>{courseHelp(course, student.verified)}</p><div className={s.actions}>{course.enrolled ? <button className={s.primary} onClick={() => route("course", course.course_key)}>수업 열기</button> : !student.verified ? <button className={s.secondary} onClick={() => route("account")}>계정 활성화</button> : <button className={s.secondary} onClick={() => void refreshAccount()}>명단 연결 다시 확인</button>}{course.can_withdraw && <button className={s.quiet} disabled={busy} onClick={() => void action("withdraw", { course_key: course.course_key })}>신청 취소</button>}</div></article>)}</div><section className={s.guide}><h2>실습 파일과 로그인은 별도로 관리합니다.</h2><p>IDE의 작업 자동 저장은 공식 제출이 아닙니다. 채점을 받으려면 IDE에서 ‘공식 제출’을 선택하세요. 공용 PC를 떠날 때는 IDE 종료와 웹 로그아웃을 모두 진행하세요.</p></section></>}
      {student && view === "catalog" && <><section className={s.panel}><label className={s.search}>수업 검색<input value={search} onChange={event => setSearch(event.target.value)} placeholder="교과목명·코드·학기·분반" /></label><div className={s.catalog}>{available.map(course => <label key={course.course_key} className={s.catalogRow}><input type="checkbox" checked={selected.includes(course.course_key)} disabled={course.status !== "available" || course.enrolled || busy} onChange={event => setSelected(keys => event.target.checked ? [...keys, course.course_key] : keys.filter(key => key !== course.course_key))} /><span><strong>{course.name || course.course_key}</strong><small>{course.code} · {courseMeta(course)}</small><small>{courseHelp(course, student.verified)}</small></span><span className={s.badge}>{courseStatus(course, student.verified)}</span></label>)}</div>{!available.length && <p className={s.empty}>검색한 수업이 없습니다. 교과목명·코드 또는 분반을 확인하세요.</p>}<p role="status" className={s.muted}>{available.length}개 표시 · 전체 {boot.courses.length}개</p></section><div className={s.enrollBar}><div><strong>선택한 수업 {selected.length}개</strong><p>{boot.courses.filter(course => selected.includes(course.course_key)).map(course => course.section ? `${course.name} ${course.section}분반` : course.name || course.course_key).join(" · ") || "수강할 수업을 선택하세요."}</p></div><button className={s.primary} disabled={!selected.length || busy} onClick={() => void action("enroll", { course_keys: JSON.stringify(selected) }, () => route("home"))}>{busy ? "신청 중…" : `선택한 ${selected.length}개 수업 신청`}</button></div></>}
      {student && view === "account" && <div className={s.grid}><section className={s.panel}><h2>계정 정보</h2><dl><dt>학번</dt><dd>{student.student_key}</dd><dt>계정 상태</dt><dd>{student.verified ? "활성화 완료" : "교수자 본인 확인 필요"}</dd><dt>웹 로그인 만료</dt><dd>{timeText(boot.expires_at)} KST</dd></dl>{!student.verified && <form onSubmit={event => submitForm(event, "activate")}><label>교수자가 제공한 활성화 코드<input name="token" autoComplete="off" maxLength={256} required /></label><button disabled={busy} className={s.primary}>계정 활성화</button></form>}<p className={s.muted}>활성화 코드는 IDE에 입력하는 과제 수령 코드와 다릅니다.</p></section><section className={s.panel}><h2>회원 비밀번호 변경</h2><form onSubmit={event => submitForm(event, "password")}><label>현재 회원 비밀번호<input name="current_password" type="password" autoComplete="off" minLength={8} maxLength={128} required /></label><label>새 회원 비밀번호<input name="new_password" type="password" autoComplete="off" minLength={8} maxLength={128} required /></label><p className={s.muted}>변경하면 기존 회원 로그인이 종료됩니다. 새 비밀번호로 다시 로그인하세요.</p><button className={s.primary} disabled={busy}>비밀번호 변경</button></form></section></div>}
      {student && view === "course" && <><div className={s.courseTop}><button className={s.quiet} onClick={() => route("home")}>← 내 수업</button>{activeData && <p className={s.muted}>마지막 확인 {timeText(lastRead)} KST · 15초마다 갱신</p>}<div className={s.actions}>{activeData?.links.materials && <a className={s.secondary} href={activeData.links.materials}>내 자료 다운로드</a>}{activeData?.links.exam_papers && <a className={s.secondary} href={activeData.links.exam_papers}>시험지 다운로드</a>}</div></div>{!currentCourse && <div className={s.empty}><h2>이 수업을 아직 이용할 수 없습니다.</h2><p>수강 선택과 계정 활성화, 교수자 명단 연결 상태를 확인하세요.</p><button className={s.secondary} onClick={() => route("catalog")}>수강 상태 확인</button></div>}{courseLoading && !activeData && <p role="status">수업 정보를 확인하고 있습니다…</p>}{courseError && <div className={s.error} role="alert"><p>{courseError}</p>{activeData && <p>마지막 확인 자료입니다. 최신 상태로 확정하지 마세요.</p>}<button className={s.secondary} onClick={() => void refreshCourse()}>다시 조회</button></div>}{activeData && <>
        {claim && <section className={s.claim} aria-label="과제 수령 코드" role="status"><div className={s.sectionHeading}><h2>{claim.title}</h2><button className={s.quiet} onClick={() => setClaim(null)}>코드 숨기기</button></div><p>Autograde IDE → 수령 코드 입력</p><code>{claim.code}</code><p>{timeText(claim.expires_at)} KST 만료 · {remainingSeconds(claim.expires_at, serverNow)}초 남음</p><p>IDE 서버 주소: <span className={s.mono}>{claim.api_url}</span></p><p className={s.muted}>코드를 IDE에 입력한 뒤 과제를 다운로드하거나 기존 실습을 여세요. 웹 비밀번호는 IDE에 입력하지 않습니다.</p></section>}
        <section><div className={s.sectionHeading}><h2>실습 과제 <span>{activeData.overview.assignments.length}</span></h2><label>표시할 과제<select value={taskFilter} onChange={event => setTaskFilter(event.target.value)}><option value="all">전체</option><option value="open">공식 제출 전</option><option value="pending">접수·채점 중</option><option value="attention">확인 필요</option><option value="completed">결과 공개</option></select></label></div><div className={s.grid}>{activeData.overview.assignments.filter(assignment => { const receipt = activeData.overview.latest.find(row => row.assignment_id === assignment.assignment_id), state = submissionState(receipt); return taskFilter === "all" || taskFilter === "open" && !receipt || taskFilter === "pending" && Boolean(receipt) && state.tone === "info" || taskFilter === "attention" && ["warning", "error"].includes(state.tone) || taskFilter === "completed" && state.scored; }).map(assignment => {
          const receipt = activeData.overview.latest.find(row => row.assignment_id === assignment.assignment_id), state = submissionState(receipt), closed = Boolean(assignment.due_at && Date.parse(assignment.due_at) <= serverNow);
          return <article className={s.course} key={assignment.assignment_id}><span className={s.badge} data-tone={state.tone}>{state.label}</span><p className={s.eyebrow}>{assignment.assignment_key}</p><h3>{assignment.title}</h3><p className={s.muted}>마감 {timeText(assignment.due_at)}{assignment.due_at && " KST"}{closed && " · 마감됨"}</p><p className={s.muted}>{assignment.accepted_at ? `IDE 수락 ${timeText(assignment.accepted_at)}` : "IDE에서 아직 수락하지 않았습니다."}</p>{receipt && <ReceiptSummary row={receipt} />}<p>{state.hint}</p><button className={s.primary} disabled={closed || busy || claimBusy || Boolean(courseError)} onClick={() => void issueClaim(assignment.assignment_id)}>{closed ? "수령 기간 마감" : assignment.accepted_at ? "수령 코드 다시 발급" : "수령 코드 발급"}</button>{assignment.accepted_at && <p className={s.muted}>IDE의 ‘내 실습’에서 같은 과제를 선택해 기존 작업을 이어갈 수 있습니다.</p>}</article>;
        })}</div>{!activeData.overview.assignments.length && <p className={s.empty}>공개된 과제가 없습니다. 교수자의 과제 공개를 기다려 주세요.</p>}{activeData.overview.assignments_truncated && <p className={s.muted}>일부 과제만 표시합니다. <a href={activeData.links.assignments}>전체 과제 확인</a></p>}</section>
        <section className={s.panel}><h2>공식 제출 이력</h2><p className={s.muted}>작업 자동 저장은 공식 제출에 포함되지 않습니다. 모든 시각은 한국 시간입니다.</p>{!activeData.overview.history.length && <p>아직 공식 제출한 기록이 없습니다.</p>}{activeData.overview.history.map(row => <details key={row.submission_id} className={s.history}><summary>{row.title} · 버전 {row.version} · {timeText(row.received_at)} · {submissionState(row).label}</summary><ReceiptSummary row={row} previous={false} /></details>)}{activeData.overview.history_truncated && <p className={s.muted}>최근 {activeData.overview.limit}건을 표시합니다. 이전 제출본과 자세한 피드백은 IDE에서 확인하세요.</p>}</section>
      </>}</>}
      <footer className={s.footer}><span>Autograde · 웹에서 준비, IDE에서 실습</span><span>자동 저장 ≠ 공식 제출 · 공용 PC에서는 웹 로그아웃과 IDE 종료</span></footer>
    </main>
  </div>;
}

function ReceiptSummary({ row, previous = true }: { row: Receipt; previous?: boolean }) {
  const state = submissionState(row);
  return <div className={s.receipt}>
    <ol className={s.progress} aria-label="현재 제출 처리 단계">{stages.map((label, index) => <li key={label} data-active={index === state.stage} data-complete={index < state.stage}>{label}</li>)}</ol>
    <dl className={s.scores}><div><dt>이번 제출 점수</dt><dd>{state.scored ? scoreText(row.score, row.max_score) : "점수 미확정"}</dd></div>{previous && <div><dt>이전 제출 중 최고점</dt><dd>{scoreText(row.previous_best_score, row.previous_best_max_score)}</dd><small>{row.previous_best_at ? timeText(row.previous_best_at) : "현재 제출 제외"}</small></div>}</dl>
    <p className={s.muted}>접수 {timeText(row.received_at)} KST · 버전 {row.version}</p><p className={s.muted}>접수번호 <span className={s.mono}>{row.submission_id}</span></p><details><summary>코드 버전 확인</summary><code className={s.mono}>{row.code_version}</code></details>
  </div>;
}
