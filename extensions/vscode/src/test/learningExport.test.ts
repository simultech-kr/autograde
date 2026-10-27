import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, writeFile, readFile, readdir, rm, symlink, link, truncate } from "node:fs/promises";
import { existsSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import * as path from "node:path";
import { inflateRawSync } from "node:zlib";
import { collectLearningCode, learningExportDirectory, MAX_LEARNING_EXPORT_BYTES, saveLearningExport, validExportPath } from "../learningExport";

const guard = () => {};
async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-export-")), source = path.join(root, "session", "workspace"), output = path.join(root, "personal");
  await mkdir(source, {recursive: true}); await mkdir(output);
  return {root, source, output, close: () => rm(root, {recursive: true, force: true})};
}
function zipFiles(archive: Buffer): Map<string, Buffer> {
  const files = new Map<string, Buffer>(); let offset = 0;
  while (archive.readUInt32LE(offset) === 0x04034b50) {
    assert.equal(archive.readUInt16LE(offset + 6), 0x800); assert.equal(archive.readUInt16LE(offset + 8), 8);
    const size = archive.readUInt32LE(offset + 18), expected = archive.readUInt32LE(offset + 22), names = archive.readUInt16LE(offset + 26), extra = archive.readUInt16LE(offset + 28);
    const start = offset + 30 + names + extra, name = archive.subarray(offset + 30, offset + 30 + names).toString("utf8"), bytes = inflateRawSync(archive.subarray(start, start + size));
    assert.equal(bytes.length, expected); files.set(name, bytes); offset = start + size;
  }
  assert.equal(archive.readUInt32LE(offset), 0x02014b50); assert.equal(archive.readUInt32LE(archive.length - 22), 0x06054b50);
  assert.equal(archive.readUInt16LE(archive.length - 14), files.size); return files;
}

test("current work exports independently as folder and standard UTF-8 ZIP, preserving originals", async () => {
  const f = await fixture();
  try {
    await mkdir(path.join(f.source, "src")); await writeFile(path.join(f.source, "src", "인사.cpp"), "int main() { return 0; }\n");
    await writeFile(path.join(f.source, "README.md"), "assignment instructions");
    const code = await collectLearningCode(f.source, guard), groups = [{directory: "current-code", title: "실습 코드", ...code}];
    const options = {assertCurrent: guard, excludedDestinations: [path.dirname(f.source)]};
    const folder = await saveLearningExport(f.output, "folder", groups, options), zip = await saveLearningExport(f.output, "zip", groups, options);
    assert.equal(await readFile(path.join(folder.destination, "current-code/src/인사.cpp"), "utf8"), "int main() { return 0; }\n");
    const entries = zipFiles(await readFile(zip.destination));
    assert.equal(entries.get("current-code/src/인사.cpp")?.toString(), "int main() { return 0; }\n");
    assert.equal(entries.size, 3); assert.equal(folder.files, 2); assert.equal(zip.files, 2);
    const manifest = JSON.parse(entries.get("EXPORT-INFO.json")!.toString());
    assert.equal(manifest.schema, "autograde.learning-export.v1"); assert.equal(manifest.files.length, 2);
    assert.ok(manifest.files.every((file: {sha256: string}) => /^[a-f0-9]{64}$/.test(file.sha256)));
    assert.ok(!JSON.stringify(manifest).includes(f.root)); assert.match(manifest.note, /전체 작업 공간 백업이 아닌/);
    await rm(path.dirname(f.source), {recursive: true});
    assert.equal((await readdir(f.output)).length, 2); assert.ok((await readFile(zip.destination)).length);
  } finally {await f.close();}
});

test("code export excludes hidden state, credential files, build output and dependency trees", async () => {
  const f = await fixture();
  try {
    for (const directory of [".git", ".autograde", ".vscode", "node_modules", "build", "obj"]) {
      await mkdir(path.join(f.source, directory)); await writeFile(path.join(f.source, directory, "private.txt"), "not exported");
    }
    for (const file of [".env", "credentials.json", "token.txt", "client.pem", "binary.exe", "AUTOGRADE-BUNDLE.json"]) await writeFile(path.join(f.source, file), "not exported");
    await writeFile(path.join(f.source, "main.cpp"), "code");
    const code = await collectLearningCode(f.source, guard); assert.deepEqual(code.files.map(file => file.path), ["main.cpp"]); assert.equal(code.skipped, 12);
    const result = await saveLearningExport(f.output, "folder", [{directory: "current-code", title: "code", ...code}], {assertCurrent: guard});
    const manifest = JSON.parse(await readFile(path.join(result.destination, "EXPORT-INFO.json"), "utf8"));
    assert.equal(manifest.groups[0].excluded_entries, 12); assert.equal(result.skipped, 12);
  } finally {await f.close();}
});

