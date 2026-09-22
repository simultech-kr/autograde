import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, RequestCancelledError } from "../api";
import { SubmissionResultMonitor, type ResultUpdate } from "../submissionResultMonitor";
import type { GradeResult, SubmissionSummary } from "../types";

const receipt: SubmissionSummary = { id: "current", state: "queued", sourceDigest: "source" };
const result: GradeResult = { state: "published", sourceDigest: "source", score: 25, maxScore: 100, rubric: [], diagnostics: [],
  previousBest: { submissionId: "previous", receivedAt: "2026-09-22T00:00:00Z", score: 100, maxScore: 100 } };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}
async function until(predicate: () => boolean): Promise<void> {
  for (let tries = 0; tries < 300; tries++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 2));
  }
  assert.fail("monitor did not reach the expected state");
}

test("receipt polling is nonblocking and always fetches the same current receipt through completion", async t => {
  const states = ["received", "queued", "running", "published"];
  const fetched: string[] = [];
  const updates: ResultUpdate[] = [];
  const monitor = new SubmissionResultMonitor({
    getSubmission: async id => { fetched.push(id); return { ...receipt, state: states.shift()! }; },
    getResult: async id => { fetched.push(id); return result; },
  }, Date.now, 1);
  t.after(() => monitor.cancel());
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: value => updates.push(value), onAuthenticationRequired: () => assert.fail("unexpected auth failure") });
  assert.equal(updates.length, 1);
  assert.equal(updates[0]?.result.score, undefined);
  await until(() => updates.at(-1)?.result.state === "published");
  assert.deepEqual(fetched, Array(5).fill("current"));
  assert.equal(updates.at(-1)?.result.score, 25);
  assert.equal(updates.at(-1)?.result.previousBest?.score, 100);
  assert.equal(updates.at(-1)?.watching, false);
});

test("replacing a view or resubmitting aborts old requests and discards late old scores", async t => {
  const old = deferred<SubmissionSummary>();
  let oldSignal: AbortSignal | undefined;
  const updates: string[] = [];
  const monitor = new SubmissionResultMonitor({
    getSubmission: async (id, signal) => {
      if (id === "old") { oldSignal = signal; return old.promise; }
      return { id, state: "published", sourceDigest: "source" };
    }, getResult: async () => result,
  }, Date.now, 1);
  t.after(() => monitor.cancel());
  const oldScope = monitor.begin();
  monitor.start(oldScope, { submission: { ...receipt, id: "old" }, isSessionCurrent: () => true,
    onUpdate: value => updates.push("old:" + value.result.state), onAuthenticationRequired: () => {} });
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: value => updates.push("new:" + value.result.state), onAuthenticationRequired: () => {} });
  await until(() => updates.includes("new:published"));
  old.resolve({ ...receipt, id: "old", state: "published" });
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(oldSignal?.aborted, true);
  assert.equal(oldScope.isCurrent(), false);
  assert.deepEqual(updates.filter(value => value.startsWith("old:")), ["old:queued"]);
});

test("an older history lookup scope cannot start or overwrite a newer submission's monitor", async t => {
  const updates: ResultUpdate[] = [];
  const monitor = new SubmissionResultMonitor({getSubmission: async () => ({...receipt, state: "published"}),
    getResult: async () => result});
  t.after(() => monitor.cancel());
  const historyScope = monitor.begin();
  monitor.start(monitor.begin(), {submission: receipt, isSessionCurrent: () => true,
    onUpdate: update => updates.push(update), onAuthenticationRequired: () => {}});
  monitor.start(historyScope, {submission: {...receipt, id: "history"}, isSessionCurrent: () => true,
    onUpdate: () => assert.fail("stale history overwrote the current result"), onAuthenticationRequired: () => {}});
  await until(() => updates.at(-1)?.result.state === "published");
  assert.equal(updates.at(-1)?.result.score, 25);
});

test("logging out or closing the view prevents a late result from restoring student data", async () => {
  for (const closing of [true, false]) {
    const pending = deferred<SubmissionSummary>();
    let active = true;
    const updates: ResultUpdate[] = [];
    const monitor = new SubmissionResultMonitor({ getSubmission: async () => pending.promise, getResult: async () => result });
    monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => active,
      onUpdate: value => updates.push(value), onAuthenticationRequired: () => {} });
    if (closing) monitor.cancel(); else active = false;
    pending.resolve({ ...receipt, state: "published" });
    await new Promise(resolve => setTimeout(resolve, 3));
    assert.equal(updates.length, 1);
    monitor.cancel();
  }
});

