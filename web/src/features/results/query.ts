export const statuses = ["all", "submitted", "unsubmitted", "pending", "completed", "attention"] as const;
export type Status = typeof statuses[number];
export const sorts = ["recent", "student", "assignment"] as const;
export type Sort = typeof sorts[number];
export type ResultsQuery = { assignment: string; student: string; status: Status; sort: Sort; page: number };
export const defaultQuery: ResultsQuery = { assignment: "", student: "", status: "all", sort: "recent", page: 1 };
export const statusNames: Record<Status, string> = {
  all: "전체 상태", submitted: "제출함", unsubmitted: "미제출", pending: "접수·채점 대기", completed: "채점 완료", attention: "확인 필요",
};
export function parseQuery(params: URLSearchParams): ResultsQuery {
  const status = params.get("status"); const sort = params.get("sort");
  const page = Number(params.get("page") || "1");
  return { assignment: (params.get("assignment_id") || "").slice(0, 200), student: (params.get("student") || "").slice(0, 100),
    status: statuses.includes(status as Status) ? status as Status : "all",
    sort: sorts.includes(sort as Sort) ? sort as Sort : "recent",
    page: Number.isSafeInteger(page) && page > 0 && page <= 1000000 ? page : 1 };
}
export function pageURL(course: string, q: ResultsQuery) {
  const p = new URLSearchParams(); if (course) p.set("course_key", course);
  if (q.assignment) p.set("assignment_id", q.assignment);
  if (q.student) p.set("student", q.student);
  if (q.status !== "all") p.set("status", q.status);
  if (q.sort !== "recent") p.set("sort", q.sort);
  if (q.page > 1) p.set("page", String(q.page));
  return `/app${p.size ? `?${p}` : ""}`;
}
export function filterKey(q: ResultsQuery) { return JSON.stringify([q.assignment, q.student, q.status, q.sort]); }
export function pollDelay(failures: number, random = Math.random()) {
  return Math.min(60000, 5000 * 2 ** Math.min(failures, 4)) + Math.floor(Math.max(0, Math.min(1, random)) * 500);
}
export function formatTime(value: string | null) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(value));
}
