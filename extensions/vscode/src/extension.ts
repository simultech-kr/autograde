import { lstat, rm } from "node:fs/promises";
import { randomUUID } from "node:crypto";
import * as path from "node:path";

import * as vscode from "vscode";

import {
  ApiError,
  AutogradeClient,
  clearLegacyPersistedTokens,
  HttpTransport,
  isRetryableApiError,
  TokenManager,
} from "./api";
import { AssignmentClaimController } from "./assignmentClaim";
import { AssignmentOperationGate, confirmWorkspaceSwitch } from "./assignmentOperation";
import { AssignmentDocumentState } from "./assignmentDocument";
import { AssignmentDocumentView, ASSIGNMENT_DOCUMENT_SCHEME } from "./assignmentDocumentView";
import { AuthenticationController } from "./auth";
import {
  createSubmissionBundle,
  extractStarterBundle,
  extractSubmissionBundle,
  readWorkspaceMarker,
  sha256Hex,
  writeWorkspaceMarker,
} from "./bundle";
import {
  type AuthenticatedBundleRoot,
  discoverAuthenticatedBundleRoots,
} from "./bundleWorkspace";
import { GitPreflightError, cloneRepository, inspectRepository } from "./git";
import {
  CHECK_SERVER_CONNECTION_COMMAND,
  ServerConnectionMonitor,
} from "./connectionMonitor";
import {
  isAssignmentDownloadable,
  isAssignedRepositoryReady,
  isBundleAssignment,
  isSupportedWorkspacePlatform,
  normalizeTargetRef,
  repositoryMatches,
  safeAssignmentDirectoryName,
  safeRepositoryDirectoryName,
  selectRepositoryCloneUrl,
  targetRefMatches,
} from "./helpers";
import {
  clearSubmissionAttempt,
  getOrCreateSubmissionAttempt,
  type MementoLike,
  type SubmissionAttemptRequest,
} from "./submissionAttempt";
import {
  clearLegacyPersistedStudentState,
  clearStudentSessionResidue,
  EphemeralStudentState,
  getRememberedSubmissions,
  mergeServerSubmissionIds,
  rememberSubmission,
} from "./studentSessionState";
import { ServiceAddressController } from "./serviceAddress";
import { AssignmentTreeItem, AssignmentsTreeProvider } from "./tree";
import type { Assignment, GradeResult, SubmissionSummary } from "./types";
import { clearResultPanel, pauseDisplayedResult, refreshDisplayedResult, showResultPanel } from "./resultPanel";
import { diagnosticText, feedbackLocation, summarizeResult } from "./resultSummary";
import { prepareClaimWorkspace, resumeClaimWorkspace } from "./claimWorkspace";
import { showAcceptedAssignments } from "./acceptedAssignments";
import { startAssignment, type AssignmentStartAction } from "./assignmentStart";
import { revealAssignmentFolder } from "./assignmentFolderView";
import { containsPath, courseDirectory, freshAssignmentPath, preferredSubmissionRoot, requireAuthorizedActiveAssignment, validateExistingAssignment } from "./assignmentWorkspace";
import { saveAssignmentDocuments } from "./submissionPreflight";
import { SubmissionResultMonitor, type ResultScope } from "./submissionResultMonitor";
import { DownloadDiagnostic, DownloadFailure, stageLabels } from "./downloadDiagnostic";
import { clearDownloadDiagnostic, rememberDownloadDiagnostic, showDownloadDiagnostic } from "./downloadDiagnosticPanel";
import { LEARNING_HOME_COMMAND, shouldOpenLearningHome } from "./learningHome";
import { LearningHomeView } from "./learningHomeView";
import { createResultDiagnosticTarget, publishResultDiagnostics, rememberResultDiagnosticTarget, type ResultDiagnosticTarget } from "./resultDiagnostics";
import { EXAM_MODE_COMMAND, type ExamScope } from "./examMode";
import { ExamModeView } from "./examModeView";
import { StudentMaterialsView } from "./studentMaterialsView";
import { ContentSyncView, CONTENT_SYNC_COMMAND } from "./contentSyncView";
import { learningEnvironment } from "./learningEnvironment";
import { ClassroomActivityReporter, classroomEnvironment, isClassroomFileUri, isManagedClassroomSave, type ClassroomSharingStatus } from "./classroomActivity";
import { ClassroomActionError, ClassroomCommands, CLASSROOM_COMMAND_LABELS, type ClassroomCommandResponse } from "./classroomCommands";
import { findSafeStarterPreview } from "./bundleWorkspace";
import { SharedLearningSession, END_LEARNING_SESSION_COMMAND } from "./sharedLearningSession";
import { LearningExportView, LEARNING_EXPORT_COMMAND } from "./learningExportView";
import { ManagedExtensions, MANAGED_EXTENSIONS_COMMAND } from "./managedExtensions";
import { ManagedExtensionsApi } from "./managedExtensionsApi";
import { ManagedExtensionsInstaller } from "./managedExtensionsView";
import { LearningCheckpointView, SAVE_LEARNING_CHECKPOINT_COMMAND, RESTORE_LEARNING_CHECKPOINT_COMMAND } from "./learningCheckpointView";
import { learningCheckpointLabel, type LearningCheckpointStatus } from "./learningCheckpoint";
import { ServerDownloadsView, SERVER_DOWNLOADS_COMMAND } from "./serverDownloadsView";
let diagnosticExtensionVersion = "0.5.8";

const SUCCESSFUL_SUBMISSION_STATES = new Set(["accepted", "queued", "running", "graded", "published"]);
const FAILED_SUBMISSION_STATES = new Set(["rejected", "infra_failed", "assessment_failed"]);

interface ResultUi {
  readonly monitor: SubmissionResultMonitor;
  readonly tree: AssignmentsTreeProvider;
  readonly diagnostics: vscode.DiagnosticCollection;
  readonly diagnosticTargets: Map<string, ResultDiagnosticTarget>;
  assignmentId?: string;
  onAuthenticationRequired(state: EphemeralStudentState): void;
}

