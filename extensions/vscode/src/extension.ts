import { lstat, rm } from "node:fs/promises";
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
import { AuthenticationController } from "./auth";
import {
  createSubmissionBundle,
  extractStarterBundle,
  readWorkspaceMarker,
  sha256Hex,
  writeWorkspaceMarker,
} from "./bundle";
import {
  type AuthenticatedBundleRoot,
  discoverAuthenticatedBundleRoots,
  findSafeStarterPreview,
} from "./bundleWorkspace";
import { GitPreflightError, cloneRepository, inspectRepository } from "./git";
import {
  isAssignmentDownloadable,
  isAssignedRepositoryReady,
  isBundleAssignment,
  isSupportedWorkspacePlatform,
  normalizeTargetRef,
  repositoryMatches,
  resolveAssignmentDiagnosticPath,
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
import type { Assignment, GradeResult, ResultDiagnostic, SubmissionSummary } from "./types";

const SUCCESSFUL_SUBMISSION_STATES = new Set(["accepted", "queued", "running", "graded", "published"]);
const FAILED_SUBMISSION_STATES = new Set(["rejected", "infra_failed", "assessment_failed"]);

export async function activate(context: vscode.ExtensionContext): Promise<void> {
  await Promise.all([
    clearLegacyPersistedTokens(context.secrets),
    clearLegacyPersistedStudentState(context.globalState),
  ]);
  const transport = new HttpTransport(
    () => vscode.workspace
      .getConfiguration("autograde")
      .get<string>("serviceBaseUrl", "http://127.0.0.1:18080"),
    globalThis.fetch.bind(globalThis),
    () => vscode.workspace
      .getConfiguration("autograde")
      .get<boolean>("allowInsecureHttpPilot", false),
  );
  const tokens = new TokenManager(transport);
  const client = new AutogradeClient(transport, tokens);
  const treeProvider = new AssignmentsTreeProvider();
  const treeView = vscode.window.createTreeView("autograde.assignments", { treeDataProvider: treeProvider });
  const output = vscode.window.createOutputChannel("Autograde");
  const diagnostics = vscode.languages.createDiagnosticCollection("autograde");
  let studentState = new EphemeralStudentState();
  let authenticationUiState = false;
  const extensionVersion = String(context.extension.packageJSON.version ?? "0.0.0");

  const updateAuthenticationUI = async (knownState?: boolean): Promise<boolean> => {
    const authenticated = knownState ?? await tokens.hasSession();
    authenticationUiState = authenticated;
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
    treeView.message = authenticated
      ? "과제를 펼쳐 파일 다운로드 버튼을 선택하세요."
      : "위의 로그인 또는 수령 코드 버튼으로 과제를 시작하세요.";
    return authenticated;
  };

  const refreshAssignments = async (showSuccess = true): Promise<readonly Assignment[]> => {
    const activeStudentState = studentState;
    const assignments = await client.getAssignments();
    if (!activeStudentState.isActive()) {
      return [];
    }
    treeProvider.setAssignments(assignments);
    await vscode.commands.executeCommand(
      "setContext",
      "autograde.hasDownloadableAssignments",
      assignments.some(isAssignmentDownloadable),
    );
    await mergeServerSubmissionIds(activeStudentState, client.transport.getBaseUrl(), assignments);
    await updateAuthenticationUI(true);
    if (showSuccess) {
      void vscode.window.showInformationMessage(`과제 ${assignments.length}개를 불러왔습니다.`);
    }
    return assignments;
  };

  const auth = new AuthenticationController(client, extensionVersion, (authenticated) => {
    studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
    void updateAuthenticationUI(authenticated);
    if (authenticated) {
      void refreshAssignments(false).catch(showCommandError);
    }
  });
  const assignmentClaims = new AssignmentClaimController(client, extensionVersion, async (assignmentId) => {
    studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
    await updateAuthenticationUI(true);
    const assignments = await refreshAssignments(false);
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
      await cloneAssignment(client, treeProvider, refreshAssignments);
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
    );
  });

  const clearSessionUiForAddressChange = async (): Promise<void> => {
    studentState = clearStudentSessionResidue(studentState, treeProvider, output, diagnostics);
    await updateAuthenticationUI(false);
  };
  const serviceAddresses = new ServiceAddressController(
    client,
    () => auth.signOut(),
    clearSessionUiForAddressChange,
  );

  context.subscriptions.push(
    treeView,
    output,
    diagnostics,
    vscode.commands.registerCommand(
      "autograde.configureServiceAddress",
      () => runCommand(() => serviceAddresses.configure()),
    ),
    vscode.commands.registerCommand("autograde.signIn", () => runCommand(() => auth.signIn())),
    vscode.commands.registerCommand("autograde.signOut", () => runCommand(() => auth.signOut())),
    vscode.commands.registerCommand(
      "autograde.redeemAssignmentClaim",
      () => runCommand(async () => {
        if (!vscode.workspace.isTrusted) {
          throw new Error("수령 코드로 파일을 받으려면 현재 workspace를 신뢰해야 합니다.");
        }
        ensureSupportedWorkspacePlatform("수령 코드 입력 및 다운로드");
        await assignmentClaims.redeem();
      }),
    ),
    vscode.commands.registerCommand("autograde.refreshAssignments", () => runCommand(() => refreshAssignments())),
    vscode.commands.registerCommand(
      "autograde.cloneAssignment",
      (item?: AssignmentTreeItem) => runCommand(() => cloneAssignment(client, treeProvider, refreshAssignments, item)),
    ),
    vscode.commands.registerCommand(
      "autograde.submitCurrentCommit",
      (item?: AssignmentTreeItem) => runCommand(() => submitCurrentCommit(studentState, client, refreshAssignments, item)),
    ),
    vscode.commands.registerCommand(
      "autograde.viewLatestResult",
      (item?: AssignmentTreeItem) => runCommand(() => viewLatestResult(studentState, client, treeProvider, output, diagnostics, item)),
    ),
    vscode.workspace.onDidGrantWorkspaceTrust(() => treeProvider.setAssignments(treeProvider.getAssignments())),
    vscode.workspace.onDidChangeConfiguration((event) => {
      if (
        !event.affectsConfiguration("autograde.serviceBaseUrl") &&
        !event.affectsConfiguration("autograde.allowInsecureHttpPilot")
      ) {
        return;
      }
      const wasAuthenticated = authenticationUiState;
      void runCommand(async () => {
        await tokens.clear();
        await clearSessionUiForAddressChange();
        if (wasAuthenticated) {
          void vscode.window.showWarningMessage(
            "Autograde 연결 설정이 바뀌어 이 기기의 로그인을 지웠습니다. Settings에서 직접 주소를 바꾼 경우 기존 서버 세션은 만료될 때까지 남을 수 있습니다.",
          );
        }
      });
    }),
  );

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
}

