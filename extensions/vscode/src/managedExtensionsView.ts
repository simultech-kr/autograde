import * as vscode from "vscode";
import * as path from "node:path";
import { mkdtemp, writeFile, rm, lstat, realpath } from "node:fs/promises";
import { ExtensionInstallError, extensionTarget, type ExtensionInstaller, type ExtensionRelease } from "./managedExtensions";
import type { SharedLearningSession } from "./sharedLearningSession";
import type { LearningEnvironment } from "./learningEnvironment";
import type { AssignmentOperationGate } from "./assignmentOperation";

const INSTALL_COURSE_EXTENSION_COMMAND = "_autograde.installCourseExtension";

export class ManagedExtensionsInstaller implements ExtensionInstaller {
  public readonly target = extensionTarget(process.platform, process.arch);
  public readonly editorVersion = vscode.version;
  constructor(private readonly session: SharedLearningSession | undefined, private readonly environment: LearningEnvironment,
    private readonly operations: AssignmentOperationGate, private readonly own: vscode.Extension<unknown>) {}
  public restriction(): string | undefined {
    if (!this.session || this.environment.policy === "unmanaged" || vscode.env.remoteName) return "unsupported_client";
    if (this.environment.policy === "restricted") return "restricted_policy";
    if (!vscode.workspace.isTrusted) return "untrusted_workspace";
    return undefined;
  }
  public installedVersion(id: string): string | undefined {
    const extension = id === this.own.id ? this.own : vscode.extensions.getExtension(id);
    const version: unknown = extension?.packageJSON.version;
    return typeof version === "string" ? version : undefined;
  }
  public isBuiltin(id: string): boolean {
    if (id === this.own.id || id === "autograde.autograde-ide-profile") return true;
    const extension = vscode.extensions.getExtension(id);
    if (!extension || !this.session) return false;
    const relative = path.relative(path.join(this.session.root, "r", "extensions"), extension.extensionPath);
    // Only user extensions in the disposable extension directory are managed here.
    return relative === ".." || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative);
  }
  public async confirm(release: ExtensionRelease, signal: AbortSignal): Promise<boolean> {
    const cancellation = new vscode.CancellationTokenSource();
    const abort = () => cancellation.cancel(); signal.addEventListener("abort", abort, {once: true});
    if (signal.aborted) cancellation.cancel();
    try {
      const choice = await vscode.window.showQuickPick([
        {label: "설치", approved: true, detail: "교수자가 공개한 확장을 이번 실습에 설치합니다. 설치 상태를 교수자에게 전달합니다."},
        {label: "나중에", approved: false, detail: "자동 설치하지 않습니다. ‘수업 확장 설치·업데이트 확인’에서 다시 확인할 수 있습니다."},
      ], {title: `수업 확장 · ${release.extension_id} ${release.version}`, ignoreFocusOut: true,
        placeHolder: "공용 실습 종료 시 설치 파일도 정리되며, 다음 실습에서는 다시 설치합니다."}, cancellation.token);
      return choice?.approved === true;
    } finally {signal.removeEventListener("abort", abort); cancellation.dispose();}
  }
  public async install(release: ExtensionRelease, bytes: Uint8Array, guard: () => void): Promise<void> {
    await this.operations.run(async () => {
      guard(); const restriction = this.restriction(); if (restriction) throw new ExtensionInstallError(restriction);
      if (!this.session) throw new ExtensionInstallError("unsupported_client");
      // Revalidate the launcher-owned session; never stage into a student assignment or a persistent home.
      await this.session.assertWorkspacePath(this.session.workspace); guard();
      const runtime = path.join(this.session.root, "r"), stat = await lstat(runtime);
      if (!stat.isDirectory() || stat.isSymbolicLink() || await realpath(runtime) !== runtime) throw new ExtensionInstallError("package_invalid");
      const temporary = await mkdtemp(path.join(runtime, "extension-install-"));
      try {
        const file = path.join(temporary, `${release.release_id}.vsix`);
        await writeFile(file, bytes, {flag: "wx", mode: 0o600}); guard();
        if (this.restriction()) throw new ExtensionInstallError(this.restriction()!);
        // Code-OSS authenticates this built-in caller and rechecks the staged file's hash.
        // Never fall back to the public VSIX installer, including on an older IDE.
        try {
          await vscode.commands.executeCommand(INSTALL_COURSE_EXTENSION_COMMAND, vscode.Uri.file(file), release.sha256);
        } catch (error) {
          if (error instanceof Error && error.message === `command '${INSTALL_COURSE_EXTENSION_COMMAND}' not found`) {
            throw new ExtensionInstallError("unsupported_client");
          }
          throw error;
        }
      } finally {await rm(temporary, {recursive: true, force: true});}
    }, () => {throw new ExtensionInstallError("install_failed");});
  }
  public notify(message: string): void {void vscode.window.showInformationMessage(message);}
}
