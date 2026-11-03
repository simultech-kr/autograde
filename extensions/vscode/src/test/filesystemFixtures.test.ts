import assert from "node:assert/strict";
import * as path from "node:path";
import test from "node:test";
import { symlinkDirectory, symlinkFileOrSkip } from "./filesystemFixtures";

test("directory fixtures use absolute Windows junction targets and POSIX directory links", async () => {
  for (const platform of ["win32", "darwin", "linux"] as const) {
    const calls: string[][] = [];
    await symlinkDirectory("fixture-target", "fixture-alias", {platform, createLink: async (...args) => { calls.push(args); }});
    assert.deepEqual(calls, [[path.resolve("fixture-target"), "fixture-alias", platform === "win32" ? "junction" : "dir"]]);
  }
});

test("directory fixture errors always fail instead of bypassing boundary tests", async () => {
  const error = Object.assign(new Error("denied"), {code: "EPERM"});
  await assert.rejects(symlinkDirectory("target", "alias", {platform: "win32", createLink: async () => { throw error; }}), error);
});

test("supported file symlinks run normally on Windows and POSIX", async () => {
  for (const platform of ["win32", "darwin", "linux"] as const) {
    const calls: string[][] = [], skipped: string[] = [];
    const result = await symlinkFileOrSkip({skip: reason => { skipped.push(String(reason)); }}, "target", "alias", {
      platform, createLink: async (...args) => { calls.push(args); },
    });
    assert.equal(result, true); assert.deepEqual(skipped, []); assert.deepEqual(calls, [["target", "alias", "file"]]);
  }
});

test("only known Windows file-link capability failures skip the individual fixture", async () => {
  for (const code of ["EPERM", "EACCES", "ENOTSUP"]) {
    const skipped: string[] = [];
    assert.equal(await symlinkFileOrSkip({skip: reason => { skipped.push(String(reason)); }}, "target", "alias", {
      platform: "win32", createLink: async () => { throw Object.assign(new Error("fixture denied"), {code}); },
    }), false);
    assert.equal(skipped.length, 1); assert.ok(skipped[0]?.includes(code));
  }
});

test("unexpected Windows failures and POSIX permission failures cannot be marked as passing or skipped", async () => {
  for (const [platform, code] of [["win32", "ENOENT"], ["win32", "EIO"], ["darwin", "EPERM"], ["linux", "EACCES"]] as const) {
    const error = Object.assign(new Error("unexpected fixture failure"), {code}), skipped: string[] = [];
    await assert.rejects(symlinkFileOrSkip({skip: reason => { skipped.push(String(reason)); }}, "target", "alias", {
      platform, createLink: async () => { throw error; },
    }), error);
    assert.deepEqual(skipped, []);
  }
});
