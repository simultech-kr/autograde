import assert from "node:assert/strict";
import test from "node:test";
import { ApiError } from "../api";
import { ClassroomActionError, ClassroomCommands, parseClassroomCommands, type ClassroomCommand,
  type ClassroomCommandContext, type ClassroomCommandResponse } from "../classroomCommands";

const flush = async () => {for (let i = 0; i < 30; i++) await Promise.resolve();};
const start = Date.parse("2026-10-25T01:00:00Z");
class Clock {
  public time = start; private next = 0;
  public timers = new Map<number, {at: number; run: () => void}>();
  public now = () => this.time;
  public setTimeout = (run: () => void, ms: number) => {
    const id = ++this.next; this.timers.set(id, {at: this.time + ms, run}); return id as unknown as ReturnType<typeof setTimeout>;
  };
  public clearTimeout = (id: ReturnType<typeof setTimeout>) => {this.timers.delete(id as unknown as number);};
  public async advance(ms: number) {
    const end = this.time + ms;
    for (let i = 0; i < 1000; i++) {
      const next = [...this.timers].sort((a, b) => a[1].at - b[1].at).find(([, t]) => t.at <= end);
      if (!next) {this.time = end; await flush(); return;}
      this.time = next[1].at; this.timers.delete(next[0]); next[1].run(); await flush();
    }
    throw new Error("timer busy loop");
  }
}
const command = (changes: Partial<ClassroomCommand> = {}): ClassroomCommand => ({command_id: "cmd_test001", kind: "notice",
  assignment_id: "a1", message: "안내", status: "received", created_by: "teacher", expires_at: new Date(start + 600000).toISOString(), ...changes});
function fixture() {
  const clock = new Clock();
  let context: ClassroomCommandContext | undefined = {generation: 1, baseUrl: "https://test.invalid", assignmentId: "a1", clientSessionId: "client-test-0001", workspaceKey: "/local/root"};
  const state = {commands: [command()], responses: [] as ClassroomCommandResponse[], prompts: 0, actions: 0, polls: 0,
    statuses: [] as string[], allow: true, failPoll: false, failAccepted: false, failFinal: false,
    duringPrompt: async () => {}, duringAction: async (_guard: () => void) => {},
    result: {status: "completed", result_code: "notice_acknowledged"} as ClassroomCommandResponse,
    signals: [] as AbortSignal[], duringPoll: async () => {}};
  const runner = new ClassroomCommands({
    pollClassroomCommands: async (_scope, signal) => {state.polls++; state.signals.push(signal); await state.duringPoll(); if (state.failPoll) throw new ApiError("offline", 503);
      return state.commands.filter(row => ["received", "accepted"].includes(row.status));},
    respondClassroomCommand: async (id, response) => {
      state.responses.push(response);
      if (response.status === "accepted") {
        state.commands = state.commands.map(row => row.command_id === id ? {...row, status: "accepted"} : row);
        if (state.failAccepted) throw new ApiError("accepted response lost", 503);
      } else {
        if (state.failFinal) throw new ApiError("result response lost", 503);
        state.commands = state.commands.filter(row => row.command_id !== id);
      }
    },
  }, () => context, async () => {state.prompts++; await state.duringPrompt(); return state.allow;},
  async (_command, guard) => {state.actions++; guard(); await state.duringAction(guard); guard(); return state.result;},
  value => state.statuses.push(value), clock);
  return {runner, clock, state, setContext(value: ClassroomCommandContext | undefined) {context = value; runner.reconcile();}, getContext: () => context};
}