interface AssignmentWorkspaces {
  readonly attached: Map<string, vscode.Uri>;
  readonly status: vscode.StatusBarItem;
  readonly sharedSession?: SharedLearningSession;
  selected?: { rootPath: string; assignment: Assignment };
  onSelected?(assignmentId: string): void;
  onActivity?(kind: "submission_started" | "submission_finished", assignmentId: string): void;
}

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  const sharedSession = await SharedLearningSession.fromEnvironment(vscode.env.appName, process.env);
  await Promise.all([
    clearLegacyPersistedTokens(context.secrets),
    clearLegacyPersistedStudentState(context.globalState),
  ]);
  const transport = new HttpTransport(
    () => vscode.workspace
      .getConfiguration("autograde")
      .get<string>("serviceBaseUrl", "http://127.0.0.1:20000"),
    globalThis.fetch.bind(globalThis),
    () => vscode.workspace
      .getConfiguration("autograde")
      .get<boolean>("allowInsecureHttpPilot", false),
  );
  const tokens = new TokenManager(transport);
  const client = new AutogradeClient(transport, tokens);
  const documentState = new AssignmentDocumentState();
  const treeProvider = new AssignmentsTreeProvider(documentState);
  const documentView = new AssignmentDocumentView(documentState,
    id => client.getAssignmentDocument(id),
    id => treeProvider.getAssignments().some(assignment => assignment.id === id && isBundleAssignment(assignment)),
    () => treeProvider.setAssignments(treeProvider.getAssignments()));
  const treeView = vscode.window.createTreeView("autograde.assignments", { treeDataProvider: treeProvider });
  const output = vscode.window.createOutputChannel("Autograde");
  const diagnostics = vscode.languages.createDiagnosticCollection("autograde");
  const diagnosticTargets = new Map<string, ResultDiagnosticTarget>();
  const resultMonitor = new SubmissionResultMonitor(client);
  const connectionMonitor = new ServerConnectionMonitor(
    transport,
    vscode.window.createStatusBarItem(
      "autograde.serverConnection",
      vscode.StatusBarAlignment.Left,
      100,
    ),
  );
  const assignmentWorkspaces: AssignmentWorkspaces = {
    attached: new Map(),
    status: vscode.window.createStatusBarItem("autograde.assignmentFolder", vscode.StatusBarAlignment.Left, 99),
    sharedSession,
  };
  assignmentWorkspaces.status.command = "autograde.openAssignment";
  const clearWorkspaceSelection = (clearFolders = false): void => {
    assignmentWorkspaces.selected = undefined;
    assignmentWorkspaces.status.hide();
    if (clearFolders) assignmentWorkspaces.attached.clear();
  };
  let studentState = new EphemeralStudentState();
  let authenticationUiState = false;
  let classroomIdentity: number | undefined;
  let classroomCommands: ClassroomCommands | undefined;
  let managedExtensions: ManagedExtensions | undefined;
  let learningCheckpoints: LearningCheckpointView | undefined;
  let serverDownloads: ServerDownloadsView | undefined;
  let checkpointStatus: LearningCheckpointStatus = {state: "inactive"};
  let classroomCommandStatus = "교수자 요청 대기 · 과제 연결 필요";
  let examAssignmentId: string | undefined;
  const examScope = (): ExamScope | undefined => {
    if (!authenticationUiState || !studentState.isActive()) return undefined;
    const accepted = treeProvider.getAssignments();
    const id = examAssignmentId ?? (accepted.length === 1 ? accepted[0]?.id : undefined);
    if (!id || !accepted.some(item => item.id === id)) return undefined;
    try { return {generation: tokens.getSessionGeneration(), assignmentId: id, autogradeOrigin: new URL(transport.getBaseUrl()).origin}; }
    catch { return undefined; }
  };
  const examMode = new ExamModeView(examScope);
  const environment = learningEnvironment(vscode.env.appName, process.env);
  const studentMaterials = new StudentMaterialsView(client, {
    defaultDirectory: async () => environment.materialsDirectory ? vscode.Uri.file(environment.materialsDirectory) : undefined,
    managedDirectory: () => sharedSession?.materials,
    assignmentDirectories: () => [
      ...Array.from(assignmentWorkspaces.attached.values()).filter(uri => uri.scheme === "file").map(uri => uri.fsPath),
      ...(assignmentWorkspaces.selected ? [assignmentWorkspaces.selected.rootPath] : []),
    ],
    onAuthenticationRequired: async () => {
      studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
      await updateAuthenticationUI(false);
    },
  });
  const contentSync = new ContentSyncView(client, {
    cacheRoot: () => environment.materialsDirectory ?? path.join(context.globalStorageUri.fsPath, "learning-materials"),
    defaultDirectory: () => environment.materialsDirectory ? vscode.Uri.file(environment.materialsDirectory) : undefined,
    excludedExportDirectories: () => sharedSession ? [sharedSession.root] : [],
    assignmentDirectories: () => [
      ...Array.from(assignmentWorkspaces.attached.values()).filter(uri => uri.scheme === "file").map(uri => uri.fsPath),
      ...(assignmentWorkspaces.selected ? [assignmentWorkspaces.selected.rootPath] : []),
    ],
    onAuthenticationRequired: async () => {
      studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
      await updateAuthenticationUI(false);
    },
  });
  const learningExport = new LearningExportView(client, {
    currentWorkspace: () => {
      const selected = assignmentWorkspaces.selected;
      if (selected) return {rootPath: selected.rootPath, assignmentId: selected.assignment.id, title: selected.assignment.title};
      // A reload or expired login must not prevent rescuing this session's own
      // local drafts before ending it. No old identity or token is recovered.
      if (sharedSession) return {rootPath: sharedSession.workspace, title: "이번 공용 실습의 작업 폴더"};
      const folders = (vscode.workspace.workspaceFolders ?? []).filter(folder => ["file", "vscode-remote"].includes(folder.uri.scheme));
      const active = vscode.window.activeTextEditor?.document.uri;
      const folder = active ? folders.find(item => item.uri.scheme === active.scheme && item.uri.authority === active.authority
        && containsPath(item.uri.fsPath, active.fsPath)) : folders.length === 1 ? folders[0] : undefined;
      return folder ? {rootPath: folder.uri.fsPath, title: folder.name} : undefined;
    },
    assignments: () => treeProvider.getAssignments(),
    cacheRoot: () => sharedSession?.materials ?? environment.materialsDirectory ?? path.join(context.globalStorageUri.fsPath, "learning-materials"),
    excludedDestinations: () => [
      ...(sharedSession ? [sharedSession.root] : []),
      path.join(context.globalStorageUri.fsPath, "learning-materials"),
      ...Array.from(assignmentWorkspaces.attached.values()).map(uri => uri.fsPath),
    ],
  });
  assignmentWorkspaces.onSelected = id => { examAssignmentId = id; examMode.reconcile(); classroom.reconcile(); classroomCommands?.reconcile(); managedExtensions?.reconcile(); learningCheckpoints?.reconcile(); };
  let classroomStatus: ClassroomSharingStatus = "inactive";
  let classroomDisclosureIdentity: number | undefined;
  const learningHome = new LearningHomeView(() => ({
    authenticated: authenticationUiState,
    trusted: vscode.workspace.isTrusted,
    hasWorkspace: (vscode.workspace.workspaceFolders?.length ?? 0) > 0,
    learningPolicy: environment.policy,
    classroomStatus,
    classroomCommandStatus,
    checkpointStatus: learningCheckpointLabel(checkpointStatus),
    sharedSession: Boolean(sharedSession),
    sessionNeedsRestart: Boolean(sharedSession?.authenticationUsed && !authenticationUiState),
  }));
  const reportedEnvironment = classroomEnvironment(process.platform, vscode.env.appName, vscode.env.remoteName);
  const classroom = new ClassroomActivityReporter(client, () => {
    const selected = assignmentWorkspaces.selected;
    if (!reportedEnvironment || !authenticationUiState || !studentState.isActive() || !vscode.workspace.isTrusted
      || classroomIdentity !== tokens.getSessionGeneration() || !selected
      || !treeProvider.getAssignments().some(item => item.id === selected.assignment.id)) return undefined;
    return { baseUrl: transport.getBaseUrl(), generation: classroomIdentity, assignmentId: selected.assignment.id };
  }, reportedEnvironment ?? { os: "linux", ide: "vscode" }, status => {
    classroomStatus = status; learningHome.refresh();
    classroomCommands?.reconcile();
    managedExtensions?.reconcile(); learningCheckpoints?.reconcile();
    const identity = tokens.getSessionGeneration();
    if (status === "sharing" && classroomDisclosureIdentity !== identity) {
      classroomDisclosureIdentity = identity;
      void vscode.window.showInformationMessage(
        "수업 상태 공유: 연결된 과제의 IDE 연결·저장 발생·제출 요청을 교수자에게 전달합니다. 활동 공유 자체는 코드 내용이나 다른 프로그램 활동을 수집하지 않습니다. 별도로 연결된 과제 코드는 변경 시 30초마다 서버에 저장하며, 공식 제출·채점은 별도입니다.",
        "공유 범위 보기",
      ).then(action => { if (action && identity === tokens.getSessionGeneration()) learningHome.show(); });
    }
  });
  assignmentWorkspaces.onActivity = (kind, id) => classroom.record(kind, id);
  let classroomSaveCheckPending = false;
  let submitting = false;
  const extensionVersion = String(context.extension.packageJSON.version ?? "0.0.0");
  diagnosticExtensionVersion = extensionVersion;

  const updateAuthenticationUI = async (knownState?: boolean): Promise<boolean> => {
    const authenticated = knownState ?? await tokens.hasSession();
    if (!authenticated) { examAssignmentId = undefined; examMode.clear(); studentMaterials.clear(); contentSync.clear(); learningExport.clear(); }
    if (!authenticated) { resultMonitor.cancel(); clearResultPanel(); clearDownloadDiagnostic(); documentView.clear(); }
    if (!authenticated) clearWorkspaceSelection(true);
    if (!authenticated) diagnosticTargets.clear();
    authenticationUiState = authenticated;
    classroomIdentity = authenticated ? tokens.getSessionGeneration() : undefined;
    classroom.reconcile();
    classroomCommands?.reconcile();
    managedExtensions?.reconcile(); learningCheckpoints?.reconcile();
    learningHome.refresh();
    serverDownloads?.reconcile();
    await Promise.all([
      vscode.commands.executeCommand(
        "setContext",
        "autograde.authenticated",
        authenticated,
      ),
      ...(!authenticated
        ? [vscode.commands.executeCommand(
            "setContext",
            "autograde.hasDownloadableAssignments",
            false,
          )]
        : []),
    ]);
    // A nonempty TreeView.message hides viewsWelcome, including its login buttons.
    treeView.message = authenticated && treeProvider.getAssignments().length > 0
      ? sharedSession
        ? "이번 공용 실습에 연결된 과제입니다. 다른 과제로 바꾸려면 필요한 코드를 내보내고 실습 종료 후 새 세션에서 연결하세요."
        : "현재 수령 코드로 연결된 과제입니다. 기존 과제 열기로 이어서 작업하세요. 다른 과제는 해당 과제의 새 수령 코드로 연결합니다."
      : undefined;
    return authenticated;
  };

  const resultUi: ResultUi = {
    monitor: resultMonitor, tree: treeProvider, diagnostics, diagnosticTargets,
    onAuthenticationRequired: (expectedState) => {
      if (studentState !== expectedState || !expectedState.isActive()) return;
      void tokens.clear();
      resultMonitor.cancel();
      clearResultPanel(); clearDownloadDiagnostic();
      studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
      void updateAuthenticationUI(false);
      void vscode.window.showWarningMessage(sharedSession
        ? "로그인이 만료되어 자동 결과 확인을 중단했습니다. 접수·채점은 유지됩니다. 필요한 현재 코드를 내보낸 뒤 공용 실습을 종료하고 새 세션에서 연결하세요."
        : "로그인이 만료되어 자동 결과 확인을 중단했습니다. 접수·채점은 유지됩니다. 학생 웹에서 새 수령 코드를 받아 연결하세요.");
    },
  };

  const refreshAssignments = async (showSuccess = true): Promise<readonly Assignment[]> => {
    const activeStudentState = studentState;
    const assignments = await client.getAssignments();
    if (!activeStudentState.isActive()) {
      return [];
    }
    treeProvider.setAssignments(assignments);
    examMode.reconcile();
    await vscode.commands.executeCommand(
      "setContext",
      "autograde.hasDownloadableAssignments",
      assignments.some(isAssignmentDownloadable),
    );
    await mergeServerSubmissionIds(activeStudentState, client.transport.getBaseUrl(), assignments);
    await updateAuthenticationUI(true);
    if (showSuccess) {
      void vscode.window.showInformationMessage(`수락한 실습과제 ${assignments.length}개를 불러왔습니다.`);
    }
    return assignments;
  };

  const auth = new AuthenticationController(client, extensionVersion, (authenticated) => {
    learningExport.clear();
    clearWorkspaceSelection(true);
    studentMaterials.clear();
    contentSync.clear();
    examMode.clear(); examAssignmentId = undefined;
    diagnosticTargets.clear();
    documentView.clear();
    resultMonitor.cancel();
    clearDownloadDiagnostic();
    clearResultPanel();
    studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
    void updateAuthenticationUI(authenticated);
    if (authenticated) {
      void refreshAssignments(false).catch(showCommandError);
    }
  });
  const assignmentClaims = new AssignmentClaimController(client, extensionVersion, async (assignmentId) => {
    learningExport.clear();
    if (sharedSession) {
      try { await sharedSession.markAuthenticated(); }
      catch (error) { await tokens.clear(); await updateAuthenticationUI(false); throw error; }
    }
    studentMaterials.clear();
    contentSync.clear();
    const origin = transport.getBaseUrl();
    const generation = tokens.getSessionGeneration();
    const current = () => origin === transport.getBaseUrl() && generation === tokens.getSessionGeneration();
    examMode.clear(); examAssignmentId = assignmentId;
    clearResultPanel();
    diagnosticTargets.clear();
    clearWorkspaceSelection(true);
    documentView.clear();
    resultMonitor.cancel();
    clearDownloadDiagnostic();
    studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
    await updateAuthenticationUI(true);
    if (!current()) return;
    const assignments = await refreshAssignments(false);
    if (!current()) return;
    const accepted = assignmentId
      ? assignments.find((assignment) => assignment.id === assignmentId)
      : undefined;
    if (assignmentId && !accepted) {
      throw new Error(
        "수령 코드는 확인되었지만 과제 목록에서 찾을 수 없습니다. 잠시 후 과제 새로고침을 실행하세요.",
      );
    }
    if (!assignmentId) {
      if (!assignments.some(isAssignmentDownloadable)) {
        void vscode.window.showInformationMessage(
          "과제 승인과 로그인은 복구했지만 지금 다운로드 가능한 과제가 없습니다. 잠시 후 새로고침하세요.",
        );
        return;
      }
      await cloneAssignment(client, treeProvider, refreshAssignments, undefined, assignmentWorkspaces);
      return;
    }
    if (!accepted) {
      return;
    }
    if (!isAssignmentDownloadable(accepted)) {
      void vscode.window.showInformationMessage(
        `${accepted.title} 과제를 수락했습니다. 다운로드 준비가 끝나면 과제 새로고침 후 받으세요.`,
      );
      return;
    }
    await cloneAssignment(
      client,
      treeProvider,
      refreshAssignments,
      new AssignmentTreeItem(accepted),
      assignmentWorkspaces,
    );
  });

  const clearSessionUiForAddressChange = async (): Promise<void> => {
    resultMonitor.cancel();
    clearResultPanel();
    studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
    await updateAuthenticationUI(false);
  };
  const serviceAddresses = new ServiceAddressController(
    client,
    () => auth.signOut(),
    clearSessionUiForAddressChange,
  );
  const assignmentOperations = new AssignmentOperationGate();
  const runAssignmentCommand = (action: () => Promise<unknown>): Promise<void> => runCommand(() =>
    assignmentOperations.run(action, () => {
      void vscode.window.showInformationMessage("수령 코드 입력 또는 과제 작업이 진행 중입니다. 열린 입력창이나 진행 알림에서 완료·취소한 뒤 다시 선택하세요.");
    }),
  );
  const runServerDownload = async <T>(action: () => Promise<T>): Promise<T> => {
    if (assignmentOperations.isBusy || submitting) throw new Error("과제 작업이 진행 중입니다. 완료하거나 취소한 뒤 다시 다운로드하세요.");
    return await assignmentOperations.tryRun(action) as T;
  };
  serverDownloads = new ServerDownloadsView(client, {
    authenticated: () => authenticationUiState && studentState.isActive(),
    onAuthenticationRequired: async () => {
      studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
      await updateAuthenticationUI(false);
    },
    onActionError: error => { if (!(error instanceof vscode.CancellationError)) showCommandError(error); },
    downloadAssignment: id => runServerDownload(async () => {
      const origin = transport.getBaseUrl(), generation = tokens.getSessionGeneration();
      const assignments = await refreshAssignments(false);
      if (origin !== transport.getBaseUrl() || generation !== tokens.getSessionGeneration()) return;
      const assignment = assignments.find(item => item.id === id);
      if (!assignment || !isAssignmentDownloadable(assignment)) throw new Error("현재 인증으로 받을 수 있는 과제가 아닙니다. 목록을 새로고침하고 수령 코드를 확인하세요.");
      await cloneAssignment(client, treeProvider, refreshAssignments, new AssignmentTreeItem(assignment), assignmentWorkspaces);
    }),
    downloadMaterial: id => runServerDownload(() => studentMaterials.show(id)),
    downloadContent: id => runServerDownload(() => contentSync.show(undefined, undefined, id)),
    connect: async () => { await vscode.commands.executeCommand("autograde.redeemAssignmentClaim"); },
  });
  learningCheckpoints = new LearningCheckpointView(client, {
    currentScope: () => {
      const selected = assignmentWorkspaces.selected;
      const uri = selected && assignmentWorkspaces.attached.get(path.resolve(selected.rootPath));
      if (!authenticationUiState || !studentState.isActive() || !vscode.workspace.isTrusted || !selected || !uri
        || classroomIdentity !== tokens.getSessionGeneration() || !["file", "vscode-remote"].includes(uri.scheme)
        || !treeProvider.getAssignments().some(item => item.id === selected.assignment.id && isBundleAssignment(item))) return undefined;
      return {baseUrl: transport.getBaseUrl(), generation: tokens.getSessionGeneration(), assignmentId: selected.assignment.id,
        rootPath: selected.rootPath, scheme: uri.scheme, authority: uri.authority ?? ""};
    },
    isBusy: () => assignmentOperations.isBusy || submitting,
    withCaptureLock: action => assignmentOperations.tryRun(action),
    sharedSession,
    connect: async (uri, scope, current) => {
      const assignment = treeProvider.getAssignments().find(item => item.id === scope.assignmentId);
      if (assignment && current()) await connectAssignmentFolder(assignmentWorkspaces, uri, assignment, current);
    },
    onStatus: status => { checkpointStatus = status; learningHome.refresh(); },
  });
  classroomCommands = new ClassroomCommands(client, () => {
    const scope = classroom.getCommandContext();
    return scope && assignmentWorkspaces.selected ? {...scope, workspaceKey: assignmentWorkspaces.selected.rootPath} : undefined;
  }, async (command, signal) => {
    const approve = command.kind === "notice" ? "확인" : "수락";
    const title = CLASSROOM_COMMAND_LABELS[command.kind];
    const plain = (value: string) => value.replace(/[\u0000-\u001f\u007f]/g, " ").replace(/\$\(/g, "＄(");
    const detail = [plain(command.message),
      command.kind === "save_submit" ? "현재 연결 과제의 파일만 저장·제출합니다. 본인 작업물 확인창이 다시 표시됩니다." :
        command.kind === "sync_materials" ? "받을 강의자료를 직접 선택합니다. 기존 과제 코드는 덮어쓰지 않습니다." :
        command.kind === "check_environment" ? "서버 연결·과제 폴더·OS만 확인합니다. 컴파일러 실행·설치·파일 내용 전송은 하지 않습니다." : "",
      "확인 여부·처리 결과를 교수자에게 전달합니다.",
    ].filter(Boolean).join("\n");
    const cancellation = new vscode.CancellationTokenSource();
    const abort = () => cancellation.cancel(); signal.addEventListener("abort", abort, {once: true});
    if (signal.aborted) cancellation.cancel();
    try {
      const choice = await vscode.window.showQuickPick([
        {label: approve, description: plain(assignmentWorkspaces.selected?.assignment.title ?? "현재 연결 수업"), detail, approved: true},
        {label: "거절", detail: "요청을 실행하지 않고 거절 여부만 전달합니다.", approved: false},
      ], {title: `교수자 요청 · ${title} · ${plain(command.created_by)}`, ignoreFocusOut: true,
        placeHolder: "요청 내용을 확인한 뒤 수락 또는 거절하세요. 확인 전에는 실행하지 않습니다."}, cancellation.token);
      return choice?.approved === true;
    } finally {signal.removeEventListener("abort", abort); cancellation.dispose();}
  }, async (command, guard, signal) => {
    let response: ClassroomCommandResponse = {status: "failed", result_code: "action_failed"};
    await assignmentOperations.run(async () => {
      guard();
      const selected = assignmentWorkspaces.selected;
      if (!selected || !vscode.workspace.isTrusted) throw new ClassroomActionError("workspace_unavailable");
      if (command.assignment_id && command.assignment_id !== selected.assignment.id) throw new ClassroomActionError("scope_changed");
      if (command.kind === "notice") {response = {status: "completed", result_code: "notice_acknowledged"}; return;}
      if (command.kind === "sync_materials") {
        const outcome = await contentSync.show(guard, signal); guard();
        response = outcome === "synced" ? {status: "completed", result_code: "materials_synced"}
          : {status: "failed", result_code: outcome === "cancelled" ? "cancelled" : outcome === "unavailable" ? "unsupported" : "action_failed"};
        return;
      }
      if (command.kind === "check_environment") {
        const stat = await lstat(selected.rootPath).catch(() => undefined); guard();
        const remote = reportedEnvironment?.remote_name;
        response = {status: "completed", result_code: "environment_checked", result: {checks: {
          server: "ok", workspace: stat?.isDirectory() && !stat.isSymbolicLink() ? "ok" : "missing", compiler: "not_checked",
          os: reportedEnvironment?.os ?? "other", remote: !remote ? "none" : remote === "wsl" || remote === "ssh-remote" ? remote : "other",
        }}}; return;
      }
      if (!isBundleAssignment(selected.assignment)) throw new ClassroomActionError("unsupported");
      ensureSupportedWorkspacePlatform("교수자 과제 열기·제출 요청");
      await validateExistingAssignment(selected.rootPath, client.getBaseUrl(), selected.assignment.id); guard();
      if (command.kind === "open_assignment") {
        // Keep the authenticated window. No new login, arbitrary path, task,
        // download, or workspace replacement can be triggered by the server.
        const uri = bundleRootUri(assignmentWorkspaces, selected.rootPath);
        const preview = await findSafeStarterPreview(selected.rootPath); guard();
        await vscode.commands.executeCommand("workbench.view.explorer"); guard();
        const inWorkspace = (vscode.workspace.workspaceFolders ?? []).some(folder =>
          isClassroomFileUri(uri, folder.uri) && containsPath(folder.uri.fsPath, selected.rootPath));
        if (inWorkspace) {await vscode.commands.executeCommand("revealInExplorer", uri); guard();}
        if (preview) {
          const document = await vscode.workspace.openTextDocument(vscode.Uri.joinPath(uri, path.basename(preview))); guard();
          await vscode.window.showTextDocument(document, {preview: true}); guard();
        } else if (!inWorkspace) throw new ClassroomActionError("workspace_unavailable");
        response = {status: "completed", result_code: "assignment_opened"}; return;
      }
      if (command.kind === "save_submit") {
        if (submitting) throw new ClassroomActionError("action_failed");
        submitting = true;
        try {
          const receipt = await submitCurrentBundle(studentState, client, selected.rootPath, selected.assignment, resultUi, assignmentWorkspaces, guard);
          guard();
          response = receipt ? {status: "completed", result_code: "submission_received", result: {submission_id: receipt.id}}
            : {status: "failed", result_code: "cancelled"};
        } finally {submitting = false;}
      }
    }, () => {throw new ClassroomActionError("action_failed");});
    return response;
  }, message => {classroomCommandStatus = message; learningHome.refresh();});
  classroomCommands.reconcile();
  managedExtensions = new ManagedExtensions(new ManagedExtensionsApi(client), () => classroom.getCommandContext(),
    new ManagedExtensionsInstaller(sharedSession, environment, assignmentOperations, context.extension));
  managedExtensions.reconcile();
  const redeemAssignmentClaim = async (): Promise<void> => {
    sharedSession?.assertCanAuthenticate();
    studentMaterials.clear();
    contentSync.clear();
    examMode.clear();
    ensureSupportedWorkspacePlatform("수령 코드 입력 및 다운로드");
    if (sharedSession) {
      await sharedSession.assertWorkspacePath(sharedSession.workspace);
      if (!(vscode.workspace.workspaceFolders ?? []).some(folder => path.resolve(folder.uri.fsPath) === sharedSession.workspace)) {
        throw new Error("공용 실습의 기본 작업 폴더가 닫혔습니다. IDE를 종료하고 실행기로 새 실습 세션을 시작하세요.");
      }
    } else if (!await prepareClaimWorkspace(context.globalState)) return;
    if (!vscode.workspace.isTrusted) {
      throw new Error("수령 코드로 파일을 받으려면 현재 workspace를 신뢰해야 합니다.");
    }
    await assignmentClaims.redeem();
  };

  let endingSharedSession = false;
  const endSharedSession = async (): Promise<void> => {
    if (!sharedSession || endingSharedSession) return;
    endingSharedSession = true;
    try {
    const action = await vscode.window.showWarningMessage("공용 실습을 종료하면 이번 세션의 코드·자료·IDE 기록을 정리합니다.", {
      modal: true, detail: "서버 저장이 확인된 작업본·공식 제출본과 세션 밖에 내보낸 파일은 보존됩니다. 종료 전에 현재 작업의 서버 저장을 잠시 시도합니다. 저장을 확인하지 못한 코드는 먼저 내보내세요. 종료를 취소하면 파일이 남아 있으므로 다음 학생에게 자리를 넘기지 마세요.",
    }, "먼저 내보내기", "실습 종료");
    if (action === "먼저 내보내기") { await vscode.commands.executeCommand("autograde.exportLearning"); return; }
    if (action !== "실습 종료") return;
    const saved = await learningCheckpoints?.flushBeforeExit();
    if (!saved && assignmentWorkspaces.selected) {
      const decision = await vscode.window.showWarningMessage("최신 작업의 서버 저장을 확인하지 못했습니다.",
        {modal: true, detail: "종료 후와 다음 시작 시 로컬 실습 파일이 정리됩니다. 서버에 저장되지 않은 코드는 복구할 수 없으므로 필요한 파일은 먼저 내보내세요."},
        "먼저 내보내기", "저장 없이 종료");
      if (decision === "먼저 내보내기") { await vscode.commands.executeCommand("autograde.exportLearning"); return; }
      if (decision !== "저장 없이 종료") return;
    }
    // Revoke best-effort; local exit/cleanup must remain available when offline.
    try { if (await tokens.hasSession()) await client.revokeCurrentSession(); } catch { /* The server session expires independently. */ }
    await tokens.clear();
    studentMaterials.clear(); contentSync.clear(); examMode.clear();
    studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
    await updateAuthenticationUI(false);
    // The launcher owns cleanup and waits for the whole IDE process. Never delete on extension-host reload.
    await vscode.commands.executeCommand("workbench.action.quit");
    } finally { endingSharedSession = false; }
  };

  context.subscriptions.push(
    classroom,
    learningCheckpoints,
    vscode.commands.registerCommand(SAVE_LEARNING_CHECKPOINT_COMMAND, () => runCommand(() => learningCheckpoints!.saveNow())),
    vscode.commands.registerCommand(RESTORE_LEARNING_CHECKPOINT_COMMAND, () => runAssignmentCommand(() => learningCheckpoints!.restore())),
    classroomCommands,
    managedExtensions,
    vscode.commands.registerCommand(MANAGED_EXTENSIONS_COMMAND, () => managedExtensions!.check(true)),
    vscode.workspace.onDidChangeTextDocument(event => { if (event.contentChanges.length) learningCheckpoints?.recordEdit(event.document.uri); }),
    vscode.workspace.onDidSaveTextDocument(document => {
      learningCheckpoints?.recordEdit(document.uri);
      // Never inspect or report unrelated workspaces, unsaved buffers, or non-file resources.
      const selected = assignmentWorkspaces.selected;
      if (classroomSaveCheckPending || !selected || !authenticationUiState
        || !isClassroomFileUri(document.uri, assignmentWorkspaces.attached.get(path.resolve(selected.rootPath)))) return;
      const generation = tokens.getSessionGeneration();
      const baseUrl = transport.getBaseUrl();
      classroomSaveCheckPending = true;
      void isManagedClassroomSave(selected.rootPath, document.uri.fsPath).then(managed => {
        if (managed && selected === assignmentWorkspaces.selected && generation === tokens.getSessionGeneration()
          && baseUrl === transport.getBaseUrl()) classroom.record("file_saved", selected.assignment.id);
      }).catch(() => { /* Best-effort hints must not disrupt editing. */ })
        .finally(() => { classroomSaveCheckPending = false; });
    }),
    contentSync,
    vscode.commands.registerCommand(CONTENT_SYNC_COMMAND, () => runAssignmentCommand(() => contentSync.show())),
    learningExport,
    vscode.commands.registerCommand(LEARNING_EXPORT_COMMAND, () => runAssignmentCommand(() => learningExport.show())),
    studentMaterials,
    vscode.commands.registerCommand("autograde.studentMaterials", () => runAssignmentCommand(() => studentMaterials.show())),
    examMode,
    vscode.commands.registerCommand(EXAM_MODE_COMMAND, () => examMode.show()),
    learningHome,
    vscode.commands.registerCommand(LEARNING_HOME_COMMAND, () => learningHome.show()),
    serverDownloads,
    vscode.commands.registerCommand(SERVER_DOWNLOADS_COMMAND, () => serverDownloads!.show()),
    vscode.commands.registerCommand(END_LEARNING_SESSION_COMMAND, () => runCommand(endSharedSession)),
    vscode.workspace.onDidChangeWorkspaceFolders(() => { learningHome.refresh(); classroom.reconcile(); classroomCommands?.reconcile(); managedExtensions?.reconcile(); learningCheckpoints?.reconcile(); }),
    documentView,
    treeProvider.onDidChangeTreeData(() => { documentView.reconcileAccepted(); classroom.reconcile(); classroomCommands?.reconcile(); managedExtensions?.reconcile(); learningCheckpoints?.reconcile(); }),
    vscode.workspace.registerTextDocumentContentProvider(ASSIGNMENT_DOCUMENT_SCHEME, documentView),
    vscode.commands.registerCommand("autograde.viewAssignmentDocument", (item?: AssignmentTreeItem) => runAssignmentCommand(async () => {
      const candidates = treeProvider.getAssignments().filter(isBundleAssignment);
      const requested = item?.assignment ?? (candidates.length === 1 ? candidates[0] : await pickAssignment(candidates));
      const assignment = treeProvider.getAssignments().find(candidate => candidate.id === requested?.id && isBundleAssignment(candidate));
      if (assignment) await documentView.show(assignment);
    })),
    { dispose: clearDownloadDiagnostic },
    vscode.commands.registerCommand("autograde.downloadDiagnostics", () => showDownloadDiagnostic()),
    { dispose: clearResultPanel },
    { dispose: () => resultMonitor.cancel() },
    vscode.commands.registerCommand("autograde.refreshDisplayedResult", () => runCommand(refreshDisplayedResult)),
    vscode.commands.registerCommand("autograde.pauseDisplayedResult", pauseDisplayedResult),
    connectionMonitor,
    assignmentWorkspaces.status,
    treeView,
    output,
    diagnostics,
    vscode.commands.registerCommand(
      "autograde.configureServiceAddress",
      () => runAssignmentCommand(async () => { sharedSession?.assertCanAuthenticate(); await serviceAddresses.configure(); }),
    ),
    vscode.commands.registerCommand(
      CHECK_SERVER_CONNECTION_COMMAND,
      () => runCommand(() => connectionMonitor.checkNow()),
    ),
    vscode.commands.registerCommand("autograde.signIn", () => runAssignmentCommand(async () => {
      sharedSession?.assertCanAuthenticate();
      studentMaterials.clear(); contentSync.clear(); examMode.clear();
      await auth.signIn();
      if (sharedSession && await tokens.hasSession()) {
        try { await sharedSession.markAuthenticated(); learningHome.refresh(); }
        catch (error) { await tokens.clear(); await updateAuthenticationUI(false); throw error; }
      }
    })),
    vscode.commands.registerCommand("autograde.signOut", () => sharedSession ? runCommand(endSharedSession)
      : runCommand(() => { studentMaterials.clear(); contentSync.clear(); examMode.clear(); return auth.signOut(); })),
    vscode.commands.registerCommand(
      "autograde.redeemAssignmentClaim",
      () => runAssignmentCommand(redeemAssignmentClaim),
    ),
    vscode.commands.registerCommand("autograde.refreshAssignments", () => runCommand(() => refreshAssignments())),
    vscode.commands.registerCommand("autograde.acceptedAssignmentHistory", () => runAssignmentCommand(() => showAcceptedAssignments(client,
      async id => {
        const assignment = treeProvider.getAssignments().find(candidate => candidate.id === id);
        if (!assignment) throw new Error("현재 연결된 과제 목록을 새로고침한 뒤 다시 시도하세요.");
        await openExistingAssignment(client, treeProvider, assignmentWorkspaces, new AssignmentTreeItem(assignment));
      },
      async () => { await redeemAssignmentClaim(); },
    ))),
    vscode.commands.registerCommand(
      "autograde.cloneAssignment",
      (item?: AssignmentTreeItem) => runAssignmentCommand(() => cloneAssignment(client, treeProvider, refreshAssignments, item, assignmentWorkspaces)),
    ),
    vscode.commands.registerCommand(
      "autograde.openAssignment",
      (item?: AssignmentTreeItem) => runAssignmentCommand(() => openExistingAssignment(client, treeProvider, assignmentWorkspaces, item)),
    ),
    vscode.commands.registerCommand("autograde.openCourseWorkspace", () => runAssignmentCommand(async () => {
      if (sharedSession) throw new Error("공용 실습은 현재 수업 폴더에서 계속 작업하세요. 폴더 전환으로 인증이 종료되면 내보낸 뒤 새 실습 세션을 시작해야 합니다.");
      const target = assignmentWorkspaces.selected;
      if (!target) throw new Error("기존 과제 열기로 과제 폴더를 먼저 연결하세요.");
      const origin = transport.getBaseUrl();
      const generation = tokens.getSessionGeneration();
      const current = () => origin === transport.getBaseUrl() && generation === tokens.getSessionGeneration();
      const uri = bundleRootUri(assignmentWorkspaces, target.rootPath);
      const course = await courseDirectory(target.rootPath);
      if (!current()) return;
      const action = "수업 폴더로 전환";
      await confirmWorkspaceSwitch({
        isCurrent: current,
        validate: () => validateExistingAssignment(target.rootPath, origin, target.assignment.id),
        confirm: async () => await vscode.window.showInformationMessage("탐색기와 빌드의 작업 영역을 수업 폴더로 바꿉니다.", {
          modal: true, detail: `${course}\n\n창이 다시 열리면서 메모리의 로그인은 종료됩니다. 새 수령 코드로 다시 연결한 뒤 기존 과제 이어서 열기를 선택하세요. 저장하지 않은 파일은 VS Code의 저장 안내를 따라 주세요.`,
        }, action) === action,
        open: async () => { await vscode.commands.executeCommand("vscode.openFolder", vscode.Uri.joinPath(uri, path.relative(target.rootPath, course)), false); },
      });
    })),
    vscode.commands.registerCommand(
      "autograde.submitCurrentCommit",
      (item?: AssignmentTreeItem) => runAssignmentCommand(async () => {
        if (submitting) {
          void vscode.window.showInformationMessage("앞선 제출의 접수를 확인하고 있습니다. 접수번호가 표시된 뒤 다시 제출하세요.");
          return;
        }
        submitting = true;
        try { await submitCurrentCommit(studentState, client, refreshAssignments, resultUi, assignmentWorkspaces, item); }
        finally { submitting = false; }
      }),
    ),
    vscode.commands.registerCommand(
      "autograde.submissionHistory",
      (item?: AssignmentTreeItem) => runAssignmentCommand(() => viewSubmissionHistory(studentState, client, treeProvider, output, diagnostics, resultUi, assignmentWorkspaces, item)),
    ),
    vscode.commands.registerCommand(
      "autograde.viewLatestResult",
      (item?: AssignmentTreeItem) => runAssignmentCommand(() => viewLatestResult(studentState, client, treeProvider, output, diagnostics, resultUi, item)),
    ),
    treeView.onDidChangeSelection(event => {
      const selected = event.selection[0];
      documentView.select(selected instanceof AssignmentTreeItem ? selected.assignment.id : selected?.assignmentId);
      const selectedId = selected instanceof AssignmentTreeItem ? selected.assignment.id : selected?.assignmentId;
      if (selectedId) { examAssignmentId = selectedId; examMode.reconcile(); }
      if (selected instanceof AssignmentTreeItem) {
        if (resultUi.assignmentId && resultUi.assignmentId !== selected.assignment.id) {
          resultMonitor.cancel();
          clearResultPanel();
          diagnostics.clear();
        }
        resultUi.assignmentId = selected.assignment.id;
      }
    }),
    vscode.workspace.onDidGrantWorkspaceTrust(() => {
      treeProvider.setAssignments(treeProvider.getAssignments());
      learningHome.refresh();
      serverDownloads?.reconcile();
      classroom.reconcile(); learningCheckpoints?.reconcile();
    }),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (event.affectsConfiguration("autograde.examControlBaseUrl")) examMode.clear();
      if (
        !event.affectsConfiguration("autograde.serviceBaseUrl") &&
        !event.affectsConfiguration("autograde.allowInsecureHttpPilot")
      ) {
        return;
      }
      examMode.clear();
      studentMaterials.clear();
      contentSync.clear();
      void connectionMonitor.handleAddressChange();
      const wasAuthenticated = authenticationUiState;
      void runCommand(async () => {
        await tokens.clear();
        await clearSessionUiForAddressChange();
        if (wasAuthenticated) {
          void vscode.window.showWarningMessage(
            sharedSession
              ? "연결 설정이 바뀌어 이 기기의 로그인을 지웠습니다. 필요한 현재 코드를 내보낸 뒤 공용 실습을 종료하고 새 세션에서 연결하세요. 기존 서버 세션은 만료될 때까지 남을 수 있습니다."
              : "Autograde 연결 설정이 바뀌어 이 기기의 로그인을 지웠습니다. Settings에서 직접 주소를 바꾼 경우 기존 서버 세션은 만료될 때까지 남을 수 있습니다.",
          );
        }
      });
    }),
  );

  connectionMonitor.start();
  await vscode.commands.executeCommand("setContext", "autograde.sharedSession", Boolean(sharedSession));
  if (sharedSession) {
    learningHome.show();
    void vscode.window.showInformationMessage(sharedSession.authenticationUsed
      ? "이 공용 실습의 인증이 종료되었습니다. 필요한 현재 코드를 내보낸 뒤 ‘공용 실습 종료’를 누르고 새 세션에서 연결하세요."
      : "공용 실습: 새 작업 폴더를 사용합니다. IDE를 완전히 종료하면 코드·자료·기록이 정리됩니다. 보관할 내용은 먼저 내보내세요.");
  }
  if (shouldOpenLearningHome(vscode.env.appName, vscode.workspace.workspaceFolders?.length ?? 0,
    Boolean(vscode.workspace.workspaceFile), vscode.window.tabGroups.all.reduce((count, group) => count + group.tabs.length, 0))) {
    learningHome.show();
  }
  void updateAuthenticationUI(false);
  void tokens.hasSession().then((hasSession) => {
    void updateAuthenticationUI(hasSession);
    if (hasSession) {
      return refreshAssignments(false);
    }
    return undefined;
  }).catch(() => {
    treeProvider.clear();
  });
  // Restore navigation only after a requested folder transition, never stored authentication.
  void runCommand(() => resumeClaimWorkspace(context.globalState, () => learningHome.show()));
}

