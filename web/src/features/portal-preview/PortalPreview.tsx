"use client";

import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { addPreviewRoster, initialStudents, parsePreviewRoster, sampleRoster, statusText, type EnrollmentStatus, type PreviewStudent } from "./previewData";
import { CourseSelection, type CourseKey } from "./CourseSelection";
import s from "./portal-preview.module.css";
import { CoursePreview } from "./CoursePreview";
import { SubmissionPreview } from "./SubmissionPreview";

type Screen = "student" | "signup" | "enroll" | "instructor" | "course";
type Assignment = "process" | "files" | "pattern";
type Modal = "assignment" | "roster" | "guide" | "student" | null;
type IconName = "grid" | "book" | "arrow" | "check" | "clock" | "users" | "upload" | "search" | "close" | "file" | "code" | "moon" | "sun" | "help" | "plus";
const icons: Record<IconName, ReactNode> = {
  grid: <><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></>,
  book: <><path d="M4 4h6a3 3 0 0 1 3 3v14a4 4 0 0 0-4-3H4zM13 7a3 3 0 0 1 3-3h5v14h-4a4 4 0 0 0-4 3"/></>,
  arrow: <path d="M5 12h14m-6-6 6 6-6 6"/>, check: <path d="m5 12 4 4L19 6"/>,
  clock: <><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/></>,
  users: <><circle cx="9" cy="7" r="3"/><path d="M3 21v-3a6 6 0 0 1 12 0v3m1-17a3 3 0 0 1 0 6m2 4a5 5 0 0 1 3 4v3"/></>,
  upload: <><path d="M12 16V3m-5 5 5-5 5 5M4 16v5h16v-5"/></>,
  search: <><circle cx="10" cy="10" r="6"/><path d="m15 15 6 6"/></>,
  close: <path d="m6 6 12 12M6 18 18 6"/>,
  file: <><path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6m-6 4h6"/></>,
  code: <path d="m8 6-6 6 6 6m8-12 6 6-6 6M14 3l-4 18"/>,
  moon: <path d="M20 14A9 9 0 0 1 10 3a9 9 0 1 0 10 11Z"/>,
  sun: <><circle cx="12" cy="12" r="4"/><path d="M12 1v2m0 18v2M1 12h2m18 0h2M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2"/></>,
  help: <><circle cx="12" cy="12" r="9"/><path d="M9 8a3 3 0 0 1 6 1c0 2-3 2-3 4m0 3h.01"/></>,
  plus: <path d="M12 5v14M5 12h14"/>,
};
function Icon({ name }: { name: IconName }) {
  return <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{icons[name]}</svg>;
}
function Status({ status }: { status: EnrollmentStatus }) {
  return <span className={`${s.status} ${s[status]}`}><Icon name={status === "matched" ? "check" : status === "pending" ? "clock" : "users"}/>{statusText[status]}</span>;
}
function Dialog({ title, children, close }: { title: string; children: ReactNode; close: () => void }) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    ref.current?.showModal();
    return () => { previous?.focus(); };
  }, []);
  return <dialog ref={ref} className={s.dialog} aria-labelledby="preview-dialog-title" onCancel={close} onClick={event => { if (event.target === event.currentTarget) close(); }}>
    <div className={s.dialogHeader}><div><span className={s.kicker}>AUTOGRADE · 디자인 시안</span><h2 id="preview-dialog-title">{title}</h2></div><button type="button" className={s.iconButton} aria-label="닫기" onClick={close}><Icon name="close"/></button></div>
    <div className={s.dialogBody}>{children}</div>
  </dialog>;
}

