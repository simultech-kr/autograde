"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { studentRequest, StudentApiError } from "./client";
import {canResumePrivate, usableClaim, ResumeBarrier, copyHandoffValue} from "./continuity";
import { parseBootstrap, parseCourse, parseClaim, coursePath, courseKey, courseMeta, timeText, scoreText, submissionState, stages, remainingSeconds, RequestVersion, StudentContractError,
  type Bootstrap, type CourseData, type Course, type Receipt, type Claim } from "./model";
import s from "./student.module.css";
import { courseLocationLabels } from "../../lib/courseLabel.ts";
import { PortalLink, PortalLocation } from "../../components/PortalLocation.tsx";
import {StudentScores} from "./StudentScores.tsx";
import {visibleCourseAssignments} from "./scoreboard.ts";
import {StudentPrivacy} from "./StudentPrivacy.tsx";
import {StudentCourseCatalog} from "./StudentCourseCatalog.tsx";
import {StudentHome} from "./StudentHome.tsx";
import {StudentMaterials} from "./StudentMaterials.tsx";
import {clearStudentSearches} from "./useStudentListHistory";
import {useLearningHome} from "./useLearningHome.ts";

type View = "home" | "catalog" | "course" | "scores" | "materials" | "account" | "privacy";
const studentMenu = [["home", "내 수업"], ["catalog", "수강 선택"], ["account", "내 계정"], ["privacy", "정보 활용 설정"]] as const;
const courseMenu = [["course", "실습 과제"], ["materials", "학습 자료"], ["scores", "내 점수"]] as const;

function studentAddress(view: View, key = "", assignment = "") {
  const params = new URLSearchParams();
  if (key) params.set("course_key", key);
  if (view !== "home" && view !== "course") params.set("view", view);
  if (key && assignment) params.set("assignment", assignment);
  return "/app/student" + (params.size ? "?" + params.toString() : "");
}

type Auth = "login" | "register" | "reset";
const errorText = (error: unknown) => error instanceof Error ? error.message : "요청을 처리하지 못했습니다. 다시 시도하세요.";

function ActivationForm({busy, activate}: {busy: boolean; activate: (token: string) => Promise<string | undefined>}) {
  const field = useRef<HTMLInputElement>(null);
  const [error, setError] = useState("");
  useEffect(() => { if (error) field.current?.focus(); }, [error]);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const input = field.current;
    if (busy || !input) return;
    const token = input.value;
    input.value = "";
    setError("");
    const message = await activate(token);
    if (message && field.current === input) setError(message);
  }
  return <form onSubmit={submit}>
    <label>교수자가 제공한 활성화 코드<input ref={field} name="token" autoComplete="off" maxLength={256} required
      aria-invalid={error ? true : undefined} aria-describedby={error ? "activation-code-error" : undefined}
      onChange={() => setError("")} /></label>
    {error && <p className={s.error} id="activation-code-error" role="alert">{error}</p>}
    <button disabled={busy} className={s.primary}>계정 활성화</button>
  </form>;
}