export function deactivate(): void {}

async function submitCurrentCommit(
  studentState: EphemeralStudentState,
  client: AutogradeClient,
  refreshAssignments: (showSuccess?: boolean) => Promise<readonly Assignment[]>,
  resultUi: ResultUi,
  workspaces: AssignmentWorkspaces,
  selectedItem?: AssignmentTreeItem,
): Promise<void> {
  if (!vscode.workspace.isTrusted) {
    throw new Error("제출하려면 이 workspace를 신뢰해야 합니다.");
  }
  const workspaceFolder = selectWorkspaceFolder();
  if (!workspaceFolder) {
    throw new Error("제출할 과제 workspace를 먼저 여세요.");
  }
  ensureSupportedWorkspacePlatform("제출");

  const assignments = await refreshAssignments(false);
  if (!studentState.isActive()) {
    return;
  }
  const selectedAssignment = selectedItem?.assignment;
  if (selectedAssignment && isBundleAssignment(selectedAssignment)) {
    const current = assignments.find((candidate) =>
      candidate.id === selectedAssignment.id && isBundleAssignment(candidate)
    );
    if (!current) {
      throw new Error("선택한 bundle 과제가 현재 학생에게 제공되지 않습니다. 과제 목록을 새로고침하세요.");
    }
    const target = await selectBundleSubmissionTarget(client, assignments, workspaces, current.id, true);
    if (target) {
      await submitCurrentBundle(studentState, client, target.rootPath, current, resultUi, workspaces);
    }
    return;
  }
  if (!selectedAssignment) {
    const target = await selectBundleSubmissionTarget(client, assignments, workspaces);
    if (target === null) return;
    if (target) {
      await submitCurrentBundle(
        studentState,
        client,
        target.rootPath,
        target.assignment,
        resultUi,
        workspaces,
      );
      return;
    }
  }

  let repositoryRoot = workspaceFolder.uri.fsPath;
  if (workspaces.sharedSession) {
    const selected = workspaces.selected;
    if (!selected || isBundleAssignment(selected.assignment)
      || !assignments.some(assignment => assignment.id === selected.assignment.id && assignment.repository)
      || (selectedAssignment && selectedAssignment.id !== selected.assignment.id)) {
      throw new Error("이번 공용 실습에서 받은 Git 과제 폴더를 먼저 연결하세요. 다른 폴더의 저장소를 대신 제출하지 않습니다.");
    }
    await workspaces.sharedSession.assertWorkspacePath(selected.rootPath);
    repositoryRoot = selected.rootPath;
  }
  let repository;
  try {
    repository = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Git repository와 원격 push 상태 확인 중" },
      () => inspectRepository(repositoryRoot),
    );
  } catch (error) {
    if (
      !selectedAssignment &&
      error instanceof GitPreflightError &&
      error.message === "현재 workspace가 Git repository가 아닙니다." &&
      assignments.some((candidate) => isBundleAssignment(candidate))
    ) {
      throw new Error(
        "제출할 bundle 과제 폴더를 찾지 못했습니다. 다운로드한 과제 폴더 안의 파일을 연 뒤 다시 제출하세요.",
      );
    }
    throw error;
  }

  const matching = assignments.filter((assignment) =>
    assignment.repository && repositoryMatches(repository.remoteUrl, assignment.repository),
  );
  if (matching.length === 0) {
    throw new GitPreflightError("현재 원격 repository는 로그인한 학생에게 할당된 과제가 아닙니다.");
  }

  let assignment = selectedItem?.assignment;
  if (assignment && !matching.some((candidate) => candidate.id === assignment?.id)) {
    throw new GitPreflightError("선택한 과제와 현재 workspace의 원격 repository가 다릅니다.");
  }
  if (!assignment) {
    assignment = matching.length === 1 ? matching[0] : await pickAssignment(matching);
  }
  if (!assignment) {
    return;
  }
  const assignedRepository = assignment.repository;
  if (!assignedRepository) {
    throw new Error("서비스 응답에 할당된 repository 정보가 없습니다.");
  }
  if (!isAssignedRepositoryReady(assignedRepository)) {
    throw new Error(`과제 repository 상태가 ready가 아닙니다 (${assignedRepository.state ?? "not_ready"}).`);
  }
  const githubRepositoryId = assignedRepository.githubRepositoryId;
  if (githubRepositoryId === undefined) {
    throw new Error("서비스 응답에 GitHub numeric repository ID가 없어 안전하게 제출할 수 없습니다.");
  }
  const targetRef = normalizeTargetRef(assignedRepository.targetRef);
  if (!targetRef) {
    throw new Error("서비스 응답에 유효한 제출 target_ref가 없습니다.");
  }
  if (!targetRefMatches(repository.remoteRef, targetRef)) {
    throw new GitPreflightError(
      `현재 branch upstream(${repository.remoteRef})이 과제 제출 ref(${targetRef})와 다릅니다. 올바른 branch로 전환해 push하세요.`,
    );
  }

  const pullRequestNumber = assignment.submissionMode?.toLowerCase() === "pull_request"
    ? await promptForPullRequestNumber()
    : undefined;
  if (assignment.submissionMode?.toLowerCase() === "pull_request" && pullRequestNumber === undefined) {
    return;
  }

  const confirm = await vscode.window.showInformationMessage(
    `${assignment.title}의 현재 commit을 제출하시겠습니까?`,
    {
      modal: true,
      detail: [
        `Branch: ${repository.branch}`,
        `Commit: ${repository.headSha.slice(0, 12)}`,
        `Remote: ${assignedRepository.fullName ?? "할당된 repository"}`,
        pullRequestNumber === undefined ? undefined : `Pull Request: #${pullRequestNumber}`,
      ].filter(Boolean).join("\n"),
    },
    "제출",
  );
  if (confirm !== "제출") {
    return;
  }
  if (!studentState.isActive()) {
    throw new Error("로그인 계정이 변경되었습니다. 현재 계정의 과제 목록에서 다시 제출하세요.");
  }

  const attemptRequest: SubmissionAttemptRequest = {
    serviceBaseUrl: client.transport.getBaseUrl(),
    assignmentId: assignment.id,
    githubRepositoryId,
    headSha: repository.headSha,
    pullRequestNumber,
  };
  const attempt = await getOrCreateSubmissionAttempt(studentState, attemptRequest);
  let submission: SubmissionSummary;
  workspaces.onActivity?.("submission_started", assignment.id);
  try {
    submission = await client.submit(
      assignment.id,
      githubRepositoryId,
      repository.headSha,
      attempt.idempotencyKey,
      pullRequestNumber,
    );
  } catch (error) {
    if (!isRetryableApiError(error)) {
      await clearSubmissionAttempt(studentState, attemptRequest, attempt.idempotencyKey);
    }
    throw error;
  }
  if (!studentState.isActive()) {
    return;
  }
  workspaces.onActivity?.("submission_finished", assignment.id);
  await rememberSubmission(studentState, client.transport.getBaseUrl(), assignment.id, submission.id);
  rememberResultDiagnosticTarget(resultUi.diagnosticTargets, submission.id,
    createResultDiagnosticTarget(workspaceFolder.uri, assignment.assignmentPath));
  await clearSubmissionAttempt(studentState, attemptRequest, attempt.idempotencyKey);

  if (!SUCCESSFUL_SUBMISSION_STATES.has(submission.state.toLowerCase()) && !FAILED_SUBMISSION_STATES.has(submission.state.toLowerCase())) {
    submission = await waitForSubmissionResolution(client, submission);
  }
  if (!studentState.isActive()) {
    return;
  }

  const normalizedState = submission.state.toLowerCase();
  if (FAILED_SUBMISSION_STATES.has(normalizedState)) {
    throw new Error(`제출이 완료되지 않았습니다 (${submission.state}). 과제 상태를 확인하세요.`);
  }
  if (SUCCESSFUL_SUBMISSION_STATES.has(normalizedState)) {
    void vscode.window.showInformationMessage(
      `${assignment.title} 제출이 접수되었습니다.`,
    );
  } else {
    void vscode.window.showInformationMessage(
      `${assignment.title} 제출을 서버가 검증 중입니다. 잠시 후 상태를 새로고침하세요.`,
    );
  }
  await refreshAssignments(false);
}