export function PortalPreview() {
  const [screen, setScreen] = useState<Screen>("student");
  const [activeCourse, setActiveCourse] = useState<CourseKey>("come3105");
  const [theme, setTheme] = useState<"light" | "dark">("light");
  const [modal, setModal] = useState<Modal>(null);
  const [students, setStudents] = useState<PreviewStudent[]>(initialStudents);
  const [systemStudents, setSystemStudents] = useState<PreviewStudent[]>([{ key: "20269903", status: "matched", joined: "10.01 09:00" }]);
  const [activeAssignment, setActiveAssignment] = useState<Assignment>("process");
  const [filter, setFilter] = useState<EnrollmentStatus | "all">("all");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<PreviewStudent | null>(null);
  const [studentKey, setStudentKey] = useState("20269903");
  const [signupError, setSignupError] = useState("");
  const [notice, setNotice] = useState("");
  const [rosterKeys, setRosterKeys] = useState<string[]>([]);
  const [rosterName, setRosterName] = useState("");
  const [rosterError, setRosterError] = useState("");
  const [claim, setClaim] = useState(false);
  const [taskFilter, setTaskFilter] = useState("all");
  const fileRequest = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const studentInput = useRef<HTMLInputElement>(null);
  useEffect(() => { setTheme(matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light"); }, []);
  const instructor = screen === "instructor";
  const matched = students.filter(row => row.status === "matched").length;
  const pending = students.filter(row => row.status === "pending").length;
  const invited = students.filter(row => row.status === "invited").length;
  const current = students.find(row => row.key === studentKey);
  const connected = current?.status === "matched";
  const systemCurrent = systemStudents.find(row => row.key === studentKey && row.status !== "invited");
  const systemConnected = systemCurrent?.status === "matched";
  const courseCount = Number(Boolean(current && current.status !== "invited")) + Number(Boolean(systemCurrent));
  const assignmentTitle = activeAssignment === "files" ? "파일 입출력과 오류 처리" : activeAssignment === "pattern" ? "책임 연쇄 패턴으로 요청 처리" : "프로세스 생성과 실행";
  function openCourse(course: CourseKey) { setActiveCourse(course); navigate("course"); }
  function openAssignment(assignment: Assignment) { setActiveAssignment(assignment); setClaim(false); setModal("assignment"); }
  const shown = students.filter(row => (filter === "all" || row.status === filter) && row.key.includes(search.trim()));

  function navigate(next: Screen) {
    fileRequest.current++; setScreen(next); setModal(null); setNotice(""); setSignupError("");
    requestAnimationFrame(() => heading.current?.focus());
  }
  function showRoster() {
    fileRequest.current++; setRosterKeys([]); setRosterName(""); setRosterError(""); setModal("roster");
  }
  function signup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget), key = String(data.get("student_key") ?? "").trim();
    if (!/^\d{4,20}$/.test(key)) { setSignupError("학번은 숫자 4~20자리로 입력해 주세요."); studentInput.current?.focus(); return; }
    setStudentKey(key);
    navigate("enroll");
    setNotice("가입 단계를 완료했습니다. 현재 운영 중인 수업에서 수강할 수업을 선택하세요. 실제 계정은 만들어지지 않습니다.");
  }
  function enroll(keys: CourseKey[]) {
    let newlyMatched = 0;
    for (const key of keys) {
      const rows = key === "come2201" ? students : systemStudents;
      const existing = rows.find(row => row.key === studentKey);
      if (existing?.status === "matched" || existing?.status === "pending") continue;
      if (existing?.status === "invited") newlyMatched++;
      (key === "come2201" ? setStudents : setSystemStudents)(currentRows => {
        const membership = currentRows.find(row => row.key === studentKey);
        if (membership) return currentRows.map(row => row.key === studentKey && row.status === "invited" ? { ...row, status: "matched", joined: "방금" } : row);
        return [...currentRows, { key: studentKey, status: "pending", joined: "방금" }];
      });
    }
    navigate("student");
    setNotice(`${keys.length}개 수업을 신청했습니다. ${newlyMatched ? `${newlyMatched}개 수업은 명단이 확인되어 바로 이용할 수 있습니다.` : "교수자의 수강 명단이 확인되면 자동 연결됩니다."} 실제 서버에는 저장하지 않았습니다.`);
  }
  async function readFile(file: File | undefined) {
    const request = ++fileRequest.current;
    setRosterError(""); setRosterKeys([]); setRosterName("");
    if (!file) return;
    if (file.size > 1024 * 1024) { setRosterError("1 MB 이하의 CSV 파일을 선택해 주세요."); return; }
    try {
      const keys = parsePreviewRoster(await file.text());
      if (fileRequest.current !== request) return;
      setRosterKeys(keys); setRosterName(file.name);
    } catch (error) { if (fileRequest.current === request) setRosterError(error instanceof Error ? error.message : "CSV를 읽을 수 없습니다."); }
  }
  function applyRoster() {
    const count = students.filter(row => row.status === "pending" && rosterKeys.includes(row.key)).length;
    setStudents(rows => addPreviewRoster(rows, rosterKeys)); setModal(null);
    setNotice(`예시 명단 ${rosterKeys.length}명을 반영하고 대기 학생 ${count}명을 수업에 연결했습니다. 실제 서버에는 저장하지 않았습니다.`);
  }

  return <div className={s.preview} data-theme={theme}>
    <div className={s.reviewBar}>
      <span className={s.reviewLabel}><span className={s.reviewDot}/>디자인 미리보기 <span>· 예시 데이터</span></span>
      <nav className={s.reviewTabs} aria-label="디자인 화면 선택">
        {([ ["student", "학생 · 내 수업"], ["signup", "회원가입"], ["enroll", "수강 선택"], ["instructor", "교수자 · 수강 명단"] ] as const).map(([id, label]) => <button key={id} aria-pressed={screen === id} onClick={() => navigate(id)}>{label}</button>)}
      </nav>
      <button className={s.themeButton} onClick={() => setTheme(theme === "light" ? "dark" : "light")} aria-label={theme === "light" ? "어두운 테마로 보기" : "밝은 테마로 보기"}><Icon name={theme === "light" ? "moon" : "sun"}/></button>
    </div>
    <div className={s.app}>
      <aside className={s.sidebar}>
        <button className={s.brand} onClick={() => navigate(instructor ? "instructor" : "student")} aria-label="Autograde 포털 홈"><span className={s.brandMark}>a<span>·</span></span><span>autograde<small>LEARNING PORTAL</small></span></button>
        <div className={s.workspace}><span className={s.workspaceIcon}><Icon name="book"/></span><span><strong>{instructor ? "소프트웨어 설계" : "나의 학습 공간"}</strong><small>{instructor ? "COME2201 · 01분반" : "2026학년도 · 2학기"}</small></span></div>
        <span className={s.navLabel}>{instructor ? "수업 운영" : "학습"}</span>
        <nav className={s.mainNav} aria-label={instructor ? "교수자 메뉴" : "학생 메뉴"}>
          {instructor ? <button className={s.navActive} aria-current="page" onClick={() => { setFilter("all"); setSearch(""); }}><Icon name="users"/>가입·수강 명단<span className={s.navCount}>{pending}</span></button> : <>
            <button className={screen === "student" ? s.navActive : undefined} aria-current={screen === "student" ? "page" : undefined} onClick={() => navigate("student")}><Icon name="grid"/>내 수업</button>
            <button className={screen === "enroll" ? s.navActive : undefined} aria-current={screen === "enroll" ? "page" : undefined} onClick={() => navigate("enroll")}><Icon name="book"/>수강 선택</button>
            <button className={screen === "signup" ? s.navActive : undefined} aria-current={screen === "signup" ? "page" : undefined} onClick={() => navigate("signup")}><Icon name="plus"/>회원가입</button>
          </>}
          <button onClick={() => setModal("guide")}><Icon name="help"/>이용 안내</button>
        </nav>
        <div className={s.sidebarNote}><span className={s.kicker}>하나의 학습 흐름</span><p>수업을 연결하고,<br/>실습을 이어가세요.</p><span>웹에서 준비 · IDE에서 실습</span></div>
        <div className={s.profile}><span className={s.avatar}>{instructor ? "교" : "학"}</span><span><strong>{instructor ? "교수자 화면" : "예시 학생"}</strong><small>{instructor ? "담당 수업 관리자" : `${studentKey} · 학습자`}</small></span><span className={s.profileDot}/></div>
      </aside>
      <div className={s.workspaceMain}>
        <header className={s.topbar}><span>{instructor ? "교수자 포털" : "학생 포털"}<span className={s.breadcrumbSlash}>/</span><strong>{screen === "signup" ? "회원가입" : screen === "enroll" ? "수강 선택" : instructor ? "가입·수강 명단" : screen === "course" ? "수업 홈" : "내 수업"}</strong></span><span className={s.term}>2026학년도 2학기 <span>·</span> {instructor ? "01분반" : "학습 포털"}</span></header>
        <nav className={s.mobileNav} aria-label="모바일 포털 메뉴"><button onClick={() => navigate("student")} aria-current={screen === "student" ? "page" : undefined}>내 수업</button><button onClick={() => navigate("enroll")} aria-current={screen === "enroll" ? "page" : undefined}>수강 선택</button><button onClick={() => setModal("guide")}>이용 안내</button><a href="/app/student">실제 학생 포털</a></nav>
        <main className={s.main} id="main" tabIndex={-1}>
          {notice && <div className={s.notice} role="status"><Icon name="check"/><span>{notice}</span><button className={s.iconButton} aria-label="알림 닫기" onClick={() => setNotice("")}><Icon name="close"/></button></div>}
          {screen === "student" && <>
            <div className={s.pageHeading}><div><p className={s.kicker}>MY LEARNING</p><h1 ref={heading} tabIndex={-1}>내 수업</h1><p>수업별 과제와 자료를 한곳에서 확인하세요.</p></div><button className={s.secondaryButton} onClick={() => navigate("enroll")}><Icon name="plus"/>수업 추가</button></div>
            <div className={s.studentLayout}>
              <div className={s.studentPrimary}>
                <section className={s.courseSection} aria-labelledby="courses-title">
                  <div className={s.sectionHeading}><h2 id="courses-title">수강 교과목 <span>{courseCount}</span></h2><span className={s.muted}>이번 학기</span></div>
                  {courseCount === 0 && <div className={s.empty}><Icon name="book"/><h3>아직 신청한 수업이 없습니다.</h3><p>현재 운영 중인 수업 목록에서 수강할 수업을 선택하세요.</p><button className={s.primaryButton} onClick={() => navigate("enroll")}>수강할 수업 선택</button></div>}
                  <div className={s.courseGrid}>
                    {systemCurrent && <article className={`${s.courseCard} ${s.primaryCourse}`}><div className={s.courseTop}><span className={s.courseIcon}><Icon name="code"/></span><Status status={systemConnected ? "matched" : "pending"}/></div><span className={s.courseCode}>COME3105 · 01분반</span><h3>시스템 프로그래밍</h3><p>C/C++ · 원격 Linux 실습</p><div className={s.courseMeta}>{systemConnected ? <><span>진행 중인 과제 <strong>1</strong></span><span>새 강의자료 <strong>2</strong></span></> : <span>교수자의 수강 명단 확인을 기다리고 있습니다.</span>}</div><button className={s.courseAction} onClick={() => systemConnected ? openCourse("come3105") : setModal("guide")}>{systemConnected ? "수업 열기" : "연결 방법 보기"} <Icon name="arrow"/></button></article>}
                    {current && current.status !== "invited" && <article className={s.courseCard}><div className={s.courseTop}><span className={s.courseIcon}><Icon name="book"/></span><Status status={connected ? "matched" : "pending"}/></div><span className={s.courseCode}>COME2201 · 01분반</span><h3>소프트웨어 설계</h3><p>설계 패턴 · C++ 실습</p><div className={s.courseHint}>{connected ? "수강 명단이 확인되었습니다. 수업을 시작할 수 있습니다." : "가입이 완료되었습니다. 교수자의 수강 명단과 일치하면 수업이 열립니다."}</div><button className={s.courseAction} onClick={() => { if (connected) { openCourse("come2201"); } else { setModal("guide"); } }}>{connected ? "수업 열기" : "연결 방법 보기"}<Icon name="arrow"/></button></article>}
                  </div>
                </section>
                <section className={s.taskSection} aria-labelledby="tasks-title"><div className={s.sectionHeading}><h2 id="tasks-title">실습 이어가기</h2><label className={s.smallSelect}><span className="sr-only">과제 상태</span><select value={taskFilter} onChange={event => setTaskFilter(event.target.value)}><option value="all">전체 과제</option><option value="open">공식 제출 전</option><option value="done">결과 공개</option></select></label></div>
                  {systemConnected && taskFilter !== "done" && <button className={s.taskRow} onClick={() => openAssignment("process")}><span className={s.taskIcon}><Icon name="code"/></span><span className={s.taskCopy}><span className={s.taskEyebrow}>COME3105 · WEEK 09</span><strong>프로세스 생성과 실행</strong><span>C · 10월 30일 13:50 마감</span></span><span className={s.due}>공식 제출 전</span><Icon name="arrow"/></button>}
                  {systemConnected && taskFilter !== "open" && <button className={s.taskRow} onClick={() => openAssignment("files")}><span className={s.taskIcon}><Icon name="file"/></span><span className={s.taskCopy}><span className={s.taskEyebrow}>COME3105 · WEEK 08</span><strong>파일 입출력과 오류 처리</strong><span>마지막 제출 · 10월 23일 13:42</span></span><span className={s.score}><strong>80<small> / 100</small></strong><span>이전 최고 95점</span></span><Icon name="arrow"/></button>}
                  {!systemConnected && <div className={s.empty}><Icon name="book"/><h3>{connected ? "수업에서 과제를 선택하세요." : courseCount ? "수업 연결을 기다리고 있습니다." : "수업 선택 후 실습을 시작하세요."}</h3><p>{connected ? "위의 수업 열기에서 실습을 시작할 수 있습니다." : "수강 명단이 확인되면 진행할 과제가 여기에 표시됩니다."}</p></div>}
                </section>
              </div>
              <aside className={s.studentAside}>
                <section className={s.nextStep}><span className={s.kicker}>NEXT STEP</span><h2>IDE에서 실습 시작</h2><p>웹에서 수령 코드를 받고<br/>Autograde IDE에 입력하세요.</p><ol className={s.steps}><li><span>1</span><div><strong>과제 선택</strong><small>수강 중인 수업에서 선택</small></div></li><li><span>2</span><div><strong>수령 코드 발급</strong><small>비밀번호 대신 코드로 연결</small></div></li><li><span>3</span><div><strong>IDE에서 열기</strong><small>다운로드 후 작성·제출</small></div></li></ol><button className={s.textButton} onClick={() => setModal("guide")}>이용 방법 확인 <Icon name="arrow"/></button></section>
                {systemConnected && <section className={s.materialSection}><div className={s.sectionHeading}><h2>새 강의자료</h2><span className={s.count}>2</span></div>{["09주차 실습 안내.pdf", "프로세스 예제 코드.zip"].map((name, i) => <button className={s.material} key={name} onClick={() => setNotice("강의자료 열기 동작 예시입니다. 실제 자료 다운로드는 서버 연결 후 제공됩니다.")}><Icon name="file"/><span><strong>{name}</strong><small>COME3105 · {i ? "ZIP · 24 KB" : "PDF · 1.2 MB"}</small></span><Icon name="arrow"/></button>)}</section>}
              </aside>
            </div>
          </>}
          {screen === "course" && <>
            <div className={s.pageHeading}><div><p className={s.kicker}>COURSE HOME · {activeCourse.toUpperCase()}</p><h1 ref={heading} tabIndex={-1}>{activeCourse === "come3105" ? "시스템 프로그래밍" : "소프트웨어 설계"}</h1><p>과제·강의자료·수업 안내를 확인하고 실습을 이어가세요.</p></div><button className={s.secondaryButton} onClick={() => navigate("student")}>내 수업으로 이동</button></div>
            <CoursePreview course={activeCourse} open={openAssignment} notify={() => setNotice("강의자료 다운로드 화면 예시입니다. 실제 자료는 실제 학생 포털에서 받을 수 있습니다.")}/>
          </>}
          {screen === "enroll" && <>
            <div className={s.pageHeading}><div><p className={s.kicker}>CHOOSE YOUR COURSES</p><h1 ref={heading} tabIndex={-1}>수강할 수업 선택</h1><p>학번 {studentKey} · 현재 운영 중인 수업을 확인하고 신청하세요.</p></div><span className={s.subtlePill}>회원가입 완료 → 수강 선택</span></div>
            <CourseSelection key={studentKey} memberships={{ come2201: current?.status, come3105: systemStudents.find(row => row.key === studentKey)?.status }} onEnroll={enroll} onBack={() => navigate("student")}/>
          </>}
          {screen === "signup" && <>
            <div className={s.pageHeading}><div><p className={s.kicker}>GET STARTED</p><h1 ref={heading} tabIndex={-1}>수업을 시작할 준비</h1><p>학번과 비밀번호로 가입하세요. 수업은 가입 후 선택합니다.</p></div><span className={s.subtlePill}>1. 회원가입 → 2. 수강 선택</span></div>
            <div className={s.signupLayout}><section className={s.signupCard}><div className={s.sectionHeading}><h2>학생 회원가입</h2><span className={s.muted}>1단계 · 계정 만들기</span></div><form onSubmit={signup} noValidate><label className={s.field}>학번<input ref={studentInput} aria-invalid={Boolean(signupError)} onChange={() => setSignupError("")} name="student_key" inputMode="numeric" defaultValue="20269909" placeholder="학번을 입력하세요" aria-describedby="signup-error" autoComplete="off"/></label><label className={s.field}>비밀번호<input value="preview-only-password" type="password" readOnly aria-describedby="preview-password-note"/><small id="preview-password-note">시안에서는 예시 비밀번호를 사용합니다. 실제 비밀번호를 입력하지 마세요.</small></label><p className={s.error} id="signup-error" role="alert">{signupError}</p><button className={s.primaryButton} type="submit">가입하고 수업 선택하기 <Icon name="arrow"/></button><p className={s.formFootnote}>실제 가입·저장 없이 화면 흐름만 확인합니다.</p></form></section>
              <aside className={s.signupGuide}><span className={s.largeIcon}><Icon name="users"/></span><h2>가입은 한 번,<br/>수업은 목록에서 선택합니다.</h2><p>먼저 학생 계정을 만들고, 현재 운영 중인 교과목과 분반을 확인한 뒤 원하는 수업을 신청하세요.</p><div className={s.infoBlock}><Icon name="check"/><div><strong>가입 후 수업 목록에서 선택</strong><p>교과목명·코드·학기·분반을 확인합니다.<br/>여러 수업을 함께 신청할 수 있습니다.</p></div></div><div className={s.infoBlock}><Icon name="clock"/><div><strong>신청한 수업의 명단과 자동 연결</strong><p>명단에 학번이 있으면 바로 연결됩니다.<br/>없으면 명단 등록을 기다립니다.</p></div></div><button className={s.textButton} onClick={() => setModal("guide")}>기존 수업 비밀번호가 있나요? <Icon name="arrow"/></button></aside>
            </div>
          </>}
          {screen === "instructor" && <>
            <div className={s.pageHeading}><div><p className={s.kicker}>COURSE MEMBERS</p><h1 ref={heading} tabIndex={-1}>가입·수강 명단</h1><p>수강 명단을 등록하면, 해당 수업을 신청한 학생이 자동으로 연결됩니다.</p></div><button className={s.primaryButton} onClick={showRoster}><Icon name="upload"/>수강 명단 등록</button></div>
            <section className={s.metrics} aria-label="수강 연결 요약">{([["수강 연결", matched, "과제와 자료 이용 가능", "matched"], ["명단 확인 대기", pending, "수강 신청 완료 · 명단에 추가 필요", "pending"], ["수강 신청 전", invited, "명단 등록 완료 · 학생 수강 선택 필요", "invited"]] as const).map(([label, count, description, id]) => <button key={id} className={filter === id ? s.metricSelected : undefined} onClick={() => setFilter(id)} aria-pressed={filter === id}><span>{label}<Icon name={id === "matched" ? "check" : id === "pending" ? "clock" : "users"}/></span><strong>{count}<small>명</small></strong><p>{description}</p></button>)}</section>
            {pending > 0 && <div className={s.actionBanner}><span className={s.actionIcon}><Icon name="clock"/></span><div><strong>가입 학생 {pending}명의 수강 명단을 확인해 주세요.</strong><p>학번이 포함된 명단을 추가하면 자동으로 연결됩니다.</p></div><button className={s.textButton} onClick={() => setFilter("pending")}>대기 학생 보기 <Icon name="arrow"/></button></div>}
            <section className={s.rosterSection} aria-labelledby="roster-title"><div className={s.rosterHeading}><h2 id="roster-title">학생 연결 현황 <span>{students.length}</span></h2><span className={s.muted}>예시 현황 · 10.28 10:00</span></div><div className={s.rosterToolbar}><div className={s.filterTabs} aria-label="연결 상태 필터">{([ ["all", "전체", students.length], ["pending", "명단 대기", pending], ["matched", "수강 연결", matched], ["invited", "수강 신청 전", invited] ] as const).map(([key, label, count]) => <button key={key} onClick={() => setFilter(key)} aria-pressed={filter === key}>{label}<span>{count}</span></button>)}</div><label className={s.search}><Icon name="search"/><input aria-label="학번 검색" placeholder="학번 검색" value={search} onChange={event => setSearch(event.target.value)}/></label></div>
              <div className={s.tableWrap}><table><caption className="sr-only">소프트웨어 설계 학생별 수강 연결 상태</caption><thead><tr><th scope="col">학번</th><th scope="col">연결 상태</th><th scope="col">수강 신청 시각</th><th scope="col">다음 단계</th><th scope="col"><span className="sr-only">상세 보기</span></th></tr></thead><tbody>{shown.map(row => <tr key={row.key}><td data-label="학번"><span className={s.rowIdentity}><span className={s.miniAvatar}>{row.key.slice(-2)}</span><strong>{row.key}</strong></span></td><td data-label="연결 상태"><Status status={row.status}/></td><td data-label="수강 신청 시각" className={s.muted}>{row.joined}</td><td data-label="다음 단계" className={s.muted}>{row.status === "matched" ? "수업 이용 가능" : row.status === "pending" ? "수강 명단에 학번 추가" : "학생 수강 선택 안내"}</td><td><button className={s.rowButton} aria-label={`${row.key} 학생 상세 보기`} onClick={() => { setSelected(row); setModal("student"); }}><Icon name="arrow"/></button></td></tr>)}</tbody></table></div>
              {shown.length === 0 && <div className={s.empty}><Icon name="search"/><h3>검색 결과가 없습니다.</h3><p>학번이나 연결 상태를 변경해 보세요.</p><button className={s.secondaryButton} onClick={() => { setSearch(""); setFilter("all"); }}>검색 초기화</button></div>}
              <div className={s.tableFooter}><span role="status">{shown.length}명 표시 · 전체 {students.length}명</span><span>학번 일치 기준 자동 연결</span></div>
            </section>
            <div className={s.bottomHelp}><Icon name="help"/><span>명단 업로드는 기존 명단에 추가합니다. 파일에 없는 학생을 삭제하지 않습니다.</span><button className={s.textButton} onClick={() => setModal("guide")}>운영 안내</button></div>
          </>}
          <footer className={s.footer}><span>Autograde <span>·</span> 수업에서 실습까지</span><span>화면 체험용 · 서버 저장 없음 · <a href="/app/student">실제 학생 포털 열기</a></span></footer>
        </main>
      </div>
    </div>
    {modal === "roster" && <Dialog title="수강 명단 등록" close={() => { fileRequest.current++; setModal(null); }}><p className={s.muted}>파일 확인 후 추가할 명단을 검토하세요. 시안에서는 실제 학생 정보 대신 예시 파일을 사용하세요.</p><div className={s.uploadArea}><Icon name="upload"/><strong>학번이 담긴 CSV 파일</strong><span>student_key 또는 학번 한 열 · 최대 1 MB</span><label className={s.secondaryButton}>예시 CSV 선택<input type="file" accept=".csv,text/csv" onChange={event => readFile(event.target.files?.[0])}/></label><button className={s.textButton} onClick={() => { fileRequest.current++; setRosterKeys(parsePreviewRoster(sampleRoster)); setRosterName("예시_수강명단.csv"); setRosterError(""); }}>파일 없이 예시 명단 불러오기</button></div><p className={s.error} role="alert">{rosterError}</p>{rosterKeys.length > 0 && <section className={s.importPreview}><h3><Icon name="file"/>{rosterName}</h3><p>중복 제외 {rosterKeys.length}명 · 기존 명단에 추가</p><div className={s.keyChips}>{rosterKeys.slice(0, 12).map(key => <span key={key}>{key}</span>)}{rosterKeys.length > 12 && <span>외 {rosterKeys.length - 12}명</span>}</div></section>}<div className={s.dialogActions}><button className={s.secondaryButton} onClick={() => { fileRequest.current++; setModal(null); }}>취소</button><button className={s.primaryButton} disabled={rosterKeys.length === 0} onClick={applyRoster}>명단 추가 · 자동 연결 체험</button></div></Dialog>}
    {modal === "student" && selected && <Dialog title={`${selected.key} 학생`} close={() => setModal(null)}><Status status={selected.status}/><dl className={s.detailList}><div><dt>수업</dt><dd>소프트웨어 설계 · 01분반</dd></div><div><dt>수강 신청 시각</dt><dd>{selected.joined}</dd></div><div><dt>명단 연결</dt><dd>{selected.status === "pending" ? "수강 허용 명단에 학번이 없습니다." : selected.status === "invited" ? "명단에 등록되어 있으며 수강 신청을 기다립니다." : "가입 학번과 수강 명단이 일치합니다."}</dd></div></dl><p className={s.muted}>수강 연결 상태와 과제 제출·점수는 별도로 관리합니다.</p>{selected.status === "pending" && <button className={s.primaryButton} onClick={showRoster}>수강 명단 추가하기 <Icon name="upload"/></button>}</Dialog>}
    {modal === "assignment" && <Dialog title="실습 과제 열기" close={() => setModal(null)}><span className={s.subtlePill}>과제 수령 흐름 예시</span><h3 className={s.assignmentTitle}>{assignmentTitle}</h3><p className={s.muted}>과제 안내와 제출 상태를 확인한 뒤 IDE로 이동합니다.</p>{activeAssignment === "files" ? <><SubmissionPreview/></> : <div className={s.hint}><Icon name="code"/><div><strong>아직 제출한 기록이 없습니다.</strong><p>{activeAssignment === "pattern" ? "요청을 순서대로 전달하고, 처리 가능한 객체에서 요청을 처리하도록 구현하세요." : "프로세스를 생성하고 자식 프로세스의 실행 결과와 종료 상태를 확인하세요."}</p></div></div>}{claim ? <div className={s.claim} role="status"><strong>수령 코드 표시 영역</strong><code>DEMO · 사용 불가</code><p>실제 화면에서는 여기서 발급한 코드를 IDE에 입력합니다.</p></div> : <button className={s.primaryButton} onClick={() => setClaim(true)}>수령 코드 발급 화면 보기 <Icon name="arrow"/></button>}<p className={s.formFootnote}>예시 과제와 점수입니다. 실제 코드 발급·제출은 수행하지 않습니다.</p></Dialog>}
    {modal === "guide" && <Dialog title="수업 연결 안내" close={() => setModal(null)}><ol className={s.guideList}><li><strong>학번과 비밀번호로 먼저 가입합니다.</strong><p>가입 시에는 수업을 선택하지 않습니다. 기존 6자리 수업 비밀번호가 있다면 먼저 기존 수업에 로그인하고 회원 계정을 연결합니다.</p></li><li><strong>현재 수업 목록에서 수강할 수업을 선택합니다.</strong><p>교과목명·코드·학기·분반을 확인하고 여러 수업을 신청할 수 있습니다. 이미 수강 중이거나 명단 확인 대기인 수업은 중복 신청하지 않습니다.</p></li><li><strong>교수자는 수업별 수강 명단을 올립니다.</strong><p>학번이 일치하면 자동 연결됩니다. 일치하지 않으면 명단 확인 대기로 표시합니다.</p></li><li><strong>학생은 과제 수령 코드를 받아 IDE를 엽니다.</strong><p>웹 비밀번호를 IDE에 넣지 않습니다. 공용 PC를 떠날 때는 웹에서도 로그아웃합니다.</p></li></ol><div className={s.hint}><Icon name="help"/><p>현재 화면은 디자인 체험용입니다. 입력 내용은 서버나 브라우저 저장소에 저장하지 않으며 새로고침하면 초기화됩니다.</p></div></Dialog>}
  </div>;
}
