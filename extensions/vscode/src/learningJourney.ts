import type { SubmissionSummary } from "./types";

const publicationStep = "채점 완료 · 공개 대기";
export const publicationGuidance = "교수자가 결과를 공개한 뒤 다시 확인하세요. 다시 제출할 필요는 없습니다.";
export const awaitingPublication = (state?: string): boolean => state === "graded" || state === "result_pending";

/** The same receipt states are used by the student portal. Saving is not submitting. */
export function submissionLabel(state?: string): string {
  return ({ received: "제출 접수", pinning: "제출 접수", verifying: "제출 접수",
    accepted: "제출 접수", queued: "채점 대기", running: "채점 중",
    graded: publicationStep, result_pending: publicationStep, published: "결과 공개",
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
  const steps = ["제출 접수", "채점 대기", "채점 중", publicationStep, "결과 공개"];
  const current = steps.indexOf(submissionLabel(state));
  return steps.map((label, index) => ({label, current: index === current, complete: index < current}));
}
