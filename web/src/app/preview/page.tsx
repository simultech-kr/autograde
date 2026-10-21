import { sampleResults } from "../../../tests/fixtures";
import { AppShell, PageHeading } from "../../components/InstructorUI";
import { ResultsTable, SummaryCards } from "../../features/results/ResultsView";
import styles from "../../features/results/dashboard.module.css";

export default function Preview() {
  return (
    <AppShell>
      <div className={styles.previewBanner}>
        <strong>화면 시안 · 가상 데이터</strong>
        <p>실제 학생 정보가 아닙니다. 이 페이지는 서버 API에 접속하지 않으며, 저장·제출·인증 기능이 없습니다.</p>
        <a href="/app">실제 교수자 화면으로 이동</a>
      </div>
      <PageHeading
        eyebrow="COME2201 · 소프트웨어 설계 · 01분반"
        title="제출·채점 현황"
        description="최근 제출부터 확인하고, 이번 점수와 이전 최고점을 구분합니다."
        label="화면 시안"
      />
      <SummaryCards summary={sampleResults.summary} />
      <p className={styles.summaryNote}>수업 전체 요약 · 가상 학생 4명 · 모든 시각 KST</p>
      <section className={styles.results} aria-label="학생 결과 시안">
        <div className={styles.sectionHeading}>
          <h2>학생별 제출 결과</h2>
          <p>이번 점수와 이전 최고점을 구분해 확인하세요.</p>
        </div>
        <div className={styles.previewNotice}>
          시안: 목록이 변경되면 위치를 유지하고 새 결과 반영을 안내합니다.
        </div>
        <ResultsTable rows={sampleResults.rows} />
      </section>
      <p className={styles.footnote}>
        화면 폭에 따라 표가 학생별 카드로 전환됩니다. 밝음·어두움·고대비는 시스템 설정을 따릅니다.
      </p>
    </AppShell>
  );
}
