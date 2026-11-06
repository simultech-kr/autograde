import { constants } from "node:fs";
import { lstat, mkdir, open, realpath } from "node:fs/promises";
import * as path from "node:path";
import { studentWorkspaceKey } from "./studentWorkspaceKey";

export const SHARED_SESSION_MARKER = ".autograde-session.json";
export const SHARED_AUTH_USED = ".autograde-auth-used";
export const END_LEARNING_SESSION_COMMAND = "autograde.endLearningSession";
export type SharedSessionCleanupMode = "on_exit" | "next_start";

function cleanupMode(value: unknown): SharedSessionCleanupMode {
  if (value === undefined) return "on_exit";
  if (value === "on_exit" || value === "next_start") return value;
  throw new Error(message);
}

/** Work-file lifetime comes from the launcher; authentication always ends with the session. */
export function sharedSessionCleanupNotice(mode: SharedSessionCleanupMode = "on_exit"): string {
  return mode === "next_start"
    ? "이번 실습의 코드·자료는 다음 IDE 실행 시 정리합니다. 정상 종료할 때 로그인 정보와 IDE 임시 기록을 정리합니다."
    : "IDE를 완전히 종료하면 이번 실습의 코드·자료·로그인 정보·IDE 임시 기록을 정리합니다. 다음 실행 시 남은 파일도 정리합니다.";
}

const message = "공용 실습 세션을 확인할 수 없습니다. IDE를 완전히 종료하고 Autograde 실행기로 다시 시작하세요.";
const inside = (parent: string, child: string): boolean => {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
};

async function regularDirectory(directory: string, privateMode = false): Promise<void> {
  const info = await lstat(directory);
  if (!info.isDirectory() || info.isSymbolicLink() || (process.getuid && info.uid !== process.getuid() && privateMode)
    || (process.platform !== "win32" && privateMode && (info.mode & 0o077) !== 0)) throw new Error(message);
}

async function noLinkedAncestors(candidate: string): Promise<void> {
  let current = path.resolve(candidate);
  for (;;) {
    await regularDirectory(current);
    const parent = path.dirname(current);
    if (parent === current) return;
    current = parent;
  }
}

async function readPrivateObject(file: string): Promise<Record<string, unknown>> {
  // Windows has no O_NOFOLLOW. A dangling link is not an unused marker.
  const before = await lstat(file);
  if (!before.isFile() || before.isSymbolicLink()) throw new Error(message);
  const handle = await open(file, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.dev !== before.dev || info.ino !== before.ino || info.nlink !== 1 || info.size > 8192 ||
      (process.getuid && info.uid !== process.getuid()) || (process.platform !== "win32" && (info.mode & 0o077) !== 0)) throw new Error(message);
    const value: unknown = JSON.parse(await handle.readFile("utf8"));
    const after = await lstat(file);
    if (!after.isFile() || after.isSymbolicLink() || after.dev !== info.dev || after.ino !== info.ino
      || after.size !== info.size || after.mtimeMs !== info.mtimeMs) throw new Error(message);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(message);
    return value as Record<string, unknown>;
  } finally { await handle.close(); }
}

type DirectoryIdentity = {dev: number; ino: number};
type StudentBinding = {studentKey: string; workspaceIdentity: DirectoryIdentity; materialsIdentity: DirectoryIdentity};
function directoryIdentity(value: unknown): DirectoryIdentity {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(message);
  const record = value as Record<string, unknown>;
  if (typeof record.dev !== "number" || !Number.isFinite(record.dev) || typeof record.ino !== "number" || !Number.isFinite(record.ino)) throw new Error(message);
  return {dev: record.dev, ino: record.ino};
}
function studentBinding(flag: Record<string, unknown>): StudentBinding {
  if (flag.schema !== 2) throw new Error(message);
  return {studentKey: studentWorkspaceKey(flag.studentKey), workspaceIdentity: directoryIdentity(flag.workspaceIdentity),
    materialsIdentity: directoryIdentity(flag.materialsIdentity)};
}