test("polling requires a registered current workspace and never sends requests while logged out", async () => {
  const f = fixture(); f.setContext(undefined); await f.clock.advance(30000);
  assert.equal(f.state.polls, 0); assert.equal(f.state.prompts, 0); f.runner.dispose();
});
test("explicit approval and server acceptance precede execution and terminal result", async () => {
  const f = fixture(); f.runner.reconcile(); await f.clock.advance(0);
  assert.equal(f.state.prompts, 1); assert.equal(f.state.actions, 1);
  assert.deepEqual(f.state.responses.map(x => x.status), ["accepted", "completed"]);
  assert.match(f.state.statuses.join(" "), /학생 확인 대기/);
  await f.clock.advance(30000); assert.equal(f.state.actions, 1); f.runner.dispose();
});
test("student decline does not acknowledge acceptance or execute an action", async () => {
  const f = fixture(); f.state.allow = false; f.runner.reconcile(); await f.clock.advance(0);
  assert.deepEqual(f.state.responses, [{status: "declined", result_code: "student_declined"}]); assert.equal(f.state.actions, 0); f.runner.dispose();
});
for (const boundary of ["logout", "account", "origin", "assignment", "folder"] as const) {
  test(`changing ${boundary} during the approval dialog never authorizes the old request`, async () => {
    const f = fixture(); f.state.duringPrompt = async () => {
      const old = f.getContext()!;
      f.setContext(boundary === "logout" ? undefined : {...old, ...(boundary === "account" ? {generation: 2} :
        boundary === "origin" ? {baseUrl: "https://other.invalid"} : boundary === "assignment" ? {assignmentId: "a2"} : {workspaceKey: "/other/root"})});
      f.state.commands = [];
    };
    f.runner.reconcile(); await f.clock.advance(0);
    assert.equal(f.state.actions, 0); assert.deepEqual(f.state.responses, []); f.runner.dispose();
  });
}
test("expired requests are not displayed, and expiration while the dialog is open blocks execution", async () => {
  for (const during of [false, true]) {
    const f = fixture();
    if (during) f.state.duringPrompt = async () => {f.clock.time += 700000;}; else f.clock.time += 700000;
    f.runner.reconcile(); await f.clock.advance(0);
    assert.equal(f.state.prompts, during ? 1 : 0); assert.equal(f.state.actions, 0); assert.deepEqual(f.state.responses, []); f.runner.dispose();
  }
});
test("an accepted acknowledgement lost in transit never triggers execution or a new prompt", async () => {
  const f = fixture(); f.state.failAccepted = true; f.runner.reconcile(); await f.clock.advance(0);
  assert.equal(f.state.actions, 0); await f.clock.advance(30000);
  assert.equal(f.state.prompts, 1); assert.equal(f.state.actions, 0); assert.equal(f.state.responses.length, 1); f.runner.dispose();
});
test("a lost terminal acknowledgement is retried without repeating the approved action", async () => {
  const f = fixture(); f.state.failFinal = true; f.runner.reconcile(); await f.clock.advance(0);
  assert.equal(f.state.actions, 1); f.state.failFinal = false; await f.clock.advance(10000);
  assert.equal(f.state.actions, 1); assert.equal(f.state.prompts, 1);
  assert.deepEqual(f.state.responses.map(row => row.status), ["accepted", "completed", "completed"]); f.runner.dispose();
});
test("accepted requests from a previous host process are not automatically executed again", async () => {
  const f = fixture(); f.state.commands = [command({status: "accepted"})]; f.runner.reconcile(); await f.clock.advance(30000);
  assert.equal(f.state.prompts, 0); assert.equal(f.state.actions, 0); f.runner.dispose();
});
test("cancelled and failed operations are reported without exposing error paths or source text", async () => {
  for (const error of [new ClassroomActionError("cancelled"), new Error("private /student/main.cpp")]) {
    const f = fixture(); f.state.duringAction = async () => {throw error;}; f.runner.reconcile(); await f.clock.advance(0);
    assert.deepEqual(f.state.responses[1], {status: "failed", result_code: error instanceof ClassroomActionError ? "cancelled" : "action_failed"}); f.runner.dispose();
  }
});
test("save-submit cannot complete without a new submission identifier", async () => {
  const f = fixture(); f.state.commands = [command({kind: "save_submit"})];
  f.state.result = {status: "completed", result_code: "submission_received"}; f.runner.reconcile(); await f.clock.advance(0);
  assert.equal(f.state.responses[1]?.status, "failed"); f.runner.dispose();
});
test("cross-assignment responses are rejected without presenting or running commands", async () => {
  const f = fixture(); f.state.commands = [command({assignment_id: "a2"})]; f.runner.reconcile(); await f.clock.advance(0);
  assert.equal(f.state.prompts, 0); assert.equal(f.state.actions, 0); f.runner.dispose();
});
test("only one prompt is active at a time and normal lifecycle disposal aborts the request", async () => {
  const f = fixture(); let finish: (() => void) | undefined;
  f.state.commands = [command(), command({command_id: "cmd_test002"})];
  f.state.duringPrompt = () => new Promise<void>(resolve => {finish = resolve;});
  f.runner.reconcile(); await f.clock.advance(0); f.runner.reconcile(); await f.clock.advance(30000);
  assert.equal(f.state.prompts, 1); assert.equal(f.state.polls, 1);
  f.runner.dispose(); finish?.(); await flush(); assert.equal(f.state.actions, 0); assert.equal(f.state.signals[0]?.aborted, true);
});
test("scope is rechecked by action guard before writing after a wait", async () => {
  const f = fixture(); f.state.duringAction = async guard => {f.setContext(undefined); assert.throws(guard, /scope_changed/);};
  f.runner.reconcile(); await f.clock.advance(0);
  assert.deepEqual(f.state.responses, [{status: "accepted"}]); f.runner.dispose();
});
test("command parser rejects unknown execution kinds, malformed identifiers, oversized messages, and duplicate IDs", () => {
  assert.equal(parseClassroomCommands({commands: [command()], server_time: new Date(start).toISOString()}).length, 1);
  for (const value of [null, [], {commands: [command({kind: "shell" as never})]}, {commands: [command({command_id: "../run"})]},
    {commands: [command({message: "x".repeat(1001)})]}, {commands: [command({assignment_id: null, kind: "save_submit"})]},
    {commands: [command(), command()]}, {commands: [command({expires_at: "tomorrow"})]}]) assert.throws(() => parseClassroomCommands(value));
});

