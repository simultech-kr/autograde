import * as vscode from "vscode";
import { lstat, realpath } from "node:fs/promises";
import * as path from "node:path";
import { ApiError, AutogradeClient, RequestCancelledError } from "./api";
import { shouldDiscardTokensAfterRefreshError } from "./authPolicy";
import { materialDirectory, materialErrorMessage, saveStudentMaterial, type StudentMaterial } from "./studentMaterials";

export interface StudentMaterialsViewOptions {
  /** Optional per-student material directory; it is a suggestion, never selected or opened automatically. */
  readonly defaultDirectory?: () => Promise<vscode.Uri | undefined>;
  /** In a shared lab session ordinary receipt stays in this disposable directory. */
  readonly managedDirectory?: () => string | undefined;
  /** All known assignment roots, in addition to the IDE's currently open folders. */
  readonly assignmentDirectories?: () => readonly string[];
  readonly onDidSave?: (material: StudentMaterial, filename: string) => void;
  /** Clear stale student UI after this operation discovers a genuinely expired session. */
  readonly onAuthenticationRequired?: () => void | Promise<void>;
}

export type StudentMaterialOutcome = "saved" | "cancelled" | "unavailable" | "failed";

interface MaterialPick extends vscode.QuickPickItem { readonly material: StudentMaterial }
const plain = (text: string) => text.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\$\(/g, "＄(");
const timestamp = (value: string) => new Date(value).toLocaleString("ko-KR", {timeZone: "Asia/Seoul"});
const size = (bytes: number) => bytes < 1024 * 1024 ? `${Math.ceil(bytes / 1024)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`;

/** Explicit file receipt only: no extraction, execution, editor opening, or saved authentication data. */
export class StudentMaterialsView implements vscode.Disposable {
  private generation = 0;
  private operation?: AbortController;
  private pickerCancellation?: vscode.CancellationTokenSource;

  public constructor(private readonly client: AutogradeClient, private readonly options: StudentMaterialsViewOptions = {}) {}

