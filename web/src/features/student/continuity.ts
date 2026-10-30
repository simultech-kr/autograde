import {remainingSeconds, type Bootstrap, type Claim} from "./model.ts";

/** Only in-memory task context may survive a revalidation of the same account. */
export function canResumePrivate(previousStudent: string | null, next: Bootstrap, selectedCourse: string, now: number): boolean {
  return Boolean(previousStudent && next.authenticated && next.student?.student_key === previousStudent
    && remainingSeconds(next.expires_at, now) > 0
    && (!selectedCourse || next.student.verified && next.courses.some(course => course.course_key === selectedCourse && course.enrolled)));
}

export function usableClaim(claim: Claim | null, assignmentId: string, now: number): boolean {
  return Boolean(claim && claim.assignment_id === assignmentId && remainingSeconds(claim.expires_at, now) > 0);
}

/** A window switch must never abort a submitted mutation or cause its replay. */
export class ResumeBarrier {
  private pending = 0;
  private resumeRequested = false;
  begin() { this.pending++; }
  request(): boolean {
    this.resumeRequested = true;
    if (this.pending) return false;
    this.resumeRequested = false;
    return true;
  }
  finish(): boolean {
    this.pending = Math.max(0, this.pending - 1);
    if (this.pending || !this.resumeRequested) return false;
    this.resumeRequested = false;
    return true;
  }
  get waiting() { return this.pending > 0; }
}

export async function copyHandoffValue(value: string, clipboard?: Pick<Clipboard, "writeText">): Promise<"copied" | "manual"> {
  if (!clipboard) return "manual";
  try { await clipboard.writeText(value); return "copied"; }
  catch { return "manual"; }
}
