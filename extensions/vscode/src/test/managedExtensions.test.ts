import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import test, { type TestContext } from "node:test";
import type { ClassroomCommandContext } from "../classroomCommands";
import {
  ManagedExtensions, ExtensionInstallError, MAX_EXTENSION_BYTES,
  parseExtensionCatalog, chooseExtensionVariants, extensionTarget, verifyExtensionBytes,
  type ExtensionRelease, type ExtensionReport, type ExtensionDistributionClient, type ExtensionInstaller,
} from "../managedExtensions";

const bytes = Buffer.from("A deterministic, authenticated VSIX byte fixture");
const release = (change: Partial<ExtensionRelease> = {}): ExtensionRelease => ({
  release_id: `er_${"a".repeat(32)}`, extension_id: "teacher.cpp-lab", version: "1.2.0",
  sha256: createHash("sha256").update(bytes).digest("hex"), size_bytes: bytes.length,
  target_platform: "universal", vscode_engine: "^1.92.0", filename: "cpp-lab-1.2.0.vsix", ...change,
});
const context = (): ClassroomCommandContext => ({
  baseUrl: "https://school.invalid:20000", generation: 1, assignmentId: "a1",
  clientSessionId: "test-client-0001", workspaceKey: "/tmp/course/workspace",
});

function fixture(t: TestContext) {
  const state = {
    scope: context() as ClassroomCommandContext | undefined,
    releases: [release()] as readonly ExtensionRelease[],
    catalogCalls: 0, confirmations: 0, downloads: 0, installs: 0,
    reports: [] as { release: string; report: ExtensionReport; scope: ClassroomCommandContext }[],
    notifications: [] as string[], allowed: true, applyOnInstall: true, builtin: false,
    restriction: undefined as string | undefined, installed: undefined as string | undefined,
    payload: bytes as Uint8Array, failReportOnce: undefined as ExtensionReport["status"] | undefined,
    catalogError: undefined as Error | undefined,
    beforeCatalog: async () => {}, beforeConfirm: async () => {}, beforeDownload: async () => {},
    beforeInstall: async () => {},
  };
  const api: ExtensionDistributionClient = {
    async catalog() {state.catalogCalls++; await state.beforeCatalog(); if (state.catalogError) throw state.catalogError; return state.releases;},
    async download() {state.downloads++; await state.beforeDownload(); return state.payload;},
    async report(item, report, scope) {
      state.reports.push({release: item.release_id, report: {...report}, scope: {...scope}});
      if (state.failReportOnce === report.status) {
        state.failReportOnce = undefined;
        throw Object.assign(new Error("temporary report failure"), {status: 503});
      }
    },
  };
  const installer: ExtensionInstaller = {
    target: "linux-x64", editorVersion: "1.143.0",
    restriction: () => state.restriction, installedVersion: () => state.installed,
    isBuiltin: () => state.builtin,
    async confirm() {state.confirmations++; await state.beforeConfirm(); return state.allowed;},
    async install(item, data, guard) {
      guard(); assert.deepEqual(data, bytes); state.installs++;
      await state.beforeInstall(); guard();
      if (state.applyOnInstall) state.installed = item.version;
    },
    notify: message => state.notifications.push(message),
  };
  const runner = new ManagedExtensions(api, () => state.scope, installer, 1_000_000);
  t.after(() => runner.dispose());
  return {state, runner, installer};
}

test("catalog accepts bounded immutable release metadata, with no install URL", () => {
  assert.deepEqual(parseExtensionCatalog({releases: [release()]}), [release()]);
  assert.deepEqual(parseExtensionCatalog({releases: []}), []);
});

test("catalog rejects traversal filenames, duplicate IDs/variants, malformed hashes and unbounded sizes", () => {
  for (const change of [
    {filename: "../a.vsix"}, {filename: "C:\\temp\\a.vsix"}, {filename: "a\n.vsix"},
    {release_id: "er_invalid"}, {extension_id: "publisher/name"}, {version: "latest"},
    {sha256: "0".repeat(63)}, {size_bytes: MAX_EXTENSION_BYTES + 1}, {size_bytes: 0},
    {size_bytes: 1.5}, {target_platform: "wsl-x64"}, {vscode_engine: ""},
  ]) {
    assert.throws(() => parseExtensionCatalog({releases: [release(change)]}), ExtensionInstallError);
  }
  assert.throws(() => parseExtensionCatalog({releases: [release(), release()]}), ExtensionInstallError);
  assert.throws(() => parseExtensionCatalog({releases: [release(), release({release_id: `er_${"b".repeat(32)}`})]}), ExtensionInstallError);
  assert.throws(() => parseExtensionCatalog({releases: Array(101).fill(release())}), ExtensionInstallError);
  for (const value of [null, [], {releases: {}}, {releases: [null]}]) assert.throws(() => parseExtensionCatalog(value), ExtensionInstallError);
});

