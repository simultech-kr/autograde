import type { Assignment } from "./types";
import {
  type MementoLike,
  PENDING_SUBMISSIONS_KEY,
} from "./submissionAttempt";

export const LATEST_SUBMISSIONS_KEY = "autograde.latestSubmissions.v1";

interface LatestSubmissionsState {
  readonly serviceBaseUrl: string;
  readonly submissions: Readonly<Record<string, string>>;
}

/**
 * Student-specific extension state that intentionally lives only for the
 * lifetime of the current Extension Host. A shared VS Code profile must not
 * carry submission metadata from one login (or student) into the next one.
 */
export class EphemeralStudentState implements MementoLike {
  private readonly values = new Map<string, unknown>();
  private active = true;

  public get<T>(key: string): T | undefined {
    if (!this.active) {
      return undefined;
    }
    return this.values.get(key) as T | undefined;
  }

  public async update(key: string, value: unknown): Promise<void> {
    if (!this.active) {
      return;
    }
    if (value === undefined) {
      this.values.delete(key);
      return;
    }
    this.values.set(key, value);
  }

  public clear(): void {
    this.active = false;
    this.values.clear();
  }

  public isActive(): boolean {
    return this.active;
  }
}

interface Clearable {
  clear(): void;
}

interface HideableOutput extends Clearable {
  hide(): void;
}

/** Clear every student-specific in-memory surface at an authentication boundary. */
export function clearStudentSessionResidue(
  state: EphemeralStudentState,
  assignmentTree: Clearable,
  output: HideableOutput,
  diagnostics: Clearable,
): EphemeralStudentState {
  state.clear();
  assignmentTree.clear();
  output.clear();
  output.hide();
  diagnostics.clear();
  return new EphemeralStudentState();
}

/** Remove student metadata persisted by earlier extension versions. */
export async function clearLegacyPersistedStudentState(storage: MementoLike): Promise<void> {
  await Promise.all([
    storage.update(LATEST_SUBMISSIONS_KEY, undefined),
    storage.update(PENDING_SUBMISSIONS_KEY, undefined),
  ]);
}

export async function rememberSubmission(
  storage: MementoLike,
  serviceBaseUrl: string,
  assignmentId: string,
  submissionId: string,
): Promise<void> {
  const stored = getRememberedSubmissions(storage, serviceBaseUrl);
  const state: LatestSubmissionsState = {
    serviceBaseUrl,
    submissions: { ...stored, [assignmentId]: submissionId },
  };
  await storage.update(LATEST_SUBMISSIONS_KEY, state);
}

export async function mergeServerSubmissionIds(
  storage: MementoLike,
  serviceBaseUrl: string,
  assignments: readonly Assignment[],
): Promise<void> {
  const stored = getRememberedSubmissions(storage, serviceBaseUrl);
  let changed = false;
  const next = { ...stored };
  for (const assignment of assignments) {
    if (assignment.latestSubmission?.id && next[assignment.id] !== assignment.latestSubmission.id) {
      next[assignment.id] = assignment.latestSubmission.id;
      changed = true;
    }
  }
  if (changed) {
    const state: LatestSubmissionsState = { serviceBaseUrl, submissions: next };
    await storage.update(LATEST_SUBMISSIONS_KEY, state);
  }
}

export function getRememberedSubmissions(
  storage: MementoLike,
  serviceBaseUrl: string,
): Readonly<Record<string, string>> {
  const state = storage.get<LatestSubmissionsState>(LATEST_SUBMISSIONS_KEY);
  return state?.serviceBaseUrl === serviceBaseUrl ? state.submissions : {};
}
