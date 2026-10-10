import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rename, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import test from "node:test";

import { courseDirectory, freshAssignmentPath, preferredSubmissionRoot, requireAuthorizedActiveAssignment, validateExistingAssignment } from "../assignmentWorkspace";
import { createSubmissionBundle, writeWorkspaceMarker } from "../bundle";
import { discoverAuthenticatedBundleRoots } from "../bundleWorkspace";

const server = "https://grade.example.edu";
test("explicit copy B remains submission target when its preview fails and copy A stays visible", () => {
  const a = { rootPath: "/course/a", containsActiveDocument: true };
  const b = { rootPath: "/course/a-copy", containsActiveDocument: false };
  assert.equal(preferredSubmissionRoot([a, b], b.rootPath), b);
  assert.equal(preferredSubmissionRoot([a, b]), a);
  assert.equal(preferredSubmissionRoot([{ ...a, containsActiveDocument: false }, b]), undefined);
});
async function assignment(parent: string, name: string, id: string): Promise<string> {
  const root = path.join(parent, name);
  await mkdir(root, { recursive: true });
  await writeWorkspaceMarker(root, { schemaVersion: 1, serviceBaseUrl: server, assignmentId: id });
  await writeFile(path.join(root, "main.cpp"), `// edited solution ${id}\n`);
  return root;
}
async function fixture(run: (root: string) => Promise<void>): Promise<void> {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-lifecycle-"));
  try { await run(root); } finally { await rm(root, { recursive: true, force: true }); }
}

test("restart and renamed/moved assignment resumes from marker without changing edited code", () => fixture(async root => {
  const old = await assignment(root, "old name", "a");
  const moved = path.join(root, "다시 시작할 과제");
  await rename(old, moved);
  assert.equal((await validateExistingAssignment(moved, server, "a")).rootPath, moved);
  assert.equal(await readFile(path.join(moved, "main.cpp"), "utf8"), "// edited solution a\n");
  const discovered = await discoverAuthenticatedBundleRoots([root], undefined, server, new Set(["a"]));
  assert.deepEqual(discovered.map(item => item.rootPath), [moved]);
}));

test("downloading while an assignment or its source subfolder is open uses a sibling directory", () => fixture(async root => {
  const current = await assignment(root, "lab-a", "a");
  await mkdir(path.join(current, "src"));
  assert.equal(await courseDirectory(current), root);
  assert.equal(await courseDirectory(path.join(current, "src")), root);
  assert.equal(await freshAssignmentPath(await courseDirectory(current), "lab-b"), path.join(root, "lab-b"));
  await assert.rejects(freshAssignmentPath(current, "lab-b"), /과제 폴더 안/);
}));

test("repeat download chooses unused suffix and preserves existing, damaged and linked targets", () => fixture(async root => {
  const original = await assignment(root, "lab", "a");
  await mkdir(path.join(root, "lab-2"));
  await writeFile(path.join(root, "lab-2", "notes.txt"), "keep partial download");
  assert.equal(await freshAssignmentPath(root, "lab"), path.join(root, "lab-3"));
  assert.equal(await readFile(path.join(original, "main.cpp"), "utf8"), "// edited solution a\n");
  assert.equal(await readFile(path.join(root, "lab-2", "notes.txt"), "utf8"), "keep partial download");
  await assert.rejects(freshAssignmentPath(root, "../outside"), /이름/);
}));

test("markers cannot authorize another assignment or another server and absent/corrupt markers stay invalid", () => fixture(async root => {
  const local = await assignment(root, "same title", "a");
  await assert.rejects(validateExistingAssignment(local, server, "b"), /현재 연결된 과제가 다릅니다/);
  await assert.rejects(validateExistingAssignment(local, "https://other.example.edu", "a"), /다른 Autograde 서버/);
  await assert.rejects(validateExistingAssignment(root, server, "a"), /없거나 손상/);
  await writeFile(path.join(local, ".autograde", "assignment.json"), "{");
  await assert.rejects(validateExistingAssignment(local, server, "a"), /없거나 손상/);
  assert.equal(await readFile(path.join(local, "main.cpp"), "utf8"), "// edited solution a\n");
}));

test("editing A while connected to B cannot silently select and submit B", () => fixture(async root => {
  const a = await assignment(root, "lab-a", "a");
  await assignment(root, "lab-b", "b");
  await assert.rejects(requireAuthorizedActiveAssignment(path.join(a, "main.cpp"), server, new Set(["b"])), /지금 연결된 과제가 아닙니다/);
  await requireAuthorizedActiveAssignment(path.join(a, "main.cpp"), server, new Set(["a"]));
  await assert.rejects(requireAuthorizedActiveAssignment(path.join(a, "main.cpp"), "https://other.example.edu", new Set(["a"])), /지금 연결된 과제가 아닙니다/);
}));

test("two copies remain separately selectable and same-title different IDs do not get substituted", () => fixture(async root => {
  const first = await assignment(root, "lab", "a");
  const second = await assignment(root, "lab-2", "a");
  await assignment(root, "lab-3", "b");
  const matches = await discoverAuthenticatedBundleRoots([root], undefined, server, new Set(["a"]));
  assert.deepEqual(new Set(matches.map(item => item.rootPath)), new Set([first, second]));
}));

test("preexisting nested assignments cannot be packaged into a different assignment", () => fixture(async root => {
  const outer = await assignment(root, "lab-a", "a");
  const nested = await assignment(outer, "lab-b", "b");
  await assert.rejects(createSubmissionBundle(outer), /다른 과제 폴더/);
  await writeFile(path.join(nested, ".autograde", "assignment.json"), "{");
  await assert.rejects(createSubmissionBundle(outer), /다른 과제 폴더/);
  await assert.rejects(requireAuthorizedActiveAssignment(path.join(nested, "main.cpp"), server, new Set(["a"])), /손상/);
  await assert.rejects(freshAssignmentPath(nested, "another"), /과제 폴더 안/);
  assert.equal(await courseDirectory(nested), root);
  assert.equal(await courseDirectory(outer), root);
}));

test("direct symlink roots cannot be resumed and aliases cannot create nested assignments", async context => fixture(async root => {
  const local = await assignment(root, "real", "a");
  const linked = path.join(root, "link");
  try { await symlink(local, linked, "dir"); } catch (error) {
    if (["EPERM", "EACCES", "ENOTSUP"].includes((error as NodeJS.ErrnoException).code ?? "")) { context.skip("symlinks unavailable"); return; }
    throw error;
  }
  await assert.rejects(validateExistingAssignment(linked, server, "a"), /없거나 손상/);
  await assert.rejects(freshAssignmentPath(linked, "b"), /과제 폴더 안/);
}));
