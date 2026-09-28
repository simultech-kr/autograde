import { createHash } from "node:crypto";
import type { Assignment, AssignmentDocument, AssignmentDocumentMetadata } from "./types";

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Optional list metadata must not break older servers' assignment/result views. */
export function parseDocumentMetadata(value: unknown): AssignmentDocumentMetadata | undefined {
  if (!record(value) || !Number.isSafeInteger(value.revision) || Number(value.revision) < 0 ||
      typeof value.sha256 !== "string" || !/^sha256:[a-f0-9]{64}$/.test(value.sha256) ||
      typeof value.updated_at !== "string" || !Number.isFinite(Date.parse(value.updated_at)) ||
      typeof value.change_note !== "string" || [...value.change_note].length > 500) return undefined;
  return {revision: Number(value.revision), sha256: value.sha256,
    updatedAt: value.updated_at, changeNote: value.change_note};
}

export function parseAssignmentDocument(payload: unknown, assignmentId: string): AssignmentDocument | undefined {
  if (record(payload) && payload.document === null) return undefined;
  const value = record(payload) ? payload.document : undefined;
  const metadata = parseDocumentMetadata(value);
  if (!record(value) || !metadata || value.assignment_id !== assignmentId ||
      typeof value.content !== "string" || [...value.content].length > 20000 ||
      !Array.isArray(value.history) || value.history.length > 50) {
    throw new Error("서비스의 과제 설명 응답 형식을 확인할 수 없습니다.");
  }
  const digest = "sha256:" + createHash("sha256").update(value.content, "utf8").digest("hex");
  if (digest !== metadata.sha256) throw new Error("과제 설명 SHA-256이 일치하지 않습니다. 다시 확인하세요.");
  const history: AssignmentDocumentMetadata[] = [];
  const revisions = new Set<number>();
  for (const entry of value.history) {
    const item = parseDocumentMetadata(entry);
    if (!item || item.revision > metadata.revision || revisions.has(item.revision)) {
      throw new Error("서비스의 설명 변경 이력을 확인할 수 없습니다.");
    }
    revisions.add(item.revision);
    history.push(item);
  }
  return {...metadata, assignmentId, content: value.content, history};
}

export interface DocumentScope { readonly assignmentId: string; isCurrent(): boolean }

/** Read acknowledgements last for one authenticated session, never on disk. */
export class AssignmentDocumentState {
  private generation = 0;
  private selectedId?: string;
  private readonly read = new Map<string, AssignmentDocumentMetadata>();

  public select(assignmentId: string | undefined): boolean {
    if (this.selectedId === assignmentId) return false;
    this.selectedId = assignmentId;
    this.generation++;
    return true;
  }

  public begin(assignmentId: string): DocumentScope {
    this.select(assignmentId);
    const generation = ++this.generation;
    return {assignmentId, isCurrent: () => generation === this.generation && assignmentId === this.selectedId};
  }

  public selected(): string | undefined { return this.selectedId; }

  public markRead(document: AssignmentDocument, scope: DocumentScope): boolean {
    if (!scope.isCurrent() || scope.assignmentId !== document.assignmentId) return false;
    const {revision, sha256, updatedAt, changeNote} = document;
    this.read.set(document.assignmentId, {revision, sha256, updatedAt, changeNote});
    return true;
  }

  public summary(assignment: Assignment): {revision: number; unread: boolean} {
    const read = this.read.get(assignment.id);
    const available = assignment.document;
    const revision = Math.max(available?.revision ?? 0, read?.revision ?? 0);
    return {revision, unread: Boolean(available && available.revision > 0 &&
      (!read || available.revision > read.revision || (available.revision === read.revision && available.sha256 !== read.sha256)))};
  }

  public clear(): void {
    this.generation++;
    this.selectedId = undefined;
    this.read.clear();
  }
}

export function documentText(title: string, document: AssignmentDocument): string {
  const updated = new Date(document.updatedAt).toLocaleString("ko-KR", {timeZone: "Asia/Seoul"});
  const header = `${title} — 과제 설명 v${document.revision}\n변경 시각: ${updated} KST\n변경 안내: ${document.changeNote}\n` +
    "읽기 전용 서버 설명입니다. 내려받은 README와 작성 중인 코드는 변경되지 않습니다.\n\n";
  const history = document.history.length ? "\n\n설명 변경 이력\n" + document.history.map(item =>
    `v${item.revision} · ${new Date(item.updatedAt).toLocaleString("ko-KR", {timeZone: "Asia/Seoul"})} KST · ${item.changeNote}`).join("\n") : "";
  return header + document.content + history;
}
