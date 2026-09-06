import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import {
  chmod,
  lstat,
  mkdir,
  mkdtemp,
  readFile,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import test from "node:test";

import * as tar from "tar-stream";

import {
  createSubmissionBundle,
  extractStarterBundle,
  readWorkspaceMarker,
  writeWorkspaceMarker,
} from "../bundle";

test("submission bundle is deterministic and excludes metadata and build trees", async () => {
  const root = await temporaryDirectory();
  try {
    await mkdir(path.join(root, "src"));
    await mkdir(path.join(root, ".git"));
    await mkdir(path.join(root, ".autograde"));
    await mkdir(path.join(root, "build"));
    await mkdir(path.join(root, "node_modules"));
    await writeFile(path.join(root, "README.md"), "answer\n");
    await writeFile(path.join(root, "src", "run.sh"), "#!/bin/sh\necho ok\n");
    await chmod(path.join(root, "src", "run.sh"), 0o755);
    await writeFile(path.join(root, ".git", "config"), "secret");
    await writeFile(path.join(root, ".autograde", "assignment.json"), "metadata");
    await writeFile(path.join(root, "build", "answer.o"), "compiled");
    await writeFile(path.join(root, "node_modules", "module.js"), "dependency");

    const first = await createSubmissionBundle(root);
    const second = await createSubmissionBundle(root);
    assert.equal(first.fileCount, 2);
    assert.equal(first.sourceBytes, Buffer.byteLength("answer\n#!/bin/sh\necho ok\n"));
    assert.equal(first.sha256, second.sha256);
    assert.deepEqual(first.archive, second.archive);

    const target = `${root}-extracted`;
    try {
      await assert.rejects(extractStarterBundle(first.archive, target), /manifest/);
      const starterArchive = await makeProtocolArchive([
        { name: "README.md", content: "answer\n" },
        { name: "src", type: "directory" },
        { name: "src/run.sh", content: "#!/bin/sh\necho ok\n", mode: 0o755 },
      ], "starter");
      const extracted = await extractStarterBundle(starterArchive, target);
      assert.equal(extracted.fileCount, 2);
      assert.equal(await readFile(path.join(target, "README.md"), "utf8"), "answer\n");
      assert.equal(await readFile(path.join(target, "src", "run.sh"), "utf8"), "#!/bin/sh\necho ok\n");
      assert.equal((await lstat(path.join(target, "src", "run.sh"))).mode & 0o777, 0o700);
      assert.equal((await lstat(path.join(target, "README.md"))).mode & 0o777, 0o600);
      await assert.rejects(lstat(path.join(target, ".git")), { code: "ENOENT" });
      await assert.rejects(lstat(path.join(target, "build")), { code: "ENOENT" });
    } finally {
      await rm(target, { recursive: true, force: true });
    }
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("starter extraction accepts Unicode files and marker is excluded on resubmission", async () => {
  const source = await temporaryDirectory();
  const target = `${source}-target`;
  try {
    await mkdir(path.join(source, "src"));
    await writeFile(path.join(source, "src", "한글.txt"), "정답\n");
    const starter = await makeProtocolArchive([
      { name: "src", type: "directory" },
      { name: "src/한글.txt", content: "정답\n" },
    ], "starter");
    await extractStarterBundle(starter, target);
    await writeWorkspaceMarker(target, {
      schemaVersion: 1,
      serviceBaseUrl: "https://grade.example.edu",
      assignmentId: "asn_1",
    });
    assert.deepEqual(await readWorkspaceMarker(target), {
      schemaVersion: 1,
      serviceBaseUrl: "https://grade.example.edu",
      assignmentId: "asn_1",
    });
    await assert.rejects(lstat(path.join(target, "AUTOGRADE-BUNDLE.json")), { code: "ENOENT" });
    const submission = await createSubmissionBundle(target);
    assert.equal(submission.fileCount, 1);
    assert.equal(await readFile(path.join(target, "src", "한글.txt"), "utf8"), "정답\n");
  } finally {
    await rm(source, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});

test("submission packaging rejects symlinks outside ignored metadata", async () => {
  const root = await temporaryDirectory();
  try {
    await writeFile(path.join(root, "answer.txt"), "answer");
    await symlink(path.join(root, "answer.txt"), path.join(root, "linked-answer.txt"));
    await assert.rejects(createSubmissionBundle(root), /symbolic link/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("submission packaging reserves the protocol manifest name", async () => {
  const root = await temporaryDirectory();
  try {
    await writeFile(path.join(root, "autograde-bundle.JSON"), "student content");
    await assert.rejects(createSubmissionBundle(root), /예약 파일명/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("submission packaging rejects Windows-reserved source names", {
  skip: process.platform === "win32",
}, async () => {
  const root = await temporaryDirectory();
  try {
    await writeFile(path.join(root, "COM¹.txt"), "student content");
    await assert.rejects(createSubmissionBundle(root), /archive 경로/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("submission packaging rejects NFKC-colliding source directories", async () => {
  const root = await temporaryDirectory();
  try {
    await mkdir(path.join(root, "Ａ"));
    await mkdir(path.join(root, "a"));
    await writeFile(path.join(root, "Ａ", "one.txt"), "one");
    await writeFile(path.join(root, "a", "two.txt"), "two");
    await assert.rejects(createSubmissionBundle(root), /충돌/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("empty workspace still produces a valid submission protocol bundle", async () => {
  const root = await temporaryDirectory();
  try {
    const bundle = await createSubmissionBundle(root);
    assert.equal(bundle.fileCount, 0);
    assert.equal(bundle.sourceBytes, 0);
    assert.ok(bundle.archive.byteLength > 0);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("starter extraction preserves validated empty directories", async () => {
  const parent = await temporaryDirectory();
  const target = path.join(parent, "assignment");
  try {
    const archive = await makeProtocolArchive([
      { name: "starter", type: "directory" },
      { name: "starter/output/", type: "directory" },
    ], "starter");
    await extractStarterBundle(archive, target);
    assert.equal((await lstat(path.join(target, "starter", "output"))).isDirectory(), true);
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});

test("starter extraction requires a matching starter manifest", async () => {
  const parent = await temporaryDirectory();
  try {
    const missingTarget = path.join(parent, "missing");
    await assert.rejects(
      extractStarterBundle(await makeArchive([{ name: "answer.txt", content: "42" }]), missingTarget),
      /AUTOGRADE-BUNDLE\.json/,
    );
    await assert.rejects(lstat(missingTarget), { code: "ENOENT" });

    const mismatchedTarget = path.join(parent, "mismatched");
    const mismatched = await makeProtocolArchive(
      [{ name: "answer.txt", content: "actual" }],
      "starter",
      [{ name: "answer.txt", content: "declared" }],
    );
    await assert.rejects(extractStarterBundle(mismatched, mismatchedTarget), /manifest/);
    await assert.rejects(lstat(mismatchedTarget), { code: "ENOENT" });
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});

for (const scenario of [
  {
    name: "parent traversal",
    entries: [{ name: "../escape.txt", content: "bad" }],
  },
  {
    name: "absolute path",
    entries: [{ name: "/tmp/escape.txt", content: "bad" }],
  },
  {
    name: "Windows drive path",
    entries: [{ name: "C:payload.py", content: "bad" }],
  },
  {
    name: "reserved metadata",
    entries: [{ name: ".GiT/config", content: "bad" }],
  },
  {
    name: "symlink",
    entries: [{ name: "answer", type: "symlink" as const, linkname: "/etc/passwd" }],
  },
  {
    name: "hardlink",
    entries: [
      { name: "answer.txt", content: "ok" },
      { name: "alias.txt", type: "link" as const, linkname: "answer.txt" },
    ],
  },
  {
    name: "case collision",
    entries: [
      { name: "README", content: "one" },
      { name: "Readme", content: "two" },
    ],
  },
  {
    name: "Unicode normalization collision",
    entries: [
      { name: "é.txt", content: "one" },
      { name: "e\u0301.txt", content: "two" },
    ],
  },
  {
    name: "file-directory collision",
    entries: [
      { name: "src", content: "file" },
      { name: "src/main.py", content: "nested" },
    ],
  },
  {
    name: "Windows reserved filename",
    entries: [{ name: "NUL.txt", content: "bad" }],
  },
  {
    name: "Windows superscript reserved filename",
    entries: [{ name: "COM¹.txt", content: "bad" }],
  },
  {
    name: "Windows invalid character",
    entries: [{ name: "answer:final.txt", content: "bad" }],
  },
  {
    name: "Windows trailing dot",
    entries: [{ name: "answer.txt.", content: "bad" }],
  },
  {
    name: "non-canonical empty component",
    entries: [{ name: "src//answer.txt", content: "bad" }],
  },
  {
    name: "implicit parent case collision",
    entries: [
      { name: "Foo/a.txt", content: "one" },
      { name: "foo/b.txt", content: "two" },
    ],
  },
  {
    name: "explicit and implicit parent collision",
    entries: [
      { name: "Foo", type: "directory" as const },
      { name: "foo/a.txt", content: "two" },
    ],
  },
  {
    name: "NFKC implicit parent collision",
    entries: [
      { name: "Ａ/a.txt", content: "one" },
      { name: "a/b.txt", content: "two" },
    ],
  },
  {
    name: "overlong component",
    entries: [{ name: "x".repeat(256), content: "bad" }],
  },
  {
    name: "overlong UTF-8 path",
    entries: [{ name: Array(21).fill("x".repeat(200)).join("/"), content: "bad" }],
  },
] as const) {
  test(`starter extraction rejects ${scenario.name} without leaving a target`, async () => {
    const parent = await temporaryDirectory();
    const target = path.join(parent, "assignment");
    try {
      const archive = await makeArchive(scenario.entries);
      await assert.rejects(extractStarterBundle(archive, target), /starter|archive|경로|충돌|파일/);
      await assert.rejects(lstat(target), { code: "ENOENT" });
    } finally {
      await rm(parent, { recursive: true, force: true });
    }
  });
}

test("starter extraction requires canonical manifest JSON bytes", async () => {
  const parent = await temporaryDirectory();
  const target = path.join(parent, "assignment");
  try {
    const content = Buffer.from("42", "utf8");
    const document = {
      directories: [],
      files: [{
        executable: false,
        path: "answer.txt",
        sha256: `sha256:${createHash("sha256").update(content).digest("hex")}`,
        size: content.byteLength,
      }],
      kind: "starter",
      schema: "autograde.bundle.v1",
      totals: { expanded_bytes: content.byteLength, file_count: 1 },
    };
    const archive = await makeArchive([
      { name: "answer.txt", content: "42" },
      { name: "AUTOGRADE-BUNDLE.json", content: `${JSON.stringify(document, undefined, 2)}\n` },
    ]);
    await assert.rejects(extractStarterBundle(archive, target), /manifest/);
    await assert.rejects(lstat(target), { code: "ENOENT" });
  } finally {
    await rm(parent, { recursive: true, force: true });
  }
});

interface TestEntry {
  readonly name: string;
  readonly content?: string;
  readonly type?: "file" | "directory" | "link" | "symlink";
  readonly linkname?: string;
  readonly mode?: number;
}

async function makeProtocolArchive(
  entries: readonly TestEntry[],
  kind: "starter" | "submission",
  manifestEntries: readonly TestEntry[] = entries,
): Promise<Uint8Array> {
  const directories = manifestEntries
    .filter((entry) => entry.type === "directory")
    .map((entry) => entry.name.replace(/\/$/, ""))
    .sort(compareUtf8);
  const files = manifestEntries
    .filter((entry) => (entry.type ?? "file") === "file")
    .map((entry) => {
      const content = Buffer.from(entry.content ?? "", "utf8");
      return {
        executable: ((entry.mode ?? 0o644) & 0o111) !== 0,
        path: entry.name,
        sha256: `sha256:${createHash("sha256").update(content).digest("hex")}`,
        size: content.byteLength,
      };
    })
    .sort((left, right) => compareUtf8(left.path, right.path));
  const manifest = `${JSON.stringify({
    directories,
    files,
    kind,
    schema: "autograde.bundle.v1",
    totals: {
      expanded_bytes: files.reduce((total, file) => total + file.size, 0),
      file_count: files.length,
    },
  })}\n`;
  return makeArchive([
    ...entries,
    { name: "AUTOGRADE-BUNDLE.json", content: manifest },
  ]);
}

async function makeArchive(entries: readonly TestEntry[]): Promise<Uint8Array> {
  const pack = tar.pack();
  const collected = collect(pack);
  for (const entry of entries) {
    const content = Buffer.from(entry.content ?? "", "utf8");
    await new Promise<void>((resolve, reject) => {
      pack.entry({
        name: entry.name,
        type: entry.type ?? "file",
        size: content.byteLength,
        linkname: entry.linkname,
        mode: entry.mode,
      }, content, (error) => error ? reject(error) : resolve());
    });
  }
  pack.finalize();
  return gzipSync(await collected);
}

function compareUtf8(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

async function collect(stream: AsyncIterable<unknown>): Promise<Buffer> {
  const chunks: Buffer[] = [];
  for await (const chunk of stream) {
    chunks.push(Buffer.from(chunk as Uint8Array));
  }
  return Buffer.concat(chunks);
}

function temporaryDirectory(): Promise<string> {
  return mkdtemp(path.join(tmpdir(), "autograde-extension-"));
}
