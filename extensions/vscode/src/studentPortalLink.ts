export const STUDENT_PORTAL_COMMAND = "autograde.openStudentPortal";

/** Explicit web origin only: never infer a web port from the grading API. */
export function studentPortalUrl(base: string, courseKey?: string): string {
  let parsed: URL;
  try { parsed = new URL(base.trim()); } catch { throw new Error("학생 포털 주소를 확인하세요."); }
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(parsed.hostname.toLowerCase());
  if (parsed.username || parsed.password || parsed.search || parsed.hash || !["", "/"].includes(parsed.pathname)
      || !(parsed.protocol === "https:" || parsed.protocol === "http:" && local)) {
    throw new Error("교수자가 안내한 학생 웹의 HTTPS 주소와 포트만 입력하세요. 경로·인증 정보는 넣지 않습니다.");
  }
  const target = new URL("/app/student", parsed.origin);
  if (courseKey && /^[a-z0-9_-]{1,96}$/.test(courseKey)) target.searchParams.set("course_key", courseKey);
  return target.toString();
}

export function configuredStudentPortalBase(setting?: {globalValue?: string; defaultValue?: string; workspaceValue?: string; workspaceFolderValue?: string}): string {
  return setting?.globalValue ?? setting?.defaultValue ?? "";
}