/** Scoped lifecycle hint, not an OS security boundary. Only the launcher deletes these files. */
export class SharedLearningSession {
  private used: boolean;
  private constructor(public readonly root: string, private readonly workspaceBase: string,
    private readonly materialsBase: string, public readonly id: string, public readonly cleanupMode: SharedSessionCleanupMode,
    private readonly rootIdentity: DirectoryIdentity, used: boolean, private binding?: StudentBinding) { this.used = used; }

  /** The launcher keeps this empty parent open so authentication never forces an extension-host reload. */
  get initialWorkspace(): string { return this.workspaceBase; }
  get workspace(): string { return this.binding ? path.join(this.workspaceBase, this.binding.studentKey) : this.workspaceBase; }
  get materials(): string {
    if (!this.binding) throw new Error("학생 인증 후 학번별 자료 폴더를 사용할 수 있습니다. 공용 실습을 다시 시작하고 수령 코드를 입력하세요.");
    return path.join(this.materialsBase, this.binding.studentKey);
  }
  get studentKey(): string | undefined { return this.binding?.studentKey; }

  static async fromEnvironment(appName: string, environment: Readonly<Record<string, string | undefined>>): Promise<SharedLearningSession | undefined> {
    if (!["Autograde IDE", "Autograde IDE Dev"].includes(appName) || environment.AUTOGRADE_LEARNING_LIFETIME !== "shared") return undefined;
    const root = environment.AUTOGRADE_SESSION_ROOT, id = environment.AUTOGRADE_SESSION_ID;
    if (!root || !path.isAbsolute(root) || root.includes("\0") || !id || !/^[a-f0-9]{24}$/.test(id)
      || !["current", `e-${id}`].includes(path.basename(root)) || path.resolve(root) !== root
      || environment.AUTOGRADE_LEARNER_WORKSPACE !== path.join(root, "workspace")
      || environment.AUTOGRADE_LEARNER_MATERIALS !== path.join(root, "materials")) throw new Error(message);
    await noLinkedAncestors(root);
    await regularDirectory(root, true);
    const stat = await lstat(root);
    const marker = await readPrivateObject(path.join(root, SHARED_SESSION_MARKER));
    const mode = cleanupMode(environment.AUTOGRADE_SESSION_CLEANUP_MODE);
    if (marker.schema !== 1 || marker.sessionId !== id || marker.root !== root || marker.rootDev !== stat.dev
      || marker.rootIno !== stat.ino || !Number.isSafeInteger(marker.launcherPid) || (marker.launcherPid as number) <= 0
      || !["launching", "running"].includes(String(marker.phase)) || cleanupMode(marker.cleanupMode) !== mode) throw new Error(message);
    try { process.kill(marker.launcherPid as number, 0); } catch { throw new Error(message); }
    const workspace = path.join(root, "workspace"), materials = path.join(root, "materials");
    await regularDirectory(workspace, true); await regularDirectory(materials, true);
    let used = false, binding: StudentBinding | undefined;
    try {
      const flag = await readPrivateObject(path.join(root, SHARED_AUTH_USED));
      if (![1, 2].includes(flag.schema as number) || flag.sessionId !== id || flag.authenticated !== true) throw new Error(message);
      if (flag.schema === 2) binding = studentBinding(flag);
      used = true;
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    const session = new SharedLearningSession(root, workspace, materials, id, mode, {dev: stat.dev, ino: stat.ino}, used, binding);
    if (binding) await session.assertStudentDirectories();
    return session;
  }

  get authenticationUsed(): boolean { return this.used; }

  assertCanAuthenticate(): void {
    if (this.used) throw new Error("공용 실습에서는 학생·과제 인증을 교체할 수 없습니다. 필요한 코드를 내보낸 뒤 ‘공용 실습 종료’를 누르고 새 IDE 세션에서 수령 코드를 입력하세요.");
  }

  async markAuthenticated(studentKey: string, assertCurrent: () => void = () => {}): Promise<void> {
    this.assertCanAuthenticate();
    const key = studentWorkspaceKey(studentKey);
    assertCurrent(); await this.assertOwnedRoot(); assertCurrent();
    await noLinkedAncestors(this.workspaceBase); await regularDirectory(this.workspaceBase, true);
    await noLinkedAncestors(this.materialsBase); await regularDirectory(this.materialsBase, true); assertCurrent();
    const handle = await open(path.join(this.root, SHARED_AUTH_USED), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0), 0o600);
    // A partial folder/marker write closes the session too. Never adopt an existing student directory.
    this.used = true;
    try {
      assertCurrent();
      const workspace = path.join(this.workspaceBase, key), materials = path.join(this.materialsBase, key);
      await mkdir(workspace, {mode: 0o700}); assertCurrent();
      await mkdir(materials, {mode: 0o700}); assertCurrent();
      await this.assertOwnedRoot();
      await noLinkedAncestors(workspace); await regularDirectory(workspace, true);
      await noLinkedAncestors(materials); await regularDirectory(materials, true); assertCurrent();
      const work = await lstat(workspace), material = await lstat(materials); assertCurrent();
      const binding: StudentBinding = {studentKey: key, workspaceIdentity: {dev: work.dev, ino: work.ino},
        materialsIdentity: {dev: material.dev, ino: material.ino}};
      await handle.writeFile(JSON.stringify({schema: 2, sessionId: this.id, authenticated: true, ...binding}));
      await handle.sync(); assertCurrent(); this.binding = binding;
    } finally { await handle.close(); }
  }