export function deactivate(): void {}

async function submitCurrentCommit(
  studentState: EphemeralStudentState,
  client: AutogradeClient,
  refreshAssignments: (showSuccess?: boolean) => Promise<readonly Assignment[]>,
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
    const target = await selectBundleSubmissionTarget(client, assignments, current.id, true);
    if (target) {
      await submitCurrentBundle(studentState, client, refreshAssignments, target.rootPath, current);
    }
    return;
  }
  if (!selectedAssignment) {
    const target = await selectBundleSubmissionTarget(client, assignments);
    if (target) {
      await submitCurrentBundle(
        studentState,
        client,
        refreshAssignments,
        target.rootPath,
        target.assignment,
      );
      return;
    }
  }

  let repository;
  try {
    repository = await vscode.window.withProgress(
      { location: vscode.ProgressLocation.Notification, title: "Git repository와 원격 push 상태 확인 중" },
      () => inspectRepository(workspaceFolder.uri.fsPath),
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
  await rememberSubmission(studentState, client.transport.getBaseUrl(), assignment.id, submission.id);
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
  refreshAssignments: (showSuccess?: boolean) => Promise<readonly Assignment[]>,
  assignmentRoot: string,
  assignment: Assignment,
): Promise<void> {
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
        ".git, .autograde 및 일반적인 build/cache 디렉터리는 포함하지 않습니다.",
        "공용 PC에서는 표시된 과제 폴더가 본인의 작업물인지 확인하세요. 로그아웃해도 이 파일은 삭제되지 않습니다.",
      ].join("\n"),
    },
    submitAction,
  );
  if (confirm !== submitAction) {
    return;
  }
  if (!studentState.isActive()) {
    throw new Error("로그인 계정이 변경되었습니다. 현재 계정의 과제 목록에서 다시 제출하세요.");
  }

  const attemptRequest: SubmissionAttemptRequest = {
    serviceBaseUrl: client.transport.getBaseUrl(),
    assignmentId: assignment.id,
    bundleSha256: bundle.sha256,
  };
  const attempt = await getOrCreateSubmissionAttempt(studentState, attemptRequest);
  let submission: SubmissionSummary;
  try {
    submission = await client.submitBundle(
      assignment.id,
      bundle.archive,
      attempt.idempotencyKey,
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
  await rememberSubmission(studentState, client.transport.getBaseUrl(), assignment.id, submission.id);
  await clearSubmissionAttempt(studentState, attemptRequest, attempt.idempotencyKey);

  if (
    !SUCCESSFUL_SUBMISSION_STATES.has(submission.state.toLowerCase()) &&
    !FAILED_SUBMISSION_STATES.has(submission.state.toLowerCase())
  ) {
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

async function cloneAssignment(
  client: AutogradeClient,
  treeProvider: AssignmentsTreeProvider,
  refreshAssignments: (showSuccess?: boolean) => Promise<readonly Assignment[]>,
  selectedItem?: AssignmentTreeItem,
): Promise<void> {
  if (!vscode.workspace.isTrusted) {
    throw new Error("과제를 다운로드하거나 clone하려면 현재 workspace를 신뢰해야 합니다.");
  }
  ensureSupportedWorkspacePlatform("과제 다운로드");

  let assignments = treeProvider.getAssignments();
  if (assignments.length === 0) {
    assignments = await refreshAssignments(false);
  }
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
  if (!assignment) {
    return;
  }

  if (isBundleAssignment(assignment)) {
    await downloadBundleAssignment(client, assignment);
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

  const folders = await vscode.window.showOpenDialog({
    canSelectFiles: false,
    canSelectFolders: true,
    canSelectMany: false,
    openLabel: "이 폴더 아래에 Clone",
    title: `${assignment.title} clone 위치 선택`,
  });
  const parentUri = folders?.[0];
  if (!parentUri) {
    return;
  }
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
  await vscode.commands.executeCommand("vscode.openFolder", targetUri, true);
}

async function downloadBundleAssignment(
  client: AutogradeClient,
  assignment: Assignment,
): Promise<void> {
  const directoryName = safeAssignmentDirectoryName(assignment);
  if (!directoryName) {
    throw new Error("서비스가 안전한 과제 디렉터리 이름을 제공하지 않았습니다.");
  }
  const parentFolder = await selectCourseWorkspaceFolder(assignment.title);
  if (!parentFolder) {
    return;
  }
  const parentUri = parentFolder.uri;
  if (await readWorkspaceMarker(parentUri.fsPath)) {
    throw new Error(
      "현재 workspace 자체가 과제 폴더입니다. 같은 로그인 세션에서 다음 과제를 받으려면 상위 수업 폴더를 workspace로 여세요.",
    );
  }
  const targetUri = vscode.Uri.joinPath(parentUri, directoryName);
  const targetPath = path.resolve(parentUri.fsPath, directoryName);
  const relation = path.relative(path.resolve(parentUri.fsPath), targetPath);
  if (relation.startsWith("..") || path.isAbsolute(relation)) {
    throw new Error("선택한 다운로드 경로가 안전하지 않습니다.");
  }
  if (await pathExists(targetPath)) {
    throw new Error(`대상 경로가 이미 존재합니다: ${directoryName}. 다른 위치를 선택하세요.`);
  }

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
        const archive = await client.getStarter(assignment.id, assignment.starterUrl, controller.signal);
        if (token.isCancellationRequested) {
          throw new vscode.CancellationError();
        }
        if (
          assignment.starterSizeBytes !== undefined &&
          archive.byteLength !== assignment.starterSizeBytes
        ) {
          throw new Error("다운로드한 starter 크기가 과제 목록의 값과 일치하지 않습니다.");
        }
        if (assignment.starterSha256 && sha256Hex(archive) !== assignment.starterSha256) {
          throw new Error("다운로드한 starter SHA-256이 과제 목록의 값과 일치하지 않습니다.");
        }
        progress.report({ message: "archive 검증 및 설치 중" });
        const starter = await extractStarterBundle(archive, targetPath);
        extracted = true;
        if (token.isCancellationRequested) {
          throw new vscode.CancellationError();
        }
        await writeWorkspaceMarker(targetPath, {
          schemaVersion: 1,
          serviceBaseUrl: client.transport.getBaseUrl(),
          assignmentId: assignment.id,
        });
        void vscode.window.showInformationMessage(
          `${assignment.title}을 다운로드했습니다 (${starter.fileCount.toLocaleString()}개 파일).`,
        );
      } catch (error) {
        if (extracted) {
          await rm(targetPath, { recursive: true, force: true }).catch(() => undefined);
        }
        if (token.isCancellationRequested) {
          throw new vscode.CancellationError();
        }
        throw error;
      } finally {
        subscription.dispose();
      }
    },
  );
  await revealDownloadedBundle(targetUri, targetPath);
}

interface BundleSubmissionTarget extends AuthenticatedBundleRoot {
  readonly assignment: Assignment;
}

async function selectBundleSubmissionTarget(
  client: AutogradeClient,
  assignments: readonly Assignment[],
  requestedAssignmentId?: string,
  required = false,
): Promise<BundleSubmissionTarget | undefined> {
  const authenticated = new Map(
    assignments
      .filter((assignment) => isBundleAssignment(assignment) && isAssignmentDownloadable(assignment))
      .map((assignment) => [assignment.id, assignment] as const),
  );
  if (requestedAssignmentId && !authenticated.has(requestedAssignmentId)) {
    throw new Error("선택한 bundle 과제가 현재 다운로드 또는 제출 가능한 상태가 아닙니다.");
  }
  const workspaceRoots = (vscode.workspace.workspaceFolders ?? []).map((folder) => folder.uri.fsPath);
  const activeDocumentPath = vscode.window.activeTextEditor?.document.uri.fsPath;
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
        "선택한 과제의 다운로드 폴더를 현재 workspace에서 찾지 못했습니다. 수업 폴더 아래에 과제를 다시 다운로드하세요.",
      );
    }
    return undefined;
  }

  const activeMatches = matches.filter((candidate) => candidate.containsActiveDocument);
  if (activeMatches.length === 1) {
    return activeMatches[0];
  }
  if (matches.length === 1) {
    return matches[0];
  }
  const picked = await vscode.window.showQuickPick(
    matches.map((candidate) => ({
      label: candidate.assignment.title,
      description: path.basename(candidate.rootPath),
      detail: candidate.rootPath,
      candidate,
    })),
    { placeHolder: "제출할 과제 폴더를 선택하세요." },
  );
  return picked?.candidate;
}

