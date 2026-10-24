import * as vscode from "vscode";
import { lstat, mkdir, mkdtemp, rm } from "node:fs/promises";
import * as path from "node:path";
import { ApiError, AutogradeClient, RequestCancelledError } from "./api";
import { extractSubmissionBundle } from "./bundle";
import { ContentApi } from "./contentApi";
import { exportContent, syncContent, type ContentManifest, type ContentContext } from "./contentSync";
import { collectLearningCode, learningExportDirectory, MAX_LEARNING_EXPORT_BYTES, MAX_LEARNING_EXPORT_FILES, saveLearningExport, type LearningExportGroup } from "./learningExport";
import { studentMaterialFilename, verifyStudentMaterial, type StudentMaterial } from "./studentMaterials";
import type { SubmissionVersion } from "./submissionHistory";
import type { Assignment } from "./types";

export const LEARNING_EXPORT_COMMAND = "autograde.exportLearning";
export interface ExportWorkspace { readonly rootPath: string; readonly assignmentId?: string; readonly title?: string }
export interface LearningExportViewOptions {
  readonly currentWorkspace: () => ExportWorkspace | undefined;
  readonly assignments: () => readonly Assignment[];
  readonly cacheRoot: () => string;
  readonly excludedDestinations: () => readonly string[];
}
type Category = "current" | "submissions" | "content" | "personal";
const plain = (text: string) => text.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\$\(/g, "＄(");
const safeId = (text: string) => text.replace(/[^A-Za-z0-9_-]/g, "_").slice(0, 100) || "item";
const contains = (root: string, file: string) => {const relative = path.relative(root, file); return relative === "" || relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);};
function hostFile(uri: vscode.Uri): boolean {
  if (uri.scheme === "file") return true;
  // Node filesystem calls run in this extension host. Never accept another remote authority.
  return uri.scheme === "vscode-remote" && Boolean(vscode.env?.remoteName) && Boolean(vscode.workspace.workspaceFolders?.some(folder =>
    folder.uri.scheme === "vscode-remote" && folder.uri.authority === uri.authority));
}

/** Student-selected independent export: no submission, restore, workspace switch or credential persistence. */
export class LearningExportView implements vscode.Disposable {
  private generation = 0;
  private operation?: AbortController;
  private pickerCancellation?: vscode.CancellationTokenSource;
  private readonly content: ContentApi;
  public constructor(private readonly client: AutogradeClient, private readonly options: LearningExportViewOptions) {this.content = new ContentApi(client);}

