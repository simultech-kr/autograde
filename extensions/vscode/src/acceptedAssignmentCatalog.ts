/** History metadata is deliberately not an Assignment and confers no file access. */
export interface AcceptedAssignmentEntry {
  readonly assignmentId: string;
  readonly title: string;
  readonly courseKey: string;
  readonly acceptedAt: string;
  readonly currentSessionAuthorized: boolean;
  readonly canReconnect: boolean;
  readonly canSubmit: boolean;
  readonly status: "available" | "closed" | "unavailable";
}

export function parseAcceptedAssignments(payload: unknown): AcceptedAssignmentEntry[] {
  const fail = (): never => { throw new Error("서버의 수락 이력 응답이 올바르지 않습니다."); };
  if (!payload || typeof payload !== "object" || !("assignments" in payload) || !Array.isArray(payload.assignments)) return fail();
  const seen = new Set<string>();
  return payload.assignments.map((entry: unknown) => {
    if (!entry || typeof entry !== "object") return fail();
    const item = entry as Record<string, unknown>;
    for (const key of ["assignment_id", "title", "course_key", "accepted_at"]) {
      if (typeof item[key] !== "string" || !item[key]) return fail();
    }
    for (const key of ["current_session_authorized", "can_reconnect", "can_submit"]) {
      if (typeof item[key] !== "boolean") return fail();
    }
    if (!["available", "closed", "unavailable"].includes(String(item.status))) return fail();
    const id = item.assignment_id as string;
    if (seen.has(id)) return fail();
    seen.add(id);
    if (item.can_submit && (!item.current_session_authorized || item.status !== "available")) return fail();
    if (item.can_reconnect && item.status !== "available") return fail();
    return { assignmentId: id, title: item.title as string, courseKey: item.course_key as string,
      acceptedAt: item.accepted_at as string, currentSessionAuthorized: item.current_session_authorized as boolean,
      canReconnect: item.can_reconnect as boolean, canSubmit: item.can_submit as boolean,
      status: item.status as AcceptedAssignmentEntry["status"] };
  });
}
