import { randomUUID } from "node:crypto";

export const PENDING_SUBMISSIONS_KEY = "autograde.pendingSubmissions.v1";
const MAX_ATTEMPT_AGE_MS = 7 * 24 * 60 * 60 * 1000;
const MAX_RETAINED_ATTEMPTS = 100;

export interface MementoLike {
  get<T>(key: string): T | undefined;
  update(key: string, value: unknown): Thenable<void>;
}

export interface SubmissionAttemptRequest {
  readonly serviceBaseUrl: string;
  readonly assignmentId: string;
  readonly githubRepositoryId?: number;
  readonly headSha?: string;
  readonly bundleSha256?: string;
  readonly pullRequestNumber?: number;
}

export interface PendingSubmissionAttempt extends SubmissionAttemptRequest {
  readonly idempotencyKey: string;
  readonly createdAt: number;
}

interface PendingSubmissionState {
  readonly attempts: Readonly<Record<string, PendingSubmissionAttempt>>;
}

export async function getOrCreateSubmissionAttempt(
  storage: MementoLike,
  request: SubmissionAttemptRequest,
  createKey: () => string = randomUUID,
  now = Date.now(),
): Promise<PendingSubmissionAttempt> {
  if (!isValidSubmissionSource(request)) {
    throw new TypeError("submission attempt에는 Git commit 또는 bundle digest가 필요합니다");
  }
  const slot = attemptSlot(request);
  const attempts = pruneAttempts(readAttempts(storage), now);
  const existing = attempts[slot];
  if (existing && sameSubmission(existing, request)) {
    return existing;
  }
  const created: PendingSubmissionAttempt = {
    ...request,
    idempotencyKey: createKey(),
    createdAt: now,
  };
  const next = { ...attempts, [slot]: created };
  const bounded = Object.fromEntries(
    Object.entries(next)
      .sort(([, left], [, right]) => right.createdAt - left.createdAt)
      .slice(0, MAX_RETAINED_ATTEMPTS),
  );
  await storage.update(PENDING_SUBMISSIONS_KEY, { attempts: bounded } satisfies PendingSubmissionState);
  return created;
}

export async function clearSubmissionAttempt(
  storage: MementoLike,
  request: SubmissionAttemptRequest,
  idempotencyKey: string,
): Promise<void> {
  const attempts = readAttempts(storage);
  const slot = attemptSlot(request);
  if (attempts[slot]?.idempotencyKey !== idempotencyKey) {
    return;
  }
  const next = { ...attempts };
  delete next[slot];
  await storage.update(PENDING_SUBMISSIONS_KEY, { attempts: next } satisfies PendingSubmissionState);
}

function attemptSlot(request: Pick<SubmissionAttemptRequest, "serviceBaseUrl" | "assignmentId">): string {
  return `${encodeURIComponent(request.serviceBaseUrl)}:${encodeURIComponent(request.assignmentId)}`;
}

function sameSubmission(
  attempt: PendingSubmissionAttempt,
  request: SubmissionAttemptRequest,
): boolean {
  return (
    attempt.serviceBaseUrl === request.serviceBaseUrl &&
    attempt.assignmentId === request.assignmentId &&
    attempt.githubRepositoryId === request.githubRepositoryId &&
    attempt.headSha?.toLowerCase() === request.headSha?.toLowerCase() &&
    attempt.bundleSha256?.toLowerCase() === request.bundleSha256?.toLowerCase() &&
    attempt.pullRequestNumber === request.pullRequestNumber
  );
}

function readAttempts(storage: MementoLike): Record<string, PendingSubmissionAttempt> {
  const state = storage.get<PendingSubmissionState>(PENDING_SUBMISSIONS_KEY);
  if (!state || typeof state !== "object" || !state.attempts || typeof state.attempts !== "object") {
    return {};
  }
  return Object.fromEntries(
    Object.entries(state.attempts).filter((entry): entry is [string, PendingSubmissionAttempt] => {
      const value = entry[1];
      return Boolean(
        value &&
        typeof value === "object" &&
        typeof value.serviceBaseUrl === "string" &&
        typeof value.assignmentId === "string" &&
        isValidSubmissionSource(value) &&
        typeof value.idempotencyKey === "string" &&
        typeof value.createdAt === "number" &&
        (value.pullRequestNumber === undefined || typeof value.pullRequestNumber === "number"),
      );
    }),
  );
}

function isValidSubmissionSource(value: SubmissionAttemptRequest): boolean {
  const hasGitSource =
    typeof value.githubRepositoryId === "number" &&
    Number.isSafeInteger(value.githubRepositoryId) &&
    value.githubRepositoryId > 0 &&
    typeof value.headSha === "string" &&
    /^[0-9a-f]{40}$/i.test(value.headSha) &&
    value.bundleSha256 === undefined;
  const hasBundleSource =
    value.githubRepositoryId === undefined &&
    value.headSha === undefined &&
    typeof value.bundleSha256 === "string" &&
    /^[0-9a-f]{64}$/i.test(value.bundleSha256) &&
    value.pullRequestNumber === undefined;
  return hasGitSource || hasBundleSource;
}

function pruneAttempts(
  attempts: Readonly<Record<string, PendingSubmissionAttempt>>,
  now: number,
): Record<string, PendingSubmissionAttempt> {
  return Object.fromEntries(
    Object.entries(attempts).filter(([, attempt]) =>
      Number.isFinite(attempt.createdAt) &&
      attempt.createdAt >= now - MAX_ATTEMPT_AGE_MS &&
      attempt.createdAt <= now + 60_000
    ),
  );
}
