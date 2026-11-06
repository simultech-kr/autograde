import assert from "node:assert/strict";
import { randomBytes } from "node:crypto";
import { chmod, link, lstat, mkdir, mkdtemp, readFile, realpath, rename, rm, writeFile } from "node:fs/promises";
import * as os from "node:os";
import * as path from "node:path";
import test from "node:test";
import { SharedLearningSession, SHARED_AUTH_USED, SHARED_SESSION_MARKER } from "../sharedLearningSession";
import { symlinkDirectory, symlinkFileOrSkip } from "./filesystemFixtures";

async function fixture(legacy = false) {
  const parent = await mkdtemp(path.join(await realpath(os.tmpdir()), "ag-shared-extension-"));
  const id = randomBytes(12).toString("hex"), root = path.join(parent, legacy ? `e-${id}` : "current");
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

test("cleanup policy is normalized from matching launcher marker/environment; legacy absence stays on_exit", async () => {
  const f = await fixture();
  try {
    assert.equal((await f.read())!.cleanupMode, "on_exit");
    for (const cleanupMode of ["on_exit", "next_start"] as const) {
      await writeFile(path.join(f.root, SHARED_SESSION_MARKER), JSON.stringify({...f.marker, cleanupMode}), {mode: 0o600});
      const session = (await SharedLearningSession.fromEnvironment("Autograde IDE", {...f.environment, AUTOGRADE_SESSION_CLEANUP_MODE: cleanupMode}))!;
      assert.equal(session.cleanupMode, cleanupMode);
      await session.assertWorkspacePath(f.workspace);
    }
  } finally { await f.dispose(); }
});

test("unknown, inconsistent and changed cleanup policies fail closed", async () => {
  const f = await fixture();
  try {
    for (const [markerMode, environmentMode] of [["next_start", "on_exit"], ["on_exit", "next_start"],
      ["next_start", undefined], [undefined, "next_start"], ["unknown", "unknown"], ["", ""]]) {
      await writeFile(path.join(f.root, SHARED_SESSION_MARKER), JSON.stringify({...f.marker, cleanupMode: markerMode}), {mode: 0o600});
      await assert.rejects(SharedLearningSession.fromEnvironment("Autograde IDE", {...f.environment, AUTOGRADE_SESSION_CLEANUP_MODE: environmentMode}), /공용 실습/);
    }
    await writeFile(path.join(f.root, SHARED_SESSION_MARKER), JSON.stringify(f.marker), {mode: 0o600});
    const session = (await f.read())!;
    await writeFile(path.join(f.root, SHARED_SESSION_MARKER), JSON.stringify({...f.marker, cleanupMode: "next_start"}), {mode: 0o600});
    await assert.rejects(session.assertWorkspacePath(f.workspace), /공용 실습/);
    await assert.rejects(session.markAuthenticated("20211001"), /공용 실습/);
  } finally { await f.dispose(); }
});

test("first authentication is remembered without tokens, subsequent login and host reload are blocked", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!;
    assert.equal(session.authenticationUsed, false); session.assertCanAuthenticate();
    await session.markAuthenticated("20211001");
    assert.equal(session.authenticationUsed, true);
    const marker = JSON.parse(await readFile(path.join(f.root, SHARED_AUTH_USED), "utf8"));
    assert.equal(marker.schema, 2); assert.equal(marker.sessionId, f.id); assert.equal(marker.authenticated, true);
    assert.equal(marker.studentKey, "20211001");
    assert.equal(session.initialWorkspace, f.workspace);
    assert.equal(session.workspace, path.join(f.workspace, "20211001"));
    assert.equal(session.materials, path.join(f.materials, "20211001"));
    await session.assertWorkspacePath(session.workspace); await session.assertMaterialsDirectory();
    assert.equal((await lstat(session.workspace)).ino, marker.workspaceIdentity.ino);
    assert.throws(() => session.assertCanAuthenticate(), /새 IDE 세션/);
    await assert.rejects(session.markAuthenticated("20211001"), /새 IDE 세션/);
    const reloaded = (await f.read())!;
    assert.equal(reloaded.authenticationUsed, true);
    assert.equal(reloaded.studentKey, "20211001");
    assert.equal(reloaded.workspace, session.workspace); assert.equal(reloaded.materials, session.materials);
    await reloaded.assertWorkspacePath(reloaded.workspace);
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
    await symlinkDirectory(external, path.join(f.workspace, "linked"));
    await assert.rejects(session.assertWorkspacePath(path.join(f.workspace, "linked")), /공용 실습/);
    assert.equal(await readFile(path.join(external, "keep.cpp"), "utf8"), "keep");
  } finally { await f.dispose(); }
});

