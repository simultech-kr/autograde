import { createHash } from "node:crypto";
import type { ClassroomCommandContext } from "./classroomCommands";

const semver = require("semver") as {valid(v: string): string | null; gt(a: string, b: string): boolean; satisfies(v: string, range: string): boolean};
export const MANAGED_EXTENSIONS_COMMAND = "autograde.manageCourseExtensions";
export const MAX_EXTENSION_BYTES = 64 * 1024 * 1024;
export interface ExtensionRelease {
  readonly release_id: string;
  readonly extension_id: string;
  readonly version: string;
  readonly sha256: string;
  readonly size_bytes: number;
  readonly target_platform: string;
  readonly vscode_engine: string;
  readonly filename: string;
}
export interface ExtensionReport {
  readonly status: "offered" | "declined" | "installing" | "reload_required" | "installed" | "failed";
  readonly result_code: string;
  readonly installed_version?: string;
}
export interface ExtensionDistributionClient {
  catalog(scope: ClassroomCommandContext, signal: AbortSignal): Promise<readonly ExtensionRelease[]>;
  download(release: ExtensionRelease, scope: ClassroomCommandContext, signal: AbortSignal): Promise<Uint8Array>;
  report(release: ExtensionRelease, report: ExtensionReport, scope: ClassroomCommandContext, signal: AbortSignal): Promise<void>;
}
export class ExtensionInstallError extends Error {
  constructor(public readonly code: string) { super(code); }
}
const platforms = new Set(["universal", "linux-x64", "linux-arm64", "linux-armhf", "darwin-x64", "darwin-arm64", "win32-x64", "win32-arm64", "win32-ia32", "alpine-x64", "alpine-arm64", "web"]);
export function parseExtensionCatalog(value: unknown): readonly ExtensionRelease[] {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ExtensionInstallError("package_invalid");
  const releases = (value as Record<string, unknown>).releases;
  if (!Array.isArray(releases) || releases.length > 200) throw new ExtensionInstallError("package_invalid");
  const ids = new Set<string>(), variants = new Set<string>();
  return releases.map((raw: unknown) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new ExtensionInstallError("package_invalid");
    const row = raw as Record<string, unknown>;
    if (typeof row.release_id !== "string" || !/^er_[a-f0-9]{32}$/.test(row.release_id) || ids.has(row.release_id)
      || typeof row.extension_id !== "string" || !/^[a-z0-9][a-z0-9-]{0,127}\.[a-z0-9][a-z0-9-]{0,127}$/.test(row.extension_id)
      || typeof row.version !== "string" || row.version.length > 96 || !semver.valid(row.version)
      || typeof row.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(row.sha256)
      || !Number.isSafeInteger(row.size_bytes) || Number(row.size_bytes) < 1 || Number(row.size_bytes) > MAX_EXTENSION_BYTES
      || typeof row.target_platform !== "string" || !platforms.has(row.target_platform)
      || typeof row.vscode_engine !== "string" || !row.vscode_engine || row.vscode_engine.length > 200
      || typeof row.filename !== "string" || row.filename.length > 255 || /[\\/\x00-\x1f\x7f]/.test(row.filename) || !row.filename.toLowerCase().endsWith(".vsix")) {
      throw new ExtensionInstallError("package_invalid");
    }
    const variant = `${row.extension_id}/${row.target_platform}`;
    if (variants.has(variant)) throw new ExtensionInstallError("package_invalid");
    ids.add(row.release_id); variants.add(variant);
    return row as unknown as ExtensionRelease;
  });
}
export function extensionTarget(platform: string, arch: string): string { return `${platform}-${arch === "arm" ? "armhf" : arch}`; }
export function chooseExtensionVariants(releases: readonly ExtensionRelease[], target: string): readonly ExtensionRelease[] {
  const chosen = new Map<string, ExtensionRelease>();
  for (const release of releases) {
    const previous = chosen.get(release.extension_id);
    const rank = (item: ExtensionRelease) => item.target_platform === target ? 2 : item.target_platform === "universal" ? 1 : 0;
    if (!previous || rank(release) > rank(previous)) chosen.set(release.extension_id, release);
  }
  return [...chosen.values()];
}
export function verifyExtensionBytes(release: ExtensionRelease, bytes: Uint8Array): void {
  if (bytes.byteLength !== release.size_bytes || createHash("sha256").update(bytes).digest("hex") !== release.sha256) {
    throw new ExtensionInstallError("hash_mismatch");
  }
}
export interface ExtensionInstaller {
  readonly target: string;
  readonly editorVersion: string;
  restriction(): string | undefined;
  installedVersion(id: string): string | undefined;
  isBuiltin(id: string): boolean;
  confirm(release: ExtensionRelease, signal: AbortSignal): Promise<boolean>;
  install(release: ExtensionRelease, bytes: Uint8Array, guard: () => void): Promise<void>;
  notify(message: string): void;
}
const same = (a?: ClassroomCommandContext, b?: ClassroomCommandContext) => a?.baseUrl === b?.baseUrl
  && a?.generation === b?.generation && a?.assignmentId === b?.assignmentId && a?.clientSessionId === b?.clientSessionId;