  public async show(): Promise<void> {
    this.clear(); const generation = this.generation, origin = this.client.getBaseUrl(), identity = this.client.tokens.getSessionGeneration?.();
    const controller = new AbortController(); this.operation = controller;
    const workspace = this.options.currentWorkspace(), workspaceKey = workspace?.rootPath;
    const current = () => generation === this.generation && !controller.signal.aborted && origin === this.client.getBaseUrl() &&
      identity === this.client.tokens.getSessionGeneration?.() && workspaceKey === this.options.currentWorkspace()?.rootPath;
    const guard = () => {if (!current()) throw new RequestCancelledError(); if (!vscode.workspace.isTrusted) throw new Error("신뢰하는 학습 폴더에서 내보내기를 실행하세요.");};
    const pick = async <T extends vscode.QuickPickItem>(items: readonly T[], options: vscode.QuickPickOptions): Promise<T[] | undefined> => {
      const cancellation = new vscode.CancellationTokenSource(); this.pickerCancellation = cancellation;
      try {const result = await vscode.window.showQuickPick(items, {...options, canPickMany: true}, cancellation.token); guard(); return result;}
      finally {cancellation.dispose(); if (this.pickerCancellation === cancellation) this.pickerCancellation = undefined;}
    };
    const pickOne = async <T extends vscode.QuickPickItem>(items: readonly T[], options: vscode.QuickPickOptions): Promise<T | undefined> => {
      const cancellation = new vscode.CancellationTokenSource(); this.pickerCancellation = cancellation;
      try {const result = await vscode.window.showQuickPick(items, {...options, canPickMany: false}, cancellation.token); guard(); return result;}
      finally {cancellation.dispose(); if (this.pickerCancellation === cancellation) this.pickerCancellation = undefined;}
    };
    const excluded = () => [...this.options.excludedDestinations(), ...(workspaceKey ? [workspaceKey] : []),
      ...(vscode.workspace.workspaceFolders ?? []).filter(folder => hostFile(folder.uri)).map(folder => folder.uri.fsPath)];
    let staging: string | undefined, stagingIdentity: Awaited<ReturnType<typeof lstat>> | undefined;
    try {
      guard(); let authenticationUnavailable = false;
      const authenticated = await this.client.tokens.hasSession().catch(() => {authenticationUnavailable = true; return false;}); guard();
      const categories: (vscode.QuickPickItem & {category: Category})[] = [];
      if (workspace) categories.push({label: "작성 중인 코드", description: plain(workspace.title ?? "현재 과제 폴더"), detail: "저장된 코드의 독립 복사본 · 미저장 파일은 다음 단계에서 확인", category: "current", picked: true});
      if (authenticated) categories.push(
        {label: "이전 제출본", description: "접수 시점별 코드 선택", category: "submissions"},
        {label: "강의자료", description: "현재 공개된 Git 콘텐츠 선택", category: "content"},
        {label: "개인 배포 자료", description: "본인에게 배포된 PDF·ZIP 선택", category: "personal"});
      if (!categories.length) {await vscode.window.showInformationMessage("내보낼 코드 폴더를 열거나 수령 코드로 연결하세요. 서버 자료는 학생 인증이 필요합니다."); return;}
      const selected = await pick(categories, {title: "내 학습내용 내보내기 · 항목 선택", placeHolder: authenticationUnavailable
        ? "서버 인증을 확인할 수 없어 현재 코드만 제공합니다. 로컬 코드는 연결 없이 내보낼 수 있습니다."
        : "여러 항목을 선택할 수 있습니다. 공식 제출과 별개이며 기존 파일은 변경하지 않습니다."});
      if (!selected?.length) return;
      const chosen = new Set(selected.map(item => item.category));
      const versions: {assignment: Assignment; version: SubmissionVersion}[] = [];
      const manifests: ContentManifest[] = []; let contentContext: ContentContext | undefined;
      const materials: StudentMaterial[] = [];
      if (chosen.has("submissions")) {
        const assignments = this.options.assignments().filter(item => item.deliveryMode === "bundle" || item.submissionMode === "bundle");
        if (!assignments.length) throw new Error("현재 인증된 과제에 내려받을 제출본이 없습니다. 해당 과제의 수령 코드로 다시 연결하세요.");
        const assignmentChoices = await pick(assignments.map(assignment => ({label: plain(assignment.title), description: assignment.id, assignment})), {title: "제출본을 가져올 과제"});
        if (!assignmentChoices?.length) return;
        for (const {assignment} of assignmentChoices) {
          const history = await this.client.getSubmissionHistory(assignment.id); guard();
          if (!history.submissions.length) throw new Error(`${plain(assignment.title)}: 서버가 접수한 제출본이 없습니다.`);
          const choices = await pick(history.submissions.map(version => ({label: new Date(version.receivedAt).toLocaleString(),
            description: `${version.state} · ${version.sourceDigest.slice(0, 12)}`, version})),
          {title: `${plain(assignment.title)} · 제출본 선택${history.hasMore ? " (최근 100건)" : ""}`, placeHolder: "여러 버전을 선택할 수 있습니다. 현재 제출 대상으로 연결하지 않습니다."});
          if (!choices?.length) return;
          versions.push(...choices.map(({version}) => ({assignment, version})));
        }
      }
      if (chosen.has("content")) {
        contentContext = await this.content.context(controller.signal); guard();
        const catalog = await this.content.catalog(contentContext, controller.signal); guard();
        if (!catalog.collections.length) throw new Error("현재 공개된 강의자료가 없습니다. 항목 선택에서 강의자료를 제외하고 다시 진행하세요.");
        const choices = await pick(catalog.collections.map(collection => ({label: plain(collection.title), description: `배포 v${collection.generation}`, collection})), {title: "내보낼 강의자료 선택"});
        if (!choices?.length) return;
        for (const {collection} of choices) {const manifest = await this.content.manifest(collection.release_id, contentContext, controller.signal); guard(); manifests.push(manifest);}
      }
      if (chosen.has("personal")) {
        const catalog = await this.client.getStudentMaterials(controller.signal); guard();
        const available = catalog.materials.filter(material => material.availability === "open");
        if (!available.length) throw new Error("현재 받을 수 있는 개인 배포 자료가 없습니다. 항목 선택에서 개인 배포 자료를 제외하세요.");
        const choices = await pick(available.map(material => ({label: plain(material.title), description: `${material.format.toUpperCase()} · v${material.fileRevision}`, material})), {title: "내보낼 개인 배포 자료 선택"});
        if (!choices?.length) return; materials.push(...choices.map(item => item.material));
      }
      const format = await pickOne([{label: "ZIP 압축 파일", format: "zip" as const}, {label: "새 폴더", format: "folder" as const}],
        {title: "저장 형식", placeHolder: "ZIP 또는 폴더 중 하나를 선택하세요."});
      if (!format) return;
      const destination = await vscode.window.showOpenDialog({title: "학습 공간 밖의 내보내기 저장 위치", canSelectFiles: false,
        canSelectFolders: true, canSelectMany: false, openLabel: "여기에 내보내기 · 종료 후에도 보존"}); guard();
      if (!destination?.[0]) return;
      if (!hostFile(destination[0])) throw new Error("현재 IDE가 접근할 수 있는 같은 실행 환경의 개인 폴더나 USB를 선택하세요.");
      const outputDirectory = await learningExportDirectory(destination[0].fsPath, excluded()); guard();
      let skippedEditors = 0, mustRemainSaved = chosen.has("current");
      const scopedDirty = () => workspace ? vscode.workspace.textDocuments.filter(document => document.isDirty && hostFile(document.uri) && contains(workspace.rootPath, document.uri.fsPath)) : [];
      const assertSavedCode = () => {
        guard();
        if (mustRemainSaved && scopedDirty().length) throw new Error("저장 후에도 현재 과제에 미저장 변경이 남아 있습니다. 편집을 잠시 멈추고 모든 파일을 저장한 뒤 다시 내보내세요.");
      };
      if (chosen.has("current") && workspace) {
        const untitled = vscode.workspace.textDocuments.filter(document => document.isDirty && document.uri.scheme === "untitled");
        if (untitled.length) {
          const answer = await vscode.window.showWarningMessage(`이름 없는 문서 ${untitled.length}개는 내보내기에 포함되지 않습니다.`,
            {modal: true, detail: "포함하려면 취소하고 파일 → 다른 이름으로 저장으로 현재 코드 폴더에 저장한 뒤 다시 내보내세요."}, "이름 없는 문서 제외하고 계속"); guard();
          if (!answer) return; skippedEditors = untitled.length;
        }
        const dirty = scopedDirty();
        if (dirty.length) {
          const answer = await vscode.window.showWarningMessage(`현재 과제에 저장하지 않은 파일 ${dirty.length}개가 있습니다.`,
            {modal: true, detail: "저장된 파일만 내보냅니다. 이름 없는 새 편집기 문서는 먼저 코드 폴더에 저장하세요."}, "저장하고 내보내기", "디스크에 저장된 내용만"); guard();
          if (!answer) return;
          if (answer === "저장하고 내보내기") {
            for (const document of dirty) {guard(); if (!await document.save()) throw new Error("저장하지 못한 파일이 있어 내보내기를 중단했습니다."); guard();}
          } else if (answer === "디스크에 저장된 내용만") {mustRemainSaved = false; skippedEditors += dirty.length;}
          else return;
        }
      }
      assertSavedCode();
      let skipped = skippedEditors;
      await vscode.window.withProgress({location: vscode.ProgressLocation.Notification, title: "내 학습내용 내보내기", cancellable: true}, async (progress, cancellation) => {
        const subscription = cancellation.onCancellationRequested(() => controller.abort());
        try {
          if (cancellation.isCancellationRequested) controller.abort(); guard();
          const groups: LearningExportGroup[] = []; let fileCount = 0, byteCount = 0;
          const addGroup = (group: LearningExportGroup) => {
            fileCount += group.files.length; byteCount += group.files.reduce((sum, file) => sum + file.bytes.byteLength, 0);
            if (groups.length >= 100 || fileCount > MAX_LEARNING_EXPORT_FILES || byteCount > MAX_LEARNING_EXPORT_BYTES) throw new Error("선택한 항목의 합계가 파일 5,000개 또는 100 MiB를 초과합니다. 나누어 내보내세요.");
            groups.push(group);
          };
          if (chosen.has("current") && workspace) {
            const code = await collectLearningCode(workspace.rootPath, assertSavedCode); assertSavedCode(); skipped += code.skipped;
            addGroup({directory: "current-code", title: workspace.title ?? "작성 중인 코드", files: code.files, skipped: code.skipped + skippedEditors});
          }
          if (versions.length || manifests.length) {
            const cache = this.options.cacheRoot(); await mkdir(cache, {recursive: true, mode: 0o700}); guard();
            if ((await lstat(cache)).isSymbolicLink()) throw new Error("내보내기 임시 저장 경로가 올바르지 않습니다.");
            staging = await mkdtemp(path.join(cache, "export-stage-")); stagingIdentity = await lstat(staging);
          }
          for (const {assignment, version} of versions) {
            progress.report({message: "제출본 확인·수신"}); guard();
            const bytes = await this.client.getSubmissionSource(version, controller.signal); guard();
            const target = path.join(staging!, safeId(version.id)); await extractSubmissionBundle(bytes, target); guard();
            const code = await collectLearningCode(target, guard); skipped += code.skipped;
            addGroup({directory: `submissions/${safeId(assignment.id)}/${safeId(version.id)}`, title: `${assignment.title} · ${version.receivedAt}`, files: code.files, skipped: code.skipped});
          }
          for (const manifest of manifests) {
            progress.report({message: "강의자료 변경분 수신·검증"}); guard();
            const options = {root: this.options.cacheRoot(), origin, context: contentContext!, assertCurrent: guard,
              assignmentDirectories: workspaceKey ? [workspaceKey] : [], signal: controller.signal};
            await syncContent(this.content, manifest, options); guard();
            const target = await exportContent(this.content, manifest, options, staging!); guard();
            const files = await collectLearningCode(target, guard, false);
            addGroup({directory: `course-materials/${manifest.collection_id}-v${manifest.generation}`, title: manifest.title, files: files.files});
          }
          for (const material of materials) {
            progress.report({message: "개인 배포 자료 수신·검증"}); guard();
            const bytes = await this.client.getStudentMaterialFile(material, controller.signal); guard(); verifyStudentMaterial(bytes, material);
            addGroup({directory: `personal-materials/${safeId(material.id)}`, title: material.title, files: [{path: studentMaterialFilename(material), bytes}]});
          }
          for (const manifest of manifests) {await this.content.verify(manifest, controller.signal); guard();}
          progress.report({message: "독립 복사본 저장"});
          const result = await saveLearningExport(outputDirectory, format.format, groups, {assertCurrent: assertSavedCode, excludedDestinations: excluded()});
          await vscode.window.showInformationMessage(`내보내기 완료: ${result.destination}\n파일 ${result.files}개 · ${groups.length}개 항목. 코드의 숨김·인증·빌드 항목과 선택에서 제외한 미저장 문서 ${skipped}개 제외. 내보낸 파일은 IDE 종료 후에도 남습니다. 공식 제출은 별도로 진행하세요.`);
        } finally {subscription.dispose();}
      });
    } catch (error) {
      if (error instanceof RequestCancelledError || controller.signal.aborted) return;
      if (current()) await vscode.window.showWarningMessage(error instanceof ApiError && error.status === 401
        ? "서버 자료를 받으려면 유효한 학생 인증이 필요합니다. 현재 코드는 로그인 없이 내보낼 수 있습니다. 공용 실습에서는 먼저 코드를 내보내고 실습 종료 후 새로 실행하세요."
        : error instanceof Error ? error.message : "내보내기에 실패했습니다. 저장 위치와 연결을 확인하세요.");
    } finally {
      if (staging && stagingIdentity) {const info = await lstat(staging).catch(() => undefined); if (info?.ino === stagingIdentity.ino && info.dev === stagingIdentity.dev) await rm(staging, {recursive: true, force: true});}
      if (this.operation === controller) this.operation = undefined;
    }
  }
  public clear(): void {this.generation++; this.operation?.abort(); this.operation = undefined; this.pickerCancellation?.cancel(); this.pickerCancellation?.dispose(); this.pickerCancellation = undefined;}
  public dispose(): void {this.clear();}
}
