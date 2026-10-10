import { parseBootstrap, parseResults } from "../contracts/instructor.ts";
import type { ResultsQuery } from "../../features/results/query.ts";

const PREFIX = "/instructor/api/v1";
export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly requestId: string;
  constructor(status: number, code: string, message: string, requestId = "") {
    super(message); this.status = status; this.code = code; this.requestId = requestId;
  }
}
export function terminalError(error: unknown): boolean {
  return error instanceof ApiError && ([401, 403, 404].includes(error.status) || error.code === "next_ui_disabled");
}
async function read<T>(url: string, parse: (v: unknown) => T, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, {
    method: "GET", credentials: "same-origin", cache: "no-store", redirect: "error",
    headers: { Accept: "application/json" }, signal,
  });
  let body: unknown;
  try { body = await response.json(); } catch {
    throw new ApiError(response.status, "invalid_response", "서버의 응답을 확인하지 못했습니다. 연결 주소와 서버 상태를 확인해 주세요.");
  }
  if (!response.ok) {
    const b = body && typeof body === "object" ? body as Record<string, unknown> : {};
    const e = b.error && typeof b.error === "object" ? b.error as Record<string, unknown> : {};
    throw new ApiError(response.status, typeof e.code === "string" ? e.code : "request_failed",
      typeof e.message === "string" ? e.message : "요청을 처리하지 못했습니다.", typeof b.request_id === "string" ? b.request_id : "");
  }
  return parse(body);
}
export function bootstrap(signal: AbortSignal) { return read(`${PREFIX}/bootstrap`, parseBootstrap, signal); }
export function resultsURL(course: string, query: ResultsQuery, version?: string): string {
  const p = new URLSearchParams({ status: query.status, sort: query.sort, page: String(query.page), page_size: "20" });
  if (query.assignment) p.set("assignment_id", query.assignment);
  if (query.student) p.set("student", query.student);
  if (version) p.set("version", version);
  return `${PREFIX}/courses/${encodeURIComponent(course)}/results?${p}`;
}
export function results(course: string, query: ResultsQuery, signal: AbortSignal, version?: string) {
  return read(resultsURL(course, query, version), parseResults, signal);
}
export function entryURL(course = "") {
  const p = new URLSearchParams({ screen: "results" });
  if (course) p.set("course_key", course);
  return `${PREFIX}/entry?${p}`;
}
