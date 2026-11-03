import {courseMeta, type Course, type CourseData} from "./model";
import s from "./student.module.css";

type Destination = "catalog" | "account" | "privacy" | "course" | "scores" | "materials";
export function StudentHome({courses, verified, busy, navigate, refresh, withdraw}: {
  courses: Course[]; verified: boolean; busy: boolean;
  navigate: (view: Destination, course?: string) => void; refresh: () => void; withdraw: (course: string) => void;
}) {
  const learning = courses.filter(course => course.enrolled), waiting = courses.filter(course => !course.enrolled);
  return <>
    <section className={s.learningWelcome} aria-label="학습 시작 안내">
      <div><span className={s.eyebrow}>MY LEARNING</span><h2>{learning.length ? "오늘의 실습을 이어가세요" : "첫 수업을 준비해 볼까요?"}</h2><p>{learning.length ? "아래에서 수업을 열면 실습 과제와 필요한 자료를 한곳에서 확인할 수 있어요." : "수강할 교과목과 분반을 선택하면 명단 확인 후 실습을 시작할 수 있어요."}</p></div>
      <dl className={s.learningCounts}><div><dt>이용 가능한 수업</dt><dd>{learning.length}<span>개</span></dd></div><div><dt>연결 확인 중</dt><dd>{waiting.length}<span>개</span></dd></div></dl>
    </section>
    <div className={s.sectionHeading}><h2>내 수업 <span>{courses.length}</span></h2><button className={s.secondary} onClick={() => navigate("catalog")}>수업 추가</button></div>
    {!courses.length && <div className={s.empty}><h2>수강할 수업을 선택해 주세요</h2><p>교과목명과 분반을 확인하고 신청하면 준비가 시작됩니다.</p><button className={s.primary} onClick={() => navigate("catalog")}>수강할 수업 선택</button></div>}
    <div className={s.grid}>{courses.map(course => <article className={s.course} key={course.course_key} aria-label={`${course.name || course.course_key} 수업`}>
      <div className={s.courseCardTop}><span className={s.eyebrow}>{course.code}</span><span className={s.badge} data-tone={course.enrolled ? "success" : "warning"}>{course.enrolled ? "수강 중" : course.status === "suspended" ? "수강 상태 확인 필요" : !verified ? "계정 활성화 대기" : "명단 확인 대기"}</span></div>
      <h3>{course.name || course.course_key}</h3><p className={s.muted}>{courseMeta(course)}</p>
      <p>{course.enrolled ? "실습 과제부터 자료, 제출 결과까지 이 수업에서 이어가세요." : !verified ? "교수자에게 받은 활성화 코드를 입력하면 명단을 연결할 수 있어요." : "교수자 명단에 학번이 등록되면 연결됩니다. 오래 기다렸다면 교수자에게 확인해 주세요."}</p>
      <div className={s.actions}>{course.enrolled ? <><button className={s.primary} onClick={() => navigate("course", course.course_key)}>수업 열기 <span aria-hidden="true">→</span></button><button className={s.secondary} onClick={() => navigate("materials", course.course_key)}>학습 자료</button><button className={s.secondary} onClick={() => navigate("scores", course.course_key)}>내 점수</button></> : !verified ? <button className={s.primary} onClick={() => navigate("account")}>계정 활성화</button> : <button className={s.secondary} onClick={refresh}>명단 연결 다시 확인</button>}
      {course.can_withdraw && <button className={s.quiet} disabled={busy} onClick={() => withdraw(course.course_key)}>신청 취소</button>}</div>
    </article>)}</div>
    <section aria-labelledby="student-tools-heading"><div className={s.sectionHeading}><h2 id="student-tools-heading">내 학습 관리</h2><p className={s.muted}>필요한 작업을 바로 선택하세요.</p></div><div className={s.taskGrid}>
      <button className={s.taskTile} onClick={() => navigate("scores", learning[0]?.course_key || "")}><span className={s.taskMark} aria-hidden="true">01</span><span><strong>제출 결과 살펴보기</strong><small>이번 점수와 이전 최고점을 비교해 보세요.</small></span><span aria-hidden="true">→</span></button>
      <button className={s.taskTile} onClick={() => navigate("account")}><span className={s.taskMark} aria-hidden="true">02</span><span><strong>계정 관리하기</strong><small>본인 확인과 비밀번호 변경을 진행하세요.</small></span><span aria-hidden="true">→</span></button>
      <button className={s.taskTile} onClick={() => navigate("privacy")}><span className={s.taskMark} aria-hidden="true">03</span><span><strong>정보 활용 설정</strong><small>연구·수업 개선 참여 여부를 직접 선택하세요.</small></span><span aria-hidden="true">→</span></button>
    </div></section>
    <section className={s.learningSteps} aria-label="실습 진행 순서"><h2>실습은 이렇게 이어집니다</h2><ol><li><strong>수업에서 과제 선택</strong><span>수령 코드를 발급받아 IDE에 입력해요.</span></li><li><strong>IDE에서 작성·실행</strong><span>과제를 해결하고 ‘공식 제출’을 눌러요.</span></li><li><strong>점수와 피드백 확인</strong><span>결과를 보고 코드를 개선할 수 있어요.</span></li></ol><p>공용 PC를 떠날 때는 IDE를 종료하고 학생 웹에서도 로그아웃해 주세요.</p></section>
  </>;
}

export function StudentMaterials({data}: {data: CourseData}) {
  return <section aria-labelledby="student-materials-heading"><div className={s.sectionHeading}><h2 id="student-materials-heading">이 수업의 학습 자료</h2></div><p>교수자가 배포한 자료를 확인하고 필요한 파일을 내려받으세요. 개인별 자료는 본인에게 배포된 것만 표시됩니다.</p>
    <div className={s.grid}>
      {[{href:data.links.materials,title:"강의·개인 배포 자료",help:"강의자료와 본인에게 배포된 PDF·ZIP 파일을 확인할 수 있어요.",action:"내 자료 다운로드"}, {href:data.links.exam_papers,title:"시험·평가 자료",help:"교수자가 공개한 시험지와 평가 자료를 확인할 수 있어요.",action:"시험지 다운로드"}].map(item => <article className={s.course} key={item.title}><span className={s.badge}>{item.href ? "자료함 이용 가능" : "아직 준비 중"}</span><h3>{item.title}</h3><p>{item.help}</p>{item.href ? <a className={s.primary} href={item.href}>{item.action} <span aria-hidden="true">→</span></a> : <p className={s.muted}>자료함이 열리면 이곳에서 바로 이용할 수 있어요.</p>}</article>)}
    </div><p className={s.materialNote}>실습 코드의 다운로드와 제출은 ‘실습 과제’에서 코드를 발급받은 뒤 Autograde IDE에서 진행해 주세요.</p>
  </section>;
}
