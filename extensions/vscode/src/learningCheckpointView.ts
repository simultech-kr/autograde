import { randomUUID } from "node:crypto";
import { lstat, realpath, rm } from "node:fs/promises";
import * as path from "node:path";
import * as vscode from "vscode";
import type { AutogradeClient } from "./api";
import { containsPath, validateExistingAssignment } from "./assignmentWorkspace";
import { createSubmissionBundle, extractSubmissionBundle, writeWorkspaceMarker, type SubmissionBundle } from "./bundle";
import { LearningCheckpointAutosaver, learningCheckpointLabel, sameLearningScope, type LearningCheckpointScope, type LearningCheckpointStatus, type SavedLearningSnapshot } from "./learningCheckpoint";
import { saveAssignmentDocuments } from "./submissionPreflight";

export const SAVE_LEARNING_CHECKPOINT_COMMAND = "autograde.saveLearningCheckpoint";
export const RESTORE_LEARNING_CHECKPOINT_COMMAND = "autograde.restoreLearningCheckpoint";

/** A backup is independent of official submission. Credentials and work bytes stay in memory. */
export class LearningCheckpointView {
  readonly autosaver: LearningCheckpointAutosaver;
  private readonly statusBar = vscode.window.createStatusBarItem("autograde.learningCheckpoint", vscode.StatusBarAlignment.Left, 98);
  private restoreFlight?: AbortController;
  private disposed = false;
  constructor(private readonly client: AutogradeClient, private readonly options: {
    currentScope(): LearningCheckpointScope | undefined;
    isBusy(): boolean;
    withCaptureLock?(action: () => Promise<SubmissionBundle | undefined>): Promise<SubmissionBundle | undefined>;
    sharedSession?: {readonly workspace: string; assertWorkspacePath(candidate: string): Promise<void>};
    connect(root: vscode.Uri, scope: LearningCheckpointScope, current: () => boolean): Promise<void>;
    onStatus(status: LearningCheckpointStatus): void;
  }) {
    this.statusBar.command = SAVE_LEARNING_CHECKPOINT_COMMAND;
    this.autosaver = new LearningCheckpointAutosaver({
      currentScope: () => this.scope(),
      capture: async (scope, saveDirty, current) => {
        if (saveDirty && this.options.isBusy()) return undefined;
        const capture = async () => {
          if (!current()) return undefined;
          await this.validateRoot(scope); if (!current()) return undefined;
          if (saveDirty) {
            // Refuse linked destinations before asking VS Code to save an edited document.
            const root = await realpath(scope.rootPath);
            for (const document of vscode.workspace.textDocuments) {
              if (!document.isDirty || document.uri.scheme !== scope.scheme || (document.uri.authority ?? "") !== scope.authority
                || !containsPath(scope.rootPath, document.uri.fsPath)) continue;
              const parent = await realpath(path.dirname(document.uri.fsPath));
              const info = await lstat(document.uri.fsPath).catch((error: NodeJS.ErrnoException) => { if (error.code !== "ENOENT") throw error; return undefined; });
              if (!containsPath(root, parent) || info?.isSymbolicLink() || (info && (!info.isFile() || info.nlink !== 1))) throw new Error("연결된 파일은 자동 저장할 수 없습니다.");
            }
            if (!current() || !await saveAssignmentDocuments(scope.rootPath, () => vscode.workspace.textDocuments, async () => true, current, scope)) return undefined;
          }
          await this.validateRoot(scope); if (!current()) return undefined;
          const bundle = await createSubmissionBundle(scope.rootPath);
          await this.validateRoot(scope);
          if (saveDirty && vscode.workspace.textDocuments.some(document => document.isDirty && document.uri.scheme === scope.scheme
            && (document.uri.authority ?? "") === scope.authority && containsPath(scope.rootPath, document.uri.fsPath))) {
            throw new Error("저장 중 새 편집이 생겼습니다. 잠시 후 다시 저장하세요.");
          }
          return current() ? bundle : undefined;
        };
        return saveDirty && this.options.withCaptureLock ? this.options.withCaptureLock(capture) : capture();
      },
      upload: (scope, bundle, signal, key) => client.saveLearningCheckpoint(scope, bundle.archive, bundle.sha256, signal, key),
      baselineOnlyUntilChange: Boolean(options.sharedSession),
      onStatus: status => {
        if (status.state === "inactive") this.statusBar.hide();
        else {
          const icon = status.state === "saved" ? "cloud" : status.state === "saving" ? "sync~spin" : ["failed", "unavailable", "retrying"].includes(status.state) ? "warning" : "clock";
          this.statusBar.text = `$(${icon}) 작업 서버 저장${status.state === "saved" ? " 확인" : status.state === "saving" ? " 중" : status.state === "waiting" ? " 대기" : " 확인 필요"}`;
          this.statusBar.tooltip = `${learningCheckpointLabel(status)}\n현재 연결 과제의 변경 코드를 30초마다 저장합니다. 공식 제출·채점은 별도입니다. 클릭하면 지금 저장합니다.`;
          this.statusBar.show();
        }
        options.onStatus(status);
      },
    });
  }
  private scope(): LearningCheckpointScope | undefined {
    try { return !this.disposed && vscode.workspace.isTrusted ? this.options.currentScope() : undefined; } catch { return undefined; }
  }
  recordEdit(document: {readonly scheme: string; readonly authority?: string; readonly fsPath: string}): void {
    const scope = this.scope();
    if (scope && document.scheme === scope.scheme && (document.authority ?? "") === scope.authority
      && containsPath(scope.rootPath, document.fsPath)) this.autosaver.recordEdit();
  }
  reconcile(): void {
    this.autosaver.reconcile();
    // Restore requests also use scope guards after every awaited operation.
    if (!this.scope()) this.restoreFlight?.abort();
  }
  private async validateRoot(scope: LearningCheckpointScope): Promise<void> {
    await this.options.sharedSession?.assertWorkspacePath(scope.rootPath);
    await validateExistingAssignment(scope.rootPath, scope.baseUrl, scope.assignmentId);
  }
  async saveNow(showMessage = true): Promise<boolean> {
    if (this.options.isBusy()) {
      if (showMessage) void vscode.window.showInformationMessage("과제 작업이 끝난 뒤 다시 서버 저장을 눌러 주세요.");
      return false;
    }
    const saved = await this.autosaver.saveNow();
    if (showMessage) {
      if (saved) void vscode.window.showInformationMessage("현재 작업을 서버에 저장했습니다. 공식 제출과 채점은 ‘현재 과제 제출’에서 진행하세요.");
      else void vscode.window.showWarningMessage(`작업 서버 저장을 확인하지 못했습니다. ${learningCheckpointLabel(this.autosaver.getStatus())}. 필요한 코드는 내보내기로 보관하세요.`);
    }
    return saved;
  }
  async flushBeforeExit(timeoutMs = 5000): Promise<boolean> {
    if (!this.scope()) return false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      return await Promise.race([this.saveNow(false), new Promise<boolean>(resolve => {
        timer = setTimeout(() => { this.autosaver.abortCurrent(); resolve(false); }, timeoutMs);
      })]);
    } finally { if (timer) clearTimeout(timer); }
  }
  exitEvidence(): string {
    this.autosaver.reconcile();
    const status = this.autosaver.getStatus();
    return `${learningCheckpointLabel(status)}. ${status.state === "saved" ? "이 응답 이후 새로 편집한 내용은 다시 저장해야 합니다." : "표시된 이전 저장본에 현재 변경이 포함되었다고 보장하지 않습니다."}`;
  }
  /** Capture this assignment now; never choose the newest history item from another upload. */
  async saveForExecution(scope: LearningCheckpointScope): Promise<SavedLearningSnapshot> {
    if (!sameLearningScope(scope, this.scope())) throw new Error("연결된 과제가 변경되었습니다.");
    let saved = await this.saveNow(false);
    // Save participants may edit a document while it is being saved. Re-capture once;
    // continuous edits must leave the operation incomplete rather than use an old receipt.
    if (!saved && this.autosaver.getStatus().state === "waiting" && this.autosaver.getStatus().savedAt
      && sameLearningScope(scope, this.scope())) saved = await this.saveNow(false);
    const snapshot = saved && sameLearningScope(scope, this.scope()) ? this.autosaver.getSavedSnapshot() : undefined;
    if (!snapshot) throw new Error("현재 코드의 서버 저장을 확인하지 못했습니다. 편집을 잠시 멈추고, 저장 상태와 연결을 확인한 뒤 다시 시도하세요. 연속 저장 제한이면 10초 후 재시도하세요.");
    return snapshot;
  }
  async restore(): Promise<void> {
    const scope = this.scope();
    if (!scope) throw new Error("새 수령 코드로 연결하고 과제 폴더를 먼저 여세요.");
    if (this.restoreFlight) return;
    const controller = new AbortController(); this.restoreFlight = controller;
    const current = () => !controller.signal.aborted && sameLearningScope(scope, this.scope());
    const guard = () => { if (!current()) throw new Error("연결된 과제 또는 인증이 변경되어 복원을 중단했습니다."); };
    let created: {target: string; dev: number; ino: number} | undefined;
    try {
      await this.validateRoot(scope); guard();
      const versions = await this.client.getLearningCheckpoints(scope, controller.signal); guard();
      if (!versions.length) { void vscode.window.showInformationMessage("이 과제의 서버 작업 저장본이 없습니다. 공식 제출본은 제출 이력에서 확인하세요."); return; }
      const selected = await vscode.window.showQuickPick(versions.map(checkpoint => ({
        label: new Date(checkpoint.created_at).toLocaleString(),
        description: `작업 저장 v${checkpoint.version} · ${checkpoint.file_count}개 파일`,
        detail: `${checkpoint.archive_sha256.slice(0, 12)} · 새 폴더로 복원, 현재 코드 보존`, checkpoint,
      })), {title: "서버 작업 저장본 선택 · 공식 제출 아님", ignoreFocusOut: true}); guard();
      if (!selected) return;
      const parent = this.options.sharedSession ? vscode.Uri.file(this.options.sharedSession.workspace)
        : (await vscode.window.showOpenDialog({canSelectFolders: true, canSelectFiles: false, canSelectMany: false,
          title: "현재 과제 밖의 복원 위치", openLabel: "이 위치의 새 폴더에 복원"}))?.[0]; guard();
      if (!parent) return;
      if (!this.options.sharedSession && (parent.scheme !== scope.scheme || (parent.authority ?? "") !== scope.authority)) throw new Error("현재 과제와 같은 실행 환경의 폴더를 선택하세요.");
      const actualParent = await realpath(parent.fsPath); guard();
      if (containsPath(await realpath(scope.rootPath), actualParent)) throw new Error("현재 과제 폴더 밖의 위치를 선택하세요. 기존 과제 코드는 덮어쓰지 않습니다.");
      await this.options.sharedSession?.assertWorkspacePath(actualParent); guard();
      const target = path.join(actualParent, `autograde-work-${randomUUID()}`);
      const archive = await this.client.getLearningCheckpointBundle(scope, selected.checkpoint, controller.signal); guard();
      await this.validateRoot(scope); guard();
      await this.options.sharedSession?.assertWorkspacePath(actualParent); guard();
      await extractSubmissionBundle(archive, target);
      const info = await lstat(target); created = {target, dev: info.dev, ino: info.ino}; guard();
      await writeWorkspaceMarker(target, {schemaVersion: 1, serviceBaseUrl: scope.baseUrl, assignmentId: scope.assignmentId}); guard();
      await this.options.sharedSession?.assertWorkspacePath(target); guard();
      const connectedCurrent = () => current() || (!controller.signal.aborted && sameLearningScope({...scope, rootPath: target}, this.scope()));
      await this.options.connect(vscode.Uri.joinPath(parent, path.relative(parent.fsPath, target)), scope, connectedCurrent);
      if (!connectedCurrent()) throw new Error("인증이 변경되어 복원을 중단했습니다.");
      // Connecting changes the selected folder. It is intentional; do not treat it as stale cleanup.
      created = undefined;
      this.autosaver.recordRestored(selected.checkpoint);
      void vscode.window.showInformationMessage("서버 작업 저장본을 새 과제 폴더로 열었습니다. 코드 확인 후 공식 제출을 진행하세요.");
    } catch (error) {
      if (created) {
        const info = await lstat(created.target).catch(() => undefined);
        if (info?.isDirectory() && !info.isSymbolicLink() && info.dev === created.dev && info.ino === created.ino) await rm(created.target, {recursive: true, force: true});
      }
      throw error;
    } finally { if (this.restoreFlight === controller) this.restoreFlight = undefined; }
  }
  dispose(): void { this.disposed = true; this.restoreFlight?.abort(); this.autosaver.dispose(); this.statusBar.dispose(); }
}
