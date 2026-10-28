import { ContractError, courseKey, type PreviousBest } from "./instructor.ts";

export type ConnectionState = "online" | "stale" | "offline" | "never_connected";
export const commandKinds = ["notice", "open_assignment", "sync_materials", "check_environment", "save_submit"] as const;
export type CommandKind = typeof commandKinds[number];
export const commandStatuses = ["queued", "received", "accepted", "completed", "declined", "failed", "expired"] as const;
export type ClassroomCommand = {
  command_id: string; kind: CommandKind; assignment_id: string | null; message: string;
  status: typeof commandStatuses[number]; created_by: string; created_at: string; expires_at: string;
  received_at: string | null; accepted_at: string | null; finished_at: string | null;
  result_code: string | null;
  result: { submission_id?: string; checks?: Record<string, string> } | null;
};
export type ClassroomActivity = { kind: string; received_at: string; assignment_id: string | null };
export type ClassroomSubmission = {
  submission_id: string; received_at: string; state: string; score: number | null;
  max_score: number | null; code_version: string | null; delivery_mode: string;
};
export type ClassroomStudent = {
  student_key: string; student_name: string;
  commands_version?: string;
  command_attention?: boolean;
  connection: { state: ConnectionState; last_seen_at: string | null; environment: { os: string; remote_name: string; ide: string } | null };
  activity: ClassroomActivity | null;
  submission: { state: string; submitted: boolean; latest: ClassroomSubmission | null; previous_best: PreviousBest | null };
};
type Metadata = { contract_version: "1"; request_id: string; course_key: string; version: string; server_time: string; selected_assignment_id: string | null };
export type ClassroomSnapshot = Metadata & {
  assignments: { assignment_id: string; title: string; assignment_key: string; release_id: string; delivery_mode: string }[];
  summary: { total: number; online: number; stale: number; offline: number; never_connected: number; submitted: number; pending: number };
  students: ClassroomStudent[];
};
export type ClassroomDetail = Metadata & { student: ClassroomStudent & {
  activities: ClassroomActivity[]; submissions: ClassroomSubmission[];
  code_storage: { state: string; verified: boolean; message: string };
  analysis: { workspace_opened: number; file_saved: number; submission_started: number; submission_finished: number; window: string; note: string };
  controls: { supported: boolean; reason: string; actions: CommandKind[] };
  commands: ClassroomCommand[];
} };
type Obj = Record<string, unknown>;
const obj = (v: unknown): Obj => { if (!v || typeof v !== "object" || Array.isArray(v)) throw new ContractError(); return v as Obj; };
const str = (v: unknown): string => { if (typeof v !== "string") throw new ContractError(); return v; };
const bool = (v: unknown): boolean => { if (typeof v !== "boolean") throw new ContractError(); return v; };
const num = (v: unknown): number => { if (typeof v !== "number" || !Number.isFinite(v) || v < 0) throw new ContractError(); return v; };
const count = (v: unknown): number => { const n = num(v); if (!Number.isSafeInteger(n)) throw new ContractError(); return n; };
const nullable = <T>(v: unknown, parse: (x: unknown) => T): T | null => v === null ? null : parse(v);
const list = <T>(v: unknown, parse: (x: unknown) => T): T[] => { if (!Array.isArray(v)) throw new ContractError(); return v.map(parse); };
const time = (v: unknown): string => { const s = str(v); if (!/^\d{4}-\d\d-\d\dT/.test(s) || !Number.isFinite(Date.parse(s))) throw new ContractError(); return s; };
function meta(v: Obj): Metadata {
  if (v.contract_version !== "1") throw new ContractError();
  const version = str(v.version); if (!/^[a-f0-9]{64}$/.test(version)) throw new ContractError();
  return { contract_version: "1", request_id: str(v.request_id), course_key: courseKey(v.course_key), version,
    server_time: time(v.server_time), selected_assignment_id: nullable(v.selected_assignment_id, str) };
}
function activity(value: unknown): ClassroomActivity { const v = obj(value); return { kind: str(v.kind), received_at: time(v.received_at), assignment_id: nullable(v.assignment_id, str) }; }
function score(v: Obj) {
  const score = nullable(v.score, num), max_score = nullable(v.max_score, num);
  if ((score === null) !== (max_score === null) || (score !== null && max_score !== null && (max_score <= 0 || score > max_score))) throw new ContractError();
  return { score, max_score };
}
function submission(value: unknown): ClassroomSubmission {
  const v = obj(value); return { submission_id: str(v.submission_id), received_at: time(v.received_at), state: str(v.state), ...score(v), code_version: nullable(v.code_version, str), delivery_mode: str(v.delivery_mode) };
}
function previous(value: unknown): PreviousBest {
  const v = obj(value), scored = score(v); if (scored.score === null || scored.max_score === null) throw new ContractError();
  return { submission_id: str(v.submission_id), received_at: time(v.received_at), score: scored.score, max_score: scored.max_score };
}
function student(value: unknown): ClassroomStudent {
  const v = obj(value), c = obj(v.connection), s = obj(v.submission), state = str(c.state);
  if (!["online", "stale", "offline", "never_connected"].includes(state)) throw new ContractError();
  const commandsVersion = v.commands_version === undefined ? undefined : str(v.commands_version);
  if (commandsVersion !== undefined && !/^[a-f0-9]{64}$/.test(commandsVersion)) throw new ContractError();
  return { student_key: str(v.student_key), student_name: str(v.student_name), commands_version: commandsVersion,
    command_attention: v.command_attention === undefined ? false : bool(v.command_attention),
    connection: { state: state as ConnectionState, last_seen_at: nullable(c.last_seen_at, time), environment: nullable(c.environment, value => {
      const e = obj(value); return { os: str(e.os), remote_name: str(e.remote_name), ide: str(e.ide) };
    }) }, activity: nullable(v.activity, activity),
    submission: { state: str(s.state), submitted: bool(s.submitted), latest: nullable(s.latest, submission), previous_best: nullable(s.previous_best, previous) } };
}
export function parseClassroom(value: unknown): ClassroomSnapshot {
  const v = obj(value), s = obj(v.summary), students = list(v.students, student);
  if (new Set(students.map(s => s.student_key)).size !== students.length) throw new ContractError();
  const summary = { total: count(s.total), online: count(s.online), stale: count(s.stale), offline: count(s.offline), never_connected: count(s.never_connected), submitted: count(s.submitted), pending: count(s.pending) };
  if (summary.total !== students.length) throw new ContractError();
  return { ...meta(v), summary, students, assignments: list(v.assignments, value => { const a = obj(value); return { assignment_id: str(a.assignment_id), title: str(a.title), assignment_key: str(a.assignment_key), release_id: str(a.release_id), delivery_mode: str(a.delivery_mode) }; }) };
}
export function parseClassroomDetail(value: unknown): ClassroomDetail {
  const v = obj(value), s = obj(v.student), c = obj(s.code_storage), a = obj(s.analysis), controls = obj(s.controls);
  return { ...meta(v), student: { ...student(s), activities: list(s.activities, activity), submissions: list(s.submissions, submission),
    code_storage: { state: str(c.state), verified: bool(c.verified), message: str(c.message) },
    analysis: { workspace_opened: count(a.workspace_opened), file_saved: count(a.file_saved), submission_started: count(a.submission_started), submission_finished: count(a.submission_finished), window: str(a.window), note: str(a.note) },
    controls: { supported: bool(controls.supported), reason: str(controls.reason), actions: list(controls.actions ?? [], commandKind) },
    commands: list(s.commands ?? [], parseClassroomCommand) } };
}

