type CourseContext = {
  name: string; code: string; course_key: string; section?: string;
  term?: string; year?: string | number | null; semester?: string | number | null;
};

export function sectionLabel(value?: string): string {
  const section = value?.trim();
  return section && !section.endsWith("분반") ? section + "분반" : section || "";
}

export function courseTerm(course: Pick<CourseContext, "term" | "year" | "semester">): string {
  const semester = course.semester === "summer" ? "여름학기" : course.semester === "winter" ? "겨울학기" : course.semester && `${course.semester}학기`;
  return course.term || [course.year && `${course.year}학년도`, semester].filter(Boolean).join(" ");
}

export function courseLocationLabel(course: CourseContext): string {
  const name = course.name || course.code || course.course_key;
  return [name, courseTerm(course), sectionLabel(course.section), name !== course.code ? course.code : ""].filter(Boolean).join(" · ");
}

// Older rows can share all display fields, including a missing term. Only those
// students/instructors with two such choices need the unique offering key.
export function courseLocationLabels(courses: readonly CourseContext[]): Map<string, string> {
  const labels = new Map<string, string>(), seen = new Set<string>(), duplicates = new Set<string>();
  for (const course of courses) {
    const label = courseLocationLabel(course);
    if (seen.has(label)) duplicates.add(label);
    seen.add(label);
    labels.set(course.course_key, label);
  }
  for (const [key, label] of labels) {
    if (duplicates.has(label)) labels.set(key, label + " · " + key);
  }
  return labels;
}
