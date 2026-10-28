import type { SubmissionSummary } from "./types";

/** The same receipt states are used by the student portal. Saving is not submitting. */
export function submissionLabel(state?: string): string {
  return ({ received: "제출 접수", pinning: "제출 접수", verifying: "제출 접수",
    accepted: "제출 접수", queued: "채점 대기", running: "채점 중",
    graded: "결과 공개 대기", result_pending: "결과 공개 대기", published: "결과 공개",
    rejected: "처리 확인 필요", infra_failed: "처리 확인 필요", assessment_failed: "처리 확인 필요", failed: "처리 확인 필요",
  } as Record<string, string>)[state ?? ""] ?? (state ? "처리 확인 필요" : "공식 제출 전");
}

export function publicSubmissionScore(value?: Pick<SubmissionSummary, "state" | "score" | "maxScore">): string | undefined {
  if (value?.state !== "published" || !Number.isFinite(value.score) || !Number.isFinite(value.maxScore)
      || value.maxScore! <= 0 || value.score! < 0 || value.score! > value.maxScore!) return undefined;
  return `${value.score} / ${value.maxScore}점`;
}

export function submissionTime(value?: string): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "서버 제공 시각 없음";
  return new Intl.DateTimeFormat("ko-KR", { timeZone: "Asia/Seoul", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(value)) + " (한국 시간)";
}

export function receiptProgress(state?: string): { label: string; current: boolean; complete: boolean }[] {
  const stage = state === "published" ? 3 : ["running", "graded", "result_pending"].includes(state ?? "") ? 2
    : state === "queued" ? 1 : ["received", "pinning", "verifying", "accepted"].includes(state ?? "") ? 0 : -1;
  return ["제출 접수", "채점 대기", "채점 중", "결과 공개"].map((label, index) => ({label, current: index === stage, complete: index < stage}));
}
