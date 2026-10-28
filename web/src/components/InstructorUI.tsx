import type { ReactNode } from "react";
import type { Course } from "../lib/contracts/instructor";
import { courseNavigation, type InstructorSection } from "../features/navigation";
import styles from "./instructor-ui.module.css";

export function AppShell({ children, testId, course, section, resultsEnabled = false }: {
  children: ReactNode; testId?: string; course?: Course;
  section?: InstructorSection; resultsEnabled?: boolean;
}) {
  const links = courseNavigation(course, section, resultsEnabled);
  return (
    <div className={styles.shell} data-testid={testId} data-portal="instructor">
      <header className={styles.header}>
        <a className={styles.brand} href="/instructor">
          <span className={styles.mark} aria-hidden="true">A</span>
          <span>Autograde</span>
          <span className={styles.audience}>교수자 포털</span>
        </a>
        <nav className={styles.portalNav} aria-label="포털 이동">
          <a href="/">포털 홈</a>
          <a href="/app/student">학생 포털</a>
        </nav>
      </header>
      {links.length > 0 && course && <div className={styles.courseBar} data-testid="course-navigation">
        <p className={styles.courseContext}>
          <span>선택한 수업</span>
          <strong>{course.name || course.code || course.course_key}{course.section ? ` · ${course.section}분반` : ""}</strong>
          <span className={styles.courseCode}>{course.course_key}</span>
        </p>
        <nav className={styles.courseNav} aria-label="수업 메뉴">
          {links.map(link => <a key={link.id} href={link.href} aria-current={link.current ? "page" : undefined}>{link.label}</a>)}
        </nav>
      </div>}
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
