import { execFile } from "node:child_process";
import { realpath } from "node:fs/promises";
import * as path from "node:path";

import { normalizeRepositoryLocator, selectRepositoryCloneUrl } from "./helpers";
import type { AssignedRepository } from "./types";

export interface RepositoryState {
  readonly root: string;
  readonly branch: string;
  readonly headSha: string;
  readonly remoteName: string;
  readonly remoteUrl: string;
  readonly remoteRef: string;
  readonly remoteHeadSha: string;
  readonly dirty: boolean;
}

export class GitPreflightError extends Error {
  public constructor(message: string) {
    super(message);
    this.name = "GitPreflightError";
  }
}

export async function cloneRepository(
  cloneUrl: string,
  targetPath: string,
  signal?: AbortSignal,
): Promise<void> {
  if (!selectRepositoryCloneUrl({ cloneUrl } satisfies AssignedRepository)) {
    throw new GitPreflightError("서비스가 제공한 clone URL이 올바르지 않습니다.");
  }
  await runGitProcess(["clone", "--", cloneUrl, targetPath], {
    signal,
    timeout: 5 * 60_000,
  });
}

export async function inspectRepository(workspacePath: string): Promise<RepositoryState> {
  let root: string;
  try {
    root = await runGit(workspacePath, ["rev-parse", "--show-toplevel"]);
  } catch {
    throw new GitPreflightError("현재 workspace가 Git repository가 아닙니다.");
  }

  let canonicalRoot: string;
  let canonicalWorkspace: string;
  try {
    [canonicalRoot, canonicalWorkspace] = await Promise.all([
      realpath(root),
      realpath(path.resolve(workspacePath)),
    ]);
  } catch {
    throw new GitPreflightError("현재 workspace의 Git repository를 확인할 수 없습니다.");
  }
  const workspaceRelation = path.relative(canonicalRoot, canonicalWorkspace);
  if (workspaceRelation.startsWith("..") || path.isAbsolute(workspaceRelation)) {
    throw new GitPreflightError("현재 workspace의 Git repository를 확인할 수 없습니다.");
  }
  root = canonicalRoot;

  const [headSha, branch, status] = await Promise.all([
    runGit(root, ["rev-parse", "HEAD"]),
    runGit(root, ["symbolic-ref", "--quiet", "--short", "HEAD"]).catch(() => ""),
    runGit(root, ["status", "--porcelain=v1", "--untracked-files=normal"]),
  ]);
  if (!branch) {
    throw new GitPreflightError("detached HEAD에서는 제출할 수 없습니다. 제출 branch로 전환하세요.");
  }
  if (status !== "") {
    throw new GitPreflightError("commit되지 않은 변경 사항이 있습니다. 모든 변경을 commit한 뒤 제출하세요.");
  }

  const remoteName = await runGit(root, ["config", "--get", `branch.${branch}.remote`]).catch(() => "");
  const remoteRef = await runGit(root, ["config", "--get", `branch.${branch}.merge`]).catch(() => "");
  if (!remoteName || !remoteRef || remoteName === ".") {
    throw new GitPreflightError("현재 branch에 원격 upstream이 없습니다. 먼저 branch를 push하세요.");
  }

  const remoteUrl = await runGit(root, ["remote", "get-url", remoteName]).catch(() => "");
  if (!remoteUrl || !normalizeRepositoryLocator(remoteUrl)) {
    throw new GitPreflightError("현재 branch의 원격 repository를 확인할 수 없습니다.");
  }

  let remoteOutput: string;
  try {
    remoteOutput = await runGit(root, ["ls-remote", "--exit-code", "--", remoteName, remoteRef], 20_000);
  } catch {
    throw new GitPreflightError("원격 branch를 확인할 수 없습니다. GitHub 인증과 push 상태를 확인하세요.");
  }
  const remoteHeadSha = remoteOutput.split(/\s+/)[0]?.toLowerCase();
  if (!remoteHeadSha || !/^[0-9a-f]{40,64}$/.test(remoteHeadSha)) {
    throw new GitPreflightError("원격 branch에 제출할 commit이 없습니다. 먼저 push하세요.");
  }
  if (remoteHeadSha !== headSha.toLowerCase()) {
    throw new GitPreflightError("현재 HEAD가 원격 branch에 push되지 않았습니다. push 후 다시 제출하세요.");
  }

  return {
    root,
    branch,
    headSha: headSha.toLowerCase(),
    remoteName,
    remoteUrl,
    remoteRef,
    remoteHeadSha,
    dirty: false,
  };
}

function runGit(cwd: string, args: readonly string[], timeout = 15_000): Promise<string> {
  return runGitProcess(["-C", cwd, ...args], { cwd, timeout });
}

function runGitProcess(
  args: readonly string[],
  options: { readonly cwd?: string; readonly timeout: number; readonly signal?: AbortSignal },
): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(
      "git",
      [...args],
      {
        cwd: options.cwd,
        encoding: "utf8",
        maxBuffer: 4 * 1024 * 1024,
        timeout: options.timeout,
        signal: options.signal,
        windowsHide: true,
      },
      (error, stdout) => {
        if (error) {
          // stderr may contain a credential-bearing remote URL; never propagate it.
          reject(new Error("Git command failed."));
          return;
        }
        resolve(stdout.trim());
      },
    );
  });
}
