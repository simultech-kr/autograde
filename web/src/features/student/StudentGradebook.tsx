import {timeText, type Gradebook} from "./model";
import s from "./student.module.css";

export function StudentGradebook({publication}: {publication?: Gradebook | null}) {
  return <section className={s.gradebook} aria-labelledby="instructor-gradebook-heading">
    <div className={s.sectionHeading}><div><p className={s.eyebrow}>COURSE GRADEBOOK</p><h2 id="instructor-gradebook-heading">교수자 공개 성적</h2></div>{publication && <span className={s.badge}>공개본 v{publication.version}</span>}</div>
    <p>중간고사·기말고사·출석 등 교수자가 등록한 결과입니다. 본인에게 공개된 항목만 확인할 수 있습니다.</p>
    {!publication ? <div className={s.empty}><h3>아직 공개된 성적표가 없습니다</h3><p>교수자가 성적을 공개하면 여기에 표시됩니다.</p></div> : <>
      <div className={s.gradebookMeta}><h3>{publication.title}</h3><p className={s.muted}>공개 시각 {timeText(publication.published_at)} KST</p></div>
      <dl className={s.gradebookGrid}>{publication.items.map(item => <div key={item.key} data-gradebook-item={item.key}><dt>{item.label}</dt><dd data-empty={item.value === null}>{item.value === null ? "미입력" : String(item.value)}</dd></div>)}</dl>
      <p className={s.muted}>엑셀에 등록된 값을 그대로 표시합니다. 빈 항목은 0점으로 계산하지 않습니다. 배점·반영 비율과 성적 문의는 담당 교수자에게 확인하세요.</p>
    </>}
  </section>;
}