function commandKind(value: unknown): CommandKind {
  const kind = str(value); if (!(commandKinds as readonly string[]).includes(kind)) throw new ContractError(); return kind as CommandKind;
}
export function parseClassroomCommand(value: unknown): ClassroomCommand {
  const v = obj(value), status = str(v.status);
  if (!(commandStatuses as readonly string[]).includes(status)) throw new ContractError();
  const result = nullable(v.result, value => {
    const r = obj(value); if (Object.keys(r).some(k => !["submission_id", "checks"].includes(k))) throw new ContractError();
    const parsed: NonNullable<ClassroomCommand["result"]> = {};
    if (r.submission_id !== undefined) parsed.submission_id = str(r.submission_id);
    if (r.checks !== undefined) {
      const checks = obj(r.checks), options: Record<string, string[]> = { server: ["ok", "failed"], workspace: ["ok", "missing", "untrusted"], compiler: ["available", "missing", "not_checked"], os: ["macos", "linux", "windows", "other"], remote: ["none", "wsl", "ssh-remote", "other"] };
      parsed.checks = {};
      for (const [key, value] of Object.entries(checks)) { if (!options[key]?.includes(str(value))) throw new ContractError(); parsed.checks[key] = str(value); }
    }
    return parsed;
  });
  return { command_id: str(v.command_id), kind: commandKind(v.kind), assignment_id: nullable(v.assignment_id, str), message: str(v.message), status: status as ClassroomCommand["status"], created_by: str(v.created_by), created_at: time(v.created_at), expires_at: time(v.expires_at), received_at: nullable(v.received_at, time), accepted_at: nullable(v.accepted_at, time), finished_at: nullable(v.finished_at, time), result_code: nullable(v.result_code, str), result };
}
export function parseCommandCreated(value: unknown) {
  const v = obj(value); if (v.contract_version !== "1") throw new ContractError();
  return { command: parseClassroomCommand(v.command), duplicate: bool(v.duplicate) };
}