test("remaining request lifetime uses server time, caps TTL, and shrinks on subsequent polls", () => {
  const local = start + 86400000;
  const initial = parseClassroomCommands({commands: [command()], server_time: new Date(start + 100000).toISOString()}, local)[0]!;
  assert.equal(initial.localDeadline, local + 500000);
  const later = parseClassroomCommands({commands: [command()], server_time: new Date(start + 120000).toISOString()}, local + 20000)[0]!;
  assert.equal(later.localDeadline, initial.localDeadline);
  const old = parseClassroomCommands({commands: [command()], server_time: new Date(start + 700000).toISOString()}, local)[0]!;
  assert.equal(old.localDeadline, local);
  const far = parseClassroomCommands({commands: [command({expires_at: new Date(start + 86400000).toISOString()})], server_time: new Date(start).toISOString()}, local)[0]!;
  assert.equal(far.localDeadline, local + 600000);
  assert.throws(() => parseClassroomCommands({commands: [], server_time: "invalid"}));
});

test("expiry aborts an open student confirmation and cannot perform a late action", async () => {
  const f = fixture(); let aborted = false;
  const runner = new ClassroomCommands({
    pollClassroomCommands: async () => [command()], respondClassroomCommand: async () => {throw new Error("must not respond");},
  }, f.getContext, async (_command, signal) => new Promise<boolean>(resolve => {
    signal.addEventListener("abort", () => {aborted = true; resolve(true);}, {once: true});
  }), async () => {throw new Error("must not execute");}, message => f.state.statuses.push(message), f.clock);
  runner.reconcile(); await f.clock.advance(600001);
  assert.equal(aborted, true); assert.match(f.state.statuses.join(" "), /만료/); runner.dispose(); f.runner.dispose();
});

test("a receipt rejected as older than the request becomes submission-unconfirmed, without resubmitting", async () => {
  const f = fixture(); const replies: ClassroomCommandResponse[] = []; let actions = 0;
  const runner = new ClassroomCommands({
    pollClassroomCommands: async () => [command({kind: "save_submit"})],
    respondClassroomCommand: async (_id, response) => {replies.push(response); if (response.status === "completed") throw new ApiError("old receipt", 409);},
  }, f.getContext, async () => true, async () => {actions++; return {status: "completed", result_code: "submission_received", result: {submission_id: "old-receipt"}};}, () => {}, f.clock);
  runner.reconcile(); await f.clock.advance(30000);
  assert.equal(actions, 1); assert.deepEqual(replies.map(row => [row.status, row.result_code]),
    [["accepted", undefined], ["completed", "submission_received"], ["failed", "submission_unconfirmed"]]);
  runner.dispose(); f.runner.dispose();
});