async function submitCurrentBundle(
  studentState: EphemeralStudentState,
  client: AutogradeClient,
  assignmentRoot: string,
  assignment: Assignment,
  resultUi: ResultUi,
  workspaces: AssignmentWorkspaces,
  assertRequestCurrent: () => void = () => {},
): Promise<SubmissionSummary | undefined> {
  assertRequestCurrent();
  if (!isAssignmentDownloadable(assignment)) {
    throw new Error(`과제 bundle이 아직 다운로드 가능한 상태가 아닙니다 (${assignment.status ?? "not_ready"}).`);
  }
  const marker = await readWorkspaceMarker(assignmentRoot);
  if (
    !marker ||
    marker.serviceBaseUrl !== client.transport.getBaseUrl() ||
    marker.assignmentId !== assignment.id
  ) {
    throw new Error(
      "과제 폴더의 표시가 현재 서버 과제와 일치하지 않습니다. 과제 목록에서 다시 다운로드하세요.",
    );
  }

  const origin = client.transport.getBaseUrl();
  const stillCurrent = (): boolean => {
    assertRequestCurrent();
    return studentState.isActive() && client.transport.getBaseUrl() === origin;
  };
  const submissionWorkspace = (vscode.workspace.workspaceFolders ?? []).filter(folder => {
    const relative = path.relative(folder.uri.fsPath, assignmentRoot);
    return relative !== ".." && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative);
  }).sort((left, right) => right.uri.fsPath.length - left.uri.fsPath.length)[0];
  const submissionUri = submissionWorkspace?.uri ?? workspaces.attached.get(path.resolve(assignmentRoot));
  if (!submissionUri) throw new Error("기존 과제 열기에서 제출할 과제 폴더를 먼저 연결하세요.");
  const diagnosticTarget = createResultDiagnosticTarget(
    vscode.Uri.joinPath(submissionUri, path.relative(submissionUri.fsPath, assignmentRoot)),
  );
  if (!await saveAssignmentDocuments(assignmentRoot, () => vscode.workspace.textDocuments, async (files) => {
    const save = "저장 후 제출";
    return await vscode.window.showInformationMessage("이 과제에 저장하지 않은 코드가 있습니다.",
      { modal: true, detail: `${files.join("\n")}\n\n이 파일들을 저장한 뒤 제출합니다. 다른 과제의 파일은 저장하지 않습니다.` }, save) === save;
  }, stillCurrent, submissionUri)) return;
  assertRequestCurrent();

  const bundle = await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: `${assignment.title} 제출 bundle 생성 중`,
    },
    () => createSubmissionBundle(assignmentRoot),
  );
  const submitAction = "본인 작업물 확인 후 제출";
  const confirm = await vscode.window.showInformationMessage(
    `${assignment.title} 과제 폴더의 파일을 제출하시겠습니까?`,
    {
      modal: true,
      detail: [
        `파일: ${bundle.fileCount.toLocaleString()}개`,
        `원본 크기: ${formatBytes(bundle.sourceBytes)}`,
        `Bundle SHA-256: ${bundle.sha256.slice(0, 16)}…`,
        `과제 폴더: ${assignmentRoot}`,
        "저장된 디스크 파일을 제출합니다. 이후 변경은 다음 제출에 반영됩니다.",
        ".git, .autograde 및 일반적인 build/cache 디렉터리는 포함하지 않습니다.",
        "공용 PC에서는 표시된 과제 폴더가 본인의 작업물인지 확인하세요. 로그아웃해도 이 파일은 삭제되지 않습니다.",
      ].join("\n"),
    },
    submitAction,
  );
  if (confirm !== submitAction) {
    return;
  }
  assertRequestCurrent();
  if (!studentState.isActive()) {
    throw new Error("로그인 계정이 변경되었습니다. 현재 계정의 과제 목록에서 다시 제출하세요.");
  }

  const attemptRequest: SubmissionAttemptRequest = {
    serviceBaseUrl: client.transport.getBaseUrl(),
    assignmentId: assignment.id,
    bundleSha256: bundle.sha256,
  };
  const attempt = await getOrCreateSubmissionAttempt(studentState, attemptRequest);
  const resultScope = resultUi.monitor.begin();
  resultUi.assignmentId = assignment.id;
  clearResultPanel();
  resultUi.diagnostics.clear();
  let submission: SubmissionSummary;
  workspaces.onActivity?.("submission_started", assignment.id);
  try {
    assertRequestCurrent();
    submission = await client.submitBundle(
      assignment.id,
      bundle.archive,
      attempt.idempotencyKey,
    );
    // The server may have accepted the upload even if the classroom request
    // expires or the workspace changes while awaiting its response. Preserve
    // that server receipt, but never restore stale result UI into a new scope.
    assertRequestCurrent();
  } catch (error) {
    if (!isRetryableApiError(error)) {
      await clearSubmissionAttempt(studentState, attemptRequest, attempt.idempotencyKey);
    }
    throw error;
  }
  if (!studentState.isActive()) {
    return;
  }
  workspaces.onActivity?.("submission_finished", assignment.id);
  await rememberSubmission(studentState, client.transport.getBaseUrl(), assignment.id, submission.id);
  rememberResultDiagnosticTarget(resultUi.diagnosticTargets, submission.id, diagnosticTarget);
  await clearSubmissionAttempt(studentState, attemptRequest, attempt.idempotencyKey);

  assertRequestCurrent();
  resultUi.tree.setAssignments(resultUi.tree.getAssignments().map(item => item.id === assignment.id
    ? { ...item, latestSubmission: submission } : item));
  watchSubmissionResult(resultUi, resultScope, studentState, client, assignment, submission, "이번 제출");
  void vscode.window.showInformationMessage(`${assignment.title} 제출 접수 완료 · 접수번호 ${submission.id}. 채점 결과는 자동으로 확인합니다.`);
  return submission;
}

