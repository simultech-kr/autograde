import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { chmod, lstat, mkdir, mkdtemp, readFile, realpath, rename, rm, symlink, writeFile } from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import test from "node:test";
import { SharedLearningSession, SHARED_AUTH_USED, SHARED_SESSION_MARKER } from "../sharedLearningSession";

async function fixture() {
  const parent = await mkdtemp(path.join(await realpath(os.tmpdir()), "ag-shared-extension-"));
  const id = randomBytes(12).toString("hex"), root = path.join(parent, `e-${id}`);
  await mkdir(root, {mode: 0o700});
  const workspace = path.join(root, "workspace"), materials = path.join(root, "materials");
  await mkdir(workspace, {mode: 0o700}); await mkdir(materials, {mode: 0o700});
  const stat = await lstat(root);
  const marker = {schema: 1, sessionId: id, root, launcherPid: process.pid, idePid: process.pid,
    rootDev: stat.dev, rootIno: stat.ino, phase: "running", policy: "restricted", createdAt: new Date().toISOString()};
  const environment = {AUTOGRADE_LEARNING_LIFETIME: "shared", AUTOGRADE_SESSION_ROOT: root, AUTOGRADE_SESSION_ID: id,
    AUTOGRADE_LEARNER_WORKSPACE: workspace, AUTOGRADE_LEARNER_MATERIALS: materials};
  await writeFile(path.join(root, SHARED_SESSION_MARKER), JSON.stringify(marker), {mode: 0o600});
  return {parent, root, id, workspace, materials, marker, environment,
    read: () => SharedLearningSession.fromEnvironment("Autograde IDE", environment),
    dispose: () => rm(parent, {recursive: true, force: true})};
}

test("shared lifecycle hints do not apply to ordinary VS Code or persistent launch", async () => {
  assert.equal(await SharedLearningSession.fromEnvironment("Visual Studio Code", {AUTOGRADE_LEARNING_LIFETIME: "shared"}), undefined);
  assert.equal(await SharedLearningSession.fromEnvironment("Autograde IDE", {}), undefined);
  assert.equal(await SharedLearningSession.fromEnvironment("Autograde IDE", {AUTOGRADE_LEARNING_LIFETIME: "persistent"}), undefined);
  await assert.rejects(SharedLearningSession.fromEnvironment("Autograde IDE", {AUTOGRADE_LEARNING_LIFETIME: "shared"}), /공용 실습/);
});

test("first authentication is remembered without tokens, subsequent login and host reload are blocked", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!;
    assert.equal(session.authenticationUsed, false); session.assertCanAuthenticate();
    await session.markAuthenticated();
    assert.equal(session.authenticationUsed, true);
    assert.deepEqual(JSON.parse(await readFile(path.join(f.root, SHARED_AUTH_USED), "utf8")), {schema: 1, sessionId: f.id, authenticated: true});
    assert.throws(() => session.assertCanAuthenticate(), /새 IDE 세션/);
    await assert.rejects(session.markAuthenticated(), /새 IDE 세션/);
    const reloaded = (await f.read())!;
    assert.equal(reloaded.authenticationUsed, true);
    assert.throws(() => reloaded.assertCanAuthenticate(), /새 IDE 세션/);
  } finally { await f.dispose(); }
});

test("only current managed workspace descendants are allowed; materials and exports are not work roots", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!, nested = path.join(f.workspace, "assignment");
    await mkdir(nested); await session.assertWorkspacePath(f.workspace); await session.assertWorkspacePath(nested);
    for (const outside of [f.parent, f.root, f.materials]) await assert.rejects(session.assertWorkspacePath(outside), /이번 세션/);
    const external = path.join(f.parent, "external"); await mkdir(external); await writeFile(path.join(external, "keep.cpp"), "keep");
    await symlink(external, path.join(f.workspace, "linked"), "dir");
    await assert.rejects(session.assertWorkspacePath(path.join(f.workspace, "linked")), /공용 실습/);
    assert.equal(await readFile(path.join(external, "keep.cpp"), "utf8"), "keep");
  } finally { await f.dispose(); }
});

test("forged root name, different session, dead supervisor and changed inode are rejected", async () => {
  const f = await fixture();
  try {
    for (const replacement of [{sessionId: "f".repeat(24)}, {root: f.parent}, {rootIno: f.marker.rootIno + 1},
      {phase: "prepared"}, {launcherPid: 2147483647}]) {
      await writeFile(path.join(f.root, SHARED_SESSION_MARKER), JSON.stringify({...f.marker, ...replacement}), {mode: 0o600});
      await assert.rejects(f.read(), /공용 실습/);
    }
    await assert.rejects(SharedLearningSession.fromEnvironment("Autograde IDE", {...f.environment, AUTOGRADE_SESSION_ROOT: f.parent}), /공용 실습/);
    await assert.rejects(SharedLearningSession.fromEnvironment("Autograde IDE", {...f.environment, AUTOGRADE_LEARNER_WORKSPACE: f.parent}), /공용 실습/);
  } finally { await f.dispose(); }
});

test("symlinked or corrupt authentication-used marker is never treated as an unused session", async () => {
  const f = await fixture();
  try {
    const file = path.join(f.root, SHARED_AUTH_USED), external = path.join(f.parent, "external.json");
    await writeFile(external, JSON.stringify({schema: 1, sessionId: f.id, authenticated: true}), {mode: 0o600});
    await symlink(external, file); await assert.rejects(f.read()); await rm(file);
    await writeFile(file, JSON.stringify({schema: 1, sessionId: "other", authenticated: true}), {mode: 0o600});
    await assert.rejects(f.read(), /공용 실습/);
    assert.equal(JSON.parse(await readFile(external, "utf8")).authenticated, true);
  } finally { await f.dispose(); }
});

test("root replacement after activation cannot receive authentication marker or workspace writes", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!;
    await rename(f.root, `${f.root}-old`); await mkdir(f.root, {mode: 0o700}); await mkdir(f.workspace, {mode: 0o700});
    await assert.rejects(session.markAuthenticated(), /공용 실습/);
    await assert.rejects(session.assertWorkspacePath(f.workspace), /공용 실습/);
    await assert.rejects(readFile(path.join(f.root, SHARED_AUTH_USED)), {code: "ENOENT"});
  } finally { await f.dispose(); }
});

test("nonprivate session or marker permissions fail closed", {skip: process.platform === "win32"}, async () => {
  const f = await fixture();
  try {
    await chmod(f.root, 0o755); await assert.rejects(f.read(), /공용 실습/); await chmod(f.root, 0o700);
    await chmod(path.join(f.root, SHARED_SESSION_MARKER), 0o644); await assert.rejects(f.read(), /공용 실습/);
  } finally { await f.dispose(); }
});
