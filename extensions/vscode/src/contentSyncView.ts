import * as vscode from "vscode";
import { ApiError, AutogradeClient, RequestCancelledError } from "./api";
import { ContentApi } from "./contentApi";
import { contentErrorMessage, exportContent, syncContent, type ContentCollection, type ContentSyncOptions } from "./contentSync";
import { shouldDiscardTokensAfterRefreshError } from "./authPolicy";
import { learningExportDirectory } from "./learningExport";

export const CONTENT_SYNC_COMMAND = "autograde.syncContent";
export interface ContentSyncViewOptions {
  readonly cacheRoot: () => string;
  readonly defaultDirectory?: () => vscode.Uri | undefined;
  readonly assignmentDirectories?: () => readonly string[];
  /** Managed temporary sessions must never be offered as a persistent export destination. */
  readonly excludedExportDirectories?: () => readonly string[];
  readonly onAuthenticationRequired?: () => void | Promise<void>;
}
export type ContentSyncOutcome = "synced" | "cancelled" | "unavailable" | "failed";
interface CollectionPick extends vscode.QuickPickItem { readonly collection: ContentCollection }
const plain = (text: string) => text.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\$\(/g, "＄(");

/** Manual flow: list metadata → stage changed files → explicit copy to a separate folder. */
export class ContentSyncView implements vscode.Disposable {
  private generation = 0;
  private operation?: AbortController;
  private pickerCancellation?: vscode.CancellationTokenSource;
  private readonly api: ContentApi;

  public constructor(private readonly client: AutogradeClient, private readonly options: ContentSyncViewOptions) {
    this.api = new ContentApi(client);
  }

