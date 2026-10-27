import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, readdir, realpath, rm, unlink } from "node:fs/promises";
import * as path from "node:path";
import { deflateRawSync } from "node:zlib";

export const MAX_LEARNING_EXPORT_BYTES = 100 * 1024 * 1024;
export const MAX_LEARNING_EXPORT_FILES = 5000;
export interface LearningExportFile { readonly path: string; readonly bytes: Uint8Array }
export interface LearningExportGroup { readonly directory: string; readonly title: string; readonly files: readonly LearningExportFile[]; readonly skipped?: number }
export interface LearningExportOptions { readonly assertCurrent: () => void; readonly excludedDestinations?: readonly string[] }
export interface LearningExportResult { readonly destination: string; readonly files: number; readonly bytes: number; readonly skipped: number }

const ignoredDirectories = new Set(["node_modules", "build", "dist", "out", "target", "bin", "obj", "__pycache__", "venv"]);
const ignoredFiles = /^(?:credentials|tokens?|secrets?|passwords?)(?:\.[^.]+)?$|^id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?$|\.(?:pem|key|p12|pfx|kdbx|o|obj|exe|dll|so|dylib|class|pyc|pdb|ilk|log)$/i;
const within = (parent: string, child: string): boolean => {
  const relative = path.relative(parent, child);
  return relative === "" || relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
};
const digest = (bytes: Uint8Array) => createHash("sha256").update(bytes).digest("hex");