  public async show(materialId?: string): Promise<StudentMaterialOutcome> {
    this.clear();
    const generation = this.generation;
    const baseUrl = this.client.getBaseUrl();
    const session = this.client.tokens.getSessionGeneration?.();
    const managed = this.options.managedDirectory?.();
    const isManaged = managed !== undefined;
    const controller = new AbortController();
    this.operation = controller;
    const sameOperation = () => {
      try { return generation === this.generation && !controller.signal.aborted &&
        baseUrl === this.client.getBaseUrl(); }
      catch { return false; }
    };
    const current = () => sameOperation() && session === this.client.tokens.getSessionGeneration?.()
      && managed === this.options.managedDirectory?.();
    const assertCurrent = () => { if (!current()) throw new RequestCancelledError(); };
    try {
      if (materialId !== undefined && (typeof materialId !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(materialId))) {
        throw new Error("선택한 자료를 확인할 수 없습니다. 서버 과제·자료 목록을 새로고침하세요.");
      }
      if (!await this.client.tokens.hasSession()) throw new ApiError("학생 로그인이 필요합니다.", 401, "login_required");
      assertCurrent();
      const catalog = await this.client.getStudentMaterials(controller.signal);
      assertCurrent();
      if (materialId !== undefined && !catalog.materials.some(item => item.id === materialId)) {
        await vscode.window.showWarningMessage("선택한 자료가 철회되었거나 현재 학생에게 배포되지 않았습니다. 서버 과제·자료 목록을 새로고침하세요.");
        return "unavailable";
      }
      if (!catalog.materials.length) {
        await vscode.window.showInformationMessage(`${catalog.courseKey}: 현재 학생에게 배포된 자료가 없습니다. 자료는 과제 및 성적과 별도로 관리됩니다.`);
        return "unavailable";
      }
      let material = materialId === undefined ? undefined : catalog.materials.find(item => item.id === materialId);
      if (!material) {
        const cancellation = new vscode.CancellationTokenSource();
        this.pickerCancellation = cancellation;
        const selected = await vscode.window.showQuickPick<MaterialPick>(catalog.materials.map(material => ({
          label: plain(material.title),
          description: `${material.format.toUpperCase()} · ${size(material.sizeBytes)} · 파일 v${material.fileRevision}${material.updated ? " · 수정됨" : ""}`,
          detail: `${material.availability === "open" ? "다운로드 가능" : material.availability === "scheduled" ? "배포 시작 전" : "배포 종료"} · ` +
            `${timestamp(material.opensAt)} ~ ${timestamp(material.closesAt)} KST` +
            (material.instructions ? ` · ${plain(material.instructions).slice(0, 300)}` : ""), material,
        })), {title: `${catalog.courseKey} · 내 배포 자료`,
          placeHolder: isManaged
            ? "자료를 임시로 받습니다. 실습 종료 시 삭제되며 보존하려면 ‘내 학습내용 내보내기’를 사용하세요."
            : "다운로드할 자료를 선택하세요. PDF·ZIP은 저장만 하며 자동으로 열거나 압축 해제하지 않습니다.", matchOnDescription: true}, cancellation.token);
        cancellation.dispose();
        if (this.pickerCancellation === cancellation) this.pickerCancellation = undefined;
        if (!selected) return "cancelled";
        assertCurrent();
        material = selected.material;
      }
      assertCurrent();
      if (material.availability !== "open") {
        await vscode.window.showInformationMessage(material.availability === "scheduled"
          ? `배포 시작 전입니다. ${timestamp(material.opensAt)} KST부터 다시 확인하세요.`
          : "배포 기간이 종료되었습니다. 교수자에게 기간 연장을 문의하세요.");
        return "unavailable";
      }
      if (!vscode.workspace.isTrusted) {
        await vscode.window.showWarningMessage("제한 모드에서는 자료를 저장할 수 없습니다. 현재 학습 폴더를 신뢰한 뒤 다시 다운로드하세요.");
        return "unavailable";
      }
      const checkManagedDirectory = async (): Promise<string> => {
        assertCurrent();
        if (!managed || !path.isAbsolute(managed)) throw new Error("공용 실습 자료 폴더를 확인할 수 없습니다. 실습을 끝내고 다시 시작하세요.");
        const info = await lstat(managed), canonical = await realpath(managed); assertCurrent();
        if (!info.isDirectory() || info.isSymbolicLink() || canonical !== path.resolve(managed)) {
          throw new Error("공용 실습 자료 폴더가 변경되었습니다. 임시 자료를 저장하지 않았습니다.");
        }
        return canonical;
      };
      let requestedDirectory: string;
      if (isManaged) requestedDirectory = await checkManagedDirectory();
      else {
        const suggested = await this.options.defaultDirectory?.();
        assertCurrent();
        const folders = await vscode.window.showOpenDialog({title: "과제 코드와 분리된 자료 저장 폴더 선택",
          canSelectFiles: false, canSelectFolders: true, canSelectMany: false, openLabel: "이 폴더에 자료 저장", defaultUri: suggested});
        assertCurrent();
        const folder = folders?.[0];
        if (!folder) return "cancelled";
        if (folder.scheme !== "file") throw new Error("현재 IDE가 접근할 수 있는 로컬/WSL 자료 폴더를 선택하세요.");
        requestedDirectory = folder.fsPath;
      }
      const assignments = () => [...(this.options.assignmentDirectories?.() ?? []),
        ...(vscode.workspace.workspaceFolders ?? []).filter(item => item.uri.scheme === "file").map(item => item.uri.fsPath)];
      const directory = await materialDirectory(requestedDirectory, assignments());
      assertCurrent();
      const saved = await vscode.window.withProgress({location: vscode.ProgressLocation.Notification,
        title: "개인 배포 자료 다운로드 및 무결성 확인", cancellable: true}, async (_progress, cancellation) => {
        const subscription = cancellation.onCancellationRequested(() => controller.abort());
        try {
          if (cancellation.isCancellationRequested) controller.abort();
          assertCurrent();
          if (!vscode.workspace.isTrusted) throw new RequestCancelledError();
          const bytes = await this.client.getStudentMaterialFile(material, controller.signal);
          assertCurrent();
          if (isManaged && await checkManagedDirectory() !== directory) throw new RequestCancelledError();
          return await saveStudentMaterial(directory, material, bytes, () => {
            assertCurrent();
            if (!vscode.workspace.isTrusted) throw new RequestCancelledError();
          }, assignments());
        } finally { subscription.dispose(); }
      });
      assertCurrent();
      this.options.onDidSave?.(material, saved);
      const savedInfo = await lstat(saved); assertCurrent();
      const offerLocation = async (): Promise<void> => {
        const action = await vscode.window.showInformationMessage(`자료를 저장했습니다: ${saved}\n과제 코드와 별도로 저장했습니다. ZIP은 자동 압축 해제하지 않았습니다.` +
          (isManaged ? "\n공용 실습 공간에 임시로 받았습니다. 실습 종료 시 삭제됩니다. 가져갈 자료는 ‘내 학습내용 내보내기’로 저장하세요." : ""), "저장 위치 보기");
        if (action !== "저장 위치 보기") return;
        assertCurrent();
        if (!vscode.workspace.isTrusted) throw new RequestCancelledError();
        if (isManaged) await checkManagedDirectory();
        const currentInfo = await lstat(saved); assertCurrent();
        if (!currentInfo.isFile() || currentInfo.isSymbolicLink() || currentInfo.dev !== savedInfo.dev || currentInfo.ino !== savedInfo.ino) {
          throw new Error("저장한 파일의 위치가 변경되었습니다.");
        }
        await vscode.commands.executeCommand("revealFileInOS", vscode.Uri.file(saved));
      };
      // File receipt is already complete. A notification or Finder failure must
      // not keep the assignment operation gate busy or turn a saved file into a failed download.
      void offerLocation().catch(error => {
        if (current() && !(error instanceof RequestCancelledError)) {
          void vscode.window.showWarningMessage("자료는 저장했지만 저장 위치를 열지 못했습니다. 안내된 경로에서 파일을 확인하세요.");
        }
      });
      assertCurrent();
      return "saved";
    } catch (error) {
      if (error instanceof RequestCancelledError) return "cancelled";
      if (sameOperation() && error instanceof ApiError &&
          (error.status === 401 || shouldDiscardTokensAfterRefreshError(error.status, error.code))) {
        // A rejected refresh clears TokenManager's identity generation. Unlike a new login,
        // this needs an explanation and UI reset, not silent stale-response suppression.
        const expiredGeneration = this.client.tokens.getSessionGeneration?.();
        const noSession = !await this.client.tokens.hasSession().catch(() => true);
        if (!sameOperation() || expiredGeneration !== this.client.tokens.getSessionGeneration?.()) return "cancelled";
        if (noSession) {
          // No await between the final identity/scope check and the callback. A replacement
          // student's state must never be cleared by an older operation's delayed failure.
          const cleared = this.options.onAuthenticationRequired?.();
          const notice = vscode.window.showWarningMessage(materialErrorMessage(new ApiError("", 401, "login_required")));
          await cleared;
          await notice;
          return "unavailable";
        }
      }
      if (current()) await vscode.window.showWarningMessage(isManaged && (error as NodeJS.ErrnoException)?.code === "EEXIST"
        ? "이 버전의 자료를 이미 임시로 받았습니다. 기존 파일은 보존했습니다. 가져가려면 ‘내 학습내용 내보내기’를 사용하세요."
        : materialErrorMessage(error));
      return current() ? "failed" : "cancelled";
    } finally {
      if (this.operation === controller) this.operation = undefined;
    }
  }

  public clear(): void {
    this.generation++;
    this.operation?.abort();
    this.operation = undefined;
    this.pickerCancellation?.cancel();
    this.pickerCancellation?.dispose();
    this.pickerCancellation = undefined;
  }
  public dispose(): void { this.clear(); }
}