async function viewSubmissionHistory(
  studentState: EphemeralStudentState, client: AutogradeClient,
  tree: AssignmentsTreeProvider, output: vscode.OutputChannel,
  diagnostics: vscode.DiagnosticCollection, resultUi: ResultUi, workspaces: AssignmentWorkspaces, item?: AssignmentTreeItem,
): Promise<void> {
  const origin = client.getBaseUrl();
  const generation = client.tokens.getSessionGeneration?.();
  const current = () => studentState.isActive() && client.getBaseUrl() === origin && client.tokens.getSessionGeneration?.() === generation;
  const checkSession = () => {
    if (!current()) {
      throw new Error("로그인 또는 서버가 변경되었습니다. 제출 기록을 다시 여세요.");
    }
  };
  const assignment = item?.assignment ?? await pickAssignment(tree.getAssignments().filter(isBundleAssignment));
  if (!assignment || !isBundleAssignment(assignment)) return;
  checkSession();
  const history = await client.getSubmissionHistory(assignment.id);
  checkSession();
  if (!history.submissions.length) {
    void vscode.window.showInformationMessage("서버가 접수한 제출 기록이 없습니다."); return;
  }
  const selection = await vscode.window.showQuickPick(history.submissions.map(version => ({
    label: new Date(version.receivedAt).toLocaleString(),
    description: `${version.state} · ${version.sourceDigest.slice(0, 12)}`,
    detail: version.id, version,
  })), { title: history.hasMore ? "제출 기록 (최근 100건만 표시)" : "제출 기록 (최신순)", ignoreFocusOut: true });
  if (!selection) return;
  checkSession();
  const action = await vscode.window.showQuickPick(["이 제출의 채점 결과", "새 폴더로 코드 복원"], { title: selection.detail });
  if (!action) return;
  checkSession();
  if (action === "이 제출의 채점 결과") {
    const scope = resultUi.monitor.begin();
    resultUi.assignmentId = assignment.id;
    clearResultPanel();
    try {
      const result = await client.getResult(selection.version.id);
      checkSession();
      if (!scope.isCurrent()) return;
      if (result.sourceDigest !== selection.version.sourceDigest) throw new Error("채점 결과의 제출 파일 정보가 일치하지 않습니다.");
      diagnostics.clear(); renderResult(output, assignment, result, selection.version.id, "과거 제출 기록 · " + new Date(selection.version.receivedAt).toLocaleString());
    } catch (error) {
      if (!scope.isCurrent()) return;
      checkSession();
      if (error instanceof ApiError && error.status === 404 && error.code === "result_not_available") {
        void vscode.window.showInformationMessage("아직 공개된 채점 결과가 없습니다."); return;
      }
      throw error;
    }
    return;
  }
  if (!vscode.workspace.isTrusted) throw new Error("코드 복원은 신뢰하는 workspace에서 실행하세요.");
  ensureSupportedWorkspacePlatform("제출 코드 복원");
  const folders = workspaces.sharedSession ? [vscode.Uri.file(workspaces.sharedSession.workspace)]
    : await vscode.window.showOpenDialog({ canSelectFolders: true, canSelectFiles: false,
      canSelectMany: false, openLabel: "이 폴더 아래 새 폴더에 복원" });
  if (!folders?.[0]) return;
  checkSession();
  await workspaces.sharedSession?.assertWorkspacePath(folders[0].fsPath);
  const restoreParent = workspaces.sharedSession?.workspace ?? await courseDirectory(folders[0].fsPath);
  const target = await freshAssignmentPath(restoreParent, `autograde-restore-${randomUUID()}`);
  const bytes = await client.getSubmissionSource(selection.version);
  checkSession();
  let created = false;
  try {
    await extractSubmissionBundle(bytes, target); created = true;
    checkSession();
    await writeWorkspaceMarker(target, { schemaVersion: 1, serviceBaseUrl: origin, assignmentId: assignment.id });
    checkSession();
  } catch (error) {
    if (created) await rm(target, { recursive: true, force: true });
    throw error;
  }
  const restoredUri = vscode.Uri.joinPath(folders[0], path.relative(folders[0].fsPath, target));
  await connectAssignmentFolder(workspaces, restoredUri, assignment, current);
  if (!current()) return;
  void vscode.window.showInformationMessage(`복원 완료: ${target}. 이 폴더를 제출 대상으로 연결했습니다. 과제 제출에서 확인 후 다시 제출할 수 있습니다.`);
}