async function selectCourseWorkspaceFolder(
  assignmentTitle: string,
): Promise<vscode.WorkspaceFolder | undefined> {
  const folders = vscode.workspace.workspaceFolders ?? [];
  if (folders.length === 0) {
    throw new Error(
      "과제를 다운로드하기 전에 빈 수업 폴더를 VS Code workspace로 열고 다시 로그인하세요.",
    );
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

async function revealDownloadedBundle(targetUri: vscode.Uri, targetPath: string): Promise<void> {
  const preview = await findSafeStarterPreview(targetPath);
  if (preview) {
    try {
      const previewUri = vscode.Uri.joinPath(targetUri, path.basename(preview));
      const document = await vscode.workspace.openTextDocument(previewUri);
      await vscode.window.showTextDocument(document, { preview: true });
      return;
    } catch {
      // The starter remains installed; fall back to a non-host-restarting reveal.
    }
  }
  await vscode.commands.executeCommand("revealFileInOS", targetUri);
}

async function pathExists(candidate: string): Promise<boolean> {
  try {
    await lstat(candidate);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return false;
    }
    throw new Error("clone 대상 경로를 확인할 수 없습니다.");
  }
}

function ensureSupportedWorkspacePlatform(operation: string): void {
  if (!isSupportedWorkspacePlatform(process.platform, vscode.env.remoteName)) {
    throw new Error(`${operation}은 Windows에서 VS Code Remote WSL2 workspace로 실행하세요.`);
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
  const stored = getRememberedSubmissions(studentState, serviceBaseUrl);
  const submissionId = assignment.latestSubmission?.id ?? stored[assignment.id];
  if (!submissionId) {
    throw new Error("이 과제의 제출 내역이 없습니다.");
  }

  const submission = await client.getSubmission(submissionId);
  if (!studentState.isActive()) {
    return;
  }
  if (!new Set(["graded", "published"]).has(submission.state.toLowerCase())) {
    output.appendLine(`${assignment.title}`);
    output.appendLine(`상태: ${submission.state}`);
    if (submission.headSha) {
      output.appendLine(`Commit: ${submission.headSha}`);
    }
    if (submission.sourceDigest) {
      output.appendLine(`Bundle SHA-256: ${submission.sourceDigest}`);
    }
    output.show(true);
    void vscode.window.showInformationMessage(`채점 상태: ${submission.state}`);
    return;
  }

  let result: GradeResult;
  try {
    result = await client.getResult(submissionId);
  } catch (error) {
    if (error instanceof ApiError && [404, 409, 425].includes(error.status)) {
      void vscode.window.showInformationMessage("채점은 끝났지만 결과가 아직 공개되지 않았습니다.");
      return;
    }
    throw error;
  }
  if (!studentState.isActive()) {
    return;
  }
  renderResult(output, assignment, result);
  publishDiagnostics(diagnostics, result.diagnostics, assignment.assignmentPath);
  output.show(true);
}

function renderResult(output: vscode.OutputChannel, assignment: Assignment, result: GradeResult): void {
  output.clear();
  output.appendLine(assignment.title);
  output.appendLine("=".repeat(Math.max(assignment.title.length, 12)));
  output.appendLine(`상태: ${result.state}`);
  if (result.score !== undefined) {
    output.appendLine(`점수: ${result.score}${result.maxScore === undefined ? "" : ` / ${result.maxScore}`}`);
  }
  if (result.headSha) {
    output.appendLine(`채점 Commit: ${result.headSha}`);
  }
  if (result.sourceDigest) {
    output.appendLine(`채점 Bundle SHA-256: ${result.sourceDigest}`);
  }
  if (result.rubric.length > 0) {
    output.appendLine("");
    output.appendLine("Rubric");
    for (const item of result.rubric) {
      const score = item.score === undefined ? "" : `: ${item.score}${item.maxScore === undefined ? "" : ` / ${item.maxScore}`}`;
      output.appendLine(`- ${item.name}${score}`);
      if (item.feedback) {
        output.appendLine(`  ${item.feedback}`);
      }
    }
  }
  if (result.diagnostics.length > 0) {
    output.appendLine("");
    output.appendLine("Feedback");
    for (const item of result.diagnostics) {
      const line = item.line === undefined ? "" : `:${item.line}`;
      output.appendLine(`- ${item.path}${line} ${item.message}`);
    }
  }
}

function publishDiagnostics(
  collection: vscode.DiagnosticCollection,
  feedback: readonly ResultDiagnostic[],
  assignmentPath?: string,
): void {
  collection.clear();
  const workspaceFolder = selectWorkspaceFolder();
  if (!workspaceFolder) {
    return;
  }
  const grouped = new Map<string, vscode.Diagnostic[]>();
  for (const item of feedback) {
    const absolute = resolveAssignmentDiagnosticPath(
      workspaceFolder.uri.fsPath,
      assignmentPath,
      item.path,
    );
    if (!absolute) {
      continue;
    }
    const startLine = Math.max(0, (item.line ?? 1) - 1);
    const startColumn = Math.max(0, (item.column ?? 1) - 1);
    const endLine = Math.max(startLine, (item.endLine ?? item.line ?? 1) - 1);
    const endColumn = Math.max(startColumn + 1, (item.endColumn ?? item.column ?? 1));
    const diagnostic = new vscode.Diagnostic(
      new vscode.Range(startLine, startColumn, endLine, endColumn),
      item.message,
      diagnosticSeverity(item.severity),
    );
    diagnostic.source = "Autograde";
    const entries = grouped.get(absolute) ?? [];
    entries.push(diagnostic);
    grouped.set(absolute, entries);
  }
  collection.set([...grouped.entries()].map(([file, entries]) => [vscode.Uri.file(file), entries]));
}

function diagnosticSeverity(value?: string): vscode.DiagnosticSeverity {
  switch (value?.toLowerCase()) {
    case "error":
      return vscode.DiagnosticSeverity.Error;
    case "warning":
    case "warn":
      return vscode.DiagnosticSeverity.Warning;
    case "hint":
      return vscode.DiagnosticSeverity.Hint;
    default:
      return vscode.DiagnosticSeverity.Information;
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
