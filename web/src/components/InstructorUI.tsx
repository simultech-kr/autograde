"use client";

import { useEffect, useRef, type ReactNode } from "react";
import type { Course, NavigationGroup } from "../lib/contracts/instructor";
import { courseNavigationGroups, type InstructorSection } from "../features/navigation";
import styles from "./instructor-ui.module.css";

export function AppShell({ children, testId, course, section, resultsEnabled = false, portalNavigation }: {
  children: ReactNode; testId?: string; course?: Course;
  section?: InstructorSection; resultsEnabled?: boolean;
  portalNavigation?: NavigationGroup[];
}) {
  const courseGroups = courseNavigationGroups(course, section, resultsEnabled);
  const links = courseGroups.flatMap(group => group.items);
  const globalGroups = (portalNavigation || []).map(group => ({ ...group, items: group.items.map(item => ({ ...item, current: false })) }));
  const groups = [...globalGroups.filter(group => group.label === "포털"), ...courseGroups, ...globalGroups.filter(group => group.label !== "포털")];
  const menu = useRef<HTMLDetailsElement>(null);
  useEffect(() => {
    const desktop = window.matchMedia("(min-width: 1101px)");
    const adjust = () => { if (menu.current) menu.current.open = desktop.matches; };
    adjust(); desktop.addEventListener("change", adjust);
    return () => desktop.removeEventListener("change", adjust);
  }, [Boolean(groups.length)]);
  const current = links.find(link => link.current);
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
          <a href="/student">학생 포털</a>
        </nav>
      </header>
      <div className={styles.workspace} data-navigation-visible={groups.length > 0}>
      {groups.length > 0 && <aside className={styles.courseBar} data-testid={course ? "course-navigation" : "portal-navigation"}>
        {course && <p className={styles.courseContext}>
          <span>선택한 수업</span>
          <strong>{course.name || course.code || course.course_key}{course.section ? ` · ${course.section}분반` : ""}</strong>
          <span className={styles.courseCode}>{course.course_key}</span>
        </p>}
        <details className={styles.courseMenu} ref={menu} open>
        <summary><span>{course ? "수업 메뉴" : "교수자 메뉴"}</span><strong>{current?.label || "교수자 포털"}</strong><span aria-hidden="true">⌄</span></summary>
        <nav className={styles.courseNav} aria-label={course ? "수업 메뉴" : "교수자 메뉴"}>
          {groups.map(group => <div className={styles.navGroup} key={group.label}>
            <p className={styles.navLabel}>{group.label}</p>
            {group.items.map(link =>
              <a key={link.id} href={link.href} aria-current={link.current ? "page" : undefined}><NavigationIcon name={link.id}/><span>{link.label}</span></a>)}
          </div>)}
        </nav>
        </details>
      </aside>}
      <main id="main" tabIndex={-1} className={styles.main}>
        <a className={styles.back} data-parent-link href={course ? `/courses/${course.course_key}/instructor` : "/instructor"}>
          <span aria-hidden="true">←</span> {course ? "수업 개요" : "교수자 포털"}로 돌아가기
        </a>
        <nav className={styles.breadcrumb} aria-label="현재 위치"><a href="/instructor">교수자 포털</a>
          {course && <><span aria-hidden="true">/</span><a href={`/courses/${course.course_key}/instructor`}>{course.name || course.code}</a></>}
          {current && <><span aria-hidden="true">/</span><span aria-current="page">{current.label}</span></>}
        </nav>
        {children}
      </main>
      </div>
    </div>
  );
}

function NavigationIcon({name}: {name: string}) {
  const paths: Record<string, string> = {
    home: "M3 10 12 3l9 7M5 9v12h5v-7h4v7h5V9",
    classroom: "M3 3h7v7H3zM14 3h7v7h-7zM3 14h7v7H3zM14 14h7v7h-7z",
    students: "M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2M9 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8M17 4a4 4 0 0 1 0 7M22 21v-2a4 4 0 0 0-3-4",
    registrations: "M12 3 3 7v5c0 5 9 9 9 9s9-4 9-9V7zM8 12l3 3 5-6",
    environments: "M3 4h18v14H3zM8 22h8M12 18v4M6 8l3 3-3 3M12 14h5",
    assignments: "M14 2H5v20h14V7zM14 2v6h5M8 12h8M8 16h8",
    gradebook: "M4 3h16v18H4zM4 9h16M10 3v18M4 15h16",
    results: "M4 3v18h18M8 17v-5M13 17V7M18 17v-8",
    rubrics: "M4 3h16v18H4zM8 8l2 2 4-4M8 15h8",
    content: "M3 6h7l2 3h9v11H3z",
    materials: "M3 6h7l2 3h9v11H3z",
    "exam-papers": "M14 2H5v20h14V7zM14 2v6h5M8 12h8M8 16h8",
    extensions: "M12 2 3 7v10l9 5 9-5V7zM3 7l9 5 9-5M12 12v10",
    account: "M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8M4 21v-2a8 8 0 0 1 16 0v2",
    operations: "M3 4h18v14H3zM8 22h8M12 18v4",
    settings: "M4 7h16M4 17h16M8 4v6M16 14v6",
  };
  return <svg className={styles.navIcon} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false"><path d={paths[name] || paths.home}/></svg>;
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
