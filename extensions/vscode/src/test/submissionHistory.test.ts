import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, writeFile, readFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import * as path from "node:path";
import { createSubmissionBundle, extractSubmissionBundle, extractStarterBundle, sha256Hex } from "../bundle";
import { parseSubmissionHistory, verifySubmissionSource } from "../submissionHistory";

const bytes = Buffer.from("submitted archive");
const row = { submission_id: "bsub_test", assignment_id: "asn_test", state: "queued",
  received_at: "2026-09-10T12:00:00Z", source_sha256: `sha256:${sha256Hex(bytes)}`, source_size_bytes: bytes.length };

test("history normalizes digest and validates exact downloaded source", () => {
  const history = parseSubmissionHistory({ submissions: [row], has_more: false }, "asn_test");
  const version = history.submissions[0];
  assert.ok(version);
  assert.equal(version.sourceDigest, sha256Hex(bytes));
  verifySubmissionSource(bytes, version);
  assert.throws(() => verifySubmissionSource(Buffer.from("other content"), version));
  assert.throws(() => verifySubmissionSource(Buffer.alloc(bytes.length), version));
});

test("history rejects other assignments, duplicate IDs and malformed metadata", () => {
  for (const patch of [{ assignment_id: "other" }, { submission_id: "../secret" }, { received_at: "bad date" },
    { source_sha256: "bad" }, { source_size_bytes: -1 }, { source_size_bytes: 26 * 1024 * 1024 }]) {
    assert.throws(() => parseSubmissionHistory({ submissions: [{ ...row, ...patch }], has_more: false }, "asn_test"));
  }
  assert.throws(() => parseSubmissionHistory({ submissions: [row, row], has_more: false }, "asn_test"));
  assert.throws(() => parseSubmissionHistory({ submissions: Array(101).fill(row), has_more: true }, "asn_test"));
  assert.throws(() => parseSubmissionHistory({ submissions: [row] }, "asn_test"));
});

test("submission restoration preserves original code and never overwrites current work", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-history-"));
  const target = `${root}-restore`;
  try {
    await writeFile(path.join(root, "main.cpp"), "int main(){return 0;}\n");
    const snapshot = await createSubmissionBundle(root);
    await writeFile(path.join(root, "main.cpp"), "new work");
    await assert.rejects(extractStarterBundle(snapshot.archive, target), /manifest/);
    await extractSubmissionBundle(snapshot.archive, target);
    assert.equal(await readFile(path.join(target, "main.cpp"), "utf8"), "int main(){return 0;}\n");
    assert.equal(await readFile(path.join(root, "main.cpp"), "utf8"), "new work");
    await assert.rejects(extractSubmissionBundle(snapshot.archive, target), /존재/);
  } finally {
    await rm(root, { recursive: true, force: true });
    await rm(target, { recursive: true, force: true });
  }
});
