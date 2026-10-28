export type Course = {
  course_key: string; code: string; name: string; year?: number | string; semester?: number | string;
  section: string; term?: string; status: "available" | "pending" | "matched" | "suspended";
  request_status: "pending" | "matched" | null; enrolled: boolean; eligible: boolean; can_withdraw: boolean;
};
export type Bootstrap = {
  authenticated: boolean; csrf: string; expires_at: string | null; server_time?: string;
  student: { student_key: string; verified: boolean } | null; courses: Course[]; api_base_url: string; message?: string;
};
export type Assignment = { assignment_id: string; assignment_key: string; title: string; opens_at: string | null; due_at: string | null; accepted_at: string | null };
export type Receipt = {
  submission_id: string; assignment_id: string; title: string; assignment_key: string; received_at: string;
  state: string; code_version: string; score: number | null; max_score: number | null; published_at: string | null; version: number;
  previous_best_id?: string | null; previous_best_score?: number | null; previous_best_max_score?: number | null; previous_best_at?: string | null;
};
export type CourseData = {
  student_key: string;
  course: Pick<Course, "course_key" | "name" | "code" | "section" | "year" | "semester" | "term">;
  overview: { assignments: Assignment[]; latest: Receipt[]; history: Receipt[]; history_truncated: boolean; latest_truncated: boolean; assignments_truncated: boolean; limit: number };
  links: { assignments: string; materials?: string; exam_papers?: string }; csrf: string; server_time: string; api_base_url: string;
};
export type Claim = { code: string; expires_at: string; api_url: string; assignment_id: string; title: string };
export class StudentContractError extends Error {
  constructor() { super("서버 응답을 확인할 수 없습니다. 화면을 다시 열고, 반복되면 교수자에게 문의하세요."); }
}
const object = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new StudentContractError();
  return value as Record<string, unknown>;
};
const str = (value: unknown, maximum = 1000): string => {
  if (typeof value !== "string" || value.length > maximum || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)) throw new StudentContractError();
  return value;
};
const bool = (value: unknown): boolean => { if (typeof value !== "boolean") throw new StudentContractError(); return value; };
const nullableString = (value: unknown) => value === null || value === undefined ? null : str(value);
const numeric = (value: unknown): number | null => {
  if (value === null || value === undefined) return null;
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new StudentContractError();
  return value;
};
const list = (value: unknown): unknown[] => { if (!Array.isArray(value) || value.length > 10000) throw new StudentContractError(); return value; };
const date = (value: unknown): string => { const result = str(value, 80); if (!Number.isFinite(Date.parse(result))) throw new StudentContractError(); return result; };
const dateOrNull = (value: unknown) => value === null || value === undefined ? null : date(value);
export function courseKey(value: unknown): string {
  const key = str(value, 96);
  if (!/^[a-z0-9][a-z0-9_-]*$/.test(key)) throw new StudentContractError();
  return key;
}
export function apiAddress(value: unknown): string {
  const raw = str(value, 512); let url: URL;
  try { url = new URL(raw); } catch { throw new StudentContractError(); }
  if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash || url.pathname !== "/") throw new StudentContractError();
  return url.origin;
}
export function coursePath(key: string): string { return `/student/api/course/${courseKey(key)}`; }
export function courseLink(key: string, value: unknown, suffix: string): string {
  const path = str(value, 256);
  if (path !== `/courses/${courseKey(key)}/${suffix}`) throw new StudentContractError();
  return path;
}
export function unwrap(value: unknown): unknown {
  const envelope = object(value);
  if (envelope.ok !== true || !Object.hasOwn(envelope, "result")) throw new StudentContractError();
  return envelope.result;
}
export function parseBootstrap(value: unknown): Bootstrap {
  const body = object(unwrap(value)); const authenticated = bool(body.authenticated);
  const rawStudent = body.student === null ? null : object(body.student);
  const student = rawStudent ? { student_key: str(rawStudent.student_key, 20), verified: bool(rawStudent.verified) } : null;
  if (student && !/^\d{4,20}$/.test(student.student_key) || authenticated !== Boolean(student)) throw new StudentContractError();
  const expires_at = dateOrNull(body.expires_at);
  if (authenticated && !expires_at || !authenticated && expires_at) throw new StudentContractError();
  const seen = new Set<string>();
  const courses = list(body.courses).map(value => {
    const item = object(value), key = courseKey(item.course_key), status = str(item.status);
    if (seen.has(key) || !["available", "pending", "matched", "suspended"].includes(status)) throw new StudentContractError();
    seen.add(key);
    const request_status = nullableString(item.request_status);
    if (request_status !== null && !["pending", "matched"].includes(request_status)) throw new StudentContractError();
    return { course_key: key, name: str(item.name ?? "", 255), code: str(item.code ?? key, 96), section: str(item.section ?? "", 100),
      term: str(item.term ?? "", 100), year: scalar(item.year), semester: scalar(item.semester), status: status as Course["status"],
      request_status: request_status as Course["request_status"], enrolled: bool(item.enrolled), eligible: bool(item.eligible), can_withdraw: bool(item.can_withdraw) };
  });
  return { authenticated, csrf: str(body.csrf, 256), expires_at, server_time: body.server_time === undefined ? undefined : date(body.server_time), student, courses, api_base_url: apiAddress(body.api_base_url),
    message: body.message === undefined ? undefined : str(body.message, 1000) };
}
function scalar(value: unknown): number | string | undefined {
  if (value === null || value === undefined) return undefined;
  if (typeof value === "number" && Number.isFinite(value)) return value;
  return str(value, 100);
}
function identifier(value: unknown): string { const result = str(value, 256); if (!/^[A-Za-z0-9_-]+$/.test(result)) throw new StudentContractError(); return result; }
function receipt(value: unknown): Receipt {
  const item = object(value), version = numeric(item.version);
  if (version === null || !Number.isInteger(version) || version < 1) throw new StudentContractError();
  return { submission_id: identifier(item.submission_id), assignment_id: identifier(item.assignment_id), title: str(item.title), assignment_key: str(item.assignment_key),
    received_at: date(item.received_at), state: str(item.state, 80), code_version: str(item.code_version, 128), score: numeric(item.score), max_score: numeric(item.max_score),
    published_at: dateOrNull(item.published_at), version, previous_best_id: nullableString(item.previous_best_id), previous_best_score: numeric(item.previous_best_score),
    previous_best_max_score: numeric(item.previous_best_max_score), previous_best_at: dateOrNull(item.previous_best_at) };
}
export function parseCourse(value: unknown, expected: string, expectedStudent: string): CourseData {
  const body = object(unwrap(value)), raw = object(body.course), key = courseKey(raw.course_key);
  if (key !== courseKey(expected) || body.student_key !== expectedStudent) throw new StudentContractError();
  const overview = object(body.overview), links = object(body.links);
  return { student_key: str(body.student_key, 20), course: { course_key: key, name: str(raw.name ?? ""), code: str(raw.code ?? key), section: str(raw.section ?? ""), year: scalar(raw.year), semester: scalar(raw.semester), term: str(raw.term ?? "") },
    overview: { assignments: list(overview.assignments).map(value => { const item = object(value); return { assignment_id: identifier(item.assignment_id), assignment_key: str(item.assignment_key), title: str(item.title), opens_at: dateOrNull(item.opens_at), due_at: dateOrNull(item.due_at), accepted_at: dateOrNull(item.accepted_at) }; }),
      latest: list(overview.latest).map(receipt), history: list(overview.history).map(receipt), history_truncated: bool(overview.history_truncated), latest_truncated: bool(overview.latest_truncated), assignments_truncated: bool(overview.assignments_truncated), limit: numeric(overview.limit) ?? 100 },
    links: { assignments: courseLink(key, links.assignments, "assignments"), ...(links.materials ? { materials: courseLink(key, links.materials, "materials") } : {}), ...(links.exam_papers ? { exam_papers: courseLink(key, links.exam_papers, "exam-papers") } : {}) },
    csrf: str(body.csrf, 256), server_time: date(body.server_time), api_base_url: apiAddress(body.api_base_url) };
}
export function parseClaim(value: unknown, expectedAssignment: string, expectedStudent: string): Claim {
  const body = object(unwrap(value)), item = object(body.claim), assignment_id = identifier(item.assignment_id);
  if (assignment_id !== expectedAssignment || body.student_key !== expectedStudent) throw new StudentContractError();
  return { code: str(item.code, 128), expires_at: date(item.expires_at), api_url: apiAddress(item.api_url), assignment_id, title: str(item.title) };
}
export const stages = ["제출 접수", "채점 대기", "채점 중", "결과 공개"];
export function submissionState(row?: Pick<Receipt, "state" | "score" | "max_score" | "published_at">) {
  const unscored = { scored: false, tone: "info", stage: -1 };
  if (!row) return { ...unscored, label: "공식 제출 전", hint: "IDE에서 실습을 작성하고 공식 제출하세요." };
  const failed: Record<string, string> = { rejected: "제출 확인 필요", infra_failed: "서버 처리 오류", assessment_failed: "채점 설정 확인 필요", failed: "처리 오류" };
  if (failed[row.state]) return { ...unscored, tone: "error", label: failed[row.state], hint: "접수번호와 함께 교수자에게 확인을 요청하세요. 이전 점수는 이번 제출의 결과가 아닙니다." };
  const stage = ["received", "accepted"].includes(row.state) ? 0 : row.state === "queued" ? 1 : row.state === "running" ? 2 : -1;
  if (stage >= 0) return { ...unscored, stage, label: stages[stage], hint: "현재 제출의 채점이 끝나면 점수가 표시됩니다. 다시 제출할 필요 없이 결과를 확인하세요." };
  if (row.state === "published" && row.published_at && validScore(row.score, row.max_score)) return { scored: true, tone: row.score! < row.max_score! ? "warning" : "success", stage: 3,
    label: "결과 공개", hint: "IDE의 채점 결과에서 평가 항목과 수정 안내를 확인하세요." };
  return { ...unscored, label: row.state === "graded" ? "결과 공개 대기" : "결과 확인 대기", hint: "공개된 채점 결과를 기다리고 있습니다. 점수가 확정되기 전에는 이전 결과로 대체하지 않습니다." };
}
export function validScore(score: number | null | undefined, maximum: number | null | undefined): boolean {
  return typeof score === "number" && Number.isFinite(score) && typeof maximum === "number" && Number.isFinite(maximum) && score >= 0 && maximum > 0 && score <= maximum;
}
export function scoreText(score: number | null | undefined, maximum: number | null | undefined): string {
  return validScore(score, maximum) ? `${score!.toLocaleString("ko-KR", { maximumFractionDigits: 20 })} / ${maximum!.toLocaleString("ko-KR", { maximumFractionDigits: 20 })}점` : "—";
}
export function timeText(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "—";
  return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}
export function courseMeta(course: Partial<Course>): string {
  return [course.term || [course.year && `${course.year}학년도`, course.semester && `${course.semester}학기`].filter(Boolean).join(" "), course.section && `${course.section}분반`].filter(Boolean).join(" · ") || "학기·분반 정보 미등록";
}
export function remainingSeconds(expires: string | null, now: number): number { return expires ? Math.max(0, Math.ceil((Date.parse(expires) - now) / 1000)) : 0; }
export class RequestVersion { private version = 0; next() { return ++this.version; } current(version: number) { return this.version === version; } }
