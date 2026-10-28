import { StudentContractError, courseKey } from "./model.ts";

export class StudentApiError extends Error {
  readonly status: number; readonly code: string;
  constructor(status: number, code: string, message: string) { super(message); this.status = status; this.code = code; }
}
export function allowedStudentPath(path: string): boolean {
  if (/^\/student\/api\/(bootstrap|register|login|logout|enroll|withdraw|password|reset|activate)$/.test(path)) return true;
  const match = /^\/student\/api\/course\/([a-z0-9][a-z0-9_-]{0,95})(\/claims)?$/.exec(path);
  return Boolean(match && courseKey(match[1]));
}
export async function studentRequest(path: string, fields?: Record<string, string>, signal?: AbortSignal, fetcher: typeof fetch = fetch): Promise<unknown> {
  if (!allowedStudentPath(path)) throw new StudentContractError();
  const timeout = new AbortController(), timer = setTimeout(() => timeout.abort(), 20000);
  const abort = () => timeout.abort(); signal?.addEventListener("abort", abort, { once: true });
  if (signal?.aborted) timeout.abort();
  try {
    const response = await fetcher(path, { method: fields ? "POST" : "GET", credentials: "same-origin", cache: "no-store", redirect: "error", signal: timeout.signal,
      headers: { Accept: "application/json", ...(fields ? { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8" } : {}) },
      ...(fields ? { body: new URLSearchParams(fields).toString() } : {}) });
    if (!response.headers.get("Content-Type")?.toLowerCase().includes("application/json")) throw new StudentContractError();
    const text = await response.text(); if (text.length > 2 * 1024 * 1024) throw new StudentContractError();
    let payload: unknown; try { payload = JSON.parse(text); } catch { throw new StudentContractError(); }
    if (!response.ok) {
      const value = payload as { error?: { code?: unknown; message?: unknown } };
      const code = typeof value?.error?.code === "string" && /^[a-z0-9_]{1,80}$/.test(value.error.code) ? value.error.code : "request_failed";
      const message = typeof value?.error?.message === "string" && value.error.message.length <= 1000 ? value.error.message : "요청을 처리하지 못했습니다. 다시 확인해 주세요.";
      throw new StudentApiError(response.status, code, message);
    }
    return payload;
  } catch (error) {
    if (signal?.aborted) throw error;
    if (error instanceof StudentApiError || error instanceof StudentContractError) throw error;
    throw new StudentApiError(0, timeout.signal.aborted ? "request_timeout" : "network_error", timeout.signal.aborted ? "서버 응답이 지연되고 있습니다. 잠시 후 다시 확인하세요." : "서버에 연결하지 못했습니다. 인터넷 연결을 확인하고 다시 시도하세요.");
  } finally { clearTimeout(timer); signal?.removeEventListener("abort", abort); }
}
