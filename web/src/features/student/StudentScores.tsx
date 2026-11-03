import {scoreText, timeText, type CourseData} from "./model";
import {scoreRows, scoreRowState} from "./scoreboard";
import s from "./student.module.css";

export function StudentScores({data}: {data: CourseData}) {
  const rows = scoreRows(data.overview), published = rows.filter(row => scoreRowState(row).scored).length;
  return <section aria-labelledby="student-score-heading">
    <div className={s.sectionHeading}><h2 id="student-score-heading">과제별 내 점수</h2><p className={s.muted}>조회 중인 과제 {rows.length}개 · 이번 제출 점수 공개 {published}개</p></div>
    <p className={s.guide}>이번 제출의 점수와 이전 최고점을 함께 확인하세요. 채점 중인 과제는 결과가 공개되면 자동으로 갱신됩니다. 자세한 수정 안내는 IDE의 채점 결과에서 볼 수 있어요.</p>
    {!rows.length && <p className={s.empty}>점수를 확인할 공개 과제가 없습니다.</p>}
    <div className={s.grid}>{rows.map(row => {
      const state = scoreRowState(row), receipt = row.receipt;
      return <article className={s.course} key={row.assignment_id} data-score-assignment={row.assignment_id}>
        <span className={s.badge} data-tone={state.tone}>{state.label}</span><p className={s.eyebrow}>{row.assignment_key}</p><h3>{row.title}</h3>
        <dl className={s.scores}><div><dt>이번 제출 점수</dt><dd>{state.scored && receipt ? scoreText(receipt.score, receipt.max_score) : receipt ? "점수 미확정" : "—"}</dd></div>
          <div><dt>이전 제출 중 최고점</dt><dd>{receipt?.previous_best_id ? scoreText(receipt.previous_best_score, receipt.previous_best_max_score) : "—"}</dd><small>{receipt?.previous_best_at ? timeText(receipt.previous_best_at) + " KST" : "공개된 이전 점수 없음"}</small></div></dl>
        <p>{state.hint}</p>{receipt && <><p className={s.muted}>공식 제출 버전 {receipt.version} · 접수 {timeText(receipt.received_at)} KST</p><p className={s.muted}>이전 최고점에는 이번 제출이 포함되지 않습니다.</p>{data.overview.history.some(item => item.submission_id === receipt.submission_id) ? <a className={s.quiet} href={`#history-${receipt.submission_id}`}>이번 제출 이력 보기</a> : <p className={s.muted}>최근 웹 이력의 조회 범위를 넘었습니다. IDE에서 이 제출의 상세 이력을 확인하세요.</p>}</>}
      </article>;
    })}</div>
    {(data.overview.assignments_truncated || data.overview.latest_truncated) && <p className={s.muted}>일부 과제만 표시합니다. 과제별 전체 제출 이력은 IDE에서 확인하세요.</p>}
  </section>;
}
