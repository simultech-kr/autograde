import { sampleResults } from "../../../tests/fixtures";
import { ResultsTable, SummaryCards } from "../../features/results/ResultsView";
import styles from "../../features/results/dashboard.module.css";

export default function Preview() {
  return <main id="main" className={styles.main}><div className={styles.previewBanner}><strong>화면 시안 · 가상 데이터</strong><p>실제 학생 정보가 아닙니다. 이 페이지는 서버 API에 접속하지 않으며, 저장·제출·인증 기능이 없습니다.</p><a href="/app">실제 교수자 화면으로 이동</a></div>
    <p className={styles.eyebrow}>COME2201 · 소프트웨어 설계 · 01분반</p><h1>제출·채점 현황</h1><p className={styles.description}>최근 제출부터 확인하고, 이번 점수와 이전 최고점을 구분합니다.</p><br />
    <SummaryCards summary={sampleResults.summary} /><p className={styles.summaryNote}>수업 전체 요약 · 가상 학생 4명 · 모든 시각 KST</p>
    <section className={styles.results}><div className={styles.liveBar}><p>시안: 목록이 변경되면 위치를 유지하고 새 결과 반영을 안내합니다.</p></div><ResultsTable rows={sampleResults.rows} /></section>
    <p className={styles.footnote}>화면 폭에 따라 표가 학생별 카드로 전환됩니다. 밝음·어두움·고대비는 시스템 설정을 따릅니다.</p>
  </main>;
}