test("variant selection uses the Linux WSLg process target, preferring exact over universal", () => {
  assert.equal(extensionTarget("linux", "x64"), "linux-x64");
  assert.equal(extensionTarget("linux", "arm"), "linux-armhf");
  assert.equal(extensionTarget("darwin", "arm64"), "darwin-arm64");
  const universal = release(), linux = release({release_id: `er_${"b".repeat(32)}`, target_platform: "linux-x64"});
  const windows = release({release_id: `er_${"c".repeat(32)}`, target_platform: "win32-x64"});
  for (const entries of [[universal, windows, linux], [linux, universal, windows]]) {
    assert.deepEqual(chooseExtensionVariants(entries, extensionTarget("linux", "x64")), [linux]);
  }
  assert.deepEqual(chooseExtensionVariants([windows, universal], "darwin-arm64"), [universal]);
});

test("download verification checks the actual length and SHA-256 before installation", () => {
  verifyExtensionBytes(release(), bytes);
  const corrupt = Buffer.from(bytes); corrupt[0] = corrupt[0]! ^ 1;
  assert.throws(() => verifyExtensionBytes(release(), corrupt), {code: "hash_mismatch"});
  assert.throws(() => verifyExtensionBytes(release(), bytes.subarray(1)), {code: "hash_mismatch"});
});

test("installation requires approval, verifies bytes/publication, then reports the running version", async t => {
  const {state, runner} = fixture(t);
  await runner.check();
  assert.equal(state.confirmations, 1); assert.equal(state.downloads, 1); assert.equal(state.installs, 1);
  assert.equal(state.catalogCalls, 2);
  assert.deepEqual(state.reports.map(item => item.report.status), ["offered", "installing", "installed"]);
  assert.equal(state.reports.at(-1)?.report.installed_version, "1.2.0");
  await runner.check();
  assert.equal(state.installs, 1); assert.equal(state.reports.length, 3);
});

test("an old running version remains reload-required until the runtime exposes the desired version", async t => {
  const {state, runner} = fixture(t); state.installed = "1.1.0"; state.applyOnInstall = false;
  await runner.check();
  assert.equal(state.reports.at(-1)?.report.status, "reload_required");
  assert.equal(state.reports.at(-1)?.report.installed_version, "1.1.0");
  await runner.check(true);
  assert.equal(state.installs, 1); assert.equal(state.confirmations, 1);
  state.installed = "1.2.0";
  await runner.check();
  assert.equal(state.reports.at(-1)?.report.status, "installed");
  assert.equal(state.installs, 1);
});

test("a missing IDE install bridge reports failure and requests the latest IDE app", async t => {
  const {state, runner} = fixture(t);
  state.beforeInstall = async () => {throw new ExtensionInstallError("unsupported_client");};
  await runner.check();
  assert.deepEqual(state.reports.map(item => item.report.status), ["offered", "installing", "failed"]);
  assert.deepEqual(state.reports.at(-1)?.report, {status: "failed", result_code: "unsupported_client"});
  assert.equal(state.installed, undefined);
  assert.match(state.notifications.join(" "), /최신.*Autograde IDE 앱으로 교체/);
  await runner.check();
  assert.equal(state.installs, 1); assert.equal(state.confirmations, 1);
});

test("decline avoids download/install and periodic checks do not repeatedly prompt", async t => {
  const {state, runner} = fixture(t); state.allowed = false;
  await runner.check(); await runner.check();
  assert.deepEqual(state.reports.map(item => item.report.status), ["offered", "declined"]);
  assert.equal(state.downloads, 0); assert.equal(state.installs, 0); assert.equal(state.confirmations, 1);
  state.allowed = true; await runner.check(true);
  assert.equal(state.installs, 1);
});

test("already-current version reports once without downloading or prompting", async t => {
  const {state, runner} = fixture(t); state.installed = "1.2.0";
  await runner.check(); await runner.check();
  assert.equal(state.confirmations, 0); assert.equal(state.installs, 0);
  assert.deepEqual(state.reports.map(item => item.report.status), ["installed"]);
});

for (const problem of ["restricted_policy", "untrusted_workspace", "unsupported_client", "built_in_extension", "incompatible_platform", "incompatible_engine", "newer_version_installed"] as const) {
  test(`blocks ${problem} before asking to install`, async t => {
    const {state, runner} = fixture(t);
    if (["restricted_policy", "untrusted_workspace", "unsupported_client"].includes(problem)) state.restriction = problem;
    if (problem === "built_in_extension") state.builtin = true;
    if (problem === "incompatible_platform") state.releases = [release({target_platform: "win32-x64"})];
    if (problem === "incompatible_engine") state.releases = [release({vscode_engine: ">=9.0.0"})];
    if (problem === "newer_version_installed") state.installed = "2.0.0";
    await runner.check();
    assert.equal(state.installs, 0); assert.equal(state.confirmations, 0); assert.equal(state.downloads, 0);
    assert.deepEqual(state.reports.map(item => item.report), [{status: "failed", result_code: problem}]);
  });
}

