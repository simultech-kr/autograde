import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, writeFile, symlink, rm } from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import { ApiError } from "../api";
import { ClassroomActivityReporter, CLASSROOM_HEARTBEAT_MS, classroomEnvironment, isClassroomFileUri, isManagedClassroomSave,
  type ClassroomEvent, type ClassroomScope, type ClassroomSharingStatus } from "../classroomActivity";

const flush = async (): Promise<void> => { for (let count = 0; count < 12; count += 1) await Promise.resolve(); };
class FakeClock {
  private timestamp = 0;
  private next = 0;
  public readonly timers = new Map<number, { at: number; callback: () => void }>();
  public now = (): number => this.timestamp;
  public setTimeout = (callback: () => void, delay: number): ReturnType<typeof setTimeout> => {
    const id = ++this.next;
    this.timers.set(id, { at: this.timestamp + delay, callback });
    return id as unknown as ReturnType<typeof setTimeout>;
  };
  public clearTimeout = (id: ReturnType<typeof setTimeout>): void => { this.timers.delete(id as unknown as number); };
  public async advance(milliseconds: number): Promise<void> {
    const until = this.timestamp + milliseconds;
    while (true) {
      const due = [...this.timers].sort((a, b) => a[1].at - b[1].at).find(([, timer]) => timer.at <= until);
      if (!due) break;
      this.timestamp = due[1].at;
      this.timers.delete(due[0]); due[1].callback(); await flush();
    }
    this.timestamp = until; await flush();
  }
}
function setup(sender: (event: ClassroomEvent, scope: ClassroomScope, signal: AbortSignal) => Promise<void> = async () => {}) {
  let scope: ClassroomScope | undefined;
  const calls: { event: ClassroomEvent; scope: ClassroomScope; signal: AbortSignal }[] = [];
  const statuses: ClassroomSharingStatus[] = [];
  const clock = new FakeClock();
  let session = 0;
  const reporter = new ClassroomActivityReporter({ reportClassroomEvent: async (event, current, signal) => {
    calls.push({ event, scope: current, signal }); await sender(event, current, signal);
  } }, () => scope, { os: "macos", ide: "autograde" }, status => statuses.push(status), clock, () => `session-${++session}`);
  return { reporter, calls, statuses, clock,
    connect: (generation = 1, baseUrl = "https://grade.example.edu", assignmentId = "a1") => {
      scope = { generation, baseUrl, assignmentId }; reporter.reconcile();
    },
    disconnect: () => { scope = undefined; reporter.reconcile(); },
  };
}

test("no authenticated managed workspace means no activity or heartbeat; active scope emits bounded metadata", async () => {
  const f = setup();
  f.reporter.reconcile(); f.reporter.record("file_saved", "a1");
  await f.clock.advance(100_000); assert.equal(f.calls.length, 0);
  f.connect(); await flush();
  assert.deepEqual(f.calls[0]?.event, { client_session_id: "session-1", sequence: 1, kind: "workspace_opened",
    assignment_id: "a1", environment: { os: "macos", ide: "autograde" } });
  await f.clock.advance(CLASSROOM_HEARTBEAT_MS);
  assert.equal(f.calls[1]?.event.kind, "heartbeat"); assert.equal(f.calls[1]?.event.sequence, 2);
  assert.equal(f.reporter.getStatus(), "sharing");
  f.disconnect(); await f.clock.advance(100_000);
  assert.equal(f.calls.length, 2); assert.equal(f.reporter.getStatus(), "inactive");
});

test("command context is exposed only after server presence registration and is revoked at scope boundaries", async () => {
  let release: (() => void) | undefined;
  const f = setup(async () => new Promise<void>(resolve => {release = resolve;}));
  f.connect(); assert.equal(f.reporter.getCommandContext(), undefined);
  release?.(); await flush();
  assert.equal(f.reporter.getCommandContext()?.clientSessionId, "session-1");
  f.connect(2); assert.equal(f.reporter.getCommandContext(), undefined);
  release?.(); await flush(); assert.equal(f.reporter.getCommandContext()?.generation, 2);
  f.disconnect(); assert.equal(f.reporter.getCommandContext(), undefined); f.reporter.dispose();
});

test("save bursts coalesce without paths or contents and another assignment cannot emit activity", async () => {
  const f = setup(); f.connect(); await flush();
  for (let n = 0; n < 1000; n += 1) f.reporter.record("file_saved", "a1");
  f.reporter.record("submission_started", "wrong");
  await f.clock.advance(4_999); assert.equal(f.calls.length, 1);
  await f.clock.advance(1); assert.equal(f.calls.length, 2);
  assert.equal(f.calls[1]?.event.kind, "file_saved");
  assert.deepEqual(Object.keys(f.calls[1]!.event).sort(), ["assignment_id", "client_session_id", "environment", "kind", "sequence"]);
  f.reporter.record("submission_started", "a1"); await flush();
  f.reporter.record("submission_finished", "a1"); await flush();
  assert.deepEqual(f.calls.slice(2).map(call => call.event.kind), ["submission_started", "submission_finished"]);
  f.reporter.dispose();
});

