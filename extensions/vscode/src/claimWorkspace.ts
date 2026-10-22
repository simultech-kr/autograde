import * as vscode from "vscode";
import type { MementoLike } from "./submissionAttempt";

export const PENDING_CLAIM_WORKSPACE_KEY = "autograde.pendingClaimWorkspace.v1";
export const PENDING_CLAIM_WORKSPACE_TTL_MS = 10 * 60 * 1000;

interface PendingClaimWorkspace {
  readonly targetUri: string;
  readonly createdAt: number;
}

function isPendingClaimWorkspace(value: unknown): value is PendingClaimWorkspace {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const fields = Object.keys(value);
  if (fields.length !== 2 || !fields.includes("targetUri") || !fields.includes("createdAt")) return false;
  const pending = value as PendingClaimWorkspace;
  return typeof pending.targetUri === "string" && pending.targetUri.length <= 8192 &&
    /^(file|vscode-remote):\/\//u.test(pending.targetUri) &&
    Number.isSafeInteger(pending.createdAt) && pending.createdAt >= 0;
}

function matchesClaimWorkspace(targetUri: string): boolean {
  const folders = vscode.workspace.workspaceFolders;
  return !vscode.workspace.workspaceFile && folders?.length === 1 && folders[0]?.uri.toString() === targetUri;
}

/** This is navigation intent only: never persist a claim code, token or student identity. */
export async function consumePendingClaimWorkspace(storage: MementoLike, now = Date.now()): Promise<boolean> {
  const pending = storage.get<unknown>(PENDING_CLAIM_WORKSPACE_KEY);
  if (pending === undefined) return false;
  if (!isPendingClaimWorkspace(pending) || pending.createdAt > now ||
    now - pending.createdAt >= PENDING_CLAIM_WORKSPACE_TTL_MS) {
    await storage.update(PENDING_CLAIM_WORKSPACE_KEY, undefined);
    return false;
  }
  // Another open window must not steal the destination window's continuation.
  if (!matchesClaimWorkspace(pending.targetUri)) return false;
  await storage.update(PENDING_CLAIM_WORKSPACE_KEY, undefined);
  return true;
}

/** Restore the next visible step after openFolder restarts the old extension host. */
export async function resumeClaimWorkspace(storage: MementoLike, showLearningHome: () => void): Promise<boolean> {
  const targetUri = vscode.workspace.workspaceFolders?.[0]?.uri.toString();
  if (!await consumePendingClaimWorkspace(storage)) return false;
  showLearningHome();
  if (!vscode.workspace.isTrusted) {
    await vscode.window.showInformationMessage(
      "수업 폴더가 열렸습니다. 폴더를 신뢰한 뒤 학습 홈의 ‘수령 코드로 연결 · 과제 받기’에서 계속하세요. 코드는 아직 사용하지 않았습니다.",
    );
    return true;
  }
  const enterCode = "수령 코드 입력";
  const chosen = await vscode.window.showInformationMessage(
    "수업 폴더가 준비되었습니다. 학생 웹에서 받은 수령 코드를 입력해 과제를 받으세요. 코드는 아직 사용하지 않았습니다.",
    enterCode,
  );
  // Explicit confirmation is required; never authenticate merely because a folder was opened.
  if (chosen === enterCode && vscode.workspace.isTrusted && targetUri && matchesClaimWorkspace(targetUri)) {
    await vscode.commands.executeCommand("autograde.redeemAssignmentClaim");
  }
  return true;
}

/** Opening a folder may restart the extension host; do it before consuming any claim code. */
export async function prepareClaimWorkspace(storage?: MementoLike): Promise<boolean> {
  if (vscode.workspace.workspaceFolders?.length) return true;
  const open = "수업 폴더 열기";
  const choice = await vscode.window.showInformationMessage(
    "먼저 과제를 저장할 수업 폴더를 열어 주세요.",
    { modal: true, detail: "폴더가 열린 뒤 수령 코드를 입력하면 됩니다. 아직 수령 코드를 사용하지 않았습니다." },
    open,
  );
  if (choice !== open) return false;
  const selected = await vscode.window.showOpenDialog({
    canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
    openLabel: "이 폴더를 수업 작업 영역으로 열기",
  });
  if (!selected?.[0]) return false;
  const pending: PendingClaimWorkspace = { targetUri: selected[0].toString(), createdAt: Date.now() };
  // Await persistence before replacing the window; all authentication remains in the next host.
  if (storage) await storage.update(PENDING_CLAIM_WORKSPACE_KEY, pending);
  try {
    await vscode.commands.executeCommand("vscode.openFolder", selected[0], false);
  } catch {
    if (storage) {
      const current = storage.get<unknown>(PENDING_CLAIM_WORKSPACE_KEY);
      if (isPendingClaimWorkspace(current) && current.targetUri === pending.targetUri && current.createdAt === pending.createdAt) {
        await storage.update(PENDING_CLAIM_WORKSPACE_KEY, undefined);
      }
    }
    throw new Error("수업 폴더를 열지 못했습니다. VS Code의 파일 → 폴더 열기에서 폴더를 연 뒤 수령 코드를 입력하세요. 코드는 아직 사용하지 않았습니다.");
  }
  // Never continue to authentication in the old extension host.
  return false;
}
