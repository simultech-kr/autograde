import { MetricCard, StatusBadge, type StatusTone } from "../../components/InstructorUI";
import type { ResultRow, Summary } from "../../lib/contracts/instructor";
import { formatTime } from "./query";
import styles from "./dashboard.module.css";

export function SummaryCards({ summary }: { summary: Summary }) {
  return (
    <section aria-label="제출 요약" className={styles.metrics}>
      <MetricCard
        label="제출한 학생"
        value={summary.submitted_students}
        unit="명"
        description={`수강생 ${summary.active_students.toLocaleString("ko-KR")}명 중 · 중복 제외`}
      />
      <MetricCard
        label="미제출"
        value={summary.unsubmitted}
        unit="건"
        description="공개 과제 · 학생 × 과제 기준"
      />
      <MetricCard
        label="접수·채점 중"
        value={summary.pending}
        unit="건"
        description="아직 점수가 확정되지 않은 제출"
      />
      <MetricCard
        label="채점 완료"
        value={summary.completed}
        unit="건"
        description="점수 확인 가능 · 만점 미달 포함"
      />
      <MetricCard
        label="확인 필요"
        value={summary.attention}
        unit="건"
        description="오류·만점 미달 등 · 완료와 중복 가능"
      />
    </section>
  );
}

const pendingStates = new Set(["received", "accepted", "queued", "running"]);
const completedStates = new Set(["graded", "published"]);
const failedStates = new Set(["rejected", "infra_failed", "assessment_failed", "failed"]);

// A lifecycle badge and a grade are different facts. In particular, an older
// grade must never make an in-flight or failed submission look successful.
export function resultPresentation(row: ResultRow): { tone: StatusTone; label: string; scored: boolean; note: string } {
  if (!row.submission_id) return { tone: "neutral", label: "미제출", scored: false, note: "제출 없음" };
  if (pendingStates.has(row.state)) return { tone: "info", label: row.status_label, scored: false, note: "점수 미확정" };
  if (failedStates.has(row.state)) return { tone: "error", label: row.status_label, scored: false, note: "오류 · 이력 확인" };
  if (completedStates.has(row.state) && row.score !== null && row.max_score !== null) {
    const partial = row.score < row.max_score;
    return {
      tone: partial || row.attention ? "warning" : "success",
      label: `${row.status_label}${partial ? " · 보완 필요" : row.attention ? " · 확인 필요" : ""}`,
      scored: true,
      note: partial ? "만점 미달 · 평가 항목 확인" : row.attention ? "제출 이력 확인" : "만점",
    };
  }
  return { tone: "warning", label: row.status_label, scored: false, note: "결과 확인 필요" };
}

export function refreshPresentation(state: {
  changed: boolean; paused: boolean; automatic: boolean; failed: boolean; loading: boolean; lastSuccess: string;
}): { tone: StatusTone; label: string } {
  if (state.changed) return { tone: "warning", label: "새 결과 반영 필요" };
  if (state.failed) return { tone: "error", label: state.lastSuccess ? "조회 실패 · 마지막 확인 결과 표시" : "조회 실패 · 결과 확인 필요" };
  if (state.paused) return { tone: "neutral", label: "자동 확인 일시 정지" };
  if (!state.automatic) return { tone: "neutral", label: "수동 확인 모드" };
  if (!state.lastSuccess) return { tone: "info", label: state.loading ? "첫 결과 확인 중" : "서버 응답 확인 대기" };
  return { tone: "info", label: "자동 확인 켜짐" };
}

function Score({ value, max }: { value: number | null; max: number | null }) {
  if (value === null || max === null) return <span className={styles.muted}>—</span>;
  return (
    <span className={styles.scoreValue}>
      <strong>{value.toLocaleString("ko-KR", { maximumFractionDigits: 20 })}</strong>
      <span className={styles.scoreMax}> / {max.toLocaleString("ko-KR", { maximumFractionDigits: 20 })}점</span>
    </span>
  );
}

export function ResultsTable({ rows }: { rows: ResultRow[] }) {
  if (!rows.length) {
    return (
      <div className={styles.empty}>
        <h3>표시할 결과가 없습니다</h3>
        <p>선택한 과제와 상태, 학생 검색 조건을 확인해 주세요.</p>
      </div>
    );
  }

  return (
    <table className={styles.table} role="table">
      <caption className={styles.tableCaption}>
        1행 = 학생 1명 × 과제 1개 · 최신 제출 기준 · 시각은 한국 표준시(KST)
      </caption>
      <thead role="rowgroup">
        <tr role="row">
          <th scope="col" role="columnheader">학생</th>
          <th scope="col" role="columnheader">실습 과제</th>
          <th scope="col" role="columnheader">최신 제출</th>
          <th scope="col" role="columnheader">최신 제출 점수</th>
          <th scope="col" role="columnheader">이전 최고점<span className={styles.secondaryLine}>현재 제출 제외</span></th>
          <th scope="col" role="columnheader">검토</th>
        </tr>
      </thead>
      <tbody role="rowgroup">
        {rows.map(row => {
          const presentation = resultPresentation(row);
          return (
          <tr key={row.row_key} role="row" data-testid="result-row" data-row-key={row.row_key}>
            <th scope="row" role="rowheader" data-label="학생">
              <strong>{row.student_name || row.student_key}</strong>
              {row.student_name && <span className={styles.secondaryLine}>{row.student_key}</span>}
            </th>
            <td role="cell" data-label="실습 과제">
              <strong className={styles.assignmentTitle}>{row.title || row.assignment_key}</strong>
              <span className={styles.secondaryLine}>
                {row.assignment_key}{!row.assignment_ready && " · 비공개"}
              </span>
            </td>
            <td role="cell" data-label="최신 제출">
              <StatusBadge tone={presentation.tone}>
                {presentation.label}
              </StatusBadge>
              {row.received_at && <time className={styles.secondaryLine} dateTime={row.received_at}>{formatTime(row.received_at)}</time>}
              <span className={styles.secondaryLine}>누적 제출 {row.submission_count.toLocaleString("ko-KR")}회</span>
            </td>
            <td role="cell" data-label="최신 제출 점수" data-testid="current-score" className={styles.score}>
              <Score value={presentation.scored ? row.score : null} max={presentation.scored ? row.max_score : null} />
              <span className={styles.secondaryLine}>{presentation.note}</span>
            </td>
            <td role="cell" data-label="이전 최고점 (현재 제출 제외)" data-testid="previous-best" className={styles.previousBest}>
              {row.previous_best ? (
                <>
                  <Score value={row.previous_best.score} max={row.previous_best.max_score} />
                  <time className={styles.secondaryLine} dateTime={row.previous_best.received_at}>{formatTime(row.previous_best.received_at)}</time>
                </>
              ) : <span className={styles.muted}>이전 결과 없음</span>}
            </td>
            <td role="cell" data-label="검토">
              {row.detail_url ? (
                <a className={styles.detailLink} href={row.detail_url}>
                  이력·코드 확인
                  <span className="sr-only">: {row.student_key}, {row.assignment_key}</span>
                  <span aria-hidden="true"> ↗</span>
                </a>
              ) : <span className={styles.muted}>—</span>}
            </td>
          </tr>
          );
        })}
      </tbody>
    </table>
  );
}
