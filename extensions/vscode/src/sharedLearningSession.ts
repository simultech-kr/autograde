import { constants } from "node:fs";
import { lstat, open, realpath } from "node:fs/promises";
import * as path from "node:path";

export const SHARED_SESSION_MARKER = ".autograde-session.json";
export const SHARED_AUTH_USED = ".autograde-auth-used";
export const END_LEARNING_SESSION_COMMAND = "autograde.endLearningSession";

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

/** Scoped lifecycle hint, not an OS security boundary. Only the launcher deletes these files. */
export class SharedLearningSession {
  private used: boolean;
  private constructor(public readonly root: string, public readonly workspace: string,
    public readonly materials: string, public readonly id: string, private readonly rootIdentity: {dev: number; ino: number}, used: boolean) { this.used = used; }

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
    if (marker.schema !== 1 || marker.sessionId !== id || marker.root !== root || marker.rootDev !== stat.dev
      || marker.rootIno !== stat.ino || !Number.isSafeInteger(marker.launcherPid) || (marker.launcherPid as number) <= 0
      || !["launching", "running"].includes(String(marker.phase))) throw new Error(message);
    try { process.kill(marker.launcherPid as number, 0); } catch { throw new Error(message); }
    const workspace = path.join(root, "workspace"), materials = path.join(root, "materials");
    await regularDirectory(workspace, true); await regularDirectory(materials, true);
    let used = false;
    try {
      const flag = await readPrivateObject(path.join(root, SHARED_AUTH_USED));
      if (flag.schema !== 1 || flag.sessionId !== id || flag.authenticated !== true) throw new Error(message);
      used = true;
    } catch (error) { if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error; }
    return new SharedLearningSession(root, workspace, materials, id, {dev: stat.dev, ino: stat.ino}, used);
  }

  get authenticationUsed(): boolean { return this.used; }

  assertCanAuthenticate(): void {
    if (this.used) throw new Error("공용 실습에서는 학생·과제 인증을 교체할 수 없습니다. 필요한 코드를 내보낸 뒤 ‘공용 실습 종료’를 누르고 새 IDE 세션에서 수령 코드를 입력하세요.");
  }

  async markAuthenticated(): Promise<void> {
    this.assertCanAuthenticate();
    await this.assertOwnedRoot();
    const handle = await open(path.join(this.root, SHARED_AUTH_USED), constants.O_WRONLY | constants.O_CREAT | constants.O_EXCL | (constants.O_NOFOLLOW ?? 0), 0o600);
    // Keep the boundary closed even if a write fails partway through.
    this.used = true;
    try { await handle.writeFile(JSON.stringify({schema: 1, sessionId: this.id, authenticated: true})); await handle.sync(); }
    finally { await handle.close(); }
  }

  async assertWorkspacePath(candidate: string): Promise<void> {
    await this.assertOwnedRoot();
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
      || !["launching", "running"].includes(String(marker.phase))) throw new Error(message);
  }
}
