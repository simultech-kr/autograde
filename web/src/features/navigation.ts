import type { Course } from "../lib/contracts/instructor.ts";

export type InstructorSection = "results" | "classroom";
export type NavigationItem = { id: string; label: string; href: string; current: boolean };
export type NavigationSection = { label: string; items: NavigationItem[] };

function validCourseKey(value: string): boolean {
  return value.length > 0 && value.length <= 96 && !/[^a-z0-9_-]/.test(value);
}

// URL parameters alone never select a course in the portal shell. Only a
// course in the current authenticated bootstrap may supply navigation context.
export function selectedCourse(courses: readonly Course[] | undefined, key: string): Course | undefined {
  return validCourseKey(key) ? courses?.find(course => course.course_key === key) : undefined;
}

export function courseNavigation(
  course: Course | undefined,
  section: InstructorSection | undefined,
  resultsEnabled = false,
): NavigationItem[] {
  if (!course || !validCourseKey(course.course_key)) return [];
  const base = `/courses/${course.course_key}/instructor`;
  const query = `?${new URLSearchParams({ course_key: course.course_key })}`;
  if (course.navigation) return course.navigation.flatMap(group => group.items.map(item => ({
    ...item, current: item.id === section,
    href: item.id === "results" && resultsEnabled ? `/app${query}` : item.id === "classroom" ? `/app/classroom${query}` : item.href,
  })));
  return [
    { id: "home", label: "수업 개요", href: base, current: false },
    { id: "assignments", label: "과제 관리", href: `${base}/assignments`, current: false },
    { id: "students", label: "수강생 관리", href: `${base}/students`, current: false },
    { id: "registrations", label: "명단·본인 확인", href: `${base}/registrations`, current: false },
    { id: "environments", label: "실습 환경", href: `${base}/environments`, current: false },
    // Classroom may be enabled while Next results are disabled. The Python
    // results page remains the authorized fallback in that configuration.
    { id: "results", label: "제출·채점 현황", href: resultsEnabled ? `/app${query}` : `${base}/submissions`, current: section === "results" },
    { id: "gradebook", label: "성적표 관리", href: `${base}/gradebook`, current: false },
    { id: "classroom", label: "실습 교실", href: `/app/classroom${query}`, current: section === "classroom" },
    { id: "settings", label: "수업 설정", href: `${base}/settings`, current: false },
  ];
}

export function courseNavigationGroups(course: Course | undefined, section: InstructorSection | undefined, resultsEnabled = false): NavigationSection[] {
  const links = courseNavigation(course, section, resultsEnabled);
  if (course?.navigation) return course.navigation.map(group => ({ label: group.label,
    items: group.items.map(item => links.find(link => link.id === item.id)).filter((item): item is NavigationItem => !!item),
  })).filter(group => group.items.length > 0);
  // Compatibility with a server awaiting deployment. Optional modules are not
  // guessed; the authenticated navigation contract is authoritative when present.
  return [
    { label: "수업", ids: ["home", "classroom"] },
    { label: "과제·평가", ids: ["assignments", "results", "gradebook"] },
    { label: "수강생", ids: ["students", "registrations"] },
    { label: "설정", ids: ["environments", "settings"] },
  ].map(group => ({ label: group.label, items: group.ids.map(id => links.find(link => link.id === id)).filter((item): item is NavigationItem => !!item) }))
    .filter(group => group.items.length > 0);
}
