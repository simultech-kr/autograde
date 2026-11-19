type AssignmentIdentity = { assignment_id: string; title: string };

export function assignmentTitle(assignment: Pick<AssignmentIdentity, "title">): string {
  return assignment.title.trim() || "제목 없는 과제";
}

// Use the complete course assignment list, not the current page of students.
// IDs keep equal titles distinct without leaking generated keys into labels.
export function assignmentLabels(assignments: AssignmentIdentity[]): Map<string, string> {
  const groups = new Map<string, Set<string>>();
  for (const assignment of assignments) {
    const title = assignmentTitle(assignment);
    if (!groups.has(title)) groups.set(title, new Set());
    groups.get(title)!.add(assignment.assignment_id);
  }
  const labels = new Map<string, string>();
  for (const [title, ids] of groups) {
    [...ids].sort().forEach((id, index) => labels.set(id,
      title + (ids.size > 1 ? ` · 동명 과제 ${index + 1}` : "")));
  }
  return labels;
}