  async assertMaterialsDirectory(): Promise<void> {
    await this.assertOwnedRoot();
    if (!this.binding) throw new Error(message);
    await this.assertStudentDirectories();
  }

  private async assertStudentDirectories(): Promise<void> {
    const binding = this.binding;
    if (!binding) throw new Error(message);
    const marker = await readPrivateObject(path.join(this.root, SHARED_AUTH_USED));
    if (marker.sessionId !== this.id || marker.authenticated !== true) throw new Error(message);
    const saved = studentBinding(marker);
    if (JSON.stringify(saved) !== JSON.stringify(binding)) throw new Error(message);
    for (const [directory, identity] of [[this.workspace, binding.workspaceIdentity], [this.materials, binding.materialsIdentity]] as const) {
      await noLinkedAncestors(directory); await regularDirectory(directory, true);
      const actual = await lstat(directory);
      if (actual.dev !== identity.dev || actual.ino !== identity.ino) throw new Error(message);
    }
  }

  assertStudentBound(): void {
    if (!this.binding) throw new Error("학생 인증 후 학번별 실습폴더를 준비해야 합니다. 수령 코드 연결을 완료하세요.");
  }

  async assertWorkspacePath(candidate: string): Promise<void> { await this.assertWorkPath(candidate, false); }
  // Legacy used sessions may rescue their own files without re-authenticating or receiving new files.
  async assertExportWorkspacePath(candidate: string): Promise<void> { await this.assertWorkPath(candidate, true); }

  private async assertWorkPath(candidate: string, exporting: boolean): Promise<void> {
    await this.assertOwnedRoot();
    if (this.binding || this.used && !exporting) await this.assertStudentDirectories();
    const normalized = path.resolve(candidate);
    if (!inside(this.workspace, normalized)) throw new Error("공용 실습에서는 이번 세션의 작업 폴더만 사용할 수 있습니다. 보관할 파일은 ‘내 학습내용 내보내기’를 사용하세요.");
    await noLinkedAncestors(normalized);
    if (!inside(await realpath(this.workspace), await realpath(normalized))) throw new Error(message);
  }

  private async assertOwnedRoot(): Promise<void> {
    await noLinkedAncestors(this.root);
    await regularDirectory(this.root, true);
    const info = await lstat(this.root);
    if (info.ino !== this.rootIdentity.ino || info.dev !== this.rootIdentity.dev) throw new Error(message);
    const marker = await readPrivateObject(path.join(this.root, SHARED_SESSION_MARKER));
    if (marker.sessionId !== this.id || marker.root !== this.root || marker.rootDev !== info.dev || marker.rootIno !== info.ino
      || !["launching", "running"].includes(String(marker.phase)) || cleanupMode(marker.cleanupMode) !== this.cleanupMode) throw new Error(message);
  }
}