for (const [name, replace] of [
  ["different session", (f) => ({sessionId: f.id === "f".repeat(24) ? "a".repeat(24) : "f".repeat(24)})],
  ["different root", (f) => ({root: f.parent})],
  // NTFS file IDs may exceed Number.MAX_SAFE_INTEGER, so adding 1 can leave the value unchanged.
  ["changed inode", (f) => ({rootIno: f.marker.rootIno === 0 ? 1 : 0})],
  ["prepared phase", () => ({phase: "prepared"})],
  ["dead supervisor", () => ({launcherPid: 2147483647})],
] satisfies Array<[string, (f: Awaited<ReturnType<typeof fixture>>) => Record<string, unknown>]>) {
  test(`shared session marker rejects ${name}`, async () => {
    const f = await fixture();
    try {
      const replacement = replace(f);
      assert.notDeepEqual({...f.marker, ...replacement}, f.marker, "fixture must change the marker");
      await writeFile(path.join(f.root, SHARED_SESSION_MARKER), JSON.stringify({...f.marker, ...replacement}), {mode: 0o600});
      await assert.rejects(f.read(), /공용 실습/);
    } finally { await f.dispose(); }
  });
}

test("forged root name and unmanaged workspace environment are rejected", async () => {
  const f = await fixture();
  try {
    await assert.rejects(SharedLearningSession.fromEnvironment("Autograde IDE", {...f.environment, AUTOGRADE_SESSION_ROOT: f.parent}), /공용 실습/);
    await assert.rejects(SharedLearningSession.fromEnvironment("Autograde IDE", {...f.environment, AUTOGRADE_LEARNER_WORKSPACE: f.parent}), /공용 실습/);
  } finally { await f.dispose(); }
});

test("symlinked authentication-used marker is never treated as an unused session", async (t) => {
  const f = await fixture();
  try {
    const file = path.join(f.root, SHARED_AUTH_USED), external = path.join(f.parent, "external.json");
    await writeFile(external, JSON.stringify({schema: 1, sessionId: f.id, authenticated: true}), {mode: 0o600});
    if (!await symlinkFileOrSkip(t, external, file)) return;
    await assert.rejects(f.read(), /공용 실습/);
    assert.equal(JSON.parse(await readFile(external, "utf8")).authenticated, true);
  } finally { await f.dispose(); }
});

test("dangling authentication-used marker is never treated as an unused session", async (t) => {
  const f = await fixture();
  try {
    if (!await symlinkFileOrSkip(t, path.join(f.parent, "missing.json"), path.join(f.root, SHARED_AUTH_USED))) return;
    await assert.rejects(f.read(), /공용 실습/);
  } finally { await f.dispose(); }
});

test("corrupt authentication-used marker is never treated as an unused session", async () => {
  const f = await fixture();
  try {
    const file = path.join(f.root, SHARED_AUTH_USED);
    await writeFile(file, JSON.stringify({schema: 1, sessionId: "other", authenticated: true}), {mode: 0o600});
    await assert.rejects(f.read(), /공용 실습/);
    await writeFile(file, "{", {mode: 0o600});
    await assert.rejects(f.read(), SyntaxError);
  } finally { await f.dispose(); }
});

test("hard-linked authentication-used marker is never treated as an unused session", async () => {
  const f = await fixture();
  try {
    const file = path.join(f.root, SHARED_AUTH_USED), external = path.join(f.parent, "external.json");
    await writeFile(external, JSON.stringify({schema: 1, sessionId: f.id, authenticated: true}), {mode: 0o600});
    await link(external, file);
    await assert.rejects(f.read(), /공용 실습/);
    assert.equal(JSON.parse(await readFile(external, "utf8")).authenticated, true);
  } finally { await f.dispose(); }
});

test("root replacement after activation cannot receive authentication marker or workspace writes", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!;
    await rename(f.root, `${f.root}-old`); await mkdir(f.root, {mode: 0o700}); await mkdir(f.workspace, {mode: 0o700});
    await assert.rejects(session.markAuthenticated("20211001"), /공용 실습/);
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


test("a fixed path cannot reuse an old activated session identity", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!;
    await writeFile(path.join(f.root, SHARED_SESSION_MARKER), JSON.stringify({...f.marker, sessionId: "a".repeat(24)}), {mode: 0o600});
    await assert.rejects(session.assertWorkspacePath(f.workspace), /공용 실습/);
    await assert.rejects(session.markAuthenticated("20211001"), /공용 실습/);
  } finally { await f.dispose(); }
});

test("legacy session roots remain readable during launcher migration", async () => {
  const f = await fixture(true);
  try { await (await f.read())!.assertWorkspacePath(f.workspace); }
  finally { await f.dispose(); }
});


