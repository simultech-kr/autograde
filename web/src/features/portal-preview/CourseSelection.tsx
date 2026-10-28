"use client";

import { useState } from "react";
import type { EnrollmentStatus } from "./previewData";
import s from "./portal-preview.module.css";

export type CourseKey = "come2201" | "come3105";
export const currentCourses: { key: CourseKey; title: string; summary: string }[] = [
  { key: "come2201", title: "소프트웨어 설계", summary: "설계 패턴 · C++ 실습" },
  { key: "come3105", title: "시스템 프로그래밍", summary: "C/C++ · 원격 Linux 실습" },
];

export function CourseSelection({ memberships, onEnroll, onBack }: {
  memberships: Partial<Record<CourseKey, EnrollmentStatus>>;
  onEnroll: (keys: CourseKey[]) => void;
  onBack: () => void;
}) {
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<CourseKey[]>([]);
  const query = search.trim().toLocaleLowerCase("ko-KR");
  const visible = currentCourses.filter(course => `${course.key} ${course.title} 01`.toLocaleLowerCase("ko-KR").includes(query));
  const selectedCourses = currentCourses.filter(course => selected.includes(course.key));

  function toggle(key: CourseKey) {
    setSelected(keys => keys.includes(key) ? keys.filter(item => item !== key) : [...keys, key]);
  }

  return <>
    <section className={s.catalog} aria-labelledby="available-courses-title">
      <div className={s.catalogHeader}>
        <div><h2 id="available-courses-title">현재 운영 중인 수업 <span>{currentCourses.length}</span></h2><p>2026학년도 2학기 · 수강할 수업을 모두 선택하세요.</p></div>
        <label className={s.catalogSearch}><span>수업 검색</span><input value={search} onChange={event => setSearch(event.target.value)} placeholder="교과목명 또는 코드"/></label>
      </div>
      <div className={s.catalogColumns} aria-hidden="true"><span>교과목</span><span>학기 · 분반</span><span>연결 상태</span></div>
      <div className={s.catalogList}>
        {visible.map(course => {
          const status = memberships[course.key];
          const enrolled = status === "matched" || status === "pending";
          return <label className={`${s.catalogRow} ${selected.includes(course.key) ? s.catalogSelected : ""}`} key={course.key}>
            <span className={s.catalogIdentity}>
              <input type="checkbox" checked={enrolled || selected.includes(course.key)} disabled={enrolled} onChange={() => toggle(course.key)} aria-label={`${course.title} ${course.key.toUpperCase()} 01분반 선택`} aria-describedby={`catalog-${course.key}-status`}/>
              <span><span className={s.courseCode}>{course.key.toUpperCase()}</span><strong>{course.title}</strong><small>{course.summary}</small></span>
            </span>
            <span className={s.catalogTerm}>2026학년도 2학기<small>01분반</small></span>
            <span className={s.catalogStatus} id={`catalog-${course.key}-status`}>
              <span className={`${s.status} ${status === "matched" ? s.matched : status === "pending" ? s.pending : s.invited}`}>{status === "matched" ? "수강 중" : status === "pending" ? "명단 확인 대기" : "신청 가능"}</span>
              <small>{enrolled ? "이미 내 수업에 추가됨" : status === "invited" ? "명단 확인됨 · 선택 후 바로 연결" : "신청 후 교수자 명단과 확인"}</small>
            </span>
          </label>;
        })}
        {visible.length === 0 && <div className={s.empty}><h3>검색한 수업이 없습니다.</h3><p>교과목명이나 코드를 다시 확인해 주세요.</p><button className={s.secondaryButton} onClick={() => setSearch("")}>수업 검색 초기화</button></div>}
      </div>
      <div className={s.catalogFooter}><span role="status">{visible.length}개 표시 · 전체 {currentCourses.length}개</span><span>수강 중이거나 확인 대기인 수업은 중복 신청할 수 없습니다.</span></div>
    </section>
    <div className={s.enrollmentSummary}>
      <div><strong>선택한 수업 {selected.length}개</strong><p>{selectedCourses.length ? selectedCourses.map(course => course.title).join(" · ") : "목록에서 수강할 수업을 선택하세요."}</p></div>
      <div className={s.enrollmentActions}><button className={s.secondaryButton} onClick={onBack}>내 수업으로 이동</button><button className={s.primaryButton} disabled={selected.length === 0} onClick={() => onEnroll(selected)}>선택한 {selected.length}개 수업 신청</button></div>
    </div>
    <div className={s.enrollmentHelp}><strong>수강 신청 후에는</strong><p>교수자 명단에 학번이 있으면 바로 수강할 수 있습니다. 아직 없으면 ‘명단 확인 대기’로 표시되며, 명단 등록 후 자동 연결됩니다.</p></div>
  </>;
}