test("rejects destinations in managed sessions, code trees and symlink aliases", async () => {
  const f = await fixture();
  try {
    await assert.rejects(learningExportDirectory(f.source, [path.dirname(f.source)]), /학습 공간 밖/);
    const alias = path.join(f.root, "alias"); await symlink(path.dirname(f.source), alias);
    await assert.rejects(learningExportDirectory(path.join(alias, "workspace"), [path.dirname(f.source)]), /학습 공간 밖/);
    await assert.rejects(learningExportDirectory(alias, []), /일반 폴더/);
    assert.ok(await learningExportDirectory(f.output, [path.dirname(f.source)]));
  } finally {await f.close();}
});

test("code collection rejects symbolic and hard links without reading outside source", async () => {
  for (const kind of ["symbolic", "hard"]) {
    const f = await fixture();
    try {
      const secret = path.join(f.root, "private.txt"); await writeFile(secret, "never copy");
      if (kind === "symbolic") await symlink(secret, path.join(f.source, "linked.txt")); else await link(secret, path.join(f.source, "linked.txt"));
      await assert.rejects(collectLearningCode(f.source, guard), /링크|일반 파일/); assert.deepEqual(await readdir(f.output), []);
    } finally {await f.close();}
  }
});

test("unsafe paths and case/Unicode collisions are rejected before creating output", async () => {
  const f = await fixture();
  try {
    for (const name of ["../secret", "/root", "a\\b", "CON.cpp", "part./code", "a:b", "file\0.txt"]) {
      assert.equal(validExportPath(name), false);
      await assert.rejects(saveLearningExport(f.output, "zip", [{directory: "code", title: "code", files: [{path: name, bytes: Buffer.from("x")}]}], {assertCurrent: guard}), /경로/);
    }
    for (const names of [["A.cpp", "a.cpp"], ["é.txt", "e\u0301.txt"], ["a", "a/b"]]) {
      await assert.rejects(saveLearningExport(f.output, "folder", [{directory: "code", title: "code", files: names.map(name => ({path: name, bytes: Buffer.from("x")}))}], {assertCurrent: guard}), /충돌/);
    }
    assert.deepEqual(await readdir(f.output), []);
  } finally {await f.close();}
});

test("export size/count limits fail without leaving output", async () => {
  const f = await fixture();
  try {
    await writeFile(path.join(f.source, "large.dat"), ""); await truncate(path.join(f.source, "large.dat"), MAX_LEARNING_EXPORT_BYTES + 1);
    await assert.rejects(collectLearningCode(f.source, guard), /100 MiB/);
    const files = Array.from({length: 5001}, (_, index) => ({path: `${index}.txt`, bytes: Buffer.alloc(0)}));
    await assert.rejects(saveLearningExport(f.output, "zip", [{directory: "code", title: "code", files}], {assertCurrent: guard}), /5,000/);
    assert.deepEqual(await readdir(f.output), []);
  } finally {await f.close();}
});

test("cancellation removes only newly created output and never deletes pre-existing files", async () => {
  for (const format of ["folder", "zip"] as const) {
    const f = await fixture();
    try {
      await writeFile(path.join(f.output, "keep.txt"), "keep"); let checks = 0;
      const threshold = format === "zip" ? 7 : 5;
      await assert.rejects(saveLearningExport(f.output, format, [{directory: "code", title: "code", files: [{path: "main.c", bytes: Buffer.from("int main(){}")}, {path: "README.md", bytes: Buffer.from("notes")}]}],
        {assertCurrent: () => {if (++checks >= threshold) throw new Error("cancelled");}}), /cancelled/);
      assert.deepEqual(await readdir(f.output), ["keep.txt"]); assert.equal(await readFile(path.join(f.output, "keep.txt"), "utf8"), "keep");
    } finally {await f.close();}
  }
});

test("cancelling a populated folder export removes the partial output and preserves other exports", async () => {
  const f = await fixture();
  try {
    const groups = [{directory: "code", title: "code", files: [{path: "main.c", bytes: Buffer.from("int main(){}")}, {path: "README.md", bytes: Buffer.from("notes")}]}];
    const completed = await saveLearningExport(f.output, "folder", groups, {assertCurrent: guard});
    let observedPartial = false;
    await assert.rejects(saveLearningExport(f.output, "folder", groups, {assertCurrent: () => {
      const partial = readdirSync(f.output).find(name => path.join(f.output, name) !== completed.destination && existsSync(path.join(f.output, name, "code", "main.c")));
      if (partial) {observedPartial = true; throw new Error("cancelled after first file");}
    }}), /cancelled after first file/);
    assert.equal(observedPartial, true);
    assert.deepEqual(await readdir(f.output), [path.basename(completed.destination)]);
    assert.equal(await readFile(path.join(completed.destination, "code/main.c"), "utf8"), "int main(){}");
  } finally {await f.close();}
});
