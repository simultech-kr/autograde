import * as path from "node:path";

export interface SubmissionDocument {
  readonly uri: { readonly scheme: string; readonly authority?: string; readonly fsPath: string };
  readonly isDirty: boolean;
  save(): PromiseLike<boolean>;
}

/** Save only this assignment's edited files, and fail closed on cancellation or failed saves. */
export async function saveAssignmentDocuments(
  assignmentRoot: string,
  documents: () => readonly SubmissionDocument[],
  confirm: (relativePaths: readonly string[]) => PromiseLike<boolean>,
  isCurrent: () => boolean,
  workspaceUri: { readonly scheme: string; readonly authority?: string } = { scheme: "file" },
): Promise<boolean> {
  if (!["file", "vscode-remote"].includes(workspaceUri.scheme)) {
    throw new Error("로컬 또는 WSL/원격 파일 시스템의 수업 폴더에서 제출하세요.");
  }
  const root = path.resolve(assignmentRoot);
  const pending = (): readonly SubmissionDocument[] => documents().filter(document => {
    if (document.uri.scheme !== workspaceUri.scheme ||
        (document.uri.authority ?? "") !== (workspaceUri.authority ?? "") || !document.isDirty) return false;
    const relative = path.relative(root, path.resolve(document.uri.fsPath));
    return relative !== "" && relative !== ".." && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative);
  });
  if (!isCurrent()) return false;
  const dirty = pending();
  if (dirty.length === 0) return true;
  if (!await confirm(dirty.map(document => path.relative(root, document.uri.fsPath)))) return false;
  if (!isCurrent()) return false;
  for (const document of dirty) {
    if (!isCurrent()) return false;
    let saved = false;
    try { saved = await document.save(); } catch { /* Use the actionable message below. */ }
    if (!isCurrent()) return false;
    if (!saved || document.isDirty) {
      throw new Error(`${path.relative(root, document.uri.fsPath)} 파일을 저장하지 못해 제출을 중단했습니다. 저장 위치와 권한을 확인한 뒤 다시 제출하세요.`);
    }
  }
  if (pending().length > 0) {
    throw new Error("과제에 저장되지 않은 변경이 남아 있어 제출을 중단했습니다. 모두 저장한 뒤 다시 제출하세요.");
  }
  return isCurrent();
}
