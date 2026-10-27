import assert from "node:assert/strict";
import test from "node:test";
import { LearningCheckpointAutosaver, learningCheckpointLabel, parseLearningCheckpoint, parseLearningCheckpoints,
  type LearningCheckpointScope, type LearningCheckpoint, type LearningCheckpointStatus } from "../learningCheckpoint";
import type { SubmissionBundle } from "../bundle";

const scope: LearningCheckpointScope = {baseUrl: "https://school.test", generation: 1, assignmentId: "a1", rootPath: "/work/a1", scheme: "file", authority: ""};
const bundle = (letter: string): SubmissionBundle => ({archive: Buffer.from(letter), sha256: letter.repeat(64), fileCount: 1, sourceBytes: 1});
const receipt = (hash: string): LearningCheckpoint => ({checkpoint_id: "cp_1", assignment_id: "a1", archive_sha256: hash,
  created_at: "2026-10-26T10:00:00Z", file_count: 1, source_bytes: 1, version: 1});
async function settle() { for (let n = 0; n < 30; n++) await Promise.resolve(); }
class FakeClock {
  now = 0; id = 0; timers = new Map<number, {at: number; callback: () => void}>();
  setTimeout = (callback: () => void, ms: number): ReturnType<typeof setTimeout> => { const id = ++this.id; this.timers.set(id, {at: this.now + ms, callback}); return id as unknown as ReturnType<typeof setTimeout>; };
  clearTimeout = (timer: ReturnType<typeof setTimeout>) => { this.timers.delete(timer as unknown as number); };
  async advance(ms: number) {
    const end = this.now + ms;
    while (true) { const next = [...this.timers.entries()].sort((a, b) => a[1].at - b[1].at)[0];
      if (!next || next[1].at > end) break;
      this.now = next[1].at; this.timers.delete(next[0]); next[1].callback(); await settle(); }
    this.now = end; await settle();
  }
}
function setup(shared = false) {
  let current: LearningCheckpointScope | undefined = scope, content = bundle("a");
  const uploads: {scope: LearningCheckpointScope; hash: string; signal: AbortSignal; key: string}[] = [], captures: boolean[] = [];
  const statuses: LearningCheckpointStatus[] = [], time = new FakeClock();
  let upload: (signal: AbortSignal) => Promise<void> = async () => {};
  const saver = new LearningCheckpointAutosaver({currentScope: () => current,
    capture: async (_scope, saveDirty) => { captures.push(saveDirty); return content; },
    upload: async (scope, bundle, signal, key) => { uploads.push({scope, hash: bundle.sha256, signal, key}); await upload(signal); return receipt(bundle.sha256); },
    onStatus: status => statuses.push(status), baselineOnlyUntilChange: shared,
  }, time);
  return {saver, uploads, captures, statuses, time, setScope: (next?: LearningCheckpointScope) => {current = next;},
    setContent: (letter: string) => {content = bundle(letter);}, setUpload: (next: typeof upload) => {upload = next;}};
}

test("shared starter baseline is not a receipt; edited code saves once after30s without creating submissions", async () => {
  const f = setup(true); f.saver.reconcile(); await settle();
  assert.deepEqual(f.captures, [false]); assert.equal(f.saver.getStatus().state, "waiting");
  await f.time.advance(30_000); assert.equal(f.uploads.length, 0);
  f.setContent("b"); await f.time.advance(30_000);
  assert.equal(f.uploads.length, 1); assert.equal(f.uploads[0]?.hash, "b".repeat(64));
  assert.equal(f.saver.getStatus().state, "saved"); assert.ok(f.saver.getStatus().savedAt);
  await f.time.advance(300_000); assert.equal(f.uploads.length, 1); f.saver.dispose();
});

test("ordinary reopened work uploads on first interval and explicit save stores unchanged shared starter", async () => {
  const f = setup(); f.saver.reconcile(); await settle(); await f.time.advance(30_000);
  assert.equal(f.uploads.length, 1); f.saver.dispose();
  const shared = setup(true); shared.saver.reconcile(); await settle();
  assert.equal(await shared.saver.saveNow(), true); assert.equal(shared.uploads.length, 1); shared.saver.dispose();
});

test("three failed uploads are bounded per snapshot; newer work and deliberate retry recover", async () => {
  const f = setup(); f.setUpload(async () => {throw new Error("offline");});
  f.saver.reconcile(); await settle(); await f.time.advance(1_000_000);
  assert.equal(f.uploads.length, 3); assert.equal(new Set(f.uploads.map(item => item.key)).size, 1); assert.equal(f.saver.getStatus().state, "failed");
  assert.equal(f.saver.getStatus().savedAt, undefined);
  f.setUpload(async () => {}); f.setContent("c"); await f.time.advance(120_000);
  assert.equal(f.uploads.length, 4); assert.notEqual(f.uploads[3]?.key, f.uploads[0]?.key); assert.equal(f.saver.getStatus().state, "saved");
  f.setUpload(async () => {throw {status: 404};}); f.setContent("d"); await f.time.advance(30_000);
  assert.equal(f.saver.getStatus().state, "unavailable"); await f.time.advance(1_000_000); assert.equal(f.uploads.length, 5);
  f.setUpload(async () => {}); assert.equal(await f.saver.saveNow(), true); assert.equal(f.uploads.length, 6); f.saver.dispose();
});