export function StudentPortal() {
  const [boot, setBoot] = useState<Bootstrap | null>(null), [loading, setLoading] = useState(true), [busy, setBusy] = useState(false);
  const [view, setView] = useState<View>("home"), [auth, setAuth] = useState<Auth>("login"), [selectedCourse, setSelectedCourse] = useState("");
  const [data, setData] = useState<CourseData | null>(null), [courseLoading, setCourseLoading] = useState(false), [courseError, setCourseError] = useState("");
  const [issuingAssignment, setIssuingAssignment] = useState<string | null>(null);
  const claimBusy = issuingAssignment !== null;
  const [clockOffset, setClockOffset] = useState(0);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [search, setSearch] = useState(""), [selected, setSelected] = useState<Course[]>([]);
  const [claim, setClaim] = useState<Claim | null>(null), [now, setNow] = useState(Date.now()), [lastRead, setLastRead] = useState("");
  const [taskFilter, setTaskFilter] = useState("all"), [theme, setTheme] = useState<"light" | "dark">("light");
  const [privacy, setPrivacy] = useState<"visible" | "hidden" | "checking" | "blocked">("checking");
  const [copyStatus, setCopyStatus] = useState("");
  const [historyEntry, setHistoryEntry] = useState(0);
  const lastAddress = useRef("");
  const [navigationFocus, setNavigationFocus] = useState<"heading" | {assignmentId: string} | null>(null);
  const privacyRef = useRef(privacy), selectedCourseRef = useRef(selectedCourse), claimRef = useRef(claim);
  privacyRef.current = privacy; selectedCourseRef.current = selectedCourse; claimRef.current = claim;
  const resumeBarrier = useRef(new ResumeBarrier()), resumeAccount = useRef<() => void>(() => {}), claimHeading = useRef<HTMLHeadingElement>(null), privateSurface = useRef<HTMLDivElement>(null);
  const identityVersion = useRef(new RequestVersion()), courseVersion = useRef(new RequestVersion());
  const accountController = useRef<AbortController | null>(null), courseController = useRef<AbortController | null>(null);
  const claimVersion = useRef(new RequestVersion()), claimController = useRef<AbortController | null>(null), currentIdentity = useRef<string | null>(null);
  const currentSession = useRef("");
  const actionController = useRef<AbortController | null>(null), logoutRequested = useRef(false), disposed = useRef(false), heading = useRef<HTMLHeadingElement>(null);
  const student = boot?.student, currentCourse = boot?.courses.find(item => item.course_key === selectedCourse && item.enrolled);
  const courseLogin = boot?.auth_method === "course";
  const enrolled = (boot?.courses || []).filter(item => item.status !== "available" || item.enrolled);
  const serverNow = now + clockOffset;
  const remaining = remainingSeconds(boot?.expires_at || null, serverNow);
  const activeData = data?.course.course_key === selectedCourse && data.student_key === student?.student_key && currentCourse ? data : null;
  const isCourseView = view === "course" || view === "scores" || view === "materials";
  const claimContext = activeData?.overview.assignments.find(item => item.assignment_id === claim?.assignment_id)?.title_context;
  const assignmentFilterField = useRef<HTMLSelectElement>(null);
  const assignmentRows = activeData && view === "course" ? visibleCourseAssignments(activeData.overview, taskFilter, serverNow) : [];


  const setPrivacyMode = useCallback((next: typeof privacy) => {
    privacyRef.current = next;
    // Hide before a browser snapshot. Reveal only after the checked identity's
    // render commits, so an account change cannot briefly expose the old DOM.
    if (privateSurface.current && next !== "visible") { privateSurface.current.hidden = true; privateSurface.current.inert = true; }
    setPrivacy(next);
  }, []);
  useEffect(() => {
    if (privateSurface.current) { privateSurface.current.hidden = privacy !== "visible"; privateSurface.current.inert = privacy !== "visible"; }
  }, [privacy, boot]);
  const discardPrivate = useCallback(() => {
    clearStudentSearches();
    courseController.current?.abort(); claimController.current?.abort(); claimVersion.current.next(); courseVersion.current.next(); setIssuingAssignment(null);
    setData(null); setClaim(null); setCopyStatus(""); setSelected([]); setSearch(""); setCourseError(""); setLastRead("");
  }, []);
  function clearPrivate(keepLocation = false) {
    actionController.current?.abort(); setBusy(false); discardPrivate();
    if (!keepLocation) { setSelectedCourse(""); setNavigationFocus(null); setView("home"); }
  }
  function route(next: View, key = "", assignment = "") {
    setHistoryEntry(entry => entry + 1);
    if (courseLogin && next === "catalog") next = "home";
    claimVersion.current.next(); claimController.current?.abort(); setIssuingAssignment(null); setView(next); setClaim(null); setCopyStatus(""); setNotice(""); setError("");
    setNavigationFocus(assignment ? {assignmentId: assignment} : "heading");
    if (assignment || key !== selectedCourse) setTaskFilter("all");
    if (next === "course" || next === "scores" || next === "materials") setSelectedCourse(key);
    else { courseController.current?.abort(); courseVersion.current.next(); setData(null); setSelectedCourse(""); }
    const address = studentAddress(next, key, assignment);
    if (address !== window.location.pathname + window.location.search + window.location.hash) {
      window.history.pushState(null, "", address);
    }
    lastAddress.current = window.location.href;
  }
  const applyBootstrap = useCallback((next: Bootstrap) => {
    const at = next.server_time ? Date.parse(next.server_time) : Date.now();
    const sessionContext = next.authenticated ? `${next.auth_method || "member"}:${next.csrf}` : "";
    if (!canResumePrivate(currentIdentity.current, next, selectedCourseRef.current, at)
        || currentSession.current && currentSession.current !== sessionContext) {
      discardPrivate();
    }
    currentSession.current = sessionContext;
    currentIdentity.current = next.student?.student_key || null;
    setClockOffset(next.server_time ? Date.parse(next.server_time) - Date.now() : 0);
    setNow(Date.now()); setBoot(next);
    if (next.auth_method === "course") setView(previous => previous === "catalog" ? "home" : previous);
  }, [discardPrivate]);
  const refreshAccount = useCallback(async () => {
    // Every caller, including navigation buttons, must wait for a submitted POST.
    if (resumeBarrier.current.waiting) { resumeBarrier.current.request(); return; }
    const version = identityVersion.current.next(); accountController.current?.abort(); const controller = new AbortController(); accountController.current = controller;
    setLoading(true); setError("");
    try {
      const next = parseBootstrap(await studentRequest("/student/api/bootstrap", undefined, controller.signal));
      if (!disposed.current && identityVersion.current.current(version)) {
        if (logoutRequested.current && next.authenticated) { setPrivacyMode("blocked"); return; }
        if (!next.authenticated) logoutRequested.current = false;
        applyBootstrap(next);
        setPrivacyMode(document.visibilityState === "visible" && document.hasFocus() ? "visible" : "hidden");
      }
    } catch (error) { if (!controller.signal.aborted && !disposed.current && identityVersion.current.current(version)) { setError(errorText(error)); setPrivacyMode("blocked"); if (error instanceof StudentApiError && [401, 403].includes(error.status)) { discardPrivate(); setBoot(null); currentIdentity.current = null; } } }
    finally { if (!disposed.current && identityVersion.current.current(version)) setLoading(false); }
  }, [applyBootstrap, discardPrivate, setPrivacyMode]);
  resumeAccount.current = () => {
    if (document.visibilityState !== "visible") return;
    setPrivacyMode("checking");
    if (resumeBarrier.current.request()) void refreshAccount();
  };
  const home = useLearningHome({session:student ? `${student.student_key}:${boot?.auth_method}:${boot?.csrf}` : "", studentKey:student?.student_key || "",
    courseKeys:enrolled.filter(course=>course.enrolled).map(course=>course.course_key), enabled:view === "home" && Boolean(student?.verified) && privacy === "visible" && !busy,
    onDenied:() => { discardPrivate(); setError("실습 정보를 확인할 수 없습니다. 로그인 상태를 다시 확인해 주세요."); setPrivacyMode("blocked"); }});
  function finishMutation() {
    if (resumeBarrier.current.finish() && !disposed.current && document.visibilityState === "visible") void refreshAccount();
  }
  useEffect(() => {
    disposed.current = false;
    setTheme(matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
    function readRoute() {
      const oldAddress = lastAddress.current;
      lastAddress.current = window.location.href;
      if (oldAddress && oldAddress !== lastAddress.current && oldAddress.split("#")[0] === lastAddress.current.split("#")[0]) return;
      setHistoryEntry(entry => entry + 1);
      const params = new URLSearchParams(window.location.search), key = params.get("course_key");
      const assignment = params.get("assignment") || "";
      const hasAssignmentTarget = key && !window.location.hash && /^[A-Za-z0-9_-]{1,256}$/.test(assignment);
      setNavigationFocus(hasAssignmentTarget ? {assignmentId: assignment} : null);
      setTaskFilter("all");
      claimController.current?.abort(); claimVersion.current.next(); setIssuingAssignment(null); setClaim(null);
      if (key) {
        try { setSelectedCourse(courseKey(key)); setView(params.get("view") === "scores" ? "scores" : params.get("view") === "materials" ? "materials" : "course"); } catch { setSelectedCourse(""); setView("home"); }
      } else {
        setSelectedCourse("");
        const requested = params.get("view") || "";
        if (requested === "catalog" && currentSession.current.startsWith("course:")) {
          setView("home"); window.history.replaceState(null, "", "/app/student");
        } else setView(["catalog", "account", "scores", "materials", "privacy"].includes(requested) ? requested as View : "home");
      }
    }
    function shield() {
      setPrivacyMode("hidden");
      document.querySelectorAll<HTMLInputElement>('input[type="password"],input[name="token"]').forEach(input => { input.value = ""; });
    }
    function revalidate() { if (document.visibilityState === "hidden") shield(); else resumeAccount.current(); }
    function restored(event: PageTransitionEvent) { if (event.persisted) revalidate(); }
    readRoute(); window.addEventListener("popstate", readRoute); window.addEventListener("blur", shield); window.addEventListener("pagehide", shield); window.addEventListener("focus", revalidate); window.addEventListener("pageshow", restored); document.addEventListener("visibilitychange", revalidate); void refreshAccount();
    return () => { disposed.current = true; identityVersion.current.next(); courseVersion.current.next(); accountController.current?.abort(); courseController.current?.abort(); actionController.current?.abort(); claimController.current?.abort(); claimVersion.current.next(); window.removeEventListener("popstate", readRoute); window.removeEventListener("blur", shield); window.removeEventListener("pagehide", shield); window.removeEventListener("focus", revalidate); window.removeEventListener("pageshow", restored); document.removeEventListener("visibilitychange", revalidate); };
  }, [refreshAccount, setPrivacyMode]);
  useEffect(() => { const timer = setInterval(() => setNow(Date.now()), 1000); return () => clearInterval(timer); }, []);
  useEffect(() => {
    if (boot?.authenticated && remaining === 0) {
      clearPrivate(true); setBoot(null); setNotice("로그인 시간이 끝났습니다. 다시 로그인하면 이 수업으로 돌아옵니다. 비밀번호와 수령 코드는 지웠습니다."); void refreshAccount();
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
        if (error instanceof StudentContractError || error instanceof StudentApiError && [401, 403, 404].includes(error.status)) { discardPrivate(); setBoot(null); currentIdentity.current = null; setError(errorText(error)); setPrivacyMode("blocked"); }
      }
    } finally { if (!disposed.current && courseVersion.current.current(version)) setCourseLoading(false); }
  }, [boot, selectedCourse, refreshAccount, discardPrivate, setPrivacyMode]);
  useEffect(() => {
    setCourseError("");
    if (isCourseView) void refreshCourse();
    return () => { courseVersion.current.next(); courseController.current?.abort(); };
  }, [view, selectedCourse, refreshCourse]);
  useEffect(() => {
    if (!isCourseView || !activeData) return;
    const timer = setInterval(() => { if (document.visibilityState === "visible" && privacyRef.current === "visible" && !busy) void refreshCourse(true); }, 15000);
    return () => clearInterval(timer);
  }, [view, Boolean(activeData), busy, refreshCourse]);
  useEffect(() => {
    if (!navigationFocus || privacy !== "visible") return;
    if (navigationFocus === "heading") {
      heading.current?.focus();
    } else {
      if (!activeData) return;
      const attribute = view === "scores" ? "data-score-assignment" : "data-assignment";
      const cards = privateSurface.current?.querySelectorAll<HTMLElement>(`[${attribute}]`);
      const card = Array.from(cards ?? []).find(node => node.getAttribute(attribute) === navigationFocus.assignmentId);
      if (card) {
        // History traversal can restore the browser's scroll position after
        // popstate. Place the requested card once the new DOM is painted.
        let current = true;
        requestAnimationFrame(() => {
          if (!current || privacyRef.current !== "visible" || !card.isConnected) return;
          card.tabIndex = -1;
          card.focus({preventScroll: true});
          card.scrollIntoView?.({block: "start"});
          setNavigationFocus(null);
        });
        return () => { current = false; };
      } else {
        heading.current?.focus();
      }
    }
    setNavigationFocus(null);
  }, [navigationFocus, activeData, view, privacy]);
  async function endSession() {
    if (busy || actionController.current) return;
    const csrf = boot?.csrf, version = identityVersion.current.next(); accountController.current?.abort();
    const controller = new AbortController(); actionController.current = controller; resumeBarrier.current.begin();
    logoutRequested.current = true; setBusy(true); setError(""); setNotice(""); setPrivacyMode("checking");
    discardPrivate(); setBoot(null); currentIdentity.current = null; setSelectedCourse(""); setNavigationFocus(null); setView("home");
    window.history.replaceState(null, "", "/app/student");
    try {
      // An explicit retry obtains a fresh CSRF token; it never reopens private UI.
      let next = csrf ? null : parseBootstrap(await studentRequest("/student/api/bootstrap", undefined, controller.signal));
      if (!next || next.authenticated) next = parseBootstrap(await studentRequest("/student/api/logout", {csrf: next?.csrf || csrf!}, controller.signal));
      if (disposed.current || !identityVersion.current.current(version)) return;
      if (next.authenticated) throw new StudentContractError();
      logoutRequested.current = false; applyBootstrap(next); setAuth("login"); setNotice("학생 웹에서 로그아웃했습니다.");
      setPrivacyMode(document.visibilityState === "visible" && document.hasFocus() ? "visible" : "hidden");
    } catch (error) {
      if (!controller.signal.aborted && !disposed.current && identityVersion.current.current(version)) { setError(errorText(error)); setPrivacyMode("blocked"); }
    } finally {
      if (!disposed.current && actionController.current === controller) { actionController.current = null; setBusy(false); setLoading(false); }
      finishMutation();
    }
  }
  async function action(name: string, fields: Record<string, string>, after?: () => void) {
    if (!boot || busy || actionController.current || privacyRef.current !== "visible") return;
    const version = identityVersion.current.next(); accountController.current?.abort();
    const controller = new AbortController(); actionController.current = controller; resumeBarrier.current.begin(); setBusy(true); setError(""); setNotice("");
    try {
      const next = parseBootstrap(await studentRequest(`/student/api/${name}`, { csrf: boot.csrf, ...fields }, controller.signal));
      if (disposed.current || !identityVersion.current.current(version)) return;
      applyBootstrap(next); setSelected([]); setClaim(null);
      if (!next.authenticated) { clearPrivate(); setAuth("login"); }
      else if (name === "register") route("catalog");
      else if (name === "login") { if (!selectedCourse) route("home"); }
      else after?.();
      setNotice(name === "login" ? "" : next.message || ({ login: "로그인했습니다.", register: "가입했습니다. 수강할 수업을 선택하세요.", enroll: "수강 신청 상태를 확인했습니다.", withdraw: "수강 신청을 취소했습니다.", activate: "계정 활성화 상태를 확인했습니다.", logout: "학생 웹에서 로그아웃했습니다.", password: "비밀번호를 변경했습니다. 새 비밀번호로 로그인하세요.", reset: "비밀번호를 재설정했습니다. 새 비밀번호로 로그인하세요." }[name] || "처리했습니다."));
    } catch (error) {
      if (!controller.signal.aborted && !disposed.current && identityVersion.current.current(version)) {
        if (name === "activate" && error instanceof StudentApiError && error.status === 403 && error.code === "invalid_activation_code") {
          return error.message;
        }
        setError(errorText(error));
        if (error instanceof StudentContractError || boot.authenticated && error instanceof StudentApiError && [0, 401, 403].includes(error.status)) {
          discardPrivate(); setBoot(null); currentIdentity.current = null; setPrivacyMode("blocked");
        }
      }
    } finally {
      if (!disposed.current && actionController.current === controller) { actionController.current = null; setBusy(false); setLoading(false); }
      finishMutation();
    }
  }
  function submitForm(event: FormEvent<HTMLFormElement>, name: string) {
    event.preventDefault(); if (busy) return; const form = event.currentTarget, fields = Object.fromEntries(Array.from(new FormData(form).entries()).map(([key, value]) => [key, String(value)]));
    form.querySelectorAll<HTMLInputElement>('input[type="password"],input[name="token"]').forEach(input => { input.value = ""; }); void action(name, fields);
  }
  async function issueClaim(assignmentId: string, replace = false) {
    if (!activeData || busy || claimBusy || privacyRef.current !== "visible") return;
    if (!replace && usableClaim(claim, assignmentId, serverNow)) { claimHeading.current?.focus(); setNotice("발급한 수령 코드를 확인하세요. IDE에서 이미 사용했다고 안내한 경우에만 새 코드를 발급하세요."); return; }
    const expectedCourse = selectedCourse, version = claimVersion.current.next(); const controller = new AbortController(); claimController.current = controller;
    resumeBarrier.current.begin(); setIssuingAssignment(assignmentId); setError(""); setClaim(null); setCopyStatus("");
    try {
      const value = parseClaim(await studentRequest(`${coursePath(expectedCourse)}/claims`, { csrf: activeData.csrf, assignment_id: assignmentId }, controller.signal), assignmentId, boot!.student!.student_key);
      if (!disposed.current && claimVersion.current.current(version)) { setClaim(value); requestAnimationFrame(() => claimHeading.current?.focus()); }
    } catch (error) { if (!controller.signal.aborted && !disposed.current && claimVersion.current.current(version)) { setError(errorText(error)); if (error instanceof StudentContractError || error instanceof StudentApiError && [401, 403].includes(error.status)) { discardPrivate(); setBoot(null); currentIdentity.current = null; setError(errorText(error)); setPrivacyMode("blocked"); } } }
    finally { if (!disposed.current && claimVersion.current.current(version)) setIssuingAssignment(null); finishMutation(); }
  }
  async function copyClaim(kind: "code" | "address") {
    if (!claim || privacyRef.current !== "visible" || remainingSeconds(claim.expires_at, serverNow) === 0) return;
    const expected = claim.code, label = kind === "code" ? "수령 코드" : "서버 주소";
    const outcome = await copyHandoffValue(kind === "code" ? claim.code : claim.api_url, navigator.clipboard);
    if (claimRef.current?.code === expected) setCopyStatus(outcome === "copied" ? `${label}를 복사했습니다. Autograde IDE에 붙여넣으세요.` : `자동 복사가 허용되지 않았습니다. 아래 ${label}를 선택해 직접 복사하세요.`);
  }
  const title = !student ? auth === "register" ? "학생 회원가입" : auth === "reset" ? "회원 비밀번호 재설정" : "학생 로그인" : view === "catalog" ? "수강할 수업 선택" : view === "account" ? "내 계정" : view === "privacy" ? "정보 활용 설정" : isCourseView ? currentCourse?.name || currentCourse?.code || "수업 확인" : "내 수업";
  const enrolledCourses = enrolled.filter(item => item.enrolled);
  const courseLabels = courseLocationLabels(enrolledCourses);
  const currentLabel = courseLabels.get(selectedCourse);
  const tabTitle = courseMenu.find(([key]) => key === view)?.[1];
  const locationTitle = currentCourse && tabTitle ? view === "course" ? `${currentLabel} · ${tabTitle}` : tabTitle : title;
  const courseParent = currentCourse && (view === "scores" || view === "materials");
  const homeLink = {href: studentAddress("home"), label: "내 수업", onFollow: () => route("home")};
  const ancestors: {href: string; label: string; onFollow?: () => void}[] = student && view !== "home" ? [homeLink] : [{href: "/", label: "포털 안내"}];
  if (courseParent) ancestors.push({href: studentAddress("course", selectedCourse), label: currentLabel || title, onFollow: () => route("course", selectedCourse)});
  return <div className={s.portal} data-theme={theme}>
    {privacy !== "visible" && <section className={s.privacyScreen} role="status" aria-live="polite"><h1>{privacy === "blocked" ? "로그인 상태를 다시 확인해 주세요" : privacy === "hidden" ? "학생 화면을 보호하고 있습니다" : "로그인·수강 상태 확인 중"}</h1><p>{privacy === "blocked" ? logoutRequested.current ? "로그아웃 완료를 확인하지 못했습니다. 학생 정보와 수령 코드는 지웠으며, 로그아웃을 마칠 때까지 이전 화면을 다시 열지 않습니다." : "로그인·수강 상태를 확인하지 못해 학생 정보를 가렸습니다. 다시 확인하면 진행하던 수업으로 돌아갑니다." : resumeBarrier.current.waiting ? "이미 보낸 요청의 결과를 확인하고 있습니다. 코드를 다시 발급하지 않아도 됩니다." : "돌아오면 본인과 수강 권한을 확인한 뒤 진행하던 화면을 표시합니다."}</p>{privacy === "blocked" && <div className={s.actions}><button className={s.primary} onClick={() => resumeAccount.current()}>다시 확인</button>{logoutRequested.current && <button className={s.secondary} disabled={busy} onClick={() => void endSession()}>로그아웃 다시 시도</button>}</div>}</section>}
    <div ref={privateSurface} hidden={privacy !== "visible"} inert={privacy !== "visible"} aria-hidden={privacy !== "visible"}>
    <header className={s.header}><a href="/app/student" className={s.brand}>autograde<span>학생 포털</span></a><div className={s.headerActions}><button className={s.quiet} onClick={() => setTheme(theme === "light" ? "dark" : "light")}>{theme === "light" ? "어두운 테마" : "밝은 테마"}</button>{student && <><span className={s.identity}>{student.student_key}</span><button className={s.quiet} disabled={busy} onClick={() => void endSession()}>로그아웃</button></>}</div></header>
    {student && <nav className={s.nav} aria-label="학생 메뉴">
      {studentMenu.filter(([key]) => !courseLogin || key !== "catalog").map(([key, label]) => <PortalLink
        key={key} href={studentAddress(key)} onFollow={() => route(key)}
        aria-current={view === key ? "page" : key === "home" && isCourseView ? "location" : undefined}>{label}</PortalLink>)}
      <span className={s.session}>{Math.floor(remaining / 60)}분 {String(remaining % 60).padStart(2, "0")}초 후 로그인 종료</span>
    </nav>}
    <main className={`${s.main} ${isCourseView ? s.courseMain : ""}`} id="main" tabIndex={-1}>
      <PortalLocation ancestors={ancestors} current={locationTitle}
        backLabel={courseParent ? "이 수업의 실습 과제로 돌아가기" : student && view !== "home" ? "내 수업 목록으로 돌아가기" : "포털 안내로 돌아가기"} />
      <div className={s.heading}><div><h1 ref={heading} tabIndex={-1} aria-label={currentCourse && tabTitle ? `${currentLabel} · ${tabTitle}` : undefined}>{title}</h1>{view !== "home" || !student ? <p>{student ? isCourseView && currentCourse ? `${currentCourse.code} · ${courseMeta(currentCourse)}` : view === "privacy" ? "수업을 더 좋게 만드는 연구에 참여할지, 수업별로 편하게 선택하세요." : "필요한 준비는 여기서, 실습과 제출은 IDE에서 이어가세요." : "학번과 수업 비밀번호 또는 개인 비밀번호로 로그인하세요. 기존 수업 계정은 별도 가입 없이 이용합니다."}</p> : null}</div>{student && view !== "privacy" && view !== "home" && !isCourseView && <button className={s.secondary} disabled={busy || loading} onClick={() => isCourseView ? void refreshCourse() : void refreshAccount()}>상태 새로고침</button>}</div>
      {notice && <div className={s.notice} role="status">{notice}<button aria-label="알림 닫기" onClick={() => setNotice("")}>×</button></div>}
      {error && <div className={s.error} role="alert"><p>{error}</p>{!boot && <button className={s.secondary} onClick={() => void refreshAccount()}>다시 연결</button>}{boot && <button className={s.quiet} disabled={busy} onClick={() => void refreshAccount()}>로그인·수강 상태 다시 확인</button>}</div>}
      {student && remaining > 0 && remaining <= 60 && <p className={s.warning} role="status">곧 로그인이 종료됩니다. 종료되면 다시 로그인해야 합니다. 수령 코드는 화면에서 지워집니다.</p>}
      {loading && !boot && <p role="status" className={s.empty}>학생 포털에 연결하고 있습니다…</p>}
      {boot && !student && <div className={s.authLayout}><section className={s.panel}><div className={s.tabs} aria-label="계정 시작">{([["login", "로그인"], ["register", "회원가입"], ["reset", "비밀번호 분실"]] as const).map(([key, label]) => <button key={key} disabled={busy} aria-pressed={auth === key} onClick={() => { setAuth(key); setError(""); }}>{label}</button>)}</div><form key={auth} onSubmit={event => submitForm(event, auth)}><label>학번<input name="student_key" inputMode="numeric" pattern="[0-9]{4,20}" minLength={4} maxLength={20} autoComplete="off" required /></label>{auth === "reset" && <label>관리자가 제공한 재설정 코드<input name="token" autoComplete="off" maxLength={256} required /></label>}<label>{auth === "reset" ? "새 회원 비밀번호" : auth === "register" ? "회원 비밀번호" : "비밀번호"}<input name={auth === "reset" ? "new_password" : "password"} type="password" autoComplete="off" minLength={auth === "login" ? 6 : 8} maxLength={128} required /></label>{auth === "reset" && <label>새 회원 비밀번호 확인<input name="confirm_password" type="password" autoComplete="off" minLength={8} maxLength={128} required /></label>}<p className={s.muted}>{auth === "login" ? "교수자가 준 수업 비밀번호(6자리 포함) 또는 설정한 개인 비밀번호를 입력하세요." : "새 비밀번호는 8~128자로 설정하세요."} 공용 PC에 비밀번호를 저장하지 마세요.</p><button className={s.primary} disabled={busy || loading}>{busy ? "처리 중…" : auth === "register" ? "가입하고 수업 선택" : auth === "reset" ? "비밀번호 재설정" : "로그인"}</button></form></section><aside className={s.guide}><h2>수업 계정으로 바로 시작</h2><ol><li>교수자가 등록한 학번과 수업 비밀번호로 로그인합니다. 별도 회원가입은 필요하지 않습니다.</li><li>내 수업에서 과제를 선택해 수령 코드를 발급합니다.</li><li>IDE에 코드를 입력해 실습하고, 이 포털에서 자료와 점수를 확인합니다.</li></ol><p>이미 개인 비밀번호를 설정했다면 개인 비밀번호로 로그인하세요. 이전 수업 비밀번호로 개인 계정에 로그인할 수 없습니다.</p><p>아직 수업 계정이 없다면 회원가입 후 수업 선택과 본인 확인을 진행하세요. 비밀번호를 잊었다면 교수자에게 문의하세요.</p></aside></div>}
      {student && !courseLogin && !student.verified && <div className={s.warning}><strong>계정 활성화가 필요합니다.</strong><p>수강할 수업을 신청한 뒤 교수자에게 본인 확인을 받으세요. 명단에 학번이 있어도 활성화 전에는 과제·자료를 열 수 없습니다.</p><button className={s.secondary} onClick={() => route("account")}>활성화 코드 입력</button></div>}
      {student && view === "home" && <StudentHome key={`${boot.csrf}:${historyEntry}`} navigationScope={boot.navigation_scope} courses={enrolled} verified={student.verified} busy={busy} courseLogin={courseLogin} home={home.data} homeLoading={home.loading} homeError={home.error} refreshHome={home.refresh} now={serverNow} navigate={route} refresh={() => void refreshAccount()} withdraw={key => void action("withdraw", {course_key: key})} />}
      {student && !courseLogin && view === "catalog" && <StudentCourseCatalog courses={boot.courses} verified={student.verified}
        search={search} onSearch={setSearch} selected={selected} onSelect={setSelected} busy={busy}
        onEnroll={keys => void action("enroll", {course_keys: JSON.stringify(keys)}, () => route("home"))} />}
      {student && view === "account" && <div className={s.grid}><section className={s.panel}><h2>계정 정보</h2><dl><dt>학번</dt><dd>{student.student_key}</dd><dt>계정 상태</dt><dd>{courseLogin ? "수업 계정 · 수강 확인 완료" : student.verified ? "활성화 완료" : "교수자 본인 확인 필요"}</dd><dt>웹 로그인 만료</dt><dd>{timeText(boot.expires_at)} KST</dd></dl>{!courseLogin && !student.verified && <ActivationForm key={boot.csrf} busy={busy} activate={token => action("activate", {token})} />}<p className={s.muted}>{courseLogin ? "현재 수업 비밀번호로 확인된 수업만 이용할 수 있습니다. 별도 가입 없이 과제·자료·점수를 확인하세요." : "활성화 코드는 IDE에 입력하는 과제 수령 코드와 다릅니다."}</p></section><section className={s.panel}><h2>{courseLogin ? "개인 비밀번호 설정" : "회원 비밀번호 변경"}</h2>{courseLogin && <p>원하면 본인만 아는 비밀번호로 바꿀 수 있습니다. 새 비밀번호는 수강 중인 수업에서 함께 사용합니다.</p>}<form onSubmit={event => submitForm(event, "password")}><label>{courseLogin ? "현재 수업 비밀번호" : "현재 회원 비밀번호"}<input name="current_password" type="password" autoComplete="off" minLength={courseLogin ? 6 : 8} maxLength={128} required /></label><label>새 회원 비밀번호<input name="new_password" type="password" autoComplete="off" minLength={8} maxLength={128} required /></label><label>새 회원 비밀번호 확인<input name="confirm_password" type="password" autoComplete="off" minLength={8} maxLength={128} required /></label><p className={s.muted}>변경하면 기존 웹 로그인·수령 코드·IDE 인증이 종료됩니다. 새 비밀번호로 로그인하고 새 수령 코드로 IDE를 연결하세요. 제출한 코드와 점수는 유지됩니다.</p><button className={s.primary} disabled={busy}>{courseLogin ? "개인 비밀번호 설정" : "비밀번호 변경"}</button></form></section></div>}
      {student && isCourseView && <><div className={s.courseContext}>{enrolledCourses.length > 1 && <label>현재 수업<select aria-label="현재 수업" value={selectedCourse} onChange={event => route(view, event.target.value)}><option value="" disabled>수업 선택</option>{enrolledCourses.map(item => <option key={item.course_key} value={item.course_key}>{courseLabels.get(item.course_key)}</option>)}</select></label>}{activeData && <p className={s.muted}>확인 {timeText(lastRead)} KST · 자동 갱신</p>}<button className={s.quiet} disabled={busy || loading || courseLoading} onClick={() => void refreshCourse()}>상태 새로고침</button></div>{!currentCourse && <div className={s.empty}><h2>이 수업을 아직 이용할 수 없습니다.</h2><p>{courseLogin ? "이 수업의 비밀번호로 다시 로그인하거나 교수자에게 수강 상태를 확인하세요." : "수강 선택과 계정 활성화, 교수자 명단 연결 상태를 확인하세요."}</p><button className={s.secondary} onClick={() => route(courseLogin ? "home" : "catalog")}>{courseLogin ? "내 수업 확인" : "수강 상태 확인"}</button></div>}{courseLoading && !activeData && <p role="status">수업 정보를 확인하고 있습니다…</p>}{courseError && <div className={s.error} role="alert"><p>{courseError}</p>{activeData && <p>마지막 확인 자료입니다. 최신 상태로 확정하지 마세요.</p>}<button className={s.secondary} onClick={() => void refreshCourse()}>다시 조회</button></div>}{activeData && <>
        <nav className={s.tabs} aria-label="수업 메뉴">{courseMenu.map(([key, label]) =>
          <PortalLink key={key} href={studentAddress(key, selectedCourse)} onFollow={() => route(key, selectedCourse)}
            aria-current={view === key ? "page" : undefined}>{label}</PortalLink>)}</nav>
        {view === "scores" && <StudentScores key={`${activeData.student_key}:${activeData.csrf}:${selectedCourse}:${historyEntry}`} navigationScope={boot?.navigation_scope} data={activeData} />}
        {view === "materials" && <StudentMaterials key={`${activeData.student_key}:${activeData.csrf}:${selectedCourse}:${historyEntry}`} navigationScope={boot?.navigation_scope} data={activeData} onRefresh={() => void refreshCourse()} />}
        {view === "course" && <>
        {claim && <section className={s.claim} aria-label="과제 수령 코드"><div className={s.sectionHeading}><h2 ref={claimHeading} tabIndex={-1}>{claim.title} · IDE에서 이어가기</h2><button className={s.quiet} onClick={() => { setClaim(null); setCopyStatus(""); }}>코드 지우기</button></div>{claimContext && <p>{claimContext}</p>}<ol className={s.handoffSteps}><li><strong>수령 코드 발급 완료</strong><p>아래 코드를 복사해 Autograde IDE의 수령 코드 입력란에 붙여넣으세요.</p></li><li><strong>IDE에서 과제 다운로드 또는 기존 실습 열기</strong><p>웹 비밀번호는 IDE에 입력하지 않습니다. 새 과제는 다운로드하고 이미 받은 과제는 ‘내 실습’에서 이어갑니다.</p></li><li><strong>IDE에서 수락 결과 확인</strong><p>이 웹으로 돌아와도 유효한 코드와 같은 과제 화면을 유지합니다.</p></li></ol><code tabIndex={0} aria-label="발급한 수령 코드">{claim.code}</code><div className={s.actions}><button className={s.primary} onClick={() => void copyClaim("code")}>수령 코드 복사</button><button className={s.secondary} onClick={() => void copyClaim("address")}>서버 주소 복사</button></div><p role="status" aria-live="polite" className={s.copyStatus}>{copyStatus}</p><p>{timeText(claim.expires_at)} KST 만료 · {remainingSeconds(claim.expires_at, serverNow)}초 남음</p><p>IDE 서버 주소: <span className={s.mono}>{claim.api_url}</span></p><div className={s.handoffCheck}><button className={s.secondary} disabled={courseLoading} onClick={() => void refreshCourse()}>IDE 수락 기록 확인</button><p>{activeData.overview.assignments.find(item => item.assignment_id === claim.assignment_id)?.accepted_at ? `이 과제의 IDE 수락 기록: ${timeText(activeData.overview.assignments.find(item => item.assignment_id === claim.assignment_id)?.accepted_at)} KST. 이전 수락일 수 있으므로 현재 PC의 연결 결과는 IDE에서 확인하세요.` : "아직 이 과제의 IDE 수락 기록이 없습니다. 코드를 입력한 뒤 다시 확인하세요."}</p></div><details><summary>코드가 만료되었거나 이미 사용되었다고 나오나요?</summary><p>IDE에서 사용할 수 없다고 안내한 경우에만 새 코드를 발급하세요. 코드 복사나 웹 복귀만으로 다시 발급할 필요는 없습니다.</p><button className={s.secondary} disabled={busy || claimBusy} onClick={() => void issueClaim(claim.assignment_id, true)}>새 수령 코드 발급</button></details></section>}
        <section><div className={s.sectionHeading}>
          <h2>실습 과제 <span role="status">{activeData.overview.assignments_truncated ? "조회한" : "전체"} {activeData.overview.assignments.length}개{taskFilter !== "all" && ` 중 ${assignmentRows.length}개 표시`}</span></h2>
          <label>표시할 과제<select ref={assignmentFilterField} value={taskFilter} disabled={!activeData.overview.assignments.length} onChange={event => setTaskFilter(event.target.value)}>
            <option value="all">전체</option><option value="open">공식 제출 전</option><option value="pending">접수·채점 중</option><option value="awaiting_publication">채점 완료 · 공개 대기</option><option value="attention">확인 필요</option><option value="completed">결과 공개</option>
          </select></label>
        </div><div className={s.grid}>{assignmentRows.map(({assignment, receipt, state}) => {
          const issuing = issuingAssignment === assignment.assignment_id;
          const closed = Boolean(assignment.due_at && Date.parse(assignment.due_at) <= serverNow);
          return <article className={s.course} key={assignment.assignment_id} data-assignment={assignment.assignment_id}><span className={s.badge} data-tone={state.tone}>{state.label}</span>{!assignment.title_context && <p className={s.eyebrow}>{assignment.assignment_key}</p>}<h3>{assignment.title}</h3><p className={s.muted} id={`claim-context-${assignment.assignment_id}`}>{assignment.title_context || (assignment.due_at ? `마감 ${timeText(assignment.due_at)} KST` : "마감일 미설정")}{closed && " · 마감됨"}</p><p className={s.muted}>{assignment.accepted_at ? `IDE 수락 ${timeText(assignment.accepted_at)}` : "IDE에서 아직 수락하지 않았습니다."}</p>{receipt && <ReceiptSummary row={receipt} />}<p>{state.scored ? receipt?.feedback_policy === "score_only" ? "내 점수에서 공개된 총점을 확인하세요." : "내 점수에서 평가 항목과 수정 힌트를 확인하세요." : state.hint}</p>{state.scored && <button className={s.secondary} aria-label={assignment.title_context && `${assignment.title} · ${assignment.title_context} · 평가 결과 보기`} onClick={() => route("scores", selectedCourse, assignment.assignment_id)}>평가 결과 보기</button>}<button className={s.primary} disabled={closed || busy || claimBusy || Boolean(courseError)} aria-busy={issuing || undefined} aria-describedby={assignment.title_context ? `claim-context-${assignment.assignment_id}` : undefined} onClick={() => void issueClaim(assignment.assignment_id)}>{issuing ? "수령 코드 발급 중…" : closed ? "수령 기간 마감" : usableClaim(claim, assignment.assignment_id, serverNow) ? "발급한 코드 보기" : assignment.accepted_at ? "이 실습 연결 코드 발급" : "수령 코드 발급"}</button>{assignment.accepted_at && <p className={s.muted}>IDE의 ‘내 실습’에서 같은 과제를 선택해 기존 작업을 이어갈 수 있습니다.</p>}</article>;
        })}</div>{!activeData.overview.assignments.length ? <p className={s.empty}>공개된 과제가 없습니다. 교수자의 과제 공개를 기다려 주세요.</p> : !assignmentRows.length && <div className={s.empty}>
          <p>{activeData.overview.assignments_truncated || activeData.overview.latest_truncated ? "조회한 범위에서 이 조건에 해당하는 과제가 없습니다." : taskFilter === "completed" ? "아직 결과가 공개된 과제가 없습니다." : "선택한 조건에 해당하는 과제가 없습니다."}</p>
          <button className={s.secondary} onClick={() => {setTaskFilter("all"); assignmentFilterField.current?.focus();}}>전체 과제 보기</button>
        </div>}{activeData.overview.assignments_truncated && <p className={s.muted}>일부 과제만 표시합니다. <a href={activeData.links.assignments}>전체 과제 확인</a></p>}</section></>}
        {view !== "materials" && <details className={s.panel}><summary className={s.historyHeading}>공식 제출 이력 <span>{activeData.overview.history.length}건</span></summary><p className={s.muted}>작업 자동 저장은 공식 제출에 포함되지 않습니다. 모든 시각은 한국 시간입니다.</p>{!activeData.overview.history.length && <p>아직 공식 제출한 기록이 없습니다.</p>}{activeData.overview.history.map(row => <details key={row.submission_id} id={`history-${row.submission_id}`} className={s.history}><summary>{row.title} · 버전 {row.version} · {timeText(row.received_at, "second")} · {submissionState(row).label}</summary><ReceiptSummary row={row} previous={false} /></details>)}{activeData.overview.history_truncated && <p className={s.muted}>최근 {activeData.overview.limit}건을 표시합니다. 이전 제출본은 IDE에서 확인하세요.</p>}</details>}
      </>}</>}
      {student && <StudentPrivacy key={student.student_key} studentKey={student.student_key} sessionKey={boot.csrf} visible={privacy === "visible"} settings={view === "privacy"} preferredCourse={selectedCourse} onOpenSettings={() => route("privacy")} onMutationStart={() => resumeBarrier.current.begin()} onMutationEnd={finishMutation} />}
      <footer className={s.footer}><span>Autograde · 웹에서 준비, IDE에서 실습</span>{student ? <button className={s.quiet} onClick={() => route("privacy")}>정보 활용 설정</button> : <a href="/student/privacy">정보 활용 안내</a>}<span>자동 저장 ≠ 공식 제출 · 공용 PC에서는 웹 로그아웃과 IDE 종료</span></footer>
    </main>
    </div>
  </div>;
}

function ReceiptSummary({ row, previous = true }: { row: Receipt; previous?: boolean }) {
  const state = submissionState(row);
  return <div className={s.receipt}>
    <ol className={s.progress} aria-label="현재 제출 처리 단계">{stages.map((label, index) => <li key={label} data-active={index === state.stage} aria-current={index === state.stage ? "step" : undefined} data-complete={index < state.stage}>{label}</li>)}</ol>
    <dl className={s.scores}><div><dt>이번 제출 점수</dt><dd>{state.scored ? scoreText(row.score, row.max_score) : "점수 미확정"}</dd></div>{previous && <div><dt>이전 제출 중 최고점</dt><dd>{scoreText(row.previous_best_score, row.previous_best_max_score)}</dd><small>{row.previous_best_at ? timeText(row.previous_best_at, "second") : "현재 제출 제외"}</small></div>}</dl>
    <p className={s.muted}>접수 {timeText(row.received_at, "second")} KST · 버전 {row.version}</p><p className={s.muted}>접수번호 <span className={s.mono}>{row.submission_id}</span></p><details><summary>코드 버전 확인</summary><code className={s.mono}>{row.code_version}</code></details>
  </div>;
}
