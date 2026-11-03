import {StudentContractError, unwrap, courseKey} from "./model.ts";

export type PrivacyChoice = "granted" | "declined" | "unanswered";
export type PrivacyData = {
  student_key: string; course_key: string; decision: PrivacyChoice; revision: number; updated_at: string | null;
  research_allowed: boolean;
  policy: Record<"version" | "title" | "purpose" | "items" | "retention" | "choice" | "withdrawal" | "scope" | "contact", string>;
  history: {decision: "granted" | "declined"; policy_version: string; created_at: string}[];
};
export type PrivacyCourse = PrivacyData & {course_name: string; prompt_dismissed: boolean};
export type PrivacyOverview = {student_key: string; csrf: string; policy: PrivacyData["policy"]; consents: PrivacyCourse[]};
const obj = (value: unknown): Record<string, unknown> => {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new StudentContractError();
  return value as Record<string, unknown>;
};
const str = (value: unknown): string => {
  if (typeof value !== "string" || value.length > 2000 || /[\u0000-\u001f\u007f]/.test(value)) throw new StudentContractError();
  return value;
};
export function parsePrivacy(value: unknown, course: string, student: string): PrivacyData {
  const body = obj(unwrap(value)), policy = obj(body.policy);
  if (body.course_key !== courseKey(course) || body.student_key !== student ||
      !["granted", "declined", "unanswered"].includes(String(body.decision)) ||
      !Number.isSafeInteger(body.revision) || Number(body.revision) < 0 ||
      typeof body.research_allowed !== "boolean" || body.research_allowed !== (body.decision === "granted") ||
      !Array.isArray(body.history) || body.history.length > 20) throw new StudentContractError();
  const parsedPolicy = Object.fromEntries(["version", "title", "purpose", "items", "retention", "choice", "withdrawal", "scope", "contact"].map(key => [key, str(policy[key])])) as PrivacyData["policy"];
  const date = (value: unknown) => { const text = str(value); if (!Number.isFinite(Date.parse(text))) throw new StudentContractError(); return text; };
  const history = body.history.map(value => {
    const item = obj(value); if (!["granted", "declined"].includes(String(item.decision))) throw new StudentContractError();
    return {decision: item.decision as "granted" | "declined", policy_version: str(item.policy_version), created_at: date(item.created_at)};
  });
  return {student_key: student, course_key: course, policy: parsedPolicy, decision: body.decision as PrivacyChoice,
    research_allowed: body.research_allowed, revision: body.revision as number,
    updated_at: body.updated_at === null ? null : date(body.updated_at), history};
}

export function parsePrivacyOverview(value: unknown, student: string): PrivacyOverview {
  const body = obj(unwrap(value));
  if (body.student_key !== student || typeof body.csrf !== "string" || !body.csrf || body.csrf.length > 512 ||
      /[\u0000-\u0020\u007f]/.test(body.csrf) || !Array.isArray(body.consents) || body.consents.length > 200) throw new StudentContractError();
  const policy = obj(body.policy);
  const parsedPolicy = Object.fromEntries(["version", "title", "purpose", "items", "retention", "choice", "withdrawal", "scope", "contact"].map(key => [key, str(policy[key])])) as PrivacyData["policy"];
  const seen = new Set<string>();
  const consents = body.consents.map(value => {
    const row = obj(value), course = courseKey(str(row.course_key));
    if (seen.has(course)) throw new StudentContractError();
    seen.add(course);
    const parsed = parsePrivacy({ok: true, result: {...row, policy: row.policy ?? parsedPolicy}}, course, student);
    if (Object.keys(parsedPolicy).some(key => parsed.policy[key as keyof typeof parsedPolicy] !== parsedPolicy[key as keyof typeof parsedPolicy])) throw new StudentContractError();
    if (row.prompt_dismissed !== undefined && typeof row.prompt_dismissed !== "boolean") throw new StudentContractError();
    return {...parsed, course_name: str(row.course_name), prompt_dismissed: row.prompt_dismissed === true};
  });
  return {student_key: student, csrf: body.csrf, policy: parsedPolicy, consents};
}
