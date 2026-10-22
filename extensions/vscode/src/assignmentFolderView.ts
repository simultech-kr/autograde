import * as path from "node:path";
import * as vscode from "vscode";
import { AssignmentWorkspaceError, containsPath, requireExistingAssignmentDirectory } from "./assignmentWorkspace";
import { findSafeStarterPreview } from "./bundleWorkspace";

/** Preview files without replacing the workspace or restarting its in-memory login. */
export async function revealAssignmentFolder(
  targetUri: vscode.Uri, targetPath: string, isCurrent: () => boolean = () => true,
): Promise<void> {
  if (!isCurrent()) return;
  if (path.resolve(targetUri.fsPath) !== path.resolve(targetPath)) throw new AssignmentWorkspaceError("folder_unavailable");
  await requireExistingAssignmentDirectory(targetPath);
  if (!isCurrent()) return;
  await vscode.commands.executeCommand("workbench.view.explorer");
  if (!isCurrent()) return;
  const inWorkspace = (vscode.workspace.workspaceFolders ?? []).some(folder =>
    folder.uri.scheme === targetUri.scheme && folder.uri.authority === targetUri.authority &&
    containsPath(folder.uri.fsPath, targetPath));
  if (inWorkspace) await vscode.commands.executeCommand("revealInExplorer", targetUri);
  else void vscode.window.showInformationMessage(`제출 폴더를 연결했습니다: ${targetPath}. 현재 탐색기와 빌드 작업 영역은 바뀌지 않았습니다. 폴더 전체를 열려면 Autograde의 수업 폴더로 전환을 사용하세요.`);
  if (!isCurrent()) return;
  const preview = await findSafeStarterPreview(targetPath);
  if (!isCurrent()) return;
  if (preview) {
    try {
      const previewUri = vscode.Uri.joinPath(targetUri, path.basename(preview));
      if (inWorkspace) await vscode.commands.executeCommand("revealInExplorer", previewUri);
      if (!isCurrent()) return;
      const document = await vscode.workspace.openTextDocument(previewUri);
      if (!isCurrent()) return;
      await vscode.window.showTextDocument(document, { preview: true });
    } catch {
      if (!isCurrent()) return;
      void vscode.window.showWarningMessage(`소스 미리보기를 열지 못했습니다. 파일은 보존됩니다. 다음 폴더의 파일을 직접 여세요: ${targetPath}`);
    }
  } else {
    // The folder may have disappeared while Explorer was opening.
    await requireExistingAssignmentDirectory(targetPath);
  }
}
