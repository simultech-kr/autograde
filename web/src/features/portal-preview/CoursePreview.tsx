import s from "./portal-preview.module.css";

export function CoursePreview({ course, open, notify }: { course: "come2201" | "come3105"; open: (key: "process" | "files" | "pattern") => void; notify: () => void }) {
  const system = course === "come3105";
  return <>
    <div className={s.coursePreviewSummary}><span>2026학년도 2학기 · 01분반</span><span>과제 {system ? 2 : 1}개 · 자료 {system ? 2 : 1}개</span></div>
    <section className={s.nextStep}><h2>수업 안내</h2><p>과제 안내를 읽고 수령 코드를 IDE에 입력하세요. 작성 중인 코드의 자동 저장과 공식 제출은 별도입니다.</p><p>공용 PC를 떠날 때는 IDE를 종료하고 학생 웹에서도 로그아웃하세요.</p></section>
    <section className={s.taskSection} aria-labelledby="course-tasks-title"><div className={s.sectionHeading}><h2 id="course-tasks-title">실습 과제</h2><span className={s.muted}>수업 전체 목록</span></div>
      <button className={s.taskRow} onClick={() => open(system ? "process" : "pattern")}><span className={s.taskCopy}><span className={s.taskEyebrow}>{course.toUpperCase()} · WEEK 09</span><strong>{system ? "프로세스 생성과 실행" : "책임 연쇄 패턴으로 요청 처리"}</strong><span>10월 30일 13:50 마감</span></span><span className={s.due}>공식 제출 전</span></button>
      {system && <button className={s.taskRow} onClick={() => open("files")}><span className={s.taskCopy}><span className={s.taskEyebrow}>COME3105 · WEEK 08</span><strong>파일 입출력과 오류 처리</strong><span>공식 제출 접수 · 버전 3</span></span><span className={s.score}><strong>80<small> / 100</small></strong><span>이전 제출 중 최고점 95점</span></span></button>}
    </section>
    <section className={s.materialSection}><div className={s.sectionHeading}><h2>강의자료</h2><span className={s.muted}>PDF · ZIP</span></div>{(system ? ["09주차 실습 안내.pdf", "프로세스 예제 코드.zip"] : ["책임 연쇄 패턴 실습 안내.pdf"]).map(name => <button key={name} className={s.material} onClick={notify}><span><strong>{name}</strong><small>자료 다운로드 화면 체험</small></span><span aria-hidden="true">→</span></button>)}</section>
  </>;
}