async function cloneAssignment(
  client: AutogradeClient,
  treeProvider: AssignmentsTreeProvider,
  refreshAssignments: (showSuccess?: boolean) => Promise<readonly Assignment[]>,
  selectedItem?: AssignmentTreeItem,
  workspaces?: AssignmentWorkspaces,
): Promise<void> {
  if (!vscode.workspace.isTrusted) {
    throw new Error("과제를 다운로드하거나 clone하려면 현재 workspace를 신뢰해야 합니다.");
  }
  ensureSupportedWorkspacePlatform("과제 다운로드");
  const origin = client.transport.getBaseUrl();
  const generation = client.tokens.getSessionGeneration?.();
  const current = () => origin === client.transport.getBaseUrl() && generation === client.tokens.getSessionGeneration?.();

  let assignments = treeProvider.getAssignments();
  if (assignments.length === 0) {
    assignments = await refreshAssignments(false);
  }
  if (!current()) return;
  const readyAssignments = assignments.filter(isAssignmentDownloadable);
  let assignment = selectedItem?.assignment;
  if (assignment && !readyAssignments.some((candidate) => candidate.id === assignment?.id)) {
    throw new Error("선택한 과제가 아직 다운로드 가능한 상태가 아닙니다.");
  }
  if (!assignment) {
    if (readyAssignments.length === 0) {
      throw new Error("다운로드하거나 clone할 수 있는 ready 과제가 없습니다.");
    }
    assignment = readyAssignments.length === 1 ? readyAssignments[0] : await pickAssignment(readyAssignments);
  }
  if (!assignment || !current()) {
    return;
  }

  if (isBundleAssignment(assignment)) {
    if (!workspaces) throw new Error("과제 작업 영역을 준비하지 못했습니다.");
    await downloadBundleAssignment(client, assignment, workspaces);
    return;
  }

  const repository = assignment.repository;
  const cloneUrl = repository && selectRepositoryCloneUrl(repository);
  if (!repository || !isAssignedRepositoryReady(repository) || !cloneUrl) {
    throw new Error("서비스가 ready repository의 clone URL을 제공하지 않았습니다.");
  }
  const nameFromFullName = repository.fullName?.split("/").filter(Boolean).at(-1);
  const directoryName = safeRepositoryDirectoryName(repository.name ?? nameFromFullName ?? "");
  if (!directoryName) {
    throw new Error("서비스가 안전한 repository 이름을 제공하지 않았습니다.");
  }

  const folders = workspaces?.sharedSession ? [vscode.Uri.file(workspaces.sharedSession.workspace)] : await vscode.window.showOpenDialog({
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    openLabel: "이 폴더 아래에 Clone",
    title: `${assignment.title} clone 위치 선택`,
  });
  const parentUri = folders?.[0];
  if (!parentUri || !current()) {
    return;
  }
  await workspaces?.sharedSession?.assertWorkspacePath(parentUri.fsPath);
  const targetUri = vscode.Uri.joinPath(parentUri, directoryName);
  const targetPath = path.resolve(parentUri.fsPath, directoryName);
  const relation = path.relative(path.resolve(parentUri.fsPath), targetPath);
  if (relation.startsWith("..") || path.isAbsolute(relation)) {
    throw new Error("선택한 clone 경로가 안전하지 않습니다.");
  }
  if (await pathExists(targetPath)) {
    throw new Error(
      `대상 경로가 이미 존재합니다: ${directoryName}. 이전 clone이 중단되었을 수 있으므로 폴더 내용을 확인하고 이름을 바꾸거나 삭제한 뒤 다시 시도하세요.`,
    );
  }

  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: `${assignment.title} repository clone 중`,
      cancellable: true,
    },
    async (_progress, token) => {
      if (!current()) return;
      const controller = new AbortController();
      const subscription = token.onCancellationRequested(() => controller.abort());
      try {
        await cloneRepository(cloneUrl, targetPath, controller.signal);
      } catch (error) {
        const partialDirectoryExists = await pathExists(targetPath).catch(() => false);
        if (token.isCancellationRequested) {
          if (partialDirectoryExists) {
            void vscode.window.showWarningMessage(
              `clone이 취소되었지만 ${directoryName} 폴더가 남았습니다. 내용을 확인하고 이름을 바꾸거나 삭제한 뒤 다시 시도하세요.`,
            );
          }
          throw new vscode.CancellationError();
        }
        if (partialDirectoryExists) {
          const message = error instanceof Error ? error.message : "Git clone이 실패했습니다.";
          throw new Error(
            `${message} 일부 파일이 ${directoryName} 폴더에 남았을 수 있습니다. 폴더를 확인한 뒤 다시 시도하세요.`,
          );
        }
        throw error;
      } finally {
        subscription.dispose();
      }
    },
  );
  if (current()) {
    if (workspaces?.sharedSession) await connectAssignmentFolder(workspaces, targetUri, assignment, current);
    else await vscode.commands.executeCommand("vscode.openFolder", targetUri, true);
  }
}

async function downloadBundleAssignment(
  client: AutogradeClient,
  assignment: Assignment,
  workspaces: AssignmentWorkspaces,
): Promise<void> {
  if (workspaces.sharedSession) {
    await workspaces.sharedSession.assertWorkspacePath(workspaces.sharedSession.workspace);
    // Every accepted session starts from the server starter. Later explicit downloads also use a new folder.
    await downloadFreshBundleAssignment(client, assignment, workspaces);
    return;
  }
  const origin = client.transport.getBaseUrl();
  const generation = client.tokens.getSessionGeneration?.();
  const current = () => origin === client.transport.getBaseUrl() && generation === client.tokens.getSessionGeneration?.();
  const candidates = await discoverAuthenticatedBundleRoots(
    await bundleResumeRoots(workspaces), vscode.window.activeTextEditor?.document.uri.fsPath,
    origin, new Set([assignment.id]),
  );
  if (!current()) return;
  await startAssignment<vscode.Uri>({
    existing: candidates.map(candidate => bundleRootUri(workspaces, candidate.rootPath)),
    isCurrent: current,
    chooseAction: async hasExisting => {
      const options: { label: string; description: string; value: AssignmentStartAction }[] = hasExisting ? [
        { label: "기존 과제 이어서 열기", value: "resume", description: "작성 중인 코드를 그대로 사용" },
        { label: "새 폴더에 다시 다운로드", value: "download", description: "기존 폴더와 코드는 보존" },
        { label: "다른 위치의 기존 과제 폴더 선택", value: "choose", description: "옮긴 폴더나 다른 사본 선택" },
      ] : [
        { label: "새 과제 파일 다운로드", value: "download", description: "수업 폴더에 새 폴더 생성" },
        { label: "기존 과제 폴더 선택", value: "choose", description: "이전에 받은 파일로 이어서 작업" },
      ];
      return (await vscode.window.showQuickPick(options, { title: `${assignment.title} 시작 방법` }))?.value;
    },
    chooseExisting: async folders => (await vscode.window.showQuickPick(folders.map(uri => ({
      label: path.basename(uri.fsPath), description: uri.fsPath, uri,
    })), { title: "이어서 작업할 과제 폴더" }))?.uri,
    selectFolder: async () => (await vscode.window.showOpenDialog({ canSelectFiles: false, canSelectFolders: true,
      canSelectMany: false, openLabel: "이 과제 이어서 열기" }))?.[0],
    resume: async uri => {
      await validateExistingAssignment(uri.fsPath, origin, assignment.id);
      if (current()) await connectAssignmentFolder(workspaces, uri, assignment, current);
    },
    download: () => downloadFreshBundleAssignment(client, assignment, workspaces),
  });
}

