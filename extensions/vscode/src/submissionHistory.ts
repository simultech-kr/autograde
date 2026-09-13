import { sha256Hex } from "./bundle";

export interface SubmissionVersion {
  readonly id: string;
  readonly assignmentId: string;
  readonly state: string;
  readonly receivedAt: string;
  readonly sourceDigest: string;
  readonly sourceSize: number;
}

export function parseSubmissionHistory(payload: unknown, assignmentId: string): {
  submissions: SubmissionVersion[]; hasMore: boolean;
} {
  const value = payload as Record<string, unknown> | null;
  if (!value || !Array.isArray(value.submissions) || value.submissions.length > 100 ||
      typeof value.has_more !== "boolean") throw new Error("제출 기록 응답이 올바르지 않습니다.");
  const ids = new Set<string>();
  const submissions = value.submissions.map((row: Record<string, unknown>) => {
    if (!row || typeof row.submission_id !== "string" || !/^bsub_[A-Za-z0-9_-]+$/.test(row.submission_id) ||
        ids.has(row.submission_id) || row.assignment_id !== assignmentId ||
        typeof row.state !== "string" || typeof row.received_at !== "string" ||
        !Number.isFinite(Date.parse(row.received_at)) || typeof row.source_sha256 !== "string" ||
        !/^sha256:[a-f0-9]{64}$/.test(row.source_sha256) || typeof row.source_size_bytes !== "number" ||
        !Number.isSafeInteger(row.source_size_bytes) || row.source_size_bytes < 1 ||
        row.source_size_bytes > 25 * 1024 * 1024) throw new Error("제출 기록의 과제 또는 파일 정보가 일치하지 않습니다.");
    ids.add(row.submission_id);
    return { id: row.submission_id, assignmentId, state: row.state, receivedAt: row.received_at,
      sourceDigest: row.source_sha256.slice(7), sourceSize: row.source_size_bytes };
  });
  return { submissions, hasMore: value.has_more };
}

export function verifySubmissionSource(bytes: Uint8Array, version: SubmissionVersion): void {
  if (bytes.byteLength !== version.sourceSize || sha256Hex(bytes) !== version.sourceDigest) {
    throw new Error("복원 파일이 서버 제출 기록의 크기 또는 SHA-256과 일치하지 않습니다.");
  }
}
