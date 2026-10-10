import * as vscode from "vscode";

import type { AutogradeClient } from "./api";

export async function showAcceptedAssignments(
  client: AutogradeClient,
  openCurrent: (assignmentId: string) => Promise<void>,
  reconnect: () => Promise<void>,
): Promise<void> {
  const origin = client.getBaseUrl();
  const generation = client.tokens.getSessionGeneration?.();
  const current = () => client.getBaseUrl() === origin && client.tokens.getSessionGeneration?.() === generation;
  const catalog = await client.getAcceptedAssignmentCatalog();
  if (!current()) return;
  if (!catalog.length) { void vscode.window.showInformationMessage("현재 학생·수업의 수락 이력이 없습니다."); return; }
  const picked = await vscode.window.showQuickPick(catalog.map(entry => ({
    label: entry.title,
    description: entry.currentSessionAuthorized ? "현재 연결된 과제"
      : entry.canReconnect ? "새 수령 코드로 재연결 필요" : entry.status === "closed" ? "마감 · 재연결 불가" : "현재 이용 불가",
    detail: `${entry.courseKey} · 수락 ${entry.acceptedAt} · ${entry.assignmentId}`,
    entry,
  })), { title: "수락 이력 / 과제 전환", placeHolder: "현재 학생·수업의 수락 이력입니다. 파일 접근 권한은 현재 연결된 과제에만 있습니다." });
  if (!picked || !current()) return;
  if (picked.entry.currentSessionAuthorized) { await openCurrent(picked.entry.assignmentId); return; }
  if (!picked.entry.canReconnect) {
    void vscode.window.showInformationMessage(`${picked.entry.title}은 현재 다시 연결할 수 없습니다. 마감·공개 상태를 교수자에게 확인하세요. 로컬 작업 파일은 보존됩니다.`);
    return;
  }
  const action = "새 수령 코드 입력";
  const choice = await vscode.window.showInformationMessage(`${picked.entry.title} 과제로 전환합니다.`, {
    modal: true,
    detail: "학생 웹에서 이 과제의 새 수령 코드를 발급받으세요. 연결 후 기존 폴더를 선택하면 이어서 실습하거나 다시 제출할 수 있습니다. 현재 작업 파일과 수정 내용은 보존됩니다.",
  }, action);
  if (choice === action && current()) await reconnect();
}
