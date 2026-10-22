import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import test from "node:test";

import { startAssignment, type AssignmentStartActions } from "../assignmentStart";
import { freshAssignmentPath, validateExistingAssignment } from "../assignmentWorkspace";
import { writeWorkspaceMarker } from "../bundle";

test("re-claiming A routes resume to edited existing folder without any starter download", async () => {
  const course = await mkdtemp(path.join(tmpdir(), "autograde-start-"));
  try {
    const existing = path.join(course, "edited-a");
    await mkdir(existing);
    await writeWorkspaceMarker(existing, { schemaVersion: 1, assignmentId: "a", serviceBaseUrl: "https://grade.example.edu" });
    const code = "// my unfinished answer\nint main() { return 7; }\n";
    await writeFile(path.join(existing, "main.cpp"), code);
    let requests = 0;
    let selected: string | undefined;
    const actions: AssignmentStartActions<string> = {
      existing: [existing], chooseAction: async () => "resume", isCurrent: () => true,
      chooseExisting: async () => { throw new Error("single existing folder needs no ambiguous picker"); },
      selectFolder: async () => undefined,
      resume: async root => { await validateExistingAssignment(root, "https://grade.example.edu", "a"); selected = root; },
      download: async () => { requests += 1; },
    };
    await startAssignment(actions);
    assert.equal(requests, 0); assert.equal(selected, existing);
    assert.equal(await readFile(path.join(existing, "main.cpp"), "utf8"), code);
    await startAssignment({ ...actions, chooseAction: async () => "download", download: async () => {
      requests += 1;
      const target = await freshAssignmentPath(course, "edited-a");
      assert.equal(target, path.join(course, "edited-a-2"));
      await mkdir(target); await writeFile(path.join(target, "main.cpp"), "// fresh starter");
    } });
    assert.equal(requests, 1);
    assert.equal(await readFile(path.join(existing, "main.cpp"), "utf8"), code);
  } finally { await rm(course, { recursive: true, force: true }); }
});

test("cancelled mode, copy selector, external folder picker or changed session never fall through to download", async () => {
  const unexpected = async (): Promise<void> => { throw new Error("unexpected side effect"); };
  const base: AssignmentStartActions<string> = {
    existing: ["a", "a-copy"], chooseAction: async () => undefined, isCurrent: () => true,
    chooseExisting: async () => undefined, selectFolder: async () => undefined,
    resume: unexpected, download: unexpected,
  };
  await startAssignment(base);
  await startAssignment({ ...base, chooseAction: async () => "resume" });
  await startAssignment({ ...base, chooseAction: async () => "choose" });
  await startAssignment({ ...base, chooseAction: async () => "download", isCurrent: () => false });
  let active = true;
  await startAssignment({ ...base, chooseAction: async () => "choose", isCurrent: () => active,
    selectFolder: async () => { active = false; return "old-student"; } });
});

test("multiple copies require selection and moved external folder can resume without downloading", async () => {
  const selected: string[] = [];
  const base: AssignmentStartActions<string> = {
    existing: ["a", "a-copy"], chooseAction: async () => "resume", isCurrent: () => true,
    chooseExisting: async candidates => candidates[1], selectFolder: async () => "moved-a",
    resume: async root => { selected.push(root); },
    download: async () => { throw new Error("must not download"); },
  };
  await startAssignment(base);
  await startAssignment({ ...base, existing: [], chooseAction: async () => "choose" });
  assert.deepEqual(selected, ["a-copy", "moved-a"]);
});

test("an already stale session never opens a selector and empty or unknown actions do nothing", async () => {
  const unexpected = async (): Promise<never> => { throw new Error("unexpected selector or mutation"); };
  const actions: AssignmentStartActions<string> = {
    existing: [], chooseAction: unexpected, isCurrent: () => false,
    chooseExisting: unexpected, selectFolder: unexpected, resume: unexpected, download: unexpected,
  };
  await startAssignment(actions);
  await startAssignment({ ...actions, isCurrent: () => true, chooseAction: async () => "resume" });
  await startAssignment({ ...actions, isCurrent: () => true, chooseAction: async () => "unexpected" as "resume" });
});

test("session changes during copy selection and rejected resume never fall back to a fresh download", async () => {
  let current = true;
  let resumed = 0;
  let downloaded = 0;
  const actions: AssignmentStartActions<string> = {
    existing: ["old", "new"], chooseAction: async () => "resume", isCurrent: () => current,
    chooseExisting: async folders => { current = false; return folders[1]; }, selectFolder: async () => undefined,
    resume: async () => { resumed++; throw new Error("folder was moved"); }, download: async () => { downloaded++; },
  };
  await startAssignment(actions);
  assert.equal(resumed, 0);
  current = true;
  await assert.rejects(startAssignment({ ...actions, chooseExisting: async folders => folders[1] }), /folder was moved/);
  assert.equal(resumed, 1);
  assert.equal(downloaded, 0);
});
