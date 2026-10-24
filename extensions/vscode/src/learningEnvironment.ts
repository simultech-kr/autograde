import * as path from "node:path";

export type LearningPolicy = "unmanaged" | "standard" | "restricted";
export interface LearningEnvironment {
  readonly policy: LearningPolicy;
  readonly materialsDirectory?: string;
}

/** Launcher hints for presentation and a folder-picker default, never authentication or attestation. */
export function learningEnvironment(appName: string, environment: Readonly<Record<string, string | undefined>>): LearningEnvironment {
  if (!["Autograde IDE", "Autograde IDE Dev"].includes(appName)) return {policy: "unmanaged"};
  const policy = environment.AUTOGRADE_LEARNING_POLICY;
  const workspace = environment.AUTOGRADE_LEARNER_WORKSPACE;
  const materials = environment.AUTOGRADE_LEARNER_MATERIALS;
  const absolute = (value: string | undefined): value is string => Boolean(value && !value.includes("\0") && path.isAbsolute(value));
  if (!["standard", "restricted"].includes(policy ?? "") || !absolute(workspace)) return {policy: "unmanaged"};
  // Suggest only the sibling directory created by our launcher; no arbitrary URL or executable path.
  const materialsDirectory = absolute(materials) && path.resolve(materials) === path.join(path.dirname(path.resolve(workspace)), "materials")
    ? materials : undefined;
  return {policy: policy as "standard" | "restricted", materialsDirectory};
}

export function learningPolicyLabel(policy: LearningPolicy = "unmanaged"): string {
  if (policy === "restricted") return "분리 학습 프로필 · IDE AI 제한 시작 설정";
  if (policy === "standard") return "분리 학습 프로필 · 일반 학습 설정";
  return "일반 IDE 실행 · 분리 학습 프로필 미확인";
}
