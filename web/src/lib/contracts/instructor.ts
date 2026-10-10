export type Course = { course_key: string; name: string; code: string; section: string; status: string };
export type Metadata = { contract_version: "1"; request_id: string; server_time: string };
export type Bootstrap = Metadata & {
  principal: { id: string; display_name: string; mode: "shared" | "personal" };
  courses: Course[];
  csrf: string;
  capabilities: string[];
};
export type PreviousBest = { submission_id: string; received_at: string; score: number; max_score: number };
export type ResultRow = {
  row_key: string; student_key: string; student_name: string;
  assignment_id: string; assignment_key: string; release_id: string; title: string;
  assignment_ready: boolean; submission_id: string | null; received_at: string | null;
  state: string; status_label: string; score: number | null; max_score: number | null;
  previous_best: PreviousBest | null; submission_count: number;
  detail_url: string | null; attention: boolean;
};
export type Assignment = {
  assignment_id: string; assignment_key: string; release_id: string; title: string;
  ready: boolean; students: number; submitted: number; unsubmitted: number | null;
};
export type Summary = {
  scope: "course" | "assignment"; active_students: number; submitted_students: number;
  submitted: number; unsubmitted: number; pending: number; completed: number;
  attention: number; submission_attempts: number;
};
export type Results = Metadata & {
  course_key: string; version: string; page: number; page_size: number; total: number;
  summary: Summary; assignments: Assignment[]; rows: ResultRow[];
};

export class ContractError extends Error {
  constructor() { super("서버 응답 형식이 일치하지 않습니다. 서버와 웹의 배포 버전을 확인해 주세요."); }
}
type ObjectValue = Record<string, unknown>;
function object(value: unknown): ObjectValue {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ContractError();
  return value as ObjectValue;
}
function str(value: unknown): string { if (typeof value !== "string") throw new ContractError(); return value; }
function bool(value: unknown): boolean { if (typeof value !== "boolean") throw new ContractError(); return value; }
function number(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new ContractError();
  return value;
}
function count(value: unknown): number { const n = number(value); if (!Number.isSafeInteger(n)) throw new ContractError(); return n; }
function nullable<T>(value: unknown, parse: (v: unknown) => T): T | null { return value === null ? null : parse(value); }
function array<T>(value: unknown, parse: (v: unknown) => T): T[] {
  if (!Array.isArray(value)) throw new ContractError(); return value.map(parse);
}
function timestamp(value: unknown): string {
  const s = str(value); if (!/^\d{4}-\d\d-\d\dT/.test(s) || !Number.isFinite(Date.parse(s))) throw new ContractError(); return s;
}
export function courseKey(value: unknown): string {
  const s = str(value); if (!/^[a-z0-9_-]+$/.test(s)) throw new ContractError(); return s;
}
function metadata(value: ObjectValue): Metadata {
  if (value.contract_version !== "1") throw new ContractError();
  return { contract_version: "1", request_id: str(value.request_id), server_time: timestamp(value.server_time) };
}
function course(value: unknown): Course {
  const c = object(value);
  return { course_key: courseKey(c.course_key), name: str(c.name), code: str(c.code), section: str(c.section), status: str(c.status) };
}
export function parseBootstrap(value: unknown): Bootstrap {
  const v = object(value); const p = object(v.principal);
  if (p.mode !== "shared" && p.mode !== "personal") throw new ContractError();
  const courses = array(v.courses, course);
  if (new Set(courses.map(c => c.course_key)).size !== courses.length) throw new ContractError();
  return { ...metadata(v), principal: { id: str(p.id), display_name: str(p.display_name), mode: p.mode },
    csrf: str(v.csrf), courses, capabilities: array(v.capabilities, str) };
}
function previousBest(value: unknown): PreviousBest {
  const v = object(value);
  const score = number(v.score); const maxScore = number(v.max_score);
  if (maxScore <= 0 || score > maxScore) throw new ContractError();
  return { submission_id: str(v.submission_id), received_at: timestamp(v.received_at), score, max_score: maxScore };
}
function detailURL(value: unknown, key: string): string | null {
  if (value === null) return null;
  const s = str(value);
  // Only navigate to this course's existing instructor pages, never a remote URL.
  if (!s.startsWith(`/courses/${key}/instructor/`) || /[\\\r\n]/.test(s) || /%2f|%5c|%2e/i.test(s)) throw new ContractError();
  if (s.split("?")[0].split("/").some(segment => segment === ".." || segment === ".")) throw new ContractError();
  return s;
}
function row(value: unknown, key: string): ResultRow {
  const r = object(value);
  const score = nullable(r.score, number); const maxScore = nullable(r.max_score, number);
  if ((score === null) !== (maxScore === null)) throw new ContractError();
  if (score !== null && maxScore !== null && (maxScore <= 0 || score > maxScore)) throw new ContractError();
  return {
    row_key: str(r.row_key), student_key: str(r.student_key), student_name: str(r.student_name),
    assignment_id: str(r.assignment_id), assignment_key: str(r.assignment_key), release_id: str(r.release_id), title: str(r.title),
    assignment_ready: bool(r.assignment_ready), submission_id: nullable(r.submission_id, str), received_at: nullable(r.received_at, timestamp),
    state: str(r.state), status_label: str(r.status_label), score, max_score: maxScore,
    previous_best: nullable(r.previous_best, previousBest), submission_count: count(r.submission_count),
    detail_url: detailURL(r.detail_url, key), attention: bool(r.attention),
  };
}
function assignment(value: unknown): Assignment {
  const a = object(value);
  return { assignment_id: str(a.assignment_id), assignment_key: str(a.assignment_key), release_id: str(a.release_id),
    title: str(a.title), ready: bool(a.ready), students: count(a.students), submitted: count(a.submitted), unsubmitted: nullable(a.unsubmitted, count) };
}
export function parseResults(value: unknown): Results {
  const v = object(value); const key = courseKey(v.course_key); const s = object(v.summary);
  if (s.scope !== "course" && s.scope !== "assignment") throw new ContractError();
  const version = str(v.version); if (!/^[a-f0-9]{64}$/.test(version)) throw new ContractError();
  const page = count(v.page); const pageSize = count(v.page_size);
  if (page < 1 || pageSize < 1 || pageSize > 100) throw new ContractError();
  const rows = array(v.rows, r => row(r, key));
  if (rows.length > pageSize || new Set(rows.map(r => r.row_key)).size !== rows.length) throw new ContractError();
  return { ...metadata(v), course_key: key, version, page, page_size: pageSize, total: count(v.total),
    summary: { scope: s.scope, active_students: count(s.active_students), submitted_students: count(s.submitted_students),
      submitted: count(s.submitted), unsubmitted: count(s.unsubmitted), pending: count(s.pending), completed: count(s.completed),
      attention: count(s.attention), submission_attempts: count(s.submission_attempts) },
    assignments: array(v.assignments, assignment), rows };
}