test("switching auth aborts stale upload, never overlaps flights and never displays its receipt", async () => {
  const f = setup(); let release!: () => void;
  f.setUpload(() => new Promise<void>(resolve => {release = resolve;}));
  f.saver.reconcile(); await settle(); await f.time.advance(30_000);
  assert.equal(f.uploads.length, 1);
  f.setScope({...scope, generation: 2, rootPath: "/work/other"}); f.saver.reconcile();
  assert.equal(f.uploads[0]?.signal.aborted, true); await f.time.advance(300_000); assert.equal(f.uploads.length, 1);
  f.setUpload(async () => {}); release(); await settle();
  assert.equal(f.saver.getStatus().savedAt, undefined); // Old receipt cannot cross the authentication boundary.
  await f.time.advance(30_000); assert.equal(f.uploads.length, 2);
  assert.equal(f.uploads[1]?.scope.generation, 2); f.saver.dispose();
});

test("auth loss during capture never uploads; stale saveNow cannot target replacement student", async () => {
  let selected: LearningCheckpointScope | undefined = scope, release!: () => void, count = 0;
  const saver = new LearningCheckpointAutosaver({currentScope: () => selected,
    capture: async () => {await new Promise<void>(resolve => {release = resolve;}); return bundle("a");},
    upload: async () => {count++; return receipt("a".repeat(64));}, onStatus: () => {},
  }, new FakeClock());
  saver.reconcile(); await settle(); const pending = saver.saveNow();
  selected = undefined; saver.reconcile(); release(); await pending; assert.equal(count, 0);
  assert.equal(saver.getStatus().state, "inactive"); saver.dispose();
});

test("parsers reject mismatched assignments, unsafe IDs and integrity metadata", () => {
  assert.equal(parseLearningCheckpoint(receipt("a".repeat(64)), "a1").version, 1);
  for (const patch of [{assignment_id: "other"}, {checkpoint_id: "../x"}, {archive_sha256: "no"}, {created_at: "tomorrow"}, {file_count: 5001}]) {
    assert.throws(() => parseLearningCheckpoint({...receipt("a".repeat(64)), ...patch}, "a1"));
  }
  assert.throws(() => parseLearningCheckpoints({checkpoints: Array(101).fill(receipt("a".repeat(64)))}, "a1"));
  assert.match(learningCheckpointLabel({state: "retrying", savedAt: "2026-10-26T10:00:00Z"}), /마지막 성공/);
  assert.match(learningCheckpointLabel({state: "waiting"}), /아직 서버 저장 안 됨/);
});

test("a skipped forced capture never reuses an older successful save as a current receipt", async () => {
  let skip = false;
  const saver = new LearningCheckpointAutosaver({currentScope: () => scope,
    capture: async () => skip ? undefined : bundle("a"), upload: async () => receipt("a".repeat(64)), onStatus: () => {},
  }, new FakeClock());
  assert.equal(await saver.saveNow(), true); skip = true;
  assert.equal(await saver.saveNow(), false); assert.equal(saver.getStatus().state, "waiting");
  assert.ok(saver.getStatus().savedAt); saver.dispose();
});

test("edits observed before a shared starter baseline finishes still trigger a server save", async () => {
  const f = setup(true); f.saver.reconcile(); f.saver.recordEdit(); await settle();
  await f.time.advance(30_000); assert.equal(f.uploads.length, 1); f.saver.dispose();
});

test("server semantic dedup receipt may retain an earlier gzip digest after acknowledging received bytes", async () => {
  const saver = new LearningCheckpointAutosaver({currentScope: () => scope, capture: async () => bundle("b"),
    upload: async () => ({...receipt("a".repeat(64)), received_archive_sha256: "b".repeat(64)}), onStatus: () => {},
  }, new FakeClock());
  assert.equal(await saver.saveNow(), true); saver.dispose();
});

test("typing during upload keeps last successful snapshot time but never claims newer edits are saved", async () => {
  const f = setup(); let release!: () => void;
  f.setUpload(() => new Promise<void>(resolve => {release = resolve;}));
  f.saver.reconcile(); await settle(); const saving = f.saver.saveNow(); await settle();
  f.saver.recordEdit(); f.setContent("b"); release(); assert.equal(await saving, false);
  assert.equal(f.saver.getStatus().state, "waiting"); assert.ok(f.saver.getStatus().savedAt);
  f.setUpload(async () => {}); await f.time.advance(30_000); assert.equal(f.saver.getStatus().state, "saved"); f.saver.dispose();
});

test("edits during a deferred capture cannot be counted as included in the acknowledged snapshot", async () => {
  let capture: () => Promise<SubmissionBundle> = async () => bundle("a"), release!: (bundle: SubmissionBundle) => void;
  const time = new FakeClock();
  const saver = new LearningCheckpointAutosaver({currentScope: () => scope, capture: () => capture(),
    upload: async (_scope, body) => receipt(body.sha256), onStatus: () => {},
  }, time);
  saver.reconcile(); await settle();
  capture = () => new Promise(resolve => {release = resolve;});
  const saving = saver.saveNow(); await settle(); saver.recordEdit(); release(bundle("a"));
  assert.equal(await saving, false); assert.equal(saver.getStatus().state, "waiting"); assert.ok(saver.getStatus().savedAt);
  // Even an unchanged disk bundle must not hide a new edit arriving while it was captured.
  await time.advance(30_000); saver.recordEdit(); release(bundle("a")); await settle();
  assert.equal(saver.getStatus().state, "waiting");
  capture = async () => bundle("b"); await time.advance(30_000); assert.equal(saver.getStatus().state, "saved");
  saver.dispose();
});