async function downloadFreshBundleAssignment(
  client: AutogradeClient, assignment: Assignment, workspaces: AssignmentWorkspaces,
): Promise<void> {
  const diagnostic = new DownloadDiagnostic();
  const origin = client.transport.getBaseUrl();
  const generation = client.tokens.getSessionGeneration?.();
  const current = (): boolean => client.transport.getBaseUrl() === origin && client.tokens.getSessionGeneration?.() === generation;
  const checkSession = (): void => { if (!current()) throw new DownloadFailure("AG-DL-AUTH-EXPIRED"); };
  const report = async (): Promise<void> => {
    if (!current()) return;
    try {
      const bearer = await client.tokens.getAccessToken();
      if (!current()) return;
      const payload = diagnostic.payload(diagnosticExtensionVersion, process.platform, vscode.env.remoteName) as {attempt_id:string; seq:number};
      const ack = await client.transport.request<{stored:boolean;attempt_id:string;seq:number}>(`/v1/assignments/${encodeURIComponent(assignment.id)}/download-diagnostics`,
        { method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload) },
        bearer, { expectedBaseUrl:origin, timeoutMs:3000 });
      if (!ack || ack.stored !== true || ack.attempt_id !== payload.attempt_id || ack.seq !== payload.seq) throw new Error("diagnostic ack mismatch");
      diagnostic.delivery = "서버에 전달됨";
    } catch (error) {
      diagnostic.delivery = error instanceof ApiError && [404,405].includes(error.status) ? "서버 진단 기능 미지원" : "서버 전달 실패 · 진단 정보를 복사해 문의하세요";
    }
  };
  try {
  const directoryName = safeAssignmentDirectoryName(assignment);
  if (!directoryName) {
    throw new DownloadFailure("AG-DL-LOCAL-PATH");
  }
  const parentFolder = await selectCourseWorkspaceFolder(assignment.title, workspaces.sharedSession);
  if (!parentFolder) {
    return;
  }
  checkSession();
  rememberDownloadDiagnostic(diagnostic);
  await report();
  const parentPath = workspaces.sharedSession?.workspace ?? await courseDirectory(parentFolder.uri.fsPath);
  await workspaces.sharedSession?.assertWorkspacePath(parentPath);
  const parentUri = vscode.Uri.joinPath(parentFolder.uri, path.relative(parentFolder.uri.fsPath, parentPath));
  const targetPath = await freshAssignmentPath(parentPath, directoryName);
  const targetUri = vscode.Uri.joinPath(parentUri, path.basename(targetPath));

  await vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: `${assignment.title} starter 다운로드 중`,
      cancellable: true,
    },
    async (progress, token) => {
      const controller = new AbortController();
      const subscription = token.onCancellationRequested(() => controller.abort());
      let extracted = false;
      try {
        checkSession(); diagnostic.stage = "requesting";
        await report();
        const archive = await client.getStarter(assignment.id, assignment.starterUrl, controller.signal);
        checkSession(); diagnostic.stage = "verifying";
        if (token.isCancellationRequested) {
          throw new vscode.CancellationError();
        }
        if (
          assignment.starterSizeBytes !== undefined &&
          archive.byteLength !== assignment.starterSizeBytes
        ) {
          throw new DownloadFailure("AG-DL-INTEGRITY-SIZE");
        }
        if (assignment.starterSha256 && sha256Hex(archive) !== assignment.starterSha256) {
          throw new DownloadFailure("AG-DL-INTEGRITY-HASH");
        }
        progress.report({ message: "archive 검증 및 설치 중" });
        diagnostic.stage = "installing";
        let starter;
        try { starter = await extractStarterBundle(archive, targetPath); }
        catch (error) {
          if (error && typeof error === "object" && "code" in error) throw error;
          throw new DownloadFailure("AG-DL-ARCHIVE-INVALID");
        }
        extracted = true;
        checkSession();
        if (token.isCancellationRequested) {
          throw new vscode.CancellationError();
        }
        try { await writeWorkspaceMarker(targetPath, {
          schemaVersion: 1,
          serviceBaseUrl: client.transport.getBaseUrl(),
          assignmentId: assignment.id,
        }); } catch { throw new DownloadFailure("AG-DL-MARKER-WRITE"); }
        checkSession(); diagnostic.stage = "files_ready"; diagnostic.outcome = "succeeded";
        await report();
        checkSession();
        void vscode.window.showInformationMessage(
          `${assignment.title}을 다운로드했습니다 (${starter.fileCount.toLocaleString()}개 파일).`,
        );
      } catch (error) {
        if (extracted && diagnostic.outcome !== "succeeded") {
          await rm(targetPath, { recursive: true, force: true }).catch(() => undefined);
        }
        if (token.isCancellationRequested) {
          diagnostic.fail(error, true);
          throw new vscode.CancellationError();
        }
        throw error;
      } finally {
        subscription.dispose();
      }
    },
  );
  try {
    checkSession(); diagnostic.stage = "opening";
    rememberAssignmentFolder(workspaces, targetUri, assignment);
    await revealDownloadedBundle(targetUri, targetPath, current);
    if (!current()) return;
    diagnostic.openOutcome = "opened";
  } catch (error) {
    if (!current()) return;
    diagnostic.fail(error);
    void vscode.window.showWarningMessage(`다운로드는 완료됐지만 과제 폴더를 열지 못했습니다. 파일은 보존됩니다. 탐색기에서 다음 경로를 여세요: ${targetPath}`);
  }
  await report();
  if (current()) {
    rememberDownloadDiagnostic(diagnostic);
    if (diagnostic.code) await showDownloadDiagnostic();
  }
  } catch (error) {
    if (!current()) return; // A former student's diagnostic must never reappear after sign-out.
    diagnostic.fail(error, error instanceof vscode.CancellationError);
    await report();
    if (!current()) return;
    rememberDownloadDiagnostic(diagnostic);
    if (diagnostic.outcome !== "cancelled") {
      void vscode.window.showErrorMessage(`${diagnostic.summary} · ${stageLabels[diagnostic.stage]} (${diagnostic.code})`);
      await showDownloadDiagnostic();
    }
  }
}

interface BundleSubmissionTarget extends AuthenticatedBundleRoot {
  readonly assignment: Assignment;
}

async function selectBundleSubmissionTarget(
  client: AutogradeClient,
  assignments: readonly Assignment[],
  workspaces: AssignmentWorkspaces,
  requestedAssignmentId?: string,
  required = false,
): Promise<BundleSubmissionTarget | undefined | null> {
  const authenticated = new Map(
    assignments
      .filter((assignment) => isBundleAssignment(assignment) && isAssignmentDownloadable(assignment))
      .map((assignment) => [assignment.id, assignment] as const),
  );
  if (requestedAssignmentId && !authenticated.has(requestedAssignmentId)) {
    throw new Error("선택한 bundle 과제가 현재 다운로드 또는 제출 가능한 상태가 아닙니다.");
  }
  const workspaceRoots = bundleWorkspaceRoots(workspaces);
  const activeDocumentPath = vscode.window.activeTextEditor?.document.uri.fsPath;
  if (!requestedAssignmentId) await requireAuthorizedActiveAssignment(activeDocumentPath, client.transport.getBaseUrl(), new Set(authenticated.keys()));
  const discovered = await discoverAuthenticatedBundleRoots(
    workspaceRoots,
    activeDocumentPath,
    client.transport.getBaseUrl(),
    new Set(authenticated.keys()),
  );
  const matches = discovered
    .filter((candidate) => !requestedAssignmentId || candidate.assignmentId === requestedAssignmentId)
    .flatMap((candidate): BundleSubmissionTarget[] => {
      const assignment = authenticated.get(candidate.assignmentId);
      return assignment ? [{ ...candidate, assignment }] : [];
    });
  if (matches.length === 0) {
    if (required) {
      throw new Error(
        "선택한 과제 폴더를 찾지 못했습니다. 기존 과제 열기로 다운로드한 폴더를 연결하세요.",
      );
    }
    return undefined;
  }

  const preferred = preferredSubmissionRoot(matches, workspaces.selected?.rootPath);
  if (preferred) return preferred;
  const lostSelection = workspaces.selected !== undefined &&
    !matches.some(candidate => candidate.rootPath === workspaces.selected?.rootPath);
  const picked = await vscode.window.showQuickPick(
    matches.map((candidate) => ({
      label: candidate.assignment.title,
      description: path.basename(candidate.rootPath),
      detail: candidate.rootPath,
      candidate,
    })),
    { placeHolder: lostSelection
      ? "이전에 연결한 폴더를 찾지 못했습니다. 다른 사본을 자동 제출하지 않습니다. 현재 답안 폴더를 확인해 선택하세요."
      : "제출할 과제 폴더를 선택하세요." },
  );
  return picked?.candidate ?? null;
}

function bundleWorkspaceRoots(workspaces: AssignmentWorkspaces): string[] {
  if (workspaces.sharedSession) return [workspaces.sharedSession.workspace, ...[...workspaces.attached.keys()]
    .filter(root => containsPath(workspaces.sharedSession!.workspace, root))];
  return [...new Set([
    ...(vscode.workspace.workspaceFolders ?? []).map(folder => folder.uri.fsPath),
    ...workspaces.attached.keys(),
  ])];
}

async function bundleResumeRoots(workspaces: AssignmentWorkspaces): Promise<string[]> {
  const roots = bundleWorkspaceRoots(workspaces);
  if (workspaces.sharedSession) return roots;
  return [...new Set([...roots, ...await Promise.all(roots.map(courseDirectory))])];
}

function bundleRootUri(workspaces: AssignmentWorkspaces, rootPath: string): vscode.Uri {
  const attached = workspaces.attached.get(path.resolve(rootPath));
  if (attached) return attached;
  const folders = vscode.workspace.workspaceFolders ?? [];
  const parent = folders.find(folder => containsPath(folder.uri.fsPath, rootPath)) ?? folders[0];
  if (!parent) throw new Error("기존 과제 열기에서 해당 폴더를 선택하세요.");
  return vscode.Uri.joinPath(parent.uri, path.relative(parent.uri.fsPath, rootPath));
}

function rememberAssignmentFolder(workspaces: AssignmentWorkspaces, uri: vscode.Uri, assignment: Assignment): void {
  workspaces.attached.set(path.resolve(uri.fsPath), uri);
  workspaces.selected = { rootPath: path.resolve(uri.fsPath), assignment };
  workspaces.onSelected?.(assignment.id);
  workspaces.status.text = `$(folder-opened) 제출 연결: ${assignment.title}`;
  workspaces.status.tooltip = `제출 대상으로 연결한 폴더: ${uri.fsPath}\n실제 탐색기·빌드 작업 영역과 다를 수 있습니다. 제출 전 과제와 폴더를 다시 확인합니다.`;
  workspaces.status.show();
}

async function connectAssignmentFolder(workspaces: AssignmentWorkspaces, uri: vscode.Uri, assignment: Assignment, isCurrent: () => boolean): Promise<void> {
  if (!isCurrent()) return;
  await workspaces.sharedSession?.assertWorkspacePath(uri.fsPath);
  if (!isCurrent()) return;
  rememberAssignmentFolder(workspaces, uri, assignment);
  await revealDownloadedBundle(uri, uri.fsPath, isCurrent);
}

