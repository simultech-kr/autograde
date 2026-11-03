import { symlink } from "node:fs/promises";
import * as path from "node:path";
import type { TestContext } from "node:test";

interface LinkOptions {
  platform?: NodeJS.Platform;
  createLink?: (target: string, alias: string, type: "dir" | "file" | "junction") => Promise<void>;
}

/** Directory junctions exercise real alias boundaries without Windows symlink privileges. */
export async function symlinkDirectory(target: string, alias: string, options: LinkOptions = {}): Promise<void> {
  await (options.createLink ?? symlink)(path.resolve(target), alias, (options.platform ?? process.platform) === "win32" ? "junction" : "dir");
}

/** Only fixture creation may be skipped; assertions against production code must remain outside this helper. */
export async function symlinkFileOrSkip(context: Pick<TestContext, "skip">, target: string, alias: string, options: LinkOptions = {}): Promise<boolean> {
  try {
    await (options.createLink ?? symlink)(target, alias, "file");
    return true;
  } catch (error) {
    const code = (error as NodeJS.ErrnoException).code;
    if ((options.platform ?? process.platform) !== "win32" || !["EPERM", "EACCES", "ENOTSUP"].includes(code ?? "")) throw error;
    context.skip(`Windows file-symlink fixture unavailable (${code}); requires symlink privileges. Directory-junction and hard-link checks run separately.`);
    return false;
  }
}
