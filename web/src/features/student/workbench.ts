import {courseKey, StudentContractError, unwrap, validScore} from "./model.ts";
import {StudentApiError} from "./client.ts";

export type MaterialItem = {
  id: string; kind: "material" | "exam"; title: string; filename: string | null; format: "pdf" | "zip";
  size_bytes: number | null; updated_at: string; opens_at: string; closes_at: string;
  availability: "open" | "scheduled" | "closed"; revision: number; file_revision: number; edition: number;
  instructions: string; change_note: string; requested: boolean; download_path: string | null;
};
export type MaterialCatalog = {items: MaterialItem[]; truncated: boolean};
export type FeedbackCriterion = {
  key: string; title: string; score: number | null; max_score: number | null;
  status?: "passed" | "partial" | "failed" | "blocked"; feedback: string; hint: string; location?: string;
};
export type SubmissionFeedback = {
  submission_id: string; assignment_id: string; published_at: string; feedback_available: boolean;
  score: number | null; max_score: number | null; criteria: FeedbackCriterion[]; diagnostics: {message: string; location?: string}[];
};
const record = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new StudentContractError();
  return value as Record<string, unknown>;
};
const text = (value: unknown, max = 16000): string => {
  if (typeof value !== "string" || value.length > max || /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(value)) throw new StudentContractError();
  return value;
};
const optionalText = (value: unknown, max = 16000) => value === undefined || value === null ? "" : text(value, max);
const identifier = (value: unknown): string => {const id = text(value, 256); if (!/^[A-Za-z0-9_-]+$/.test(id)) throw new StudentContractError(); return id;};
const date = (value: unknown): string => {const result = text(value, 80); if (!Number.isFinite(Date.parse(result))) throw new StudentContractError(); return result;};
const bool = (value: unknown) => {if (typeof value !== "boolean") throw new StudentContractError(); return value;};
const positive = (value: unknown) => {if (!Number.isSafeInteger(value) || Number(value) < 1) throw new StudentContractError(); return Number(value);};
const score = (value: unknown): number | null => {if (value === undefined || value === null) return null; if (typeof value !== "number" || !Number.isFinite(value) || value < 0) throw new StudentContractError(); return value;};

export function materialDownloadPath(course: string, kind: MaterialItem["kind"], id: string): string {
  const segment = kind === "material" ? "materials" : "exam-papers";
  if (!(kind === "material" ? /^mat_[A-Za-z0-9_-]+$/ : /^ep_[A-Za-z0-9_-]+$/).test(id)) throw new StudentContractError();
  return `/student/api/course/${courseKey(course)}/${segment}/${identifier(id)}/download`;
}

export function parseMaterialCatalog(value: unknown, course: string): MaterialCatalog | undefined {
  if (value === undefined) return undefined;
  const catalog = record(value), seen = new Set<string>();
  if (!Array.isArray(catalog.items) || catalog.items.length > 1000) throw new StudentContractError();
  return {truncated: bool(catalog.truncated), items: catalog.items.map(raw => {
    const item = record(raw), id = identifier(item.id), kind = text(item.kind), availability = text(item.availability), format = text(item.format);
    if (!["material", "exam"].includes(kind) || !["open", "scheduled", "closed"].includes(availability) || !["pdf", "zip"].includes(format) || kind === "exam" && format !== "pdf" || seen.has(id)) throw new StudentContractError();
    seen.add(id);
    const expected = materialDownloadPath(course, kind as MaterialItem["kind"], id);
    const filename = item.filename === null ? null : text(item.filename, 255);
    if (filename !== null && (!filename.trim() || /[\\/\u0000-\u001f\u007f]/.test(filename) || filename === "." || filename === "..")) throw new StudentContractError();
    const size_bytes = item.size_bytes === null ? null : positive(item.size_bytes);
    const cap = (kind === "exam" ? 10 : 25) * 1024 * 1024;
    if (size_bytes !== null && size_bytes > cap) throw new StudentContractError();
    if (availability === "open" ? item.download_path !== expected || filename === null || size_bytes === null : item.download_path !== null || filename !== null || size_bytes !== null) throw new StudentContractError();
    return {id, kind: kind as MaterialItem["kind"], title: text(item.title, 1000), filename, format: format as MaterialItem["format"], size_bytes,
      updated_at: date(item.updated_at), opens_at: date(item.opens_at), closes_at: date(item.closes_at), availability: availability as MaterialItem["availability"],
      revision: positive(item.revision), file_revision: positive(item.file_revision), edition: positive(item.edition),
      instructions: optionalText(item.instructions), change_note: optionalText(item.change_note), requested: bool(item.requested), download_path: availability === "open" ? expected : null};
  })};
}

