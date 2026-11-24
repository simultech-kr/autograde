import {useState} from "react";
import {courseMeta, timeText, scoreText, type Course, type LearningHome} from "./model";
import {useStudentListHistory} from "./useStudentListHistory";
import {learningTasks} from "./learningTasks";
import s from "./student.module.css";
import h from "./learningHome.module.css";

type Destination = "catalog" | "account" | "privacy" | "course" | "scores" | "materials";
export function StudentHome({courses, verified, busy, navigate, refresh, withdraw, courseLogin = false, home = null, homeLoading = false, homeError = "", refreshHome = refresh, now = Date.now(), navigationScope}: {
  navigationScope?: string;
  courses: Course[]; verified: boolean; busy: boolean; courseLogin?: boolean;
  home?: LearningHome | null; homeLoading?: boolean; homeError?: string; refreshHome?: () => void; now?:number;
  navigate: (view: Destination, course?: string, assignment?:string) => void; refresh: () => void; withdraw: (course: string) => void;
}) {
  const {option: filter, choose: setFilter} = useStudentListHistory(navigationScope, "home", ["all", "work", "pending", "results"]);
  const [expanded, setExpanded] = useState(false);
  const learning = courses.filter(course => course.enrolled), tasks = home ? learningTasks(home, courses, now) : [];
  const selected = tasks.filter(task => filter === "all" || task.category === filter), displayed = expanded ? selected : selected.slice(0, 6);
  const incomplete = home?.courses_truncated || home?.courses.some(item => item.overview.assignments_truncated || item.overview.latest_truncated);
  return <>
    {verified && learning.length > 0 && <section className={h.workbench} aria-labelledby="learning-tasks-heading">
      <div className={h.title}><div><h2 id="learning-tasks-heading">지금 확인할 실습</h2><p>마감과 제출 상태를 확인하고 바로 이어가세요.</p></div><button className={s.quiet} disabled={homeLoading} onClick={refreshHome}>실습 상태 새로고침</button></div>
      {homeError && <div className={s.error} role="alert"><p>{homeError}</p>{home && <p>아래는 마지막 확인한 상태입니다.</p>}<button className={s.secondary} onClick={refreshHome}>다시 불러오기</button></div>}
      {homeLoading && !home && <p role="status" className={h.loading}>수업별 실습과 제출 상태를 확인하고 있습니다…</p>}
      {home && <>
        <div className={h.filters} aria-label="실습 상태 필터">{([["all","전체"],["work","제출 전"],["pending","채점 진행"],["results","결과 확인"]] as const).map(([value,label]) => <button key={value} aria-pressed={filter === value} onClick={() => {setFilter(value);setExpanded(false);}}>{label}<span>{value === "all" ? tasks.length : tasks.filter(task=>task.category === value).length}</span></button>)}</div>
        <ul className={h.tasks}>{displayed.map(task => <li key={`${task.course.course_key}:${task.assignment.assignment_id}`}>
          <div className={h.taskInfo}><div className={h.meta}><span>{task.course.name || task.course.code}{task.course.section && ` · ${task.course.section}분반`}</span><span className={s.badge} data-tone={task.priority <= 1 ? "warning" : task.category === "pending" ? "info" : undefined}>{task.label}</span></div><h3>{task.assignment.title}</h3><p>{task.receiptOnly ? "마감 정보는 수업에서 확인" : task.assignment.due_at ? `마감 ${timeText(task.assignment.due_at)} KST` : "마감일 미설정"}{task.receipt && ` · 제출 v${task.receipt.version}`}{task.receipt?.published_at && task.receipt.state === "published" && ` · 이번 점수 ${scoreText(task.receipt.score,task.receipt.max_score)}`}</p></div>
          <button className={task.priority <= 2 ? s.primary : s.secondary} aria-label={`${task.assignment.title} · ${task.action}`} onClick={() => navigate(task.destination, task.course.course_key, task.assignment.assignment_id)}>{task.action}<span aria-hidden="true">→</span></button>
        </li>)}</ul>
        {!selected.length && <p className={h.empty}>{filter === "all" ? "현재 확인할 공개 실습이 없습니다. 아래에서 수업 자료를 확인할 수 있습니다." : "이 상태에 해당하는 실습이 없습니다."}</p>}
        {selected.length > 6 && <button className={s.quiet} onClick={() => setExpanded(value => !value)}>{expanded ? "간단히 보기" : `실습 ${selected.length - 6}개 더 보기`}</button>}
        <p className={h.checked}>확인 {timeText(home.server_time)} KST · 화면을 보고 있을 때 30초마다 갱신{incomplete && " · 조회 범위 내 실습입니다. 전체 내역은 각 수업에서 확인하세요."}</p>
      </>}
    </section>}
    <div className={s.sectionHeading}><h2>내 수업 목록 <span>{courses.length}</span></h2>{!courseLogin && <button className={s.secondary} onClick={() => navigate("catalog")}>수업 추가</button>}</div>
    {!courses.length && <div className={s.empty}><h2>{courseLogin ? "연결된 수업을 확인할 수 없습니다" : "수강할 수업을 선택해 주세요"}</h2><p>{courseLogin ? "교수자에게 수강 상태를 확인한 뒤 다시 로그인하세요." : "교과목명과 분반을 확인하고 신청하세요."}</p>{!courseLogin && <button className={s.primary} onClick={() => navigate("catalog")}>수강할 수업 선택</button>}</div>}
    <div className={s.grid}>{courses.map(course => <article className={s.course} key={course.course_key} aria-label={`${course.name || course.course_key} 수업`}>
      <div className={s.courseCardTop}><span className={s.eyebrow}>{course.code}</span><span className={s.badge} data-tone={course.enrolled ? "success" : "warning"}>{course.enrolled ? "수강 중" : course.status === "suspended" ? "수강 상태 확인 필요" : !verified ? "계정 활성화 대기" : "명단 확인 대기"}</span></div>
      <h3>{course.name || course.course_key}</h3><p className={s.muted}>{courseMeta(course)}</p>
      {!course.enrolled && <p>{!verified ? "교수자에게 받은 활성화 코드를 입력하면 수강 명단을 연결할 수 있습니다." : "교수자 명단에 학번이 등록되면 연결됩니다. 연결되지 않으면 교수자에게 확인해 주세요."}</p>}
      <div className={s.actions}>{course.enrolled ? <><button className={s.primary} onClick={() => navigate("course", course.course_key)}>수업 열기 <span aria-hidden="true">→</span></button><button className={s.quiet} onClick={() => navigate("materials", course.course_key)}>자료 받기</button><button className={s.quiet} onClick={() => navigate("scores", course.course_key)}>점수 확인</button></> : !verified ? <button className={s.primary} onClick={() => navigate("account")}>계정 활성화</button> : <button className={s.secondary} onClick={refresh}>명단 연결 다시 확인</button>}
      {!courseLogin && course.can_withdraw && <button className={s.quiet} disabled={busy} onClick={() => withdraw(course.course_key)}>신청 취소</button>}</div>
    </article>)}</div>
    <details className={h.help}><summary>수업 이용과 IDE 연결 안내</summary>{courseLogin && <p>입력한 비밀번호로 확인된 수업만 표시합니다. 다른 비밀번호를 쓰는 수업은 로그아웃 후 해당 비밀번호로 로그인하세요.</p>}<ol><li>수업에서 과제를 선택하고 수령 코드를 발급합니다.</li><li>Autograde IDE에 코드를 입력하고 실습을 작성·실행합니다.</li><li>IDE에서 공식 제출한 뒤 웹 또는 IDE에서 결과를 확인합니다.</li></ol><p>공용 PC를 떠날 때는 IDE를 종료하고 웹에서도 로그아웃하세요.</p></details>
  </>;
}