async function openExistingAssignment(
  client: AutogradeClient, tree: AssignmentsTreeProvider, workspaces: AssignmentWorkspaces, item?: AssignmentTreeItem,
): Promise<void> {
  ensureSupportedWorkspacePlatform("기존 과제 열기");
  if (!vscode.workspace.isTrusted) throw new Error("기존 과제를 열려면 현재 workspace를 신뢰해야 합니다.");
  const origin = client.transport.getBaseUrl();
  const generation = client.tokens.getSessionGeneration?.();
  const current = () => origin === client.transport.getBaseUrl() && generation === client.tokens.getSessionGeneration?.();
  const assignments = tree.getAssignments().filter(isBundleAssignment);
  if (!assignments.length) throw new Error(workspaces.sharedSession
    ? "현재 연결된 bundle 과제가 없습니다. 인증이 종료되었다면 필요한 코드를 내보내고 공용 실습 종료 후 새 세션에서 연결하세요."
    : "학생 웹에서 제출할 과제의 새 수령 코드를 받아 먼저 연결하세요. 기존 작업 파일은 다시 받지 않아도 됩니다.");
  const assignment = item ? assignments.find(candidate => candidate.id === item.assignment.id)
    : assignments.length === 1 ? assignments[0] : await pickAssignment(assignments);
  // Dismissing a valid assignment chooser is cancellation, not an authentication failure.
  if (!assignment && !item) return;
  if (!assignment) throw new Error("선택한 과제가 현재 연결된 목록에 없습니다. 과제 새로고침 후 다시 선택하세요.");
  if (!current()) return;
  const candidates = await discoverAuthenticatedBundleRoots(await bundleResumeRoots(workspaces),
    vscode.window.activeTextEditor?.document.uri.fsPath, origin, new Set([assignment.id]));
  if (!current()) return;
  if (workspaces.sharedSession && !candidates.length) throw new Error("이번 공용 실습에서 받은 과제 폴더가 없습니다. 과제를 새로 다운로드하거나 제출 이력에서 코드를 복원하세요.");
  const choice = await vscode.window.showQuickPick([
    ...candidates.map(candidate => ({ label: path.basename(candidate.rootPath), description: candidate.rootPath, rootPath: candidate.rootPath })),
    ...(!workspaces.sharedSession ? [{ label: "다른 위치에서 기존 과제 폴더 선택…", description: "다운로드한 과제 폴더를 직접 선택", rootPath: undefined }] : []),
  ], { title: `${assignment.title} 이어서 열기` });
  if (!choice || !current()) return;
  const uri = choice.rootPath ? bundleRootUri(workspaces, choice.rootPath) : (await vscode.window.showOpenDialog({
    canSelectFolders: true, canSelectFiles: false, canSelectMany: false, openLabel: "이 과제 이어서 열기",
  }))?.[0];
  if (!uri) return;
  if (!current()) {
    throw new Error("연결된 과제가 변경되었습니다. 기존 과제 열기를 다시 실행하세요.");
  }
  await validateExistingAssignment(uri.fsPath, origin, assignment.id);
  if (!current()) return;
  await connectAssignmentFolder(workspaces, uri, assignment, current);
}

async function selectCourseWorkspaceFolder(
  assignmentTitle: string,
  sharedSession?: SharedLearningSession,
): Promise<vscode.WorkspaceFolder | undefined> {
  if (sharedSession) {
    await sharedSession.assertWorkspacePath(sharedSession.workspace);
    return {uri: vscode.Uri.file(sharedSession.workspace), name: "공용 실습 작업 공간", index: 0};
  }
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length === 0) {
    throw new DownloadFailure("AG-DL-WORKSPACE-NOT-OPEN");
  }
  if (folders.length === 1) {
    return folders[0];
  }
  const picked = await vscode.window.showQuickPick(
    folders.map((folder) => ({
      label: folder.name,
      description: folder.uri.fsPath,
      folder,
    })),
    { placeHolder: `${assignmentTitle}을 받을 수업 workspace를 선택하세요.` },
  );
  return picked?.folder;
}

async function revealDownloadedBundle(targetUri: vscode.Uri, targetPath: string, isCurrent: () => boolean): Promise<void> {
  await revealAssignmentFolder(targetUri, targetPath, isCurrent);
}

async function pathExists(candidate: string): Promise<boolean> {
  try {
    await lstat(candidate);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return false;
    }
    throw error;
  }
}

function ensureSupportedWorkspacePlatform(operation: string): void {
  if (!isSupportedWorkspacePlatform(process.platform, vscode.env.remoteName)) {
    throw new Error(`${operation}은 Windows, macOS 또는 Linux 작업 영역에서 실행하세요. Linux 실습은 Windows에서 WSL2를 선택할 수 있습니다.`);
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KiB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MiB`;
}

async function waitForSubmissionResolution(
  client: AutogradeClient,
  initial: SubmissionSummary,
): Promise<SubmissionSummary> {
  return vscode.window.withProgress(
    {
      location: vscode.ProgressLocation.Notification,
      title: "서버가 제출물을 확인하는 중",
      cancellable: true,
    },
    async (progress, token) => {
      const expiresAt = Date.now() + 60_000;
      let current = initial;
      while (Date.now() < expiresAt && !token.isCancellationRequested) {
        const state = current.state.toLowerCase();
        progress.report({ message: current.state });
        if (SUCCESSFUL_SUBMISSION_STATES.has(state) || FAILED_SUBMISSION_STATES.has(state)) {
          return current;
        }
        await delay(2_000, token);
        current = await client.getSubmission(current.id);
      }
      return current;
    },
  );
}

async function viewLatestResult(
  studentState: EphemeralStudentState,
  client: AutogradeClient,
  treeProvider: AssignmentsTreeProvider,
  output: vscode.OutputChannel,
  diagnostics: vscode.DiagnosticCollection,
  resultUi: ResultUi,
  selectedItem?: AssignmentTreeItem,
): Promise<void> {
  const serviceBaseUrl = client.transport.getBaseUrl();
  const assignment = selectedItem?.assignment ?? await pickAssignmentWithSubmission(
    studentState,
    serviceBaseUrl,
    treeProvider.getAssignments(),
  );
  if (!assignment) {
    return;
  }
  if (!studentState.isActive()) {
    return;
  }
  output.clear();
  diagnostics.clear();
  const scope = resultUi.monitor.begin();
  resultUi.assignmentId = assignment.id;
  clearResultPanel();
  const stored = getRememberedSubmissions(studentState, serviceBaseUrl);
  const refreshed = await client.getAssignments();
  const fresh = refreshed.find(candidate => candidate.id === assignment.id);
  if (!studentState.isActive() || !scope.isCurrent()) return;
  treeProvider.setAssignments(refreshed);
  const submissionId = fresh?.latestSubmission?.id ?? stored[assignment.id];
  if (!submissionId) {
    void vscode.window.showInformationMessage("아직 제출한 코드가 없습니다. 과제를 저장하고 제출한 뒤 결과를 확인하세요.");
    return;
  }

  const submission = fresh?.latestSubmission ?? await client.getSubmission(submissionId);
  if (!studentState.isActive() || !scope.isCurrent()) return;
  watchSubmissionResult(resultUi, scope, studentState, client, assignment, submission, "선택 과제의 최신 제출");
}

function watchSubmissionResult(
  ui: ResultUi, scope: ResultScope, studentState: EphemeralStudentState, client: AutogradeClient,
  assignment: Assignment, submission: SubmissionSummary, context: string,
): void {
  const origin = client.transport.getBaseUrl();
  const diagnosticTarget = ui.diagnosticTargets.get(submission.id);
  const isCurrent = () => scope.isCurrent() && studentState.isActive() && client.transport.getBaseUrl() === origin;
  let first = true;
  ui.assignmentId = assignment.id;
  ui.monitor.start(scope, {
    submission,
    isSessionCurrent: () => studentState.isActive() && client.transport.getBaseUrl() === origin,
    onAuthenticationRequired: () => ui.onAuthenticationRequired(studentState),
    onUpdate: ({ result, notice, watching }) => {
      if (!isCurrent()) return;
      showResultPanel(assignment, result, submission.id, context, {
        refresh: () => scope.isCurrent() ? ui.monitor.refresh() : Promise.resolve(),
        pause: watching ? () => { if (scope.isCurrent()) ui.monitor.pause(); } : undefined,
        onClose: () => { if (scope.isCurrent()) { ui.monitor.cancel(); ui.diagnostics.clear(); } },
        notice, reveal: first,
      });
      first = false;
      publishResultDiagnostics(ui.diagnostics, result, diagnosticTarget, isCurrent);
      const current = ui.tree.getAssignments();
      const displayed = current.find(item => item.id === assignment.id);
      if (displayed && (!displayed.latestSubmission || displayed.latestSubmission.id === submission.id)) {
        ui.tree.setAssignments(current.map(item => item.id === assignment.id ? { ...item, latestSubmission: {
          id: submission.id, state: result.state, sourceDigest: result.sourceDigest, headSha: result.headSha,
          score: result.state === "published" ? result.score : undefined,
          maxScore: result.state === "published" ? result.maxScore : undefined, previousBest: result.previousBest,
        } } : item));
      }
    },
  });
}

function renderResult(output: vscode.OutputChannel, assignment: Assignment, result: GradeResult, receipt: string, context: string): void {
  showResultPanel(assignment, result, receipt, context);
  output.hide();
  output.clear();
  output.appendLine(assignment.title);
  output.appendLine("=".repeat(Math.max(assignment.title.length, 12)));
  output.appendLine(`상태: ${result.state}`);
  if (result.state === "published" && result.score !== undefined) {
    output.appendLine(`점수: ${result.score}${result.maxScore === undefined ? "" : ` / ${result.maxScore}`}`);
  }
  if (result.headSha) {
    output.appendLine(`채점 Commit: ${result.headSha}`);
  }
  if (result.sourceDigest) {
    output.appendLine(`채점 Bundle SHA-256: ${result.sourceDigest}`);
  }
  const view = summarizeResult(result);
  if (view.items.length > 0) {
    output.appendLine("");
    output.appendLine("Rubric");
    for (const item of view.items) {
      const score = item.score === undefined ? "" : `: ${item.score}${item.maxScore === undefined ? "" : ` / ${item.maxScore}`}`;
      output.appendLine(`- ${item.label} · ${item.title}${score}`);
      const location = feedbackLocation(item);
      if (location) output.appendLine(`  소스 위치: ${location}`);
      if (item.feedback) {
        output.appendLine(`  확인된 현상: ${item.feedback}`);
      }
      if (item.hint) output.appendLine(`  수정 가이드: ${item.hint}`);
    }
  }
  if (result.state === "published" && result.diagnostics.length > 0) {
    output.appendLine("");
    output.appendLine("Feedback");
    for (const item of result.diagnostics) {
      output.appendLine(`- ${diagnosticText(item)}`);
    }
  }
}

function selectWorkspaceFolder(): vscode.WorkspaceFolder | undefined {
  const activeUri = vscode.window.activeTextEditor?.document.uri;
  if (activeUri) {
    const activeFolder = vscode.workspace.getWorkspaceFolder(activeUri);
    if (activeFolder) {
      return activeFolder;
    }
  }
  return vscode.workspace.workspaceFolders?.[0];
}

async function pickAssignment(assignments: readonly Assignment[]): Promise<Assignment | undefined> {
  const picked = await vscode.window.showQuickPick(
    assignments.map((assignment) => ({
      label: assignment.title,
      description: assignment.courseLabel,
      assignment,
    })),
    { placeHolder: "제출할 과제를 선택하세요." },
  );
  return picked?.assignment;
}

async function promptForPullRequestNumber(): Promise<number | undefined> {
  const value = await vscode.window.showInputBox({
    title: "제출 Pull Request",
    prompt: "현재 branch를 제출하는 open/non-draft Pull Request 번호를 입력하세요.",
    placeHolder: "예: 1",
    ignoreFocusOut: true,
    validateInput: (input) => /^[1-9][0-9]*$/.test(input.trim())
      ? undefined
      : "1 이상의 Pull Request 번호를 입력하세요.",
  });
  return value === undefined ? undefined : Number(value.trim());
}

async function pickAssignmentWithSubmission(
  studentState: MementoLike,
  serviceBaseUrl: string,
  assignments: readonly Assignment[],
): Promise<Assignment | undefined> {
  const stored = getRememberedSubmissions(studentState, serviceBaseUrl);
  const available = assignments.filter((assignment) => assignment.latestSubmission || stored[assignment.id]);
  if (available.length === 0) {
    throw new Error("조회할 제출 내역이 없습니다.");
  }
  return available.length === 1 ? available[0] : pickAssignment(available);
}

function delay(milliseconds: number, token: vscode.CancellationToken): Promise<void> {
  return new Promise((resolve) => {
    if (token.isCancellationRequested) {
      resolve();
      return;
    }
    const timeout = setTimeout(() => {
      subscription.dispose();
      resolve();
    }, milliseconds);
    const subscription = token.onCancellationRequested(() => {
      clearTimeout(timeout);
      subscription.dispose();
      resolve();
    });
  });
}

async function runCommand(action: () => Promise<unknown>): Promise<void> {
  try {
    await action();
  } catch (error) {
    if (error instanceof vscode.CancellationError) {
      return;
    }
    showCommandError(error);
  }
}

function showCommandError(error: unknown): void {
  const message = error instanceof Error ? error.message : "알 수 없는 오류가 발생했습니다.";
  void vscode.window.showErrorMessage(message);
}
