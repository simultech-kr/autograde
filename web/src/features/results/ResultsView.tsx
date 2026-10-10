import type { ResultRow, Summary } from "../../lib/contracts/instructor";
import { formatTime } from "./query";
import styles from "./dashboard.module.css";

export function SummaryCards({ summary }: { summary: Summary }) {
  return <section aria-label="제출 요약" className={styles.metrics}>
    <div className={styles.metric}><span>제출한 학생</span><strong>{summary.submitted_students}<small> / {summary.active_students}명</small></strong><span>중복 제출은 한 명으로 계산</span></div>
    <div className={styles.metric}><span>채점 완료</span><strong>{summary.completed}<small>건</small></strong><span>학생 × 과제의 최신 제출 기준</span></div>
    <div className={styles.metric}><span>미제출</span><strong>{summary.unsubmitted}<small>건</small></strong><span>공개 과제의 학생 × 과제 기준</span></div>
    <div className={styles.metric}><span>확인 필요</span><strong>{summary.attention}<small>건</small></strong><span>접수·채점 대기 {summary.pending}건</span></div>
  </section>;
}
function Score({ value, max }: { value: number | null; max: number | null }) {
  return value !== null && max !== null ? <><strong>{value}</strong><span className={styles.scoreMax}> / {max}</span></> : <span className={styles.muted}>—</span>;
}
export function ResultsTable({ rows }: { rows: ResultRow[] }) {
  if (!rows.length) return <div className={styles.empty}><h3>표시할 결과가 없습니다</h3><p>선택한 과제와 상태, 학생 검색 조건을 확인해 주세요.</p></div>;
  return <table className={styles.table}>
    <caption className="sr-only">학생별 최신 제출과 이전 제출 최고점. 모든 시간은 한국 표준시입니다.</caption>
    <thead><tr><th scope="col">학생</th><th scope="col">실습 과제</th><th scope="col">최신 제출</th><th scope="col">이번 점수</th><th scope="col">이전 최고점</th><th scope="col">검토</th></tr></thead>
    <tbody>{rows.map(row => <tr key={row.row_key} data-testid="result-row" data-row-key={row.row_key}>
      <td data-label="학생"><strong>{row.student_name || row.student_key}</strong>{row.student_name && <span className={styles.secondaryLine}>{row.student_key}</span>}</td>
      <td data-label="실습 과제"><strong className={styles.assignmentTitle}>{row.title || row.assignment_key}</strong><span className={styles.secondaryLine}>{row.assignment_key}{!row.assignment_ready && " · 비공개"}</span></td>
      <td data-label="최신 제출"><span className={`${styles.badge} ${row.attention ? styles.attention : row.score !== null ? styles.completed : styles.neutral}`}>{row.status_label}</span><span className={styles.secondaryLine}>{formatTime(row.received_at)}</span><span className={styles.secondaryLine}>제출 {row.submission_count}회</span></td>
      <td data-label="이번 점수" data-testid="current-score" className={styles.score}><Score value={row.score} max={row.max_score} />{row.score === null && <span className={styles.secondaryLine}>{row.submission_id ? "점수 미확정" : "제출 없음"}</span>}</td>
      <td data-label="이전 최고점" data-testid="previous-best" className={styles.score}>{row.previous_best ? <><Score value={row.previous_best.score} max={row.previous_best.max_score} /><span className={styles.secondaryLine}>현재 제출 제외</span></> : <span className={styles.muted}>이전 결과 없음</span>}</td>
      <td data-label="검토">{row.detail_url ? <a className={styles.detailLink} href={row.detail_url}>제출 이력<span className="sr-only">: {row.student_key}, {row.assignment_key}</span> ↗</a> : <span className={styles.muted}>—</span>}</td>
    </tr>)}</tbody>
  </table>;
}
