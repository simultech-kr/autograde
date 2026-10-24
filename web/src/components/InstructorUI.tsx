import type { ReactNode } from "react";
import styles from "./instructor-ui.module.css";

export function AppShell({ children, testId }: { children: ReactNode; testId?: string }) {
  return (
    <div className={styles.shell} data-testid={testId}>
      <header className={styles.header}>
        <a className={styles.brand} href="/app">
          <span className={styles.mark} aria-hidden="true">A</span>
          <span>Autograde</span>
          <span className={styles.audience}>교수자</span>
        </a>
        <a className={styles.legacy} href="/app/classroom">실습 교실</a>
        <a className={styles.legacy} href="/instructor">기존 관리 화면 ↗</a>
      </header>
      <main id="main" tabIndex={-1} className={styles.main}>{children}</main>
    </div>
  );
}

export function PageHeading({
  eyebrow,
  title,
  description,
  label,
}: {
  eyebrow: string;
  title: string;
  description: string;
  label?: string;
}) {
  return (
    <div className={styles.heading}>
      <div className={styles.headingCopy}>
        <p className={styles.eyebrow}>{eyebrow}</p>
        <h1>{title}</h1>
        <p className={styles.description}>{description}</p>
      </div>
      {label && <span className={styles.label}>{label}</span>}
    </div>
  );
}

export function MetricCard({
  label,
  value,
  unit,
  description,
}: {
  label: string;
  value: number;
  unit: string;
  description: string;
}) {
  return (
    <div className={styles.metric}>
      <span className={styles.metricLabel}>{label}</span>
      <strong className={styles.metricValue}>
        {value.toLocaleString("ko-KR")}<small>{unit}</small>
      </strong>
      <span className={styles.metricDescription}>{description}</span>
    </div>
  );
}

export type StatusTone = "neutral" | "info" | "success" | "warning" | "error";

export function StatusBadge({
  children,
  tone = "neutral",
}: {
  children: ReactNode;
  tone?: StatusTone;
}) {
  return <span className={`${styles.badge} ${styles[tone]}`} data-tone={tone}>{children}</span>;
}
