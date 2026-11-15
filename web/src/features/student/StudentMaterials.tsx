"use client";
import {useEffect, useRef, useState} from "react";
import {timeText, type CourseData} from "./model";
import {fetchMaterial, fileSize, type MaterialItem} from "./workbench.ts";
import s from "./student.module.css";
import w from "./workbench.module.css";

export function StudentMaterials({data, onRefresh}: {data: CourseData; onRefresh?: () => void}) {
  const [query, setQuery] = useState(""), [format, setFormat] = useState("all");
  const [busy, setBusy] = useState(""), [message, setMessage] = useState(""), [error, setError] = useState("");
  const controller = useRef<AbortController | null>(null), active = useRef(true), urls = useRef(new Set<string>());
  const context = `${data.student_key}:${data.csrf}:${data.course.course_key}`;
  useEffect(() => {
    active.current = true;
    setMessage(""); setError(""); setBusy("");
    const cancel = () => {if (controller.current) {controller.current.abort(); setBusy(""); setMessage("화면을 벗어나 다운로드를 중단했습니다. 필요한 파일의 다운로드를 다시 눌러 주세요.");}};
    window.addEventListener("blur", cancel);
    return () => {active.current = false; controller.current?.abort(); window.removeEventListener("blur",cancel); for (const url of urls.current) URL.revokeObjectURL(url); urls.current.clear();};
  }, [context]);
  async function download(item: MaterialItem) {
    if (controller.current || busy) return;
    const request = new AbortController(); controller.current = request; setBusy(item.id); setError(""); setMessage("");
    const timeout = setTimeout(()=>request.abort(), 120000);
    try {
      const blob = await fetchMaterial(item, data.course.course_key, data.csrf, request.signal);
      if (!active.current || request.signal.aborted || document.visibilityState !== "visible" || !document.hasFocus()) return;
      const url = URL.createObjectURL(blob); urls.current.add(url);
      const anchor = document.createElement("a"); anchor.href = url; anchor.download = item.filename!; anchor.hidden = true;
      document.body.append(anchor); anchor.click(); anchor.remove();
      setMessage(`${item.filename} 다운로드를 브라우저에 전달했습니다. 다운로드 목록에서 저장 결과를 확인하세요.`);
      setTimeout(()=>{URL.revokeObjectURL(url); urls.current.delete(url);},60000);
    } catch (failure) {
      if (active.current && !request.signal.aborted) setError(failure instanceof TypeError ? "서버에 연결하지 못했습니다. 연결을 확인하고 다시 시도하세요." : failure instanceof Error ? failure.message : "다운로드하지 못했습니다. 연결을 확인하고 다시 시도하세요.");
      else if (active.current && document.hasFocus()) setError("다운로드가 중단되었습니다. 다시 시도하세요.");
    } finally {clearTimeout(timeout); if (controller.current === request) controller.current = null; if (active.current) setBusy("");}
  }
  const catalog = data.materials;
  const rows = (catalog?.items || []).filter(item => (format === "all" || item.format === format) && `${item.title} ${item.filename || ""}`.toLocaleLowerCase("ko-KR").includes(query.trim().toLocaleLowerCase("ko-KR")));
  const availability = {open:"다운로드 가능", scheduled:"배포 시작 전", closed:"배포 종료"};
  return <section aria-labelledby="student-materials-heading">
    <div className={s.sectionHeading}><div><h2 id="student-materials-heading">받을 자료</h2><p className={s.muted}>본인에게 배정된 자료와 수업 공통 자료입니다. 수령 코드 없이 바로 받을 수 있습니다.</p></div>{catalog && <span className={s.badge}>{catalog.items.length}개</span>}</div>
    {!catalog ? <div className={s.empty}><h3>자료 목록을 불러오지 못했습니다</h3><p>새로고침해 주세요. 계속되면 교수자에게 서버 업데이트 상태를 확인해 주세요.</p>{onRefresh && <button className={s.secondary} onClick={onRefresh}>자료 목록 새로고침</button>}</div> : <>
      {catalog.items.length > 0 && <div className={w.filters}><label>자료 검색<input type="search" placeholder="자료명 또는 파일명" value={query} onChange={event=>setQuery(event.target.value)} /></label><label>파일 형식<select value={format} onChange={event=>setFormat(event.target.value)}><option value="all">모든 파일</option><option value="pdf">PDF</option><option value="zip">ZIP</option></select></label></div>}
      {error && <div className={s.error} role="alert"><p>{error}</p>{onRefresh && <button className={s.secondary} onClick={onRefresh}>자료 목록 새로고침</button>}</div>}
      {message && <p className={s.notice} role="status">{message}</p>}
      {!rows.length && <div className={s.empty}><h3>{catalog.items.length ? "검색한 자료가 없습니다" : "아직 배정된 자료가 없습니다"}</h3><p>{catalog.items.length ? "자료명이나 파일 형식을 바꿔 보세요." : "교수자가 자료를 배포하면 이곳에 표시됩니다."}</p></div>}
      <div className={w.files}>{rows.map(item=><article className={w.file} key={item.id} aria-label={`${item.title} 자료`}>
        <div className={w.fileIcon} aria-hidden="true">{item.format.toUpperCase()}</div>
        <div className={w.fileBody}><div className={w.rowHeading}><h3>{item.title}</h3><span className={s.badge} data-tone={item.availability === "open" ? "success" : "info"}>{availability[item.availability]}</span></div>
          {item.filename && <p className={w.filename}>{item.filename}</p>}
          <p className={w.meta}>{item.kind === "exam" ? "수업 공통 자료" : "내 배포 자료"} · {item.format.toUpperCase()}{item.size_bytes !== null && ` · ${fileSize(item.size_bytes)}`} · {timeText(item.updated_at)} KST 갱신 · v{item.file_revision}</p>
          <p className={w.meta}>{item.availability === "scheduled" ? `배포 시작 ${timeText(item.opens_at)} KST` : `배포 종료 ${timeText(item.closes_at)} KST`}</p>
          {(item.file_revision > 1 || item.edition > 1) && !item.requested && item.availability === "open" && <p className={w.update}>수정된 자료입니다. 이전 파일 대신 최신 파일을 받아 주세요.</p>}
          {(item.instructions || item.change_note) && <details className={w.fileNote}><summary>배포 안내{item.change_note ? " · 변경 내용" : ""}</summary>{item.instructions && <p>{item.instructions}</p>}{item.change_note && <p><strong>변경 내용</strong><br />{item.change_note}</p>}</details>}
        </div><button className={item.availability === "open" ? s.primary : s.secondary} disabled={item.availability !== "open" || !!busy} aria-label={`${item.title} ${item.format.toUpperCase()} 다운로드`} onClick={()=>void download(item)}>{busy === item.id ? "다운로드 중…" : item.availability === "open" ? "다운로드" : item.availability === "scheduled" ? "배포 예정" : "배포 종료"}</button>
      </article>)}</div>
      {catalog.truncated && <p className={s.warning}>일부 자료만 표시합니다. 필요한 자료가 없으면 교수자에게 확인해 주세요.</p>}
      <p className={w.footnote}>공용 PC에 저장한 자료는 자동으로 지워지지 않습니다. 이용 후 파일을 본인 저장 장치로 옮기고 공용 PC의 사본을 정리하세요.</p>
    </>}
  </section>;
}
