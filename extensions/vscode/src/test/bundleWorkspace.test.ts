import assert from "node:assert/strict";
import { mkdtemp, mkdir, readFile, rm, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import test from "node:test";

import { createSubmissionBundle, writeWorkspaceMarker } from "../bundle";
import {
  discoverAuthenticatedBundleRoots,
  findSafeStarterPreview,
} from "../bundleWorkspace";

const SERVICE_URL = "http://127.0.0.1:18080";

test("course workspace resolves and bundles only the authenticated child assignment root", async () => {
  const courseRoot = await temporaryDirectory();
  try {
    const assignmentRoot = path.join(courseRoot, "observer-java");
    await mkdir(path.join(assignmentRoot, "src"), { recursive: true });
    await writeFile(path.join(courseRoot, "private-notes.txt"), "do not submit\n");
    await writeFile(path.join(assignmentRoot, "src", "Answer.java"), "final class Answer {}\n");
    await writeWorkspaceMarker(assignmentRoot, {
      schemaVersion: 1,
      serviceBaseUrl: SERVICE_URL,
      assignmentId: "observer-java",
    });

    const matches = await discoverAuthenticatedBundleRoots(
      [courseRoot],
      path.join(assignmentRoot, "src", "Answer.java"),
      SERVICE_URL,
      new Set(["observer-java"]),
    );

    assert.deepEqual(matches, [{
      rootPath: assignmentRoot,
      assignmentId: "observer-java",
      containsActiveDocument: true,
    }]);
    const bundle = await createSubmissionBundle(matches[0]!.rootPath);
    assert.equal(bundle.fileCount, 1);
    assert.equal(bundle.sourceBytes, Buffer.byteLength("final class Answer {}\n"));
  } finally {
    await rm(courseRoot, { recursive: true, force: true });
  }
});

test("markers never select assignments absent from the authenticated server projection", async () => {
  const courseRoot = await temporaryDirectory();
  try {
    const staleRoot = path.join(courseRoot, "stale-assignment");
    const wrongServerRoot = path.join(courseRoot, "wrong-server");
    await mkdir(staleRoot);
    await mkdir(wrongServerRoot);
    await writeWorkspaceMarker(staleRoot, {
      schemaVersion: 1,
      serviceBaseUrl: SERVICE_URL,
      assignmentId: "stale",
    });
    await writeWorkspaceMarker(wrongServerRoot, {
      schemaVersion: 1,
      serviceBaseUrl: "http://127.0.0.1:19090",
      assignmentId: "current",
    });

    const matches = await discoverAuthenticatedBundleRoots(
      [courseRoot],
      undefined,
      SERVICE_URL,
      new Set(["current"]),
    );

    assert.deepEqual(matches, []);
  } finally {
    await rm(courseRoot, { recursive: true, force: true });
  }
});

test("an active file locates a nested assignment root without recursive workspace scanning", async () => {
  const courseRoot = await temporaryDirectory();
  try {
    const assignmentRoot = path.join(courseRoot, "week-01", "observer-cpp");
    const source = path.join(assignmentRoot, "src", "observer.cpp");
    await mkdir(path.dirname(source), { recursive: true });
    await writeFile(source, "// answer\n");
    await writeWorkspaceMarker(assignmentRoot, {
      schemaVersion: 1,
      serviceBaseUrl: SERVICE_URL,
      assignmentId: "observer-cpp",
    });

    const matches = await discoverAuthenticatedBundleRoots(
      [courseRoot],
      source,
      SERVICE_URL,
      new Set(["observer-cpp"]),
    );

    assert.equal(matches.length, 1);
    assert.equal(matches[0]?.rootPath, assignmentRoot);
    assert.equal(matches[0]?.containsActiveDocument, true);
  } finally {
    await rm(courseRoot, { recursive: true, force: true });
  }
});

test("a workspace opened directly at the assignment root remains discoverable", async () => {
  const assignmentRoot = await temporaryDirectory();
  try {
    await writeWorkspaceMarker(assignmentRoot, {
      schemaVersion: 1,
      serviceBaseUrl: SERVICE_URL,
      assignmentId: "observer-java",
    });
    const matches = await discoverAuthenticatedBundleRoots(
      [assignmentRoot],
      undefined,
      SERVICE_URL,
      new Set(["observer-java"]),
    );
    assert.equal(matches[0]?.rootPath, assignmentRoot);
  } finally {
    await rm(assignmentRoot, { recursive: true, force: true });
  }
});

test("symlinked assignment roots are ignored", async (context) => {
  const courseRoot = await temporaryDirectory();
  const outside = await temporaryDirectory();
  try {
    await writeWorkspaceMarker(outside, {
      schemaVersion: 1,
      serviceBaseUrl: SERVICE_URL,
      assignmentId: "observer-java",
    });
    try {
      await symlink(outside, path.join(courseRoot, "observer-java"), "dir");
    } catch (error) {
      if (["EPERM", "EACCES", "ENOTSUP"].includes((error as NodeJS.ErrnoException).code ?? "")) {
        context.skip("directory symlinks are unavailable on this platform");
        return;
      }
      throw error;
    }
    const matches = await discoverAuthenticatedBundleRoots(
      [courseRoot],
      undefined,
      SERVICE_URL,
      new Set(["observer-java"]),
    );
    assert.deepEqual(matches, []);
  } finally {
    await rm(courseRoot, { recursive: true, force: true });
    await rm(outside, { recursive: true, force: true });
  }
});

test("starter preview rejects a symlinked README", async (context) => {
  const assignmentRoot = await temporaryDirectory();
  const outside = await temporaryDirectory();
  try {
    const readme = path.join(assignmentRoot, "README.md");
    await writeFile(readme, "# Lab\n");
    assert.equal(await findSafeStarterPreview(assignmentRoot), readme);
    await rm(readme);
    await writeFile(path.join(outside, "README.md"), "outside\n");
    try {
      await symlink(path.join(outside, "README.md"), readme, "file");
    } catch (error) {
      if (["EPERM", "EACCES", "ENOTSUP"].includes((error as NodeJS.ErrnoException).code ?? "")) {
        context.skip("file symlinks are unavailable on this platform");
        return;
      }
      throw error;
    }
    assert.equal(await findSafeStarterPreview(assignmentRoot), undefined);
  } finally {
    await rm(assignmentRoot, { recursive: true, force: true });
    await rm(outside, { recursive: true, force: true });
  }
});

test("starter preview prefers C++ then C then README in a Unicode/spaced path", async () => {
  const root = await temporaryDirectory();
  const assignmentRoot = path.join(root, "실습 과제");
  try {
    await mkdir(assignmentRoot);
    for (const name of ["main.cpp", "main.c", "README.md"]) {
      await writeFile(path.join(assignmentRoot, name), "// lab\n");
    }
    for (const name of ["main.cpp", "main.c", "README.md"]) {
      assert.equal(await findSafeStarterPreview(assignmentRoot), path.join(assignmentRoot, name));
      await rm(path.join(assignmentRoot, name));
    }
    assert.equal(await findSafeStarterPreview(assignmentRoot), undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("starter preview skips oversized sources and directories without loading project files", async () => {
  const root = await temporaryDirectory();
  try {
    await writeFile(path.join(root, "main.cpp"), Buffer.alloc(2 * 1024 * 1024 + 1));
    await mkdir(path.join(root, "main.c"));
    await writeFile(path.join(root, "lab.sln"), "project");
    await writeFile(path.join(root, "README.txt"), "instructions");
    assert.equal(await findSafeStarterPreview(root), path.join(root, "README.txt"));
    await rm(path.join(root, "README.txt"));
    assert.equal(await findSafeStarterPreview(root), undefined);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("starter preview skips symlinked source and falls back to README", async (context) => {
  const root = await temporaryDirectory();
  const outside = await temporaryDirectory();
  try {
    await writeFile(path.join(outside, "secret.cpp"), "outside");
    await writeFile(path.join(root, "README"), "instructions");
    try {
      await symlink(path.join(outside, "secret.cpp"), path.join(root, "main.cpp"), "file");
    } catch (error) {
      if (["EPERM", "EACCES", "ENOTSUP"].includes((error as NodeJS.ErrnoException).code ?? "")) {
        context.skip("file symlinks are unavailable on this platform");
        return;
      }
      throw error;
    }
    assert.equal(await findSafeStarterPreview(root), path.join(root, "README"));
  } finally {
    await rm(root, { recursive: true, force: true });
    await rm(outside, { recursive: true, force: true });
  }
});

test("bundle download wiring cannot replace the current VS Code workspace", async () => {
  const extensionSource = await readFile(
    path.resolve(__dirname, "../../src/extension.ts"),
    "utf8",
  );
  const downloadStart = extensionSource.indexOf("async function downloadBundleAssignment(");
  const downloadEnd = extensionSource.indexOf("interface BundleSubmissionTarget", downloadStart);
  assert.ok(downloadStart >= 0 && downloadEnd > downloadStart);
  const downloadFunction = extensionSource.slice(downloadStart, downloadEnd);
  assert.doesNotMatch(downloadFunction, /vscode\.openFolder/);
  assert.doesNotMatch(downloadFunction, /updateWorkspaceFolders/);
  assert.match(downloadFunction, /revealDownloadedBundle\(targetUri, targetPath\)/);
  const openStart = extensionSource.indexOf("async function revealDownloadedBundle(");
  const openEnd = extensionSource.indexOf("async function pathExists(", openStart);
  assert.ok(openStart >= 0 && openEnd > openStart);
  const openFunction = extensionSource.slice(openStart, openEnd);
  assert.match(openFunction, /executeCommand\("workbench.view.explorer"\)/);
  assert.match(openFunction, /executeCommand\("revealInExplorer", previewUri\)/);
  assert.match(openFunction, /vscode.Uri.joinPath\(targetUri, path.basename\(preview\)\)/);
  assert.match(openFunction, /showTextDocument\(document, \{ preview: true \}\)/);
  assert.doesNotMatch(openFunction, /revealFileInOS|Uri.file|rm\(/);
  const afterOpen = downloadFunction.slice(downloadFunction.indexOf("await revealDownloadedBundle(targetUri, targetPath)"));
  assert.match(afterOpen, /파일은 보존됩니다/);
  assert.doesNotMatch(afterOpen, /rm\(/);

  const submitStart = extensionSource.indexOf("async function submitCurrentBundle(");
  const submitEnd = extensionSource.indexOf("async function cloneAssignment(", submitStart);
  assert.ok(submitStart >= 0 && submitEnd > submitStart);
  const submitFunction = extensionSource.slice(submitStart, submitEnd);
  assert.match(submitFunction, /createSubmissionBundle\(assignmentRoot\)/);
  assert.doesNotMatch(submitFunction, /createSubmissionBundle\(workspaceFolder/);
});

async function temporaryDirectory(): Promise<string> {
  return mkdtemp(path.join(tmpdir(), "autograde-vscode-workspace-"));
}