export function feedbackPath(course: string, submission: string) {return `/student/api/course/${courseKey(course)}/submissions/${identifier(submission)}`;}
function location(item: Record<string, unknown>): string | undefined {
  if (item.path === undefined || item.path === null) return undefined;
  const path = text(item.path, 512);
  // Locations are display-only relative source paths, never links to a host path.
  if (!path || path.startsWith("/") || /[\\:\u0000-\u001f\u007f\u202a-\u202e\u2066-\u2069]/.test(path) || path.split("/").some(part => !part || part === "." || part === "..")) return undefined;
  const line = Number.isSafeInteger(item.line) && Number(item.line) > 0 && Number(item.line) <= 10000000 ? Number(item.line) : null;
  const column = line && Number.isSafeInteger(item.column) && Number(item.column) > 0 && Number(item.column) <= 10000000 ? Number(item.column) : null;
  return `${path}${line ? `:${line}${column ? `:${column}` : ""}` : ""}`;
}
export function parseSubmissionFeedback(value: unknown, course: string, student: string, submission: string, assignment: string): SubmissionFeedback {
  const envelope = record(unwrap(value)), result = record(envelope.result), available = bool(envelope.feedback_available);
  if (envelope.course_key !== courseKey(course) || envelope.student_key !== student || envelope.submission_id !== submission || envelope.assignment_id !== assignment || result.submission_id !== submission || result.state !== "published") throw new StudentContractError();
  const rubric = record(result.rubric), diagnostics = result.diagnostics;
  if (Object.keys(rubric).length > 1000 || !Array.isArray(diagnostics) || diagnostics.length > 1000 || !available && (Object.keys(rubric).length || diagnostics.length)) throw new StudentContractError();
  const titles: Record<string, string> = {compile:"컴파일", execution:"프로그램 실행", output:"출력 결과", correctness:"정확성"};
  return {submission_id: identifier(submission), assignment_id: identifier(assignment), published_at: date(result.published_at), feedback_available: available,
    score: score(result.score), max_score: score(result.max_score), criteria: Object.entries(rubric).map(([key, raw]) => {
      const item = record(raw), status = item.status;
      if (status !== undefined && !["passed", "partial", "failed", "blocked"].includes(String(status))) throw new StudentContractError();
      return {key: text(key, 256), title: optionalText(item.title, 1000) || titles[key] || key, score: score(item.score), max_score: score(item.max_score),
        status: status as FeedbackCriterion["status"], feedback: optionalText(item.feedback), hint: optionalText(item.hint), location: location(item)};
    }), diagnostics: diagnostics.map(raw => {const item = record(raw); return {message: text(item.message), location: location(item)};})};
}

export function criterionVerdict(item: FeedbackCriterion) {
  const comparable = validScore(item.score, item.max_score);
  const status = item.status === "blocked" ? "blocked" : item.status === "failed" || item.status === "partial" ? item.status : comparable ? item.score === item.max_score ? "passed" : item.score! > 0 ? "partial" : "failed" : item.status === "passed" && item.score === null && item.max_score === null ? "passed" : "unknown";
  const mismatch = comparable && (item.status === "passed" && item.score !== item.max_score || item.status === "partial" && (item.score === 0 || item.score === item.max_score) || (item.status === "failed" || item.status === "blocked") && item.score !== 0);
  const labels = {passed:"충족", partial:"부분 충족 · 수정 필요", failed:"수정 필요", blocked:"선행 단계 해결 후 재검사", unknown:"판정 없음"};
  return {status, label: labels[status] + (mismatch ? " · 점수·판정 확인 필요" : ""), mismatch, needsWork: status === "failed" || status === "partial", rank: {failed:0, partial:0, blocked:1, unknown:2, passed:3}[status]};
}

export function fileSize(bytes: number | null) {
  if (bytes === null) return "크기 미공개";
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toLocaleString("ko-KR", {maximumFractionDigits:1})} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toLocaleString("ko-KR", {maximumFractionDigits:1})} KB`;
  return `${bytes.toLocaleString("ko-KR")} B`;
}

/** A bounded, same-origin POST keeps download errors inside the learning page. */
export async function fetchMaterial(item: MaterialItem, course: string, csrf: string, signal: AbortSignal, fetcher: typeof fetch = fetch): Promise<Blob> {
  if (signal.aborted) throw new StudentApiError(0,"download_cancelled","다운로드가 중단되었습니다. 다시 시도하세요.");
  if (item.availability !== "open" || item.download_path !== materialDownloadPath(course, item.kind, item.id) || !item.filename || !item.size_bytes) throw new StudentContractError();
  const response = await fetcher(item.download_path, {method:"POST", credentials:"same-origin", cache:"no-store", redirect:"error", signal,
    headers:{Accept:"application/pdf, application/zip, application/json", "Content-Type":"application/x-www-form-urlencoded;charset=UTF-8"},
    body:new URLSearchParams({csrf, revision:String(item.revision), file_revision:String(item.file_revision)}).toString()});
  if (!response.ok) {
    const messages: Record<number,string> = {401:"로그인이 만료되었습니다. 다시 로그인한 뒤 다운로드하세요.",403:"배포 기간이나 수강 상태가 변경되었습니다. 목록을 새로고침해 주세요.",404:"이 자료를 더 이상 받을 수 없습니다. 교수자에게 배정 여부를 확인하세요.",409:"자료가 수정되었습니다. 목록을 새로고침한 뒤 최신 파일을 받으세요."};
    throw new StudentApiError(response.status,"download_failed",messages[response.status] || "자료를 내려받지 못했습니다. 잠시 후 다시 시도하세요.");
  }
  const mime = response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase();
  if (mime !== (item.format === "pdf" ? "application/pdf" : "application/zip")) throw new StudentContractError();
  const cap = (item.kind === "exam" ? 10 : 25) * 1024 * 1024;
  const advertised = response.headers.get("Content-Length");
  if (advertised !== null && Number(advertised) !== item.size_bytes) throw new StudentContractError();
  const reader = response.body?.getReader();
  if (!reader) throw new StudentContractError();
  const chunks: Uint8Array<ArrayBuffer>[] = []; let received = 0;
  try {
    while (true) {
      const {value, done} = await reader.read(); if (done) break;
      received += value.length;
      if (received > cap || received > item.size_bytes) throw new StudentContractError();
      chunks.push(new Uint8Array(value));
    }
  } catch (error) {await reader.cancel().catch(()=>{}); throw error;} finally {reader.releaseLock();}
  if (received !== item.size_bytes || signal.aborted) throw new StudentContractError();
  return new Blob(chunks, {type:mime});
}