  public async show(requestGuard?: () => void, signal?: AbortSignal): Promise<ContentSyncOutcome> {
    this.clear();
    const generation = this.generation, origin = this.client.getBaseUrl(), identity = this.client.tokens.getSessionGeneration?.();
    const initialExcluded = [...(this.options.excludedExportDirectories?.() ?? [])];
    const excluded = () => [...initialExcluded, ...(this.options.excludedExportDirectories?.() ?? [])];
    const shared = initialExcluded.length > 0;
    const controller = new AbortController(); this.operation = controller;
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, {once: true});
    if (signal?.aborted) controller.abort();
    const sameOperation = () => generation === this.generation && !controller.signal.aborted && origin === this.client.getBaseUrl();
    const current = () => sameOperation() && identity === this.client.tokens.getSessionGeneration?.();
    const guard = () => { if (!current()) throw new RequestCancelledError(); requestGuard?.(); };
    const writeGuard = () => {
      guard();
      if (!vscode.workspace.isTrusted) throw new Error("제한 모드에서는 자료 동기화·저장을 할 수 없습니다. 본인의 학습 폴더만 신뢰한 뒤 다시 시도하세요.");
    };
    const pick = async <T extends vscode.QuickPickItem>(items: readonly T[], options: vscode.QuickPickOptions): Promise<T | undefined> => {
      const cancellation = new vscode.CancellationTokenSource(); this.pickerCancellation = cancellation;
      try { return await vscode.window.showQuickPick(items, options, cancellation.token); }
      finally { cancellation.dispose(); if (this.pickerCancellation === cancellation) this.pickerCancellation = undefined; }
    };
    try {
      if (!await this.client.tokens.hasSession()) throw new ApiError("학생 로그인이 필요합니다.", 401, "login_required");
      guard();
      const context = await this.api.context(controller.signal); guard();
      const catalog = await this.api.catalog(context, controller.signal); guard();
      if (!catalog.collections.length) {
        await vscode.window.showInformationMessage(`${context.course_key}: 지금 받을 수 있는 강의자료가 없습니다. 개인 PDF·ZIP 자료는 ‘내 배포 자료’에서 확인하세요.`);
        return "unavailable";
      }
      const selected = await pick<CollectionPick>(catalog.collections.map(collection => ({
        label: plain(collection.title), description: `배포 v${collection.generation}`,
        detail: plain(collection.change_note).slice(0, 300) || "교수자가 공개한 강의자료입니다. 학생 답안과 분리하여 저장합니다.", collection,
      })), {title: `${context.course_key} · 강의자료 업데이트`, placeHolder: "자료를 선택하세요. 파일은 다음 단계에서 받습니다.", matchOnDescription: true});
      guard(); if (!selected) return "cancelled";
      const manifest = await this.api.manifest(selected.collection.release_id, context, controller.signal); guard();
      if (manifest.collection_id !== selected.collection.collection_id || manifest.generation !== selected.collection.generation) {
        throw new ApiError("자료 목록이 변경되었습니다.", 409, "content_changed");
      }
      const total = manifest.files.reduce((sum, file) => sum + file.size_bytes, 0);
      const choice = await pick([
        {label: "변경된 파일만 받기", description: shared ? "공용 실습 공간에 임시 수신 · 실습 종료 시 삭제" : "학생별 임시 캐시에 저장 · 내보내기는 나중에", action: "sync"},
        {label: "내 폴더에 저장", description: shared ? "임시 학습 공간 밖으로 내보내기 · 실습 종료 후에도 보존" : "최신 자료를 확인하고 새 폴더에 복사 · 기존 파일 보존", action: "export"},
      ], {title: `${plain(manifest.title)} · 파일 ${manifest.files.length}개 · ${(total / 1048576).toFixed(1)} MiB`,
        placeHolder: "캐시·내 폴더 모두 실제 파일을 받습니다. ZIP은 자동 압축 해제하지 않습니다."});
      guard(); if (!choice) return "cancelled"; writeGuard();
      let destination: string | undefined;
      if (choice.action === "export") {
        const folders = await vscode.window.showOpenDialog({title: shared ? "실습 종료 후에도 보존할 강의자료 내보내기 위치" : "과제 코드와 분리된 강의자료 저장 위치", canSelectFiles: false,
          canSelectFolders: true, canSelectMany: false, openLabel: shared ? "이 폴더에 내보내기" : "새 자료 폴더 만들기",
          defaultUri: shared ? undefined : this.options.defaultDirectory?.()});
        guard(); if (!folders?.[0]) return "cancelled";
        if (folders[0].scheme !== "file") throw new Error("현재 IDE에서 접근할 수 있는 로컬/WSL 폴더를 선택하세요.");
        destination = await learningExportDirectory(folders[0].fsPath, excluded()); guard();
      }
      const assignments = [...(this.options.assignmentDirectories?.() ?? []),
        ...(vscode.workspace.workspaceFolders ?? []).filter(folder => folder.uri.scheme === "file").map(folder => folder.uri.fsPath)];
      const options: ContentSyncOptions = {root: this.options.cacheRoot(), origin, context, assertCurrent: writeGuard,
        assignmentDirectories: assignments, signal: controller.signal};
      await vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: "강의자료 변경분 확인·수신", cancellable: true},
        async (progress, cancellation) => {
          const subscription = cancellation.onCancellationRequested(() => controller.abort());
          try {
            if (cancellation.isCancellationRequested) controller.abort();
            const result = await syncContent(this.api, manifest, {...options,
              onProgress: (completed, count) => progress.report({message: `${completed}/${count} 파일 검증`})});
            guard();
            if (destination) { destination = await learningExportDirectory(destination, excluded()); guard(); }
            const exported = destination ? await exportContent(this.api, manifest,
              {...options, excludedExportDirectories: excluded()}, destination) : undefined;
            guard();
            void vscode.window.showInformationMessage(`강의자료 v${manifest.generation}: 변경 파일 ${result.downloadedFiles}개 수신, ${result.reusedFiles}개 재사용.` +
              (exported ? `\n저장 위치: ${exported}\n기존 파일은 덮어쓰지 않았으며 자동으로 열거나 실행하지 않습니다.${shared ? " 내보낸 파일은 실습 종료 후에도 보존됩니다." : ""}`
                : "\n학생별 캐시에 보관했습니다. ‘강의자료 업데이트 → 내 폴더에 저장’으로 나중에 복사하세요. 내보내기에는 서버 연결이 필요합니다." +
                  (shared ? "\n임시로 받은 자료는 실습 종료 시 삭제됩니다. 보존하려면 종료 전에 내보내세요." : "")));
          } finally { subscription.dispose(); }
        });
      guard();
      return "synced";
    } catch (error) {
      if (error instanceof RequestCancelledError || controller.signal.aborted) return "cancelled";
      if (sameOperation() && error instanceof ApiError &&
          (error.status === 401 || shouldDiscardTokensAfterRefreshError(error.status, error.code))) {
        const expiredIdentity = this.client.tokens.getSessionGeneration?.();
        const noSession = !await this.client.tokens.hasSession().catch(() => true);
        if (!sameOperation() || expiredIdentity !== this.client.tokens.getSessionGeneration?.()) return "cancelled";
        if (noSession) { const cleared = this.options.onAuthenticationRequired?.(); await cleared;
          await vscode.window.showWarningMessage(contentErrorMessage(new ApiError("", 401))); return "unavailable"; }
      }
      if (current()) await vscode.window.showWarningMessage(contentErrorMessage(error));
      return "failed";
    } finally { signal?.removeEventListener("abort", abort); if (this.operation === controller) this.operation = undefined; }
  }
  public clear(): void {
    this.generation++; this.operation?.abort(); this.operation = undefined;
    this.pickerCancellation?.cancel(); this.pickerCancellation?.dispose(); this.pickerCancellation = undefined;
  }
  public dispose(): void { this.clear(); }
}
