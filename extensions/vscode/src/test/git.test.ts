import assert from "node:assert/strict";
import { execFileSync, spawn, type ChildProcess } from "node:child_process";
import {
  mkdirSync,
  mkdtempSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { basename, join } from "node:path";
import test from "node:test";

import { GitPreflightError, inspectRepository } from "../git";

function git(cwd: string, ...args: string[]): string {
  return execFileSync("git", ["-C", cwd, ...args], {
    encoding: "utf8",
    stdio: ["ignore", "pipe", "pipe"],
  }).trim();
}

async function unusedPort(): Promise<number> {
  const server = createServer();
  await new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const address = server.address();
  assert.ok(address && typeof address === "object");
  const port = address.port;
  await new Promise<void>((resolve) => server.close(() => resolve()));
  return port;
}

async function waitForDaemon(process: ChildProcess): Promise<void> {
  await new Promise<void>((resolve, reject) => {
    const timeout = setTimeout(resolve, 400);
    process.once("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    process.once("exit", (code) => {
      clearTimeout(timeout);
      reject(new Error(`git daemon exited before the test (${code ?? "signal"})`));
    });
  });
}

test("Git preflight handles WSL-style repository states and symlink workspaces", async (context) => {
  if (process.platform === "win32") {
    context.skip("the workspace extension runs inside WSL/Linux, not win32");
    return;
  }

  const temporaryAlias = mkdtempSync(join(tmpdir(), "autograde-extension-git-"));
  const root = realpathSync(temporaryAlias);
  let daemon: ChildProcess | undefined;
  context.after(() => {
    if (daemon?.exitCode === null) {
      daemon.kill("SIGTERM");
    }
    if (basename(temporaryAlias).startsWith("autograde-extension-git-")) {
      rmSync(temporaryAlias, { recursive: true, force: true });
    }
  });

  const source = join(root, "source");
  const organization = join(root, "org");
  const bare = join(organization, "student.git");
  mkdirSync(organization);
  git(root, "init", "-b", "main", source);
  git(source, "config", "user.email", "extension-test@example.invalid");
  git(source, "config", "user.name", "Extension Test");
  writeFileSync(join(source, "answer.txt"), "answer\n");
  git(source, "add", "answer.txt");
  git(source, "commit", "-m", "initial");
  git(organization, "init", "--bare", bare);
  git(bare, "symbolic-ref", "HEAD", "refs/heads/main");
  git(source, "remote", "add", "bootstrap", bare);
  git(source, "push", "bootstrap", "main:main");

  const port = await unusedPort();
  daemon = spawn("git", [
    "daemon",
    "--reuseaddr",
    "--export-all",
    `--base-path=${root}`,
    "--listen=127.0.0.1",
    `--port=${port}`,
    root,
  ], { stdio: "ignore" });
  await waitForDaemon(daemon);

  const remoteUrl = `git://127.0.0.1:${port}/org/student.git`;
  git(source, "remote", "remove", "bootstrap");
  git(source, "remote", "add", "origin", remoteUrl);
  git(source, "fetch", "origin");
  git(source, "branch", "--set-upstream-to=origin/main", "main");

  const clean = await inspectRepository(source);
  assert.equal(clean.headSha, clean.remoteHeadSha);
  assert.equal(clean.remoteRef, "refs/heads/main");

  const symlink = join(root, "source-link");
  symlinkSync(source, symlink);
  const throughSymlink = await inspectRepository(symlink);
  assert.equal(throughSymlink.root, realpathSync(source));

  writeFileSync(join(source, "untracked.txt"), "dirty\n");
  await assert.rejects(
    inspectRepository(source),
    (error: unknown) => error instanceof GitPreflightError && /commit되지 않은/.test(error.message),
  );
  rmSync(join(source, "untracked.txt"));

  git(source, "checkout", "--detach", "HEAD");
  await assert.rejects(inspectRepository(source), /detached HEAD/);
  git(source, "checkout", "main");

  git(source, "commit", "--allow-empty", "-m", "not pushed");
  await assert.rejects(inspectRepository(source), /push되지 않았습니다/);
});