for (const boundary of ["logout", "generation", "origin", "assignment", "client"] as const) {
  for (const phase of ["confirmation", "download"] as const) {
    test(`${boundary} changed during ${phase} never installs or reports under the next identity`, async t => {
      const {state, runner} = fixture(t);
      const change = async () => {
        const old = state.scope!;
        state.scope = boundary === "logout" ? undefined : {...old,
          ...(boundary === "generation" ? {generation: old.generation + 1} :
            boundary === "origin" ? {baseUrl: "https://different.invalid"} :
              boundary === "assignment" ? {assignmentId: "a2"} : {clientSessionId: "different-client"})};
      };
      if (phase === "confirmation") state.beforeConfirm = change; else state.beforeDownload = change;
      await runner.check();
      assert.equal(state.installs, 0);
      assert.deepEqual(state.reports.map(item => item.report.status), phase === "confirmation" ? ["offered"] : ["offered", "installing"]);
      assert.ok(state.reports.every(item => item.scope.generation === 1 && item.scope.assignmentId === "a1"
        && item.scope.baseUrl === "https://school.invalid:20000" && item.scope.clientSessionId === "test-client-0001"));
    });
  }
}

for (const change of ["withdrawn", "replaced", "hash"] as const) {
  test(`release ${change} while downloading prevents stale installation`, async t => {
    const {state, runner} = fixture(t);
    state.beforeDownload = async () => {state.releases = change === "withdrawn" ? [] : [release(change === "replaced"
      ? {release_id: `er_${"b".repeat(32)}`, version: "1.3.0"} : {sha256: "0".repeat(64)})];};
    await runner.check();
    assert.equal(state.downloads, 1); assert.equal(state.installs, 0);
    assert.deepEqual(state.reports.at(-1)?.report, {status: "failed", result_code: "scope_changed"});
  });
}

test("corrupt download is reported and never reaches installer", async t => {
  const {state, runner} = fixture(t); state.payload = Buffer.from("tampered");
  await runner.check();
  assert.equal(state.installs, 0);
  assert.deepEqual(state.reports.at(-1)?.report, {status: "failed", result_code: "hash_mismatch"});
});

test("trust/policy is rechecked after approval and after downloading", async t => {
  for (const phase of ["confirmation", "download"] as const) {
    const {state, runner} = fixture(t);
    const restrict = async () => {state.restriction = "untrusted_workspace";};
    if (phase === "confirmation") state.beforeConfirm = restrict; else state.beforeDownload = restrict;
    await runner.check();
    assert.equal(state.installs, 0);
    assert.deepEqual(state.reports.at(-1)?.report, {status: "failed", result_code: "untrusted_workspace"});
    runner.dispose();
  }
});

test("a transient final acknowledgement failure retries the report without reinstalling", async t => {
  const {state, runner} = fixture(t); state.failReportOnce = "installed";
  await runner.check();
  assert.equal(state.installs, 1);
  await runner.check();
  assert.equal(state.installs, 1); assert.equal(state.confirmations, 1); assert.equal(state.downloads, 1);
  assert.equal(state.reports.filter(item => item.report.status === "installed").length, 2);
});

test("overlapping polling has one flight and disposal cancels pending work", async t => {
  const {state, runner} = fixture(t);
  let continueCatalog: (() => void) | undefined;
  state.beforeCatalog = () => new Promise<void>(resolve => {continueCatalog = resolve;});
  const flight = runner.check();
  await Promise.resolve();
  await runner.check();
  assert.equal(state.catalogCalls, 1);
  runner.dispose(); continueCatalog!(); await flight;
  assert.equal(state.confirmations, 0); assert.equal(state.installs, 0); assert.equal(state.reports.length, 0);
  await runner.check(); assert.equal(state.catalogCalls, 1);
});

test("unsupported server stops automatic polling while manual retry remains available", async t => {
  const {state, runner} = fixture(t);
  state.catalogError = Object.assign(new Error("old server"), {status: 404});
  await runner.check(); await runner.check();
  assert.equal(state.catalogCalls, 1);
  state.catalogError = undefined; state.releases = [];
  await runner.check(true); assert.equal(state.catalogCalls, 2);
});

test("logged-out client does not request catalog or install files", async t => {
  const {state, runner} = fixture(t); state.scope = undefined;
  await runner.check(true);
  assert.equal(state.catalogCalls, 0); assert.equal(state.installs, 0);
  assert.match(state.notifications.join(" "), /과제를 연결/);
});