test("server student key scopes work and materials; siblings and the initial parent are no longer work roots", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!;
    assert.throws(() => session.materials, /학생 인증/);
    await session.markAuthenticated("20211001");
    const assignment = path.join(session.workspace, "problem-01"); await mkdir(assignment, {mode: 0o700});
    await session.assertWorkspacePath(assignment);
    for (const candidate of [f.workspace, f.materials, session.materials, path.join(f.workspace, "20211002")]) {
      await assert.rejects(session.assertWorkspacePath(candidate), /이번 세션/);
    }
    await assert.rejects(session.markAuthenticated("20211002"), /새 IDE 세션/);
    if (process.platform !== "win32") {
      assert.equal((await lstat(session.workspace)).mode & 0o777, 0o700);
      assert.equal((await lstat(session.materials)).mode & 0o777, 0o700);
    }
  } finally {await f.dispose();}
});

for (const studentKey of ["", "../outside", "a/b", "a\\b", ".", "..", "C:drive", "student.", " CON", "CON", "lpt1", "CON.txt", "Lpt9.cpp", "com1.data", "x".repeat(129), "student\n"]) {
  test(`nonportable student folder identity is rejected: ${JSON.stringify(studentKey)}`, async () => {
    const f = await fixture();
    try {
      const session = (await f.read())!;
      await assert.rejects(session.markAuthenticated(studentKey), /학번/);
      assert.equal(session.authenticationUsed, false);
      await assert.rejects(readFile(path.join(f.root, SHARED_AUTH_USED)), {code: "ENOENT"});
    } finally {await f.dispose();}
  });
}

for (const base of ["workspace", "materials"] as const) {
  test(`an unexpected existing ${base} student folder is never adopted or overwritten`, async () => {
    const f = await fixture();
    try {
      const existing = path.join(f[base], "20211001"); await mkdir(existing, {mode: 0o700});
      await writeFile(path.join(existing, "keep.txt"), "existing work");
      const session = (await f.read())!;
      await assert.rejects(session.markAuthenticated("20211001"), {code: "EEXIST"});
      assert.equal(await readFile(path.join(existing, "keep.txt"), "utf8"), "existing work");
      assert.equal(session.authenticationUsed, true);
      await assert.rejects(f.read()); // An incomplete binding requires a new launcher session.
    } finally {await f.dispose();}
  });

  test(`a replaced student ${base} directory cannot be reused after authentication or host reload`, async () => {
    const f = await fixture();
    try {
      const session = (await f.read())!; await session.markAuthenticated("20211001");
      const target = session[base]; await rename(target, `${target}-old`); await mkdir(target, {mode: 0o700});
      await assert.rejects(session.assertWorkspacePath(session.workspace), /공용 실습/);
      await assert.rejects(session.assertMaterialsDirectory(), /공용 실습/);
      await assert.rejects(f.read(), /공용 실습/);
    } finally {await f.dispose();}
  });
}

test("student marker mutation and string schemas cannot rebind a session", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!; await session.markAuthenticated("20211001");
    const file = path.join(f.root, SHARED_AUTH_USED), flag = JSON.parse(await readFile(file, "utf8"));
    await writeFile(file, JSON.stringify({...flag, studentKey: "20211002"}), {mode: 0o600});
    await assert.rejects(session.assertWorkspacePath(session.workspace), /공용 실습/);
    await assert.rejects(f.read());
    await writeFile(file, JSON.stringify({...flag, schema: "2"}), {mode: 0o600});
    await assert.rejects(f.read(), /공용 실습/);
  } finally {await f.dispose();}
});

test("stale authentication cannot create or reuse a student binding", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!;
    await assert.rejects(session.markAuthenticated("20211001", () => {throw new Error("stale authentication");}), /stale authentication/);
    assert.equal(session.authenticationUsed, false);
    await assert.rejects(lstat(path.join(f.workspace, "20211001")), {code: "ENOENT"});
  } finally {await f.dispose();}
});


test("a linked materials parent cannot receive student files even if its student leaf is a directory", async () => {
  const f = await fixture();
  try {
    const session = (await f.read())!; await session.markAuthenticated("20211001");
    const old = path.join(f.parent, "moved-materials"); await rename(f.materials, old);
    await symlinkDirectory(old, f.materials);
    assert.equal((await lstat(path.join(f.materials, "20211001"))).isDirectory(), true);
    await assert.rejects(session.assertMaterialsDirectory(), /공용 실습/);
    await assert.rejects(f.read(), /공용 실습/);
  } finally {await f.dispose();}
});


for (const studentKey of ["s.01", "s".repeat(65), "s".repeat(128)]) {
  test(`server-compatible student keys remain unchanged as folder names (${studentKey.length} chars)`, async () => {
    const f = await fixture();
    try {
      const session = (await f.read())!; await session.markAuthenticated(studentKey);
      assert.equal(session.workspace, path.join(f.workspace, studentKey));
      assert.equal(session.materials, path.join(f.materials, studentKey));
      await session.assertWorkspacePath(session.workspace); await session.assertMaterialsDirectory();
      const reloaded = (await f.read())!;
      assert.equal(reloaded.studentKey, studentKey); assert.equal(reloaded.workspace, session.workspace);
    } finally {await f.dispose();}
  });
}
