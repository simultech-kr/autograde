import * as path from "node:path";
import * as vscode from "vscode";
import { containsPath } from "./assignmentWorkspace";
import { findSafeStarterPreview } from "./bundleWorkspace";

/** Preview files without replacing the workspace or restarting its in-memory login. */
export async function revealAssignmentFolder(targetUri: vscode.Uri, targetPath: string): Promise<void> {
  await vscode.commands.executeCommand("workbench.view.explorer");
  const inWorkspace = (vscode.workspace.workspaceFolders ?? []).some(folder => containsPath(folder.uri.fsPath, targetPath));
  if (inWorkspace) await vscode.commands.executeCommand("revealInExplorer", targetUri);
  else void vscode.window.showInformationMessage(`제출 폴더를 연결했습니다: ${targetPath}. 현재 탐색기와 빌드 작업 영역은 바뀌지 않았습니다. 폴더 전체를 열려면 Autograde의 수업 폴더로 전환을 사용하세요.`);
  const preview = await findSafeStarterPreview(targetPath);
  if (preview) {
    try {
      const previewUri = vscode.Uri.joinPath(targetUri, path.basename(preview));
      if (inWorkspace) await vscode.commands.executeCommand("revealInExplorer", previewUri);
      const document = await vscode.workspace.openTextDocument(previewUri);
      await vscode.window.showTextDocument(document, { preview: true });
    } catch {
      void vscode.window.showWarningMessage(`소스 미리보기를 열지 못했습니다. 파일은 보존됩니다. 다음 폴더의 파일을 직접 여세요: ${targetPath}`);
    }
  }
}