/** Bounded polling; desired versions live on the server, never credentials or student files. */
export class ManagedExtensions {
  private scope?: ClassroomCommandContext;
  private timer?: ReturnType<typeof setTimeout>;
  private flight?: AbortController;
  private disposed = false;
  private unsupported = false;
  private completed = new Map<string, ExtensionReport>();
  private pending = new Map<string, {release: ExtensionRelease; report: ExtensionReport}>();
  constructor(private readonly api: ExtensionDistributionClient, private readonly current: () => ClassroomCommandContext | undefined,
    private readonly installer: ExtensionInstaller, private readonly pollMs = 30_000) {}

  public reconcile(): void {
    if (this.disposed) return;
    const next = this.current();
    if (!same(this.scope, next)) {
      this.flight?.abort(); this.completed.clear(); this.pending.clear(); this.unsupported = false;
      this.scope = next ? {...next} : undefined;
      if (this.timer) clearTimeout(this.timer); this.timer = undefined;
    }
    if (this.scope && !this.flight && !this.timer && !this.unsupported) void this.check();
  }
  public async check(manual = false): Promise<void> {
    if (this.disposed || this.flight) return;
    const next = this.current();
    if (!same(this.scope, next)) {this.completed.clear(); this.pending.clear(); this.unsupported = false; this.scope = next ? {...next} : undefined;}
    const scope = this.scope;
    if (!scope) {if (manual) this.installer.notify("과제를 연결한 뒤 수업 확장을 확인하세요."); return;}
    if (this.unsupported && !manual) return;
    if (this.timer) clearTimeout(this.timer); this.timer = undefined;
    const controller = new AbortController(); this.flight = controller;
    let catalogReceived = false;
    const guard = () => {if (this.disposed || controller.signal.aborted || !same(scope, this.current())) throw new ExtensionInstallError("scope_changed");};
    const send = async (release: ExtensionRelease, report: ExtensionReport) => {
      guard(); this.pending.set(release.release_id, {release, report});
      await this.api.report(release, report, scope, controller.signal); guard(); this.pending.delete(release.release_id);
    };
    try {
      const releases = chooseExtensionVariants(await this.api.catalog(scope, controller.signal), this.installer.target); guard();
      catalogReceived = true; this.unsupported = false;
      const active = new Set(releases.map(item => item.release_id));
      for (const id of this.completed.keys()) {if (!active.has(id)) this.completed.delete(id);}
      for (const [id, item] of this.pending) {if (active.has(id)) await send(item.release, item.report); else this.pending.delete(id);}
      if (manual && !releases.length) this.installer.notify("현재 수업에 공개된 IDE 확장이 없습니다.");
      for (const release of releases) {
        guard();
        const installed = this.installer.installedVersion(release.extension_id);
        if (installed === release.version) {
          if (this.completed.get(release.release_id)?.status !== "installed") {
            const report = {status: "installed", result_code: "version_verified", installed_version: installed} as const;
            this.completed.set(release.release_id, report); await send(release, report);
          }
          continue;
        }
        const earlier = this.completed.get(release.release_id);
        if (earlier && (!manual || earlier.status === "reload_required")) continue;
        let code = this.installer.restriction();
        if (!code && this.installer.isBuiltin(release.extension_id)) code = "built_in_extension";
        if (!code && release.target_platform !== "universal" && release.target_platform !== this.installer.target) code = "incompatible_platform";
        if (!code && !semver.satisfies(this.installer.editorVersion, release.vscode_engine)) code = "incompatible_engine";
        if (!code && installed && semver.valid(installed) && semver.gt(installed, release.version)) code = "newer_version_installed";
        if (code) {
          const report = {status: "failed", result_code: code} as const;
          this.completed.set(release.release_id, report); await send(release, report);
          if (manual) this.installer.notify(extensionFailureLabel(code));
          continue;
        }
        await send(release, {status: "offered", result_code: "approval_required"});
        const approved = await this.installer.confirm(release, controller.signal); guard();
        if (!approved) {
          const report = {status: "declined", result_code: "student_declined"} as const;
          this.completed.set(release.release_id, report); await send(release, report); continue;
        }
        let report: ExtensionReport;
        try {
          if (this.installer.restriction()) throw new ExtensionInstallError(this.installer.restriction()!);
          await send(release, {status: "installing", result_code: "installation_started"});
          let bytes: Uint8Array;
          try {bytes = await this.api.download(release, scope, controller.signal);} catch {throw new ExtensionInstallError("download_failed");}
          guard(); verifyExtensionBytes(release, bytes);
          // Recheck publication after download/confirmation. Revoked/replaced releases never get installed from a stale list.
          const fresh = await this.api.catalog(scope, controller.signal); guard();
          if (!fresh.some(item => item.release_id === release.release_id && item.sha256 === release.sha256)) throw new ExtensionInstallError("scope_changed");
          if (this.installer.restriction()) throw new ExtensionInstallError(this.installer.restriction()!);
          await this.installer.install(release, bytes, guard); guard();
          const version = this.installer.installedVersion(release.extension_id);
          report = version === release.version ? {status: "installed", result_code: "version_verified", installed_version: version}
            : {status: "reload_required", result_code: "reload_required", ...(version ? {installed_version: version} : {})};
          this.installer.notify(report.status === "installed" ? `${release.extension_id} ${release.version} 버전이 IDE에 적용되었습니다.`
            : `${release.extension_id} 설치를 접수했지만 현재 IDE에는 아직 적용되지 않았습니다. 현재 작업을 제출·내보내기한 뒤 공용 실습을 종료하고 새 실습에서 다시 확인하세요. 계속 적용 대기이면 담당자에게 IDE 앱에 포함한 배포를 요청하세요.`);
        } catch (error) {
          guard();
          const reason = error instanceof ExtensionInstallError ? error.code : "install_failed";
          report = {status: "failed", result_code: reason}; this.installer.notify(extensionFailureLabel(reason));
        }
        this.completed.set(release.release_id, report); await send(release, report);
      }
    } catch (error) {
      if (!controller.signal.aborted && same(scope, this.current())) {
        const status = Number((error as {status?: number})?.status);
        if ([401, 403].includes(status) || (!catalogReceived && [404, 405].includes(status))) this.unsupported = true;
        if (manual) this.installer.notify("수업 확장 목록·상태 보고를 확인하지 못했습니다. 서버 연결과 로그인을 확인하세요.");
      }
    } finally {
      if (this.flight === controller) this.flight = undefined;
      if (!this.disposed && this.current() && !this.unsupported) this.timer = setTimeout(() => {this.timer = undefined; void this.check();}, this.pollMs);
    }
  }
  public dispose(): void {this.disposed = true; this.flight?.abort(); if (this.timer) clearTimeout(this.timer); this.completed.clear(); this.pending.clear();}
}
export function extensionFailureLabel(code: string): string {
  const labels: Record<string, string> = {
    restricted_policy: "현재 IDE 제한 정책에서는 추가 확장을 설치할 수 없습니다.",
    untrusted_workspace: "현재 실습 폴더를 신뢰한 뒤 수업 확장 확인을 다시 실행하세요.",
    unsupported_client: "공용 실습실용 Autograde IDE에서 수업 확장을 설치할 수 있습니다.",
    built_in_extension: "내장 확장은 IDE 앱 업데이트로 변경해야 합니다. 담당자에게 앱 업데이트를 요청하세요.",
    incompatible_platform: "현재 운영체제·프로세서에 맞는 확장 파일이 없습니다. 교수자에게 알려 주세요.",
    incompatible_engine: "현재 IDE 버전과 호환되지 않는 확장입니다. 담당자에게 확인하세요.",
    newer_version_installed: "이미 더 높은 확장 버전이 있어 자동으로 이전 버전을 설치하지 않았습니다.",
    hash_mismatch: "확장 파일의 크기·검증값이 일치하지 않아 설치를 중단했습니다.",
    download_failed: "확장 파일을 받지 못했습니다. 연결 후 수업 확장 확인을 다시 실행하세요.",
    scope_changed: "수업 연결 또는 공개 버전이 변경되어 확장 설치를 중단했습니다.",
  };
  return labels[code] ?? "확장 설치를 완료하지 못했습니다. 교수자에게 상태를 확인해 주세요.";
}