test("one request in flight; logout aborts it and a new account never receives old pending activities", async () => {
  let release: (() => void) | undefined;
  const f = setup(async (_event, scope) => { if (scope.generation === 1) await new Promise<void>(resolve => { release = resolve; }); });
  f.connect(); await flush();
  f.reporter.record("submission_started", "a1"); f.reporter.record("file_saved", "a1");
  await f.clock.advance(100_000); assert.equal(f.calls.length, 1);
  f.disconnect(); assert.equal(f.calls[0]?.signal.aborted, true);
  f.connect(2); await flush(); assert.equal(f.calls.length, 1);
  release?.(); await flush();
  assert.equal(f.calls.length, 2);
  assert.equal(f.calls[1]?.event.kind, "workspace_opened"); assert.equal(f.calls[1]?.event.sequence, 1);
  assert.equal(f.calls[1]?.event.client_session_id, "session-2");
  assert.equal(f.calls[1]?.scope.generation, 2);
  f.reporter.dispose();
});

test("origin and assignment changes reset session IDs even with the same account generation", async () => {
  const f = setup(); f.connect(); await flush();
  f.reporter.record("file_saved", "a1");
  f.connect(1, "https://other.example.edu"); await flush();
  f.connect(1, "https://other.example.edu", "a2"); await flush();
  await f.clock.advance(5_000);
  assert.equal(f.calls.length, 3);
  assert.deepEqual(f.calls.map(call => [call.event.client_session_id, call.event.sequence, call.event.kind]),
    [["session-1", 1, "workspace_opened"], ["session-2", 1, "workspace_opened"], ["session-3", 1, "workspace_opened"]]);
  f.reporter.dispose();
});

for (const status of [400, 401, 403, 404, 405, 409, 422]) {
  test(`HTTP ${status} stops optional telemetry until a new scope instead of repeatedly failing`, async () => {
    const f = setup(async () => { throw new ApiError("denied", status); });
    f.connect(); await flush(); assert.equal(f.reporter.getStatus(), "unavailable");
    for (let n = 0; n < 30; n += 1) f.reporter.record("submission_started", "a1");
    await f.clock.advance(600_000); assert.equal(f.calls.length, 1);
    f.connect(2); await flush(); assert.equal(f.calls.length, 2); f.reporter.dispose();
  });
}

test("rate limiting respects bounded Retry-After and user activity cannot bypass backoff", async () => {
  let failed = false;
  const f = setup(async () => { if (!failed) { failed = true; throw new ApiError("rate limited", 429, undefined, 60_000); } });
  f.connect(); await flush(); assert.equal(f.reporter.getStatus(), "retrying");
  f.reporter.record("submission_started", "a1");
  await f.clock.advance(59_999); assert.equal(f.calls.length, 1);
  await f.clock.advance(1); assert.equal(f.calls.length, 2); assert.equal(f.reporter.getStatus(), "sharing");
  f.reporter.dispose();
});

test("network errors back off, never escape as unhandled submission failures, and dispose clears timers", async () => {
  const f = setup(async () => { throw new ApiError("offline", 0, "network_error"); });
  f.connect(); await flush();
  await f.clock.advance(20_000); assert.equal(f.calls.length, 2);
  await f.clock.advance(39_999); assert.equal(f.calls.length, 2);
  await f.clock.advance(1); assert.equal(f.calls.length, 3);
  f.reporter.record("file_saved", "a1");
  f.reporter.dispose(); assert.equal(f.clock.timers.size, 0);
  await f.clock.advance(1_000_000); assert.equal(f.calls.length, 3);
});

test("only stable allowlisted environment labels are reported, never remote host names", () => {
  assert.deepEqual(classroomEnvironment("linux", "Autograde IDE", "wsl"), { os: "linux", ide: "autograde", remote_name: "wsl" });
  assert.deepEqual(classroomEnvironment("darwin", "Visual Studio Code", "user@private-host"), { os: "macos", ide: "vscode" });
  assert.deepEqual(classroomEnvironment("win32", "Autograde IDE Dev"), { os: "windows", ide: "autograde" });
  assert.equal(classroomEnvironment("unknown", "Autograde IDE"), undefined);
});

test("WSL and remote file saves must match the connected folder URI authority", () => {
  const remote = { scheme: "vscode-remote", authority: "wsl+Ubuntu" };
  assert.equal(isClassroomFileUri(remote, remote), true);
  assert.equal(isClassroomFileUri({ scheme: "file" }, { scheme: "file" }), true);
  assert.equal(isClassroomFileUri({ scheme: "vscode-remote", authority: "ssh-remote+private" }, remote), false);
  assert.equal(isClassroomFileUri({ scheme: "file" }, remote), false);
  assert.equal(isClassroomFileUri({ scheme: "untitled" }, { scheme: "untitled" }), false);
  assert.equal(isClassroomFileUri({ scheme: "file" }, undefined), false);
});

test("save scope rejects unrelated files, hidden state, non-files and symlink escapes", async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), "autograde-classroom-test-"));
  try {
    const root = path.join(directory, "assignment"); await mkdir(root);
    const source = path.join(root, "main.cpp"); await writeFile(source, "int main(){}\n");
    const outside = path.join(directory, "private.cpp"); await writeFile(outside, "private");
    const secret = path.join(root, ".env"); await writeFile(secret, "secret");
    await symlink(outside, path.join(root, "link.cpp"));
    assert.equal(await isManagedClassroomSave(root, source), true);
    for (const file of [outside, secret, path.join(root, "link.cpp"), root, path.join(root, "absent.cpp")]) {
      assert.equal(await isManagedClassroomSave(root, file), false);
    }
  } finally { await rm(directory, { recursive: true, force: true }); }
});
