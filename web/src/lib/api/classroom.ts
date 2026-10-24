import { ApiError } from "./client.ts";
import { ContractError, parseBootstrap } from "../contracts/instructor.ts";
import { parseClassroom, parseClassroomDetail, parseCommandCreated, type CommandKind } from "../contracts/classroom.ts";

const prefix = "/instructor/api/v1";
async function read<T>(url: string, parse: (v: unknown) => T, signal: AbortSignal): Promise<T> {
  const response = await fetch(url, { credentials: "same-origin", cache: "no-store", redirect: "error", headers: { Accept: "application/json" }, signal });
  const value = await response.json().catch(() => { throw new ApiError(response.status, "invalid_response", "서버 응답을 확인하지 못했습니다."); });
  if (!response.ok) throw new ApiError(response.status, value?.error?.code || "request_failed", value?.error?.message || "요청을 처리하지 못했습니다.", value?.request_id || "");
  return parse(value);
}
export function classroomURL(course: string, assignment = "", suffix = "") {
  const query = assignment ? `?${new URLSearchParams({ assignment_id: assignment })}` : "";
  return `${prefix}/courses/${encodeURIComponent(course)}/classroom${suffix}${query}`;
}
export function classroomBootstrap(signal: AbortSignal) { return read(`${prefix}/classroom/bootstrap`, parseBootstrap, signal); }
export function classroom(course: string, assignment: string, signal: AbortSignal) { return read(classroomURL(course, assignment), parseClassroom, signal); }
export function classroomDetail(course: string, assignment: string, student: string, signal: AbortSignal) {
  return read(classroomURL(course, assignment, `/students/${encodeURIComponent(student)}`), parseClassroomDetail, signal);
}
export function classroomEntry(course = "") { return `${prefix}/entry?${new URLSearchParams({ screen: "classroom", ...(course ? { course_key: course } : {}) })}`; }
export type CommandRequest = { csrf: string; kind: CommandKind; message: string; assignment_id?: string; request_key: string };
export async function createClassroomCommand(course: string, student: string, payload: CommandRequest, signal: AbortSignal) {
  const response = await fetch(classroomURL(course, "", `/students/${encodeURIComponent(student)}/commands`), {
    method: "POST", credentials: "same-origin", redirect: "error", cache: "no-store", signal,
    headers: { Accept: "application/json", "Content-Type": "application/json" }, body: JSON.stringify(payload),
  });
  const value = await response.json().catch(() => { throw new ApiError(response.status, "invalid_response", "전송 결과를 확인하지 못했습니다. 같은 요청으로 다시 확인하세요."); });
  if (!response.ok) throw new ApiError(response.status, value?.error?.code || "request_failed", value?.error?.message || "IDE 요청을 접수하지 못했습니다.", value?.request_id || "");
  const result = parseCommandCreated(value);
  if (result.command.kind !== payload.kind || result.command.assignment_id !== (payload.assignment_id || null)) throw new ContractError();
  return result;
}
