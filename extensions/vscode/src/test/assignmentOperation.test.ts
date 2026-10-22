import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import * as path from "node:path";
import test from "node:test";
import { AssignmentOperationGate, confirmWorkspaceSwitch } from "../assignmentOperation";

test("a claim input cannot be replaced by another claim, folder, download or history picker", async () => {
  const gate = new AssignmentOperationGate();
  let finish!: () => void;
  const pending = new Promise<void>(resolve => { finish = resolve; });
  const events: string[] = [];
  const active = gate.run(async () => { events.push("claim"); await pending; }, () => assert.fail("not busy yet"));
  for (const operation of ["claim", "open", "download", "history"]) {
    await gate.run(async () => { events.push(operation); }, () => events.push(`busy:${operation}`));
  }
  assert.deepEqual(events, ["claim", "busy:claim", "busy:open", "busy:download", "busy:history"]);
  finish(); await active;
  assert.equal(events.length, 5, "refused operations are not secretly queued for later");
  await gate.run(async () => { events.push("retry"); }, () => assert.fail("gate must be released"));
  assert.equal(events.at(-1), "retry");
});

test("cancel, async failure and synchronous failure always release the gate", async () => {
  for (const failing of [
    async () => undefined,
    async () => { throw new Error("cancelled"); },
    () => { throw new Error("failed before first await"); },
  ]) {
    const gate = new AssignmentOperationGate();
    await gate.run(failing, () => assert.fail("must run")).catch(() => undefined);
    let retried = false;
    await gate.run(async () => { retried = true; }, () => assert.fail("must be released"));
    assert.equal(retried, true);
  }
});

test("all assignment input entry points use the same command gate without a nested reconnect deadlock", async () => {
  const source = await readFile(path.resolve(__dirname, "../../src/extension.ts"), "utf8");
  for (const command of ["redeemAssignmentClaim", "cloneAssignment", "openAssignment", "acceptedAssignmentHistory", "submissionHistory", "viewAssignmentDocument", "openCourseWorkspace"]) {
    const registration = source.slice(source.indexOf(`registerCommand("autograde.${command}"`) >= 0
      ? source.indexOf(`registerCommand("autograde.${command}"`)
      : source.indexOf(`"autograde.${command}",`));
    assert.match(registration.slice(0, 230), /runAssignmentCommand\(/, command);
  }
  assert.match(source, /async \(\) => \{ await redeemAssignmentClaim\(\); \}/);
  assert.match(source, /await confirmWorkspaceSwitch\(\{/);
});

test("workspace confirmation cannot open the previous folder after sign-out or server change", async () => {
  let current = true;
  let confirm!: (value: boolean) => void;
  const waiting = new Promise<boolean>(resolve => { confirm = resolve; });
  let displayed!: () => void;
  const started = new Promise<void>(resolve => { displayed = resolve; });
  let opened = false;
  const operation = confirmWorkspaceSwitch({
    isCurrent: () => current, validate: async () => undefined,
    confirm: async () => { displayed(); return waiting; },
    open: async () => { opened = true; },
  });
  await started;
  current = false;
  confirm(true);
  await operation;
  assert.equal(opened, false);
});

test("workspace confirmation rechecks a moved or deleted folder before switching", async () => {
  let validations = 0;
  let opened = false;
  await assert.rejects(confirmWorkspaceSwitch({
    isCurrent: () => true,
    validate: async () => { if (++validations === 2) throw new Error("folder moved"); },
    confirm: async () => true,
    open: async () => { opened = true; },
  }), /folder moved/);
  assert.equal(validations, 2);
  assert.equal(opened, false);
});

test("workspace cancellation and changes during either validation cannot trigger a switch", async () => {
  for (const invalidation of [0, 1, 2]) {
    let current = invalidation !== 0;
    let validations = 0;
    await confirmWorkspaceSwitch({
      isCurrent: () => current,
      validate: async () => { if (++validations === invalidation) current = false; },
      confirm: async () => true,
      open: async () => assert.fail("old login must not open a folder"),
    });
  }
  await confirmWorkspaceSwitch({ isCurrent: () => true, validate: async () => undefined,
    confirm: async () => false, open: async () => assert.fail("cancelled") });
});

test("valid confirmed workspace switches once after both validations", async () => {
  const events: string[] = [];
  await confirmWorkspaceSwitch({ isCurrent: () => true,
    validate: async () => { events.push("validate"); },
    confirm: async () => { events.push("confirm"); return true; },
    open: async () => { events.push("open"); },
  });
  assert.deepEqual(events, ["validate", "confirm", "validate", "open"]);
});
