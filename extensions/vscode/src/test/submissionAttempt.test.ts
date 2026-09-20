import assert from "node:assert/strict";
import test from "node:test";

import {
  clearSubmissionAttempt,
  getOrCreateSubmissionAttempt,
  type MementoLike,
  PENDING_SUBMISSIONS_KEY,
  type SubmissionAttemptRequest,
} from "../submissionAttempt";

class TestMemento implements MementoLike {
  private readonly values = new Map<string, unknown>();
  public updates = 0;

  public get<T>(key: string): T | undefined {
    return this.values.get(key) as T | undefined;
  }

  public async update(key: string, value: unknown): Promise<void> {
    this.updates += 1;
    this.values.set(key, value);
  }
}

const REQUEST: SubmissionAttemptRequest = {
  serviceBaseUrl: "https://grade.example.edu",
  assignmentId: "asn_1",
  githubRepositoryId: 42,
  headSha: "a".repeat(40),
};

test("submission idempotency key is persisted and reused for the same request", async () => {
  const storage = new TestMemento();
  let generated = 0;
  const createKey = (): string => `request-key-${++generated}`;

  const first = await getOrCreateSubmissionAttempt(storage, REQUEST, createKey, 1_000);
  assert.equal(first.idempotencyKey, "request-key-1");
  assert.ok(storage.get(PENDING_SUBMISSIONS_KEY));

  const replay = await getOrCreateSubmissionAttempt(storage, REQUEST, createKey, 2_000);
  assert.equal(replay.idempotencyKey, first.idempotencyKey);
  assert.equal(generated, 1);
  assert.equal(storage.updates, 1);
});

test("a changed commit receives a new key and successful receipt clears it", async () => {
  const storage = new TestMemento();
  const first = await getOrCreateSubmissionAttempt(storage, REQUEST, () => "first-key", 1_000);
  const changed = { ...REQUEST, headSha: "b".repeat(40) };
  const second = await getOrCreateSubmissionAttempt(storage, changed, () => "second-key", 2_000);

  assert.notEqual(second.idempotencyKey, first.idempotencyKey);
  await clearSubmissionAttempt(storage, changed, "wrong-key");
  const stillStored = await getOrCreateSubmissionAttempt(storage, changed, () => "unexpected", 3_000);
  assert.equal(stillStored.idempotencyKey, "second-key");

  await clearSubmissionAttempt(storage, changed, "second-key");
  const recreated = await getOrCreateSubmissionAttempt(storage, changed, () => "third-key", 4_000);
  assert.equal(recreated.idempotencyKey, "third-key");
});

test("bundle digest persists an idempotency key independently from Git fields", async () => {
  const storage = new TestMemento();
  const request: SubmissionAttemptRequest = {
    serviceBaseUrl: "https://grade.example.edu",
    assignmentId: "asn_bundle",
    bundleSha256: "a".repeat(64),
  };
  const first = await getOrCreateSubmissionAttempt(storage, request, () => "bundle-key", 1_000);
  const replay = await getOrCreateSubmissionAttempt(storage, request, () => "unexpected", 2_000);
  assert.equal(replay.idempotencyKey, first.idempotencyKey);

  const changed = { ...request, bundleSha256: "b".repeat(64) };
  const next = await getOrCreateSubmissionAttempt(storage, changed, () => "changed-key", 3_000);
  assert.equal(next.idempotencyKey, "changed-key");
});

test("a confirmed bundle resubmission gets a new key even when bytes are unchanged", async () => {
  const storage = new TestMemento();
  const request = {serviceBaseUrl:"https://grade.example.edu", assignmentId:"asn_one", bundleSha256:"a".repeat(64)};
  const first = await getOrCreateSubmissionAttempt(storage, request, () => "first", 1000);
  await clearSubmissionAttempt(storage, request, first.idempotencyKey);
  const next = await getOrCreateSubmissionAttempt(storage, request, () => "next", 2000);
  assert.notEqual(next.idempotencyKey, first.idempotencyKey);
});

test("submission attempts reject ambiguous or malformed source identities", async () => {
  const storage = new TestMemento();
  await assert.rejects(
    getOrCreateSubmissionAttempt(storage, {
      serviceBaseUrl: "https://grade.example.edu",
      assignmentId: "asn_1",
      bundleSha256: "not-a-digest",
    }),
    /bundle digest/,
  );
  await assert.rejects(
    getOrCreateSubmissionAttempt(storage, {
      ...REQUEST,
      bundleSha256: "a".repeat(64),
    }),
    /bundle digest/,
  );
});
