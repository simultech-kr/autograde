import {useRef} from "react";
import {courseMeta, type Course} from "./model";
import s from "./student.module.css";

const maxCourses = 20; // StudentRegistration.request_courses accepts 1–20 courses per request.
const canRequest = (course: Course) => course.status === "available" && !course.enrolled;

function courseStatus(course: Course, verified: boolean) {
  if (course.enrolled) return {label: "수강 중", hint: "과제·자료와 공식 제출 현황을 확인하세요."};
  if (course.status === "suspended") return {label: "수강 상태 확인 필요", hint: "수강이 중지되었습니다. 교수자에게 수강 상태 확인을 요청하세요."};
  if (course.status === "pending") {
    return verified
      ? {label: "명단 확인 대기", hint: "교수자 명단에 학번이 있는지 확인을 요청하세요. 명단이 반영되면 자동 연결됩니다."}
      : {label: "계정 활성화 대기", hint: "교수자에게 본인 확인을 받고 활성화 코드를 입력하세요."};
  }
  return {label: "신청 가능", hint: "수업을 선택해 신청하세요. 활성화된 계정과 교수자 명단을 함께 확인합니다."};
}

type CatalogProps = {
  courses: Course[]; verified: boolean; busy: boolean;
  search: string; onSearch: (value: string) => void;
  selected: Course[]; onSelect: (courses: Course[]) => void;
  onEnroll: (keys: string[]) => void;
};

export function StudentCourseCatalog({courses, verified, busy, search, onSearch, selected, onSelect, onEnroll}: CatalogProps) {
  const searchInput = useRef<HTMLInputElement>(null);
  const query = search.trim().toLocaleLowerCase("ko-KR");
  const visible = courses.filter(course =>
    [course.name, course.code, courseMeta(course)].join(" ").toLocaleLowerCase("ko-KR").includes(query));
  const catalog = new Map(courses.map(course => [course.course_key, course]));
  const selection = selected.map(previous => {
    const current = catalog.get(previous.course_key);
    return {course: current || previous, available: !!current && canRequest(current)};
  });
  const selectedKeys = new Set(selected.map(course => course.course_key));
  const hiddenCount = selected.length - visible.filter(course => selectedKeys.has(course.course_key)).length;
  const changed = selection.some(item => !item.available);
  const ready = selected.length > 0 && selected.length <= maxCourses && !changed && !busy;

  function remove(key: string) {
    onSelect(selected.filter(course => course.course_key !== key));
  }
  return <>
    <section className={s.panel} aria-label="수강할 수업 검색">
      <label className={s.search}>수업 검색<input ref={searchInput} value={search} onChange={event => onSearch(event.target.value)} placeholder="교과목명·코드·학기·분반" /></label>
      {search && <button className={s.quiet} onClick={() => {onSearch(""); searchInput.current?.focus();}}>수업 검색 초기화</button>}
      <p role="status" className={s.muted}>전체 {courses.length}개 중 {visible.length}개 표시</p>
      <div className={s.catalog}>{visible.map(course => {
        const checked = selectedKeys.has(course.course_key);
        const {label, hint} = courseStatus(course, verified);
        return <label key={course.course_key} className={s.catalogRow}>
          <input type="checkbox" checked={checked || course.enrolled || course.status === "pending"}
            disabled={busy || !canRequest(course) || !checked && selected.length >= maxCourses}
            onChange={event => event.target.checked ? onSelect([...selected, course]) : remove(course.course_key)} />
          <span><strong>{course.name || course.code}</strong><small>{course.code} · {courseMeta(course)}</small><small>{hint}</small></span>
          <span className={s.badge}>{label}</span>
        </label>;
      })}</div>
      {!visible.length && <p className={s.empty}>{courses.length ? "검색한 수업이 없습니다. 교과목명·코드 또는 분반을 확인하세요." : "현재 신청할 수 있는 수업이 없습니다. 수업이 보이지 않으면 교수자에게 확인하세요."}</p>}
    </section>
    <section className={s.enrollBar} aria-labelledby="selected-courses-title"><div>
      <h2 id="selected-courses-title">선택한 수업 {selected.length}개</h2>
      <p role="status">{query && hiddenCount > 0 && `현재 검색 결과 밖의 선택 ${hiddenCount}개를 포함합니다. `}한 번에 최대 {maxCourses}개까지 신청할 수 있습니다.</p>
      {changed && <p role="alert">수강 상태가 바뀐 수업이 있습니다. 아래에서 해당 수업을 선택 해제한 뒤 신청하세요.</p>}
      {selected.length > 0 && <ul className={s.selectionList}>{selection.map(({course, available}) => <li key={course.course_key}>
        <span><strong>{course.name || course.code}</strong> · {course.code} · {courseMeta(course)}{!available && " · 현재 신청 불가"}</span>
        <button className={s.quiet} disabled={busy} aria-label={`${course.name || course.code} · ${course.code} · ${courseMeta(course)} 선택 해제`}
          onClick={() => {remove(course.course_key); searchInput.current?.focus();}}>선택 해제</button>
      </li>)}</ul>}
      {!selected.length && <p>신청할 수업을 목록에서 선택하세요.</p>}
    </div><button className={s.primary} disabled={!ready} onClick={() => {if (ready) onEnroll([...selectedKeys]);}}>
      {busy ? "신청 중…" : `선택한 ${selected.length}개 수업 신청`}
    </button></section>
  </>;
}
