import assert from "node:assert/strict";
import test from "node:test";
import type { AutogradeClient } from "../api";
import { parseAcceptedAssignments } from "../acceptedAssignmentCatalog";

interface ModuleLoader { _load(request: string, parent: unknown, main: boolean): unknown }
const loader = require("node:module") as ModuleLoader;
const original = loader._load;
let pick = 0;
let choice: string | undefined;
let afterPick: (() => void) | undefined;
let messages: string[] = [];
const stub = { window: {
  showQuickPick: async (items: unknown[]) => { afterPick?.(); return items[pick]; },
  showInformationMessage: async (message: string) => { messages.push(message); return choice; },
} };
loader._load = function(name, parent, main) { return name === "vscode" ? stub : original.call(this, name, parent, main); };
const { showAcceptedAssignments } = require("../acceptedAssignments") as typeof import("../acceptedAssignments");
loader._load = original;
const current = { assignment_id: "a", title: "A", course_key: "course", accepted_at: "2026-09-29T00:00:00Z",
  current_session_authorized: true, can_reconnect: true, can_submit: true, status: "available" };
const other = { ...current, assignment_id: "b", title: "B", current_session_authorized: false, can_submit: false };

test("catalog is metadata only and does not normalize history entries into authorized assignments", () => {
  const [entry] = parseAcceptedAssignments({ assignments: [{ ...other, starter_url: "https://evil.test", repository: {}, latest_submission: {} }] });
  assert.deepEqual(Object.keys(entry!).sort(), ["assignmentId", "title", "courseKey", "acceptedAt", "currentSessionAuthorized", "canReconnect", "canSubmit", "status"].sort());
  assert.equal(entry?.currentSessionAuthorized, false);
  for (const malformed of [null, {}, { assignments: [current, current] },
    { assignments: [{ ...other, can_submit: true }] }, { assignments: [{ ...other, status: "closed" }] },
    { assignments: [{ ...current, current_session_authorized: "true" }] }]) {
    assert.throws(() => parseAcceptedAssignments(malformed), /올바르지/);
  }
});

test("current selection opens existing files; other available assignment requests fresh claim only after confirmation", async () => {
  const events: string[] = [];
  const client = { getBaseUrl: () => "https://grade.example.edu", tokens: { getSessionGeneration: () => 1 },
    getAcceptedAssignmentCatalog: async () => parseAcceptedAssignments({ assignments: [current, other] }) } as unknown as AutogradeClient;
  pick = 0; choice = undefined; afterPick = undefined; messages = [];
  const show = () => showAcceptedAssignments(client, async id => { events.push(`open:${id}`); }, async () => { events.push("claim"); });
  await show(); assert.deepEqual(events, ["open:a"]);
  pick = 1; await show(); assert.deepEqual(events, ["open:a"]);
  choice = "새 수령 코드 입력"; await show(); assert.deepEqual(events, ["open:a", "claim"]);
});

test("closed history and changed sessions cannot reconnect or open old student metadata", async () => {
  let generation = 1;
  const events: string[] = [];
  const client = { getBaseUrl: () => "https://grade.example.edu", tokens: { getSessionGeneration: () => generation },
    getAcceptedAssignmentCatalog: async () => parseAcceptedAssignments({ assignments: [{ ...other, status: "closed", can_reconnect: false }] }) } as unknown as AutogradeClient;
  pick = 0; choice = "새 수령 코드 입력"; afterPick = undefined; messages = [];
  const show = () => showAcceptedAssignments(client, async () => { events.push("open"); }, async () => { events.push("claim"); });
  await show(); assert.deepEqual(events, []); assert.match(messages[0]!, /다시 연결할 수 없습니다/);
  afterPick = () => { generation += 1; }; messages = [];
  await show(); assert.deepEqual(events, []); assert.deepEqual(messages, []);
  afterPick = undefined;
});