export function validExportPath(value: string): boolean {
  return value.length > 0 && Buffer.byteLength(value) <= 4096 && !/[\\\x00-\x1f\x7f]/u.test(value) &&
    value.split("/").every(part => part.length > 0 && Buffer.byteLength(part) <= 255 && part !== "." && part !== ".." &&
      !/[<>:"|?*]/u.test(part) && !/[. ]$/u.test(part) && !/^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\.|$)/iu.test(part));
}

/** Collect an explicit code folder, never following links or known private/tooling folders. */
export async function collectLearningCode(directory: string, assertCurrent: () => void, filterCode = true): Promise<{files: LearningExportFile[]; skipped: number}> {
  assertCurrent();
  const selected = path.resolve(directory), rootInfo = await lstat(selected), root = await realpath(selected);
  if (!rootInfo.isDirectory() || rootInfo.isSymbolicLink()) throw new Error("내보낼 코드의 실제 폴더를 선택하세요. 심볼릭 링크는 지원하지 않습니다.");
  const files: LearningExportFile[] = []; let total = 0, skipped = 0;
  const walk = async (relative: string): Promise<void> => {
    assertCurrent();
    const current = path.join(root, relative), info = await lstat(current);
    if (!info.isDirectory() || info.isSymbolicLink() || !within(root, await realpath(current))) throw new Error("코드 폴더가 변경되었습니다. 다시 내보내세요.");
    const entries = await readdir(current, {withFileTypes: true}); entries.sort((a, b) => a.name.localeCompare(b.name));
    for (const entry of entries) {
      assertCurrent();
      if (filterCode && (entry.name.startsWith(".") || ignoredDirectories.has(entry.name.toLowerCase()) || ignoredFiles.test(entry.name) || entry.name === "AUTOGRADE-BUNDLE.json")) {skipped++; continue;}
      const name = relative ? `${relative}/${entry.name}` : entry.name;
      if (!validExportPath(name)) throw new Error("다른 운영체제에서 안전하게 저장할 수 없는 파일명이 있습니다. 파일명을 수정하세요.");
      const filename = path.join(root, ...name.split("/")), before = await lstat(filename);
      if (before.isSymbolicLink()) throw new Error("심볼릭 링크 파일은 내보낼 수 없습니다. 필요한 원본 파일만 코드 폴더에 복사하세요.");
      if (before.isDirectory()) {await walk(name); continue;}
      if (!before.isFile() || before.nlink !== 1) throw new Error("일반 파일만 내보낼 수 있습니다. 링크·장치 파일은 제외하세요.");
      if (files.length >= MAX_LEARNING_EXPORT_FILES || total + before.size > MAX_LEARNING_EXPORT_BYTES) throw new Error("내보내기는 파일 5,000개, 전체 100 MiB까지 지원합니다.");
      if (!within(root, await realpath(filename))) throw new Error("코드 폴더 밖의 파일을 읽을 수 없습니다.");
      const handle = await open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
      try {
        const opened = await handle.stat();
        if (!opened.isFile() || opened.nlink !== 1 || opened.dev !== before.dev || opened.ino !== before.ino || opened.size !== before.size) throw new Error("파일이 변경되었습니다. 다시 내보내세요.");
        // Allocate only the verified size; a concurrently growing file must not cause an unbounded read.
        const bytes = Buffer.alloc(before.size); let read = 0;
        while (read < bytes.length) {assertCurrent(); const chunk = await handle.read(bytes, read, Math.min(1024 * 1024, bytes.length - read), read); if (!chunk.bytesRead) break; read += chunk.bytesRead;}
        const extra = await handle.read(Buffer.alloc(1), 0, 1, before.size), after = await handle.stat(); assertCurrent();
        if (read !== before.size || extra.bytesRead || after.size !== before.size || after.mtimeMs !== opened.mtimeMs) throw new Error("내보내는 동안 파일이 수정되었습니다. 저장 후 다시 시도하세요.");
        total += bytes.length; files.push({path: name, bytes});
      } finally {await handle.close();}
    }
  };
  await walk(""); return {files, skipped};
}

/** Resolve aliases before checking destination boundaries; exports survive managed-session cleanup. */
export async function learningExportDirectory(directory: string, excluded: readonly string[]): Promise<string> {
  const before = await lstat(directory);
  if (!before.isDirectory() || before.isSymbolicLink()) throw new Error("내보낼 일반 폴더를 선택하세요.");
  const canonical = await realpath(directory);
  for (const candidate of excluded) {
    const resolved = path.resolve(candidate);
    const target = await realpath(resolved).catch((error: NodeJS.ErrnoException) => {if (error.code === "ENOENT") return resolved; throw error;});
    if (within(target, canonical) || within(resolved, path.resolve(directory))) throw new Error("과제 작업 폴더와 종료 시 삭제되는 학습 공간 밖에 저장하세요. 개인 폴더나 USB를 선택할 수 있습니다.");
  }
  return canonical;
}

function flatten(groups: readonly LearningExportGroup[]): LearningExportFile[] {
  if (!groups.length) throw new Error("내보낼 항목을 하나 이상 선택하세요.");
  const files: LearningExportFile[] = [], names = new Set<string>(); let total = 0;
  for (const group of groups) {
    if (!validExportPath(group.directory)) throw new Error("내보내기 분류 경로가 올바르지 않습니다.");
    for (const file of group.files) {
      const name = `${group.directory}/${file.path}`;
      if (!validExportPath(name)) throw new Error("내보낼 파일 경로가 올바르지 않습니다.");
      const key = name.normalize("NFC").toLocaleLowerCase("en-US");
      if (names.has(key)) throw new Error("대소문자 또는 유니코드가 충돌하는 파일명이 있습니다.");
      names.add(key); total += file.bytes.byteLength;
      if (files.length >= MAX_LEARNING_EXPORT_FILES || total > MAX_LEARNING_EXPORT_BYTES) throw new Error("내보내기는 파일 5,000개, 전체 100 MiB까지 지원합니다. 항목을 나누어 내보내세요.");
      files.push({path: name, bytes: file.bytes});
    }
  }
  for (const name of names) {
    const components = name.split("/");
    for (let i = 1; i < components.length; i++) if (names.has(components.slice(0, i).join("/"))) throw new Error("파일과 폴더의 이름이 충돌합니다.");
  }
  const manifest = Buffer.from(JSON.stringify({schema: "autograde.learning-export.v1", created_at: new Date().toISOString(),
    groups: groups.map(group => ({directory: group.directory, title: group.title, files: group.files.length, excluded_entries: group.skipped ?? 0})),
    files: files.map(file => ({path: file.path, bytes: file.bytes.byteLength, sha256: digest(file.bytes)})),
    note: "전체 작업 공간 백업이 아닌 독립 복사본입니다. 코드의 숨김 항목·알려진 인증 파일·빌드 및 의존성 폴더·IDE 연결 정보는 제외합니다. 공식 제출 접수와 별개입니다."}, null, 2));
  if (total + manifest.length > MAX_LEARNING_EXPORT_BYTES) throw new Error("목록을 포함한 내보내기 크기가 100 MiB를 초과합니다.");
  return [...files, {path: "EXPORT-INFO.json", bytes: manifest}];
}

function crc32(bytes: Uint8Array): number {
  let value = 0xffffffff;
  for (const byte of bytes) {value ^= byte; for (let bit = 0; bit < 8; bit++) value = value & 1 ? (value >>> 1) ^ 0xedb88320 : value >>> 1;}
  return (value ^ 0xffffffff) >>> 0;
}

/** Bounded standard ZIP: UTF-8 names, deflate, CRC32, no ZIP64 or external executable. */
export function learningExportZip(files: readonly LearningExportFile[], assertCurrent: () => void): Buffer {
  const entries: Buffer[] = [], central: Buffer[] = []; let offset = 0;
  for (const file of files) {
    assertCurrent(); const name = Buffer.from(file.path), data = deflateRawSync(file.bytes), checksum = crc32(file.bytes);
    const local = Buffer.alloc(30); local.writeUInt32LE(0x04034b50); local.writeUInt16LE(20, 4); local.writeUInt16LE(0x800, 6);
    local.writeUInt16LE(8, 8); local.writeUInt16LE(33, 12); local.writeUInt32LE(checksum, 14);
    local.writeUInt32LE(data.length, 18); local.writeUInt32LE(file.bytes.byteLength, 22); local.writeUInt16LE(name.length, 26);
    const record = Buffer.alloc(46); record.writeUInt32LE(0x02014b50); record.writeUInt16LE(0x0314, 4); record.writeUInt16LE(20, 6);
    record.writeUInt16LE(0x800, 8); record.writeUInt16LE(8, 10); record.writeUInt16LE(33, 14); record.writeUInt32LE(checksum, 16);
    record.writeUInt32LE(data.length, 20); record.writeUInt32LE(file.bytes.byteLength, 24); record.writeUInt16LE(name.length, 28);
    record.writeUInt32LE(0x81800000, 38); record.writeUInt32LE(offset, 42);
    entries.push(local, name, data); central.push(record, name); offset += local.length + name.length + data.length;
  }
  const directory = Buffer.concat(central), end = Buffer.alloc(22); end.writeUInt32LE(0x06054b50);
  end.writeUInt16LE(files.length, 8); end.writeUInt16LE(files.length, 10); end.writeUInt32LE(directory.length, 12); end.writeUInt32LE(offset, 16);
  assertCurrent(); return Buffer.concat([...entries, directory, end]);
}

/** Only an output tree exclusively created by this operation is removed. No destination is overwritten. */
export async function saveLearningExport(directory: string, format: "folder" | "zip", groups: readonly LearningExportGroup[], options: LearningExportOptions): Promise<LearningExportResult> {
  const guard = options.assertCurrent; guard(); const files = flatten(groups);
  const parent = await learningExportDirectory(directory, options.excludedDestinations ?? []); guard();
  const nonce = randomUUID(), name = `autograde-learning-${new Date().toISOString().slice(0, 10)}-${nonce.slice(0, 8)}`;
  const destination = path.join(parent, name + (format === "zip" ? ".zip" : ""));
  let reserved: Awaited<ReturnType<typeof lstat>> | undefined, zipIdentity: Awaited<ReturnType<typeof lstat>> | undefined, published = false;
  const assertOwnedFolder = async (): Promise<void> => {
    const current = await lstat(destination);
    if (!reserved || !current.isDirectory() || current.isSymbolicLink() || current.dev !== reserved.dev || current.ino !== reserved.ino) {
      throw new Error("내보내기 대상 폴더가 변경되었습니다. 다른 폴더를 선택하여 다시 내보내세요.");
    }
  };
  try {
    guard();
    if (format === "zip") {
      const archive = learningExportZip(files, guard);
      guard(); await learningExportDirectory(parent, options.excludedDestinations ?? []);
      // Exclusive creation also works on FAT/exFAT USB devices, unlike hard links.
      guard(); const handle = await open(destination, "wx", 0o600); zipIdentity = await handle.stat();
      try {await handle.writeFile(archive); await handle.sync(); guard(); published = true;} finally {await handle.close();}
    } else {
      guard(); await learningExportDirectory(parent, options.excludedDestinations ?? []);
      // Windows cannot rename a directory over even an empty reserved directory.
      // Create our output exclusively, write exclusive files, and acknowledge
      // completion only after all writes succeed. Cancelled partial output is
      // removed only while the directory still has our original identity.
      await mkdir(destination, {mode: 0o700}); reserved = await lstat(destination);
      for (const file of files) {
        guard(); await assertOwnedFolder(); const target = path.join(destination, ...file.path.split("/"));
        await mkdir(path.dirname(target), {recursive: true, mode: 0o700});
        const handle = await open(target, "wx", 0o600); try {await handle.writeFile(file.bytes); await handle.sync();} finally {await handle.close();}
      }
      guard(); await assertOwnedFolder(); published = true;
    }
    return {destination, files: files.length - 1, bytes: files.reduce((sum, file) => sum + file.bytes.byteLength, 0), skipped: groups.reduce((sum, group) => sum + (group.skipped ?? 0), 0)};
  } finally {
    if (!published && reserved) {
      const target = await lstat(destination).catch(() => undefined);
      if (target?.isDirectory() && !target.isSymbolicLink() && target.dev === reserved.dev && target.ino === reserved.ino) {
        await rm(destination, {recursive: true, force: false, maxRetries: 2});
      }
    }
    if (!published && zipIdentity) {
      const target = await lstat(destination).catch(() => undefined);
      if (target?.dev === zipIdentity.dev && target.ino === zipIdentity.ino) await unlink(destination).catch(() => undefined);
    }
  }
}
