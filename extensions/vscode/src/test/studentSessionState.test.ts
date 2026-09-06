import assert from "node:assert/strict";
import test from "node:test";

import {
  getOrCreateSubmissionAttempt,
  PENDING_SUBMISSIONS_KEY,
  type SubmissionAttemptRequest,
} from "../submissionAttempt";
import {
  clearLegacyPersistedStudentState,
  clearStudentSessionResidue,
  EphemeralStudentState,
  getRememberedSubmissions,
  LATEST_SUBMISSIONS_KEY,
  mergeServerSubmissionIds,
  rememberSubmission,
} from "../studentSessionState";

class PersistedMemento {
  public readonly values = new Map<string, unknown>();
  public readonly deleted: string[] = [];

  public get<T>(key: string): T | undefined {
    return this.values.get(key) as T | undefined;
  }

  public async update(key: string, value: unknown): Promise<void> {
    if (value === undefined) {
      this.deleted.push(key);
      this.values.delete(key);
    } else {
      this.values.set(key, value);
    }
  }
}

const REQUEST: SubmissionAttemptRequest = {
  serviceBaseUrl: "https://grade.example.edu",
  assignmentId: "asn_1",
  bundleSha256: "a".repeat(64),
};

test("activation removes student submission metadata persisted by older versions", async () => {
  const storage = new PersistedMemento();
  storage.values.set(LATEST_SUBMISSIONS_KEY, { submissions: { asn_1: "sub_old" } });
  storage.values.set(PENDING_SUBMISSIONS_KEY, { attempts: { old: "metadata" } });
  storage.values.set("unrelated", "keep");

  await clearLegacyPersistedStudentState(storage);

  assert.deepEqual([...storage.values.entries()], [["unrelated", "keep"]]);
  assert.deepEqual(storage.deleted.sort(), [LATEST_SUBMISSIONS_KEY, PENDING_SUBMISSIONS_KEY].sort());
});

test("account boundary clears retry metadata, latest ids, output, diagnostics and assignment tree", async () => {
  const state = new EphemeralStudentState();
  await rememberSubmission(state, REQUEST.serviceBaseUrl, REQUEST.assignmentId, "sub_student_a");
  await getOrCreateSubmissionAttempt(state, REQUEST, () => "idem-student-a", 1_000);
  const calls: string[] = [];

  const nextSession = clearStudentSessionResidue(
    state,
    { clear: () => calls.push("tree.clear") },
    { clear: () => calls.push("output.clear"), hide: () => calls.push("output.hide") },
    { clear: () => calls.push("diagnostics.clear") },
  );

  assert.deepEqual(getRememberedSubmissions(state, REQUEST.serviceBaseUrl), {});
  assert.equal(state.get(PENDING_SUBMISSIONS_KEY), undefined);
  assert.equal(state.isActive(), false);
  assert.equal(nextSession.isActive(), true);
  assert.deepEqual(calls, ["tree.clear", "output.clear", "output.hide", "diagnostics.clear"]);
});

test("late writes from a previous login cannot repopulate the next student's state", async () => {
  const previous = new EphemeralStudentState();
  previous.clear();
  await rememberSubmission(previous, REQUEST.serviceBaseUrl, REQUEST.assignmentId, "sub_late");
  await getOrCreateSubmissionAttempt(previous, REQUEST, () => "idem-late", 1_000);

  const current = new EphemeralStudentState();
  assert.deepEqual(getRememberedSubmissions(previous, REQUEST.serviceBaseUrl), {});
  assert.equal(previous.get(PENDING_SUBMISSIONS_KEY), undefined);
  assert.deepEqual(getRememberedSubmissions(current, REQUEST.serviceBaseUrl), {});
});

test("idempotency survives transient retries only inside the current Extension Host session", async () => {
  const state = new EphemeralStudentState();
  const first = await getOrCreateSubmissionAttempt(state, REQUEST, () => "idem-1", 1_000);
  const retry = await getOrCreateSubmissionAttempt(state, REQUEST, () => "unexpected", 2_000);
  assert.equal(retry.idempotencyKey, first.idempotencyKey);

  state.clear();
  const afterNewLogin = await getOrCreateSubmissionAttempt(state, REQUEST, () => "idem-2", 3_000);
  assert.equal(afterNewLogin.idempotencyKey, "idem-2");
});

test("latest submission hints use the authenticated server projection in memory", async () => {
  const state = new EphemeralStudentState();
  await mergeServerSubmissionIds(state, "https://grade.example.edu", [{
    id: "asn_1",
    title: "Lab 1",
    latestSubmission: { id: "sub_1", state: "graded" },
  }]);
  assert.deepEqual(getRememberedSubmissions(state, "https://grade.example.edu"), { asn_1: "sub_1" });
  assert.deepEqual(getRememberedSubmissions(state, "https://other.example.edu"), {});
});
