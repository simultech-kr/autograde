import * as vscode from "vscode";

/** Opening a folder may restart the extension host; do it before consuming any claim code. */
export async function prepareClaimWorkspace(): Promise<boolean> {
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
  try {
    await vscode.commands.executeCommand("vscode.openFolder", selected[0], false);
  } catch {
    throw new Error("수업 폴더를 열지 못했습니다. VS Code의 파일 → 폴더 열기에서 폴더를 연 뒤 수령 코드를 입력하세요. 코드는 아직 사용하지 않았습니다.");
  }
  // Never continue to authentication in the old extension host.
  return false;
}