test("a bounded wait preserves the receipt and manual refresh resumes the same submission", async t => {
  let clock = 0;
  let done = false;
  const updates: ResultUpdate[] = [];
  const monitor = new SubmissionResultMonitor({
    getSubmission: async () => { clock += 11; return { ...receipt, state: done ? "published" : "queued" }; },
    getResult: async () => result,
  }, () => clock, 1, 10);
  t.after(() => monitor.cancel());
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: value => updates.push(value), onAuthenticationRequired: () => {} });
  await until(() => updates.at(-1)?.notice.includes("자동 확인을 쉬고") === true);
  assert.equal(updates.at(-1)?.watching, false);
  done = true; await monitor.refresh();
  assert.equal(updates.at(-1)?.result.score, 25);
});

test("temporary network failures retry, then stop with a recoverable message", async t => {
  let calls = 0;
  let online = false;
  const updates: ResultUpdate[] = [];
  const monitor = new SubmissionResultMonitor({
    getSubmission: async () => { calls++; if (!online) throw new ApiError("offline", 503); return { ...receipt, state: "published" }; },
    getResult: async () => result,
  }, Date.now, 1);
  t.after(() => monitor.cancel());
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: value => updates.push(value), onAuthenticationRequired: () => {} });
  await until(() => calls === 3 && updates.at(-1)?.watching === false);
  assert.match(updates.at(-1)!.notice, /접수는 유지.*결과 다시 확인/);
  online = true; await monitor.refresh();
  assert.equal(updates.at(-1)?.result.state, "published");
});

test("authentication failure resets authentication UI once and does not retry", async () => {
  let calls = 0; let auth = 0;
  const monitor = new SubmissionResultMonitor({
    getSubmission: async () => { calls++; throw new ApiError("expired", 401); }, getResult: async () => result,
  }, Date.now, 1);
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: () => {}, onAuthenticationRequired: () => { auth++; } });
  await until(() => auth === 1);
  await monitor.refresh();
  assert.equal(calls, 1);
});

test("unpublished results retain prior best and stop polling until explicitly refreshed", async t => {
  let publicResult = false;
  const updates: ResultUpdate[] = [];
  const monitor = new SubmissionResultMonitor({
    getSubmission: async () => ({ ...receipt, state: "graded", previousBest: result.previousBest }),
    getResult: async () => { if (!publicResult) throw new ApiError("not yet", 404, "result_not_available"); return result; },
  }, Date.now, 1);
  t.after(() => monitor.cancel());
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: value => updates.push(value), onAuthenticationRequired: () => {} });
  await until(() => updates.at(-1)?.watching === false);
  assert.equal(updates.at(-1)?.result.score, undefined);
  assert.equal(updates.at(-1)?.result.previousBest?.score, 100);
  publicResult = true; await monitor.refresh();
  assert.equal(updates.at(-1)?.result.score, 25);
});

test("pause aborts the current request and immediate refresh waits then safely restarts", async t => {
  let calls = 0;
  const updates: ResultUpdate[] = [];
  const monitor = new SubmissionResultMonitor({
    getSubmission: async (_id, signal) => {
      if (++calls > 1) return { ...receipt, state: "published" };
      return new Promise((_resolve, reject) => signal?.addEventListener("abort", () => reject(new RequestCancelledError()), { once: true }));
    }, getResult: async () => result,
  }, Date.now, 1);
  t.after(() => monitor.cancel());
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: value => updates.push(value), onAuthenticationRequired: () => {} });
  monitor.pause();
  assert.match(updates.at(-1)!.notice, /중지/);
  await monitor.refresh();
  assert.equal(calls, 2); assert.equal(updates.at(-1)?.result.score, 25);
});

test("mismatched receipt or source cannot replace the current result", async t => {
  const updates: ResultUpdate[] = [];
  const monitor = new SubmissionResultMonitor({
    getSubmission: async () => ({ ...receipt, id: "different", state: "published" }), getResult: async () => result,
  }, Date.now, 1);
  t.after(() => monitor.cancel());
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: value => updates.push(value), onAuthenticationRequired: () => {} });
  await until(() => updates.at(-1)?.watching === false);
  assert.equal(updates.at(-1)?.result.score, undefined);
  assert.match(updates.at(-1)!.notice, /접수번호 또는 제출 파일 정보가 다릅니다/);
});

test("a published receipt with a briefly unavailable result keeps polling until the current score arrives", async t => {
  let fetches = 0;
  const updates: ResultUpdate[] = [];
  const monitor = new SubmissionResultMonitor({
    getSubmission: async () => ({ ...receipt, state: "published" }),
    getResult: async () => {
      if (++fetches < 4) throw new ApiError("result being prepared", [404, 409, 425][fetches - 1]!);
      return result;
    },
  }, Date.now, 1);
  t.after(() => monitor.cancel());
  monitor.start(monitor.begin(), { submission: receipt, isSessionCurrent: () => true,
    onUpdate: value => updates.push(value), onAuthenticationRequired: () => {} });
  await until(() => updates.at(-1)?.result.state === "published");
  const pending = updates.filter(update => update.result.state === "result_pending");
  assert.equal(pending.length, 3);
  assert.ok(pending.every(update => update.watching && update.result.score === undefined));
  assert.equal(updates.at(-1)?.result.score, 25);
});
