import { createHash, randomUUID } from "node:crypto";
import {
  chmod,
  lstat,
  mkdir,
  open,
  readdir,
  readFile,
  rename,
  rm,
  writeFile,
} from "node:fs/promises";
import * as path from "node:path";
import { gunzip, gzip } from "node:zlib";

import * as tar from "tar-stream";

export const MAX_STARTER_ARCHIVE_BYTES = 25 * 1024 * 1024;
export const MAX_STARTER_EXPANDED_BYTES = 100 * 1024 * 1024;
export const MAX_SUBMISSION_BYTES = 100 * 1024 * 1024;
export const MAX_BUNDLE_FILE_BYTES = 100 * 1024 * 1024;
export const MAX_BUNDLE_FILES = 5_000;

const MAX_ARCHIVE_PATH_BYTES = 4_096;
const MAX_COMPONENT_BYTES = 255;
const MAX_MARKER_BYTES = 8_192;
const MAX_BUNDLE_MANIFEST_BYTES = 16 * 1024 * 1024;
const BUNDLE_SCHEMA = "autograde.bundle.v1";
const BUNDLE_MANIFEST_NAME = "AUTOGRADE-BUNDLE.json";
const MARKER_DIRECTORY = ".autograde";
const MARKER_FILENAME = "assignment.json";

const IGNORED_DIRECTORY_NAMES = new Set([
  ".autograde",
  ".git",
  ".gradle",
  ".mypy_cache",
  ".pytest_cache",
  ".venv",
  "__pycache__",
  "build",
  "dist",
  "node_modules",
  "out",
  "target",
]);

const WINDOWS_RESERVED_COMPONENT = /^(?:con|prn|aux|nul|com[1-9¹²³]|lpt[1-9¹²³])(?:\..*)?$/i;
const WINDOWS_INVALID_COMPONENT = /[<>:"|?*\x00-\x1f\x7f]/;

export interface SubmissionBundle {
  readonly archive: Uint8Array;
  readonly sha256: string;
  readonly fileCount: number;
  readonly sourceBytes: number;
}

export interface ExtractedStarter {
  readonly fileCount: number;
  readonly sourceBytes: number;
  readonly sha256: string;
}

export interface WorkspaceMarker {
  readonly schemaVersion: 1;
  readonly serviceBaseUrl: string;
  readonly assignmentId: string;
}

interface BundleFile {
  readonly relativePath: string;
  readonly content: Buffer;
  readonly executable: boolean;
}

interface ParsedEntry extends BundleFile {}

interface ParsedArchive {
  readonly files: ParsedEntry[];
  readonly directories: readonly string[];
}

interface ClaimedPath {
  readonly displayPath: string;
  readonly type: "file" | "directory";
  readonly explicit: boolean;
}

/**
 * Builds a byte-for-byte deterministic tar.gz from regular workspace files.
 * The server remains responsible for independently validating every entry.
 */
export async function createSubmissionBundle(workspaceRoot: string): Promise<SubmissionBundle> {
  const root = path.resolve(workspaceRoot);
  const rootStat = await lstat(root).catch(() => undefined);
  if (!rootStat?.isDirectory() || rootStat.isSymbolicLink()) {
    throw new Error("제출 workspace가 실제 디렉터리가 아닙니다.");
  }

  const claims = new Map<string, ClaimedPath>();
  const files: BundleFile[] = [];
  const directories: string[] = [];
  let sourceBytes = 0;
  await collectWorkspaceFiles(root, "", claims, files, directories, (size) => {
    sourceBytes += size;
    if (sourceBytes > MAX_SUBMISSION_BYTES) {
      throw new Error(`제출 파일의 전체 크기는 ${formatMiB(MAX_SUBMISSION_BYTES)} 이하여야 합니다.`);
    }
  });
  files.sort((left, right) => compareUtf8(left.relativePath, right.relativePath));
  directories.sort(compareUtf8);
  const manifest = createManifest("submission", directories, files);
  if (sourceBytes + manifest.byteLength > MAX_SUBMISSION_BYTES) {
    throw new Error(`제출 파일과 manifest의 전체 크기는 ${formatMiB(MAX_SUBMISSION_BYTES)} 이하여야 합니다.`);
  }

  const pack = tar.pack();
  const packed = collectReadable(pack);
  const payloadEntries = [
    ...directories.map((relativePath) => ({ relativePath, type: "directory" as const })),
    ...files.map((file) => ({ ...file, type: "file" as const })),
  ].sort((left, right) => compareUtf8(left.relativePath, right.relativePath));
  for (const entry of payloadEntries) {
    if (entry.type === "directory") {
      await writePackEntry(pack, normalizedTarHeader(`${entry.relativePath}/`, "directory", 0, true));
    } else {
      await writePackEntry(
        pack,
        normalizedTarHeader(entry.relativePath, "file", entry.content.byteLength, entry.executable),
        entry.content,
      );
    }
  }
  await writePackEntry(
    pack,
    normalizedTarHeader(BUNDLE_MANIFEST_NAME, "file", manifest.byteLength, false),
    manifest,
  );
  pack.finalize();
  const tarBytes = await packed;
  const archive = await gzipBytes(tarBytes);
  if (archive.byteLength > MAX_STARTER_ARCHIVE_BYTES) {
    throw new Error(`압축된 제출 bundle은 ${formatMiB(MAX_STARTER_ARCHIVE_BYTES)} 이하여야 합니다.`);
  }
  return {
    archive,
    sha256: sha256Hex(archive),
    fileCount: files.length,
    sourceBytes,
  };
}

/**
 * Validates the entire archive before writing, extracts into a generated
 * sibling directory, and only then renames it into the requested target.
 */
export async function extractStarterBundle(
  archive: Uint8Array,
  targetDirectory: string,
): Promise<ExtractedStarter> {
  return extractBundle(archive, targetDirectory, "starter");
}

export async function extractSubmissionBundle(
  archive: Uint8Array,
  targetDirectory: string,
): Promise<ExtractedStarter> {
  return extractBundle(archive, targetDirectory, "submission");
}

async function extractBundle(
  archive: Uint8Array, targetDirectory: string, expectedKind: "starter" | "submission",
): Promise<ExtractedStarter> {
  if (archive.byteLength === 0 || archive.byteLength > MAX_STARTER_ARCHIVE_BYTES) {
    throw new Error(`starter archive는 1 byte 이상 ${formatMiB(MAX_STARTER_ARCHIVE_BYTES)} 이하여야 합니다.`);
  }
  const target = path.resolve(targetDirectory);
  if (await exists(target)) {
    throw new Error("과제 다운로드 대상 경로가 이미 존재합니다.");
  }

  let tarBytes: Buffer;
  try {
    // Tar headers and padding add bounded overhead beyond the file payload.
    const overhead = MAX_BUNDLE_MANIFEST_BYTES +
      MAX_BUNDLE_FILES * (MAX_ARCHIVE_PATH_BYTES + 2_048) + 2_048;
    tarBytes = await gunzipBytes(archive, MAX_STARTER_EXPANDED_BYTES + overhead);
  } catch (error) {
    if (isBufferLimitError(error)) {
      throw new Error(`압축 해제된 starter는 ${formatMiB(MAX_STARTER_EXPANDED_BYTES)} 이하여야 합니다.`);
    }
    throw new Error("starter archive가 올바른 gzip 파일이 아닙니다.");
  }

  const parsed = await parseTar(tarBytes, expectedKind);
  const temp = path.join(
    path.dirname(target),
    `.${path.basename(target)}.autograde-partial-${randomUUID()}`,
  );
  let installed = false;
  try {
    await mkdir(temp, { recursive: false, mode: 0o700 });
    const directories = new Set<string>(parsed.directories);
    for (const directory of parsed.directories) {
      let parent = path.posix.dirname(directory);
      while (parent !== ".") {
        directories.add(parent);
        parent = path.posix.dirname(parent);
      }
    }
    for (const entry of parsed.files) {
      let parent = path.posix.dirname(entry.relativePath);
      while (parent !== ".") {
        directories.add(parent);
        parent = path.posix.dirname(parent);
      }
    }
    for (const directory of [...directories].sort(compareByDepthThenUtf8)) {
      await mkdir(resolveBelow(temp, directory), { recursive: false, mode: 0o700 });
    }
    for (const entry of parsed.files.sort((left, right) => compareUtf8(left.relativePath, right.relativePath))) {
      const destination = resolveBelow(temp, entry.relativePath);
      await writeFile(destination, entry.content, {
        flag: "wx",
        mode: entry.executable ? 0o700 : 0o600,
      });
      await chmod(destination, entry.executable ? 0o700 : 0o600);
    }
    if (await exists(target)) {
      throw new Error("과제 다운로드 중 대상 경로가 생성되어 설치를 중단했습니다.");
    }
    await rename(temp, target);
    installed = true;
  } finally {
    if (!installed) {
      await rm(temp, { recursive: true, force: true }).catch(() => undefined);
    }
  }

  return {
    fileCount: parsed.files.length,
    sourceBytes: parsed.files.reduce((total, entry) => total + entry.content.byteLength, 0),
    sha256: sha256Hex(archive),
  };
}

export async function writeWorkspaceMarker(
  workspaceRoot: string,
  marker: WorkspaceMarker,
): Promise<void> {
  const directory = path.join(workspaceRoot, MARKER_DIRECTORY);
  await mkdir(directory, { recursive: false, mode: 0o700 });
  const serialized = `${JSON.stringify(marker, undefined, 2)}\n`;
  await writeFile(path.join(directory, MARKER_FILENAME), serialized, {
    flag: "wx",
    mode: 0o600,
  });
}

/** Reads untrusted workspace metadata. Callers must re-check the server list. */
export async function readWorkspaceMarker(workspaceRoot: string): Promise<WorkspaceMarker | undefined> {
  const directory = path.join(workspaceRoot, MARKER_DIRECTORY);
  const markerPath = path.join(directory, MARKER_FILENAME);
  try {
    const directoryStat = await lstat(directory);
    const markerStat = await lstat(markerPath);
    if (
      !directoryStat.isDirectory() || directoryStat.isSymbolicLink() ||
      !markerStat.isFile() || markerStat.isSymbolicLink() ||
      markerStat.size > MAX_MARKER_BYTES
    ) {
      return undefined;
    }
    const handle = await open(markerPath, "r");
    let raw: string;
    try {
      const openedStat = await handle.stat();
      if (!openedStat.isFile() || openedStat.size > MAX_MARKER_BYTES) {
        return undefined;
      }
      raw = await handle.readFile({ encoding: "utf8" });
    } finally {
      await handle.close();
    }
    const parsed = JSON.parse(raw) as unknown;
    if (!isMarker(parsed)) {
      return undefined;
    }
    return parsed;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT" || error instanceof SyntaxError) {
      return undefined;
    }
    return undefined;
  }
}

function isMarker(value: unknown): value is WorkspaceMarker {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  return (
    record.schemaVersion === 1 &&
    typeof record.serviceBaseUrl === "string" &&
    record.serviceBaseUrl.length > 0 && record.serviceBaseUrl.length <= 2_048 &&
    typeof record.assignmentId === "string" &&
    record.assignmentId.length > 0 && record.assignmentId.length <= 256
  );
}

async function collectWorkspaceFiles(
  root: string,
  relativeDirectory: string,
  claims: Map<string, ClaimedPath>,
  files: BundleFile[],
  directories: string[],
  addSize: (size: number) => void,
): Promise<void> {
  const directory = relativeDirectory ? resolveBelow(root, relativeDirectory) : root;
  const directoryStat = await lstat(directory);
  if (!directoryStat.isDirectory() || directoryStat.isSymbolicLink()) {
    throw new Error(`symbolic link 디렉터리는 제출할 수 없습니다: ${relativeDirectory || "."}`);
  }
  const children = await readdir(directory);
  children.sort(compareUtf8);
  for (const name of children) {
    if (isReservedMetadataName(name)) {
      continue;
    }
    if (isIgnoredDirectoryName(name)) {
      const ignoredStat = await lstat(path.join(directory, name));
      if (ignoredStat.isDirectory() || ignoredStat.isSymbolicLink()) {
        continue;
      }
    }
    const relativePath = relativeDirectory ? `${relativeDirectory}/${name}` : name;
    const segments = validateArchivePath(relativePath);
    if (
      segments.length === 1 &&
      canonicalComponent(segments[0] ?? "") === canonicalComponent(BUNDLE_MANIFEST_NAME)
    ) {
      throw new Error(`${BUNDLE_MANIFEST_NAME}은 제출 bundle의 예약 파일명입니다.`);
    }
    const source = resolveBelow(root, relativePath);
    const before = await lstat(source);
    if (before.isSymbolicLink()) {
      throw new Error(`symbolic link는 제출할 수 없습니다: ${relativePath}`);
    }
    if (before.isDirectory()) {
      claimPath(claims, segments, "directory", true);
      directories.push(segments.join("/"));
      if (files.length + directories.length > MAX_BUNDLE_FILES) {
        throw new Error(`제출 entry는 최대 ${MAX_BUNDLE_FILES.toLocaleString()}개까지 허용됩니다.`);
      }
      await collectWorkspaceFiles(root, relativePath, claims, files, directories, addSize);
      continue;
    }
    if (!before.isFile()) {
      throw new Error(`일반 파일이 아닌 항목은 제출할 수 없습니다: ${relativePath}`);
    }
    if (before.size > MAX_BUNDLE_FILE_BYTES) {
      throw new Error(`${relativePath} 파일은 ${formatMiB(MAX_BUNDLE_FILE_BYTES)} 이하여야 합니다.`);
    }
    claimPath(claims, segments, "file", true);
    const content = await readFile(source);
    const after = await lstat(source);
    if (
      !after.isFile() || after.isSymbolicLink() ||
      before.size !== after.size || before.mtimeMs !== after.mtimeMs ||
      content.byteLength !== after.size
    ) {
      throw new Error(`제출 bundle 생성 중 파일이 변경되었습니다: ${relativePath}`);
    }
    addSize(content.byteLength);
    files.push({
      relativePath: segments.join("/"),
      content,
      executable: (after.mode & 0o111) !== 0,
    });
    if (files.length + directories.length > MAX_BUNDLE_FILES) {
      throw new Error(`제출 entry는 최대 ${MAX_BUNDLE_FILES.toLocaleString()}개까지 허용됩니다.`);
    }
  }
}

async function parseTar(tarBytes: Buffer, expectedKind: "starter" | "submission"): Promise<ParsedArchive> {
  const extract = tar.extract();
  const claims = new Map<string, ClaimedPath>();
  const entries: ParsedEntry[] = [];
  const directories: string[] = [];
  let sourceBytes = 0;
  let payloadEntries = 0;
  let manifest: Buffer | undefined;

  const completed = new Promise<void>((resolve, reject) => {
    extract.on("entry", (header, stream, next) => {
      void (async () => {
        const segments = validateArchivePath(header.name, header.type === "directory");
        const relativePath = segments.join("/");
        const manifestCollision =
          segments.length === 1 &&
          canonicalComponent(segments[0] ?? "") === canonicalComponent(BUNDLE_MANIFEST_NAME);
        if (manifestCollision) {
          if (relativePath !== BUNDLE_MANIFEST_NAME) {
            throw new Error("bundle manifest는 정확한 예약 파일명을 사용해야 합니다.");
          }
          if (header.type !== "file" || (header.mode ?? 0) & 0o111) {
            throw new Error("bundle manifest는 실행 불가능한 일반 파일이어야 합니다.");
          }
          const declaredSize = header.size;
          if (
            typeof declaredSize !== "number" ||
            !Number.isSafeInteger(declaredSize) ||
            declaredSize < 0 || declaredSize > MAX_BUNDLE_MANIFEST_BYTES
          ) {
            throw new Error("bundle manifest 크기가 허용 범위를 벗어났습니다.");
          }
          claimPath(claims, segments, "file", true);
          const content = await collectEntryBytes(stream, declaredSize, BUNDLE_MANIFEST_NAME);
          manifest = content;
          next();
          return;
        }
        payloadEntries += 1;
        if (payloadEntries > MAX_BUNDLE_FILES) {
          throw new Error(`starter entry는 최대 ${MAX_BUNDLE_FILES.toLocaleString()}개까지 허용됩니다.`);
        }
        if (header.type === "directory") {
          if (header.size !== 0) {
            throw new Error(`directory entry의 크기는 0이어야 합니다: ${header.name}`);
          }
          claimPath(claims, segments, "directory", true);
          directories.push(relativePath);
          for await (const _chunk of stream) {
            // Drain the entry; a non-zero body is rejected by the size check.
          }
          next();
          return;
        }
        if (header.type !== "file") {
          throw new Error(`starter에는 일반 파일과 디렉터리만 허용됩니다: ${header.name}`);
        }
        const declaredSize = header.size;
        if (
          typeof declaredSize !== "number" ||
          !Number.isSafeInteger(declaredSize) ||
          declaredSize < 0 || declaredSize > MAX_BUNDLE_FILE_BYTES
        ) {
          throw new Error(`${header.name} 파일은 ${formatMiB(MAX_BUNDLE_FILE_BYTES)} 이하여야 합니다.`);
        }
        claimPath(claims, segments, "file", true);
        const content = await collectEntryBytes(stream, declaredSize, header.name);
        const entryBytes = content.byteLength;
        sourceBytes += entryBytes;
        if (sourceBytes > MAX_STARTER_EXPANDED_BYTES) {
          throw new Error(`starter 파일의 전체 크기는 ${formatMiB(MAX_STARTER_EXPANDED_BYTES)} 이하여야 합니다.`);
        }
        entries.push({
          relativePath,
          content,
          executable: ((header.mode ?? 0) & 0o111) !== 0,
        });
        next();
      })().catch((error: unknown) => next(asError(error)));
    });
    extract.once("finish", resolve);
    extract.once("error", reject);
  });
  extract.end(tarBytes);
  try {
    await completed;
  } catch (error) {
    const message = error instanceof Error ? error.message : "알 수 없는 tar 오류";
    throw new Error(`안전하지 않거나 손상된 starter archive입니다: ${message}`);
  }
  if (!manifest) {
    throw new Error(`안전하지 않거나 손상된 starter archive입니다: ${BUNDLE_MANIFEST_NAME}이 없습니다.`);
  }
  const expectedManifest = createManifest(expectedKind, directories.sort(compareUtf8), entries.sort(
    (left, right) => compareUtf8(left.relativePath, right.relativePath),
  ));
  if (!manifest.equals(expectedManifest)) {
    throw new Error("안전하지 않거나 손상된 starter archive입니다: manifest가 내용과 일치하지 않습니다.");
  }
  if (sourceBytes + manifest.byteLength > MAX_STARTER_EXPANDED_BYTES) {
    throw new Error(`starter 파일과 manifest의 전체 크기는 ${formatMiB(MAX_STARTER_EXPANDED_BYTES)} 이하여야 합니다.`);
  }
  return { files: entries, directories };
}

async function collectEntryBytes(
  stream: AsyncIterable<unknown>,
  declaredSize: number,
  name: string,
): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let entryBytes = 0;
  for await (const chunk of stream) {
    const bytes = Buffer.from(chunk as Uint8Array);
    entryBytes += bytes.byteLength;
    if (entryBytes > declaredSize) {
      throw new Error(`starter 파일 크기가 header와 일치하지 않습니다: ${name}`);
    }
    chunks.push(bytes);
  }
  if (entryBytes !== declaredSize) {
    throw new Error(`starter 파일 크기가 header와 일치하지 않습니다: ${name}`);
  }
  return Buffer.concat(chunks, entryBytes);
}

function claimPath(
  claims: Map<string, ClaimedPath>,
  segments: readonly string[],
  type: "file" | "directory",
  explicit: boolean,
): void {
  for (let index = 0; index < segments.length; index += 1) {
    const displayPath = segments.slice(0, index + 1).join("/");
    const canonical = segments.slice(0, index + 1).map(canonicalComponent).join("/");
    const leaf = index === segments.length - 1;
    const claimedType = leaf ? type : "directory";
    const claimedExplicit = leaf ? explicit : false;
    const existing = claims.get(canonical);
    if (!existing) {
      claims.set(canonical, { displayPath, type: claimedType, explicit: claimedExplicit });
      continue;
    }
    if (existing.displayPath !== displayPath) {
      throw new Error(`대소문자 또는 Unicode가 충돌하는 경로입니다: ${existing.displayPath}, ${displayPath}`);
    }
    if (existing.type !== claimedType) {
      throw new Error(`파일과 디렉터리가 충돌하는 경로입니다: ${displayPath}`);
    }
    if (leaf && existing.explicit && claimedExplicit) {
      throw new Error(`중복된 archive 경로입니다: ${displayPath}`);
    }
    if (leaf && claimedExplicit && !existing.explicit) {
      claims.set(canonical, { ...existing, explicit: true });
    }
  }
}

function validateArchivePath(value: string, allowDirectorySuffix = false): string[] {
  const normalizedValue = allowDirectorySuffix && value.endsWith("/")
    ? value.slice(0, -1)
    : value;
  if (
    normalizedValue.length === 0 ||
    Buffer.byteLength(normalizedValue, "utf8") > MAX_ARCHIVE_PATH_BYTES ||
    normalizedValue.includes("\\") ||
    normalizedValue.includes("\0") ||
    normalizedValue.startsWith("/") ||
    /^[A-Za-z]:/.test(normalizedValue)
  ) {
    throw new Error(`안전하지 않은 archive 경로입니다: ${printablePath(value)}`);
  }
  const segments = normalizedValue.split("/");
  for (const segment of segments) {
    const canonical = canonicalComponent(segment);
    if (
      segment === "" || segment === "." || segment === ".." ||
      Buffer.byteLength(segment, "utf8") > MAX_COMPONENT_BYTES ||
      WINDOWS_INVALID_COMPONENT.test(segment) ||
      segment.endsWith(".") || segment.endsWith(" ") ||
      WINDOWS_RESERVED_COMPONENT.test(segment) ||
      canonical === ".git" || canonical === ".autograde"
    ) {
      throw new Error(`허용되지 않는 archive 경로입니다: ${printablePath(value)}`);
    }
  }
  return segments;
}

function canonicalComponent(value: string): string {
  // NFKC plus the most consequential Unicode case-fold expansions catches
  // filesystem aliases across default macOS, Windows, and Linux setups.
  return value
    .normalize("NFKC")
    .toLocaleLowerCase("en-US")
    .replaceAll("ß", "ss")
    .replaceAll("ς", "σ");
}

function isIgnoredDirectoryName(value: string): boolean {
  return IGNORED_DIRECTORY_NAMES.has(canonicalComponent(value));
}

function isReservedMetadataName(value: string): boolean {
  const canonical = canonicalComponent(value);
  return canonical === ".git" || canonical === ".autograde";
}

function resolveBelow(root: string, relativePath: string): string {
  const resolvedRoot = path.resolve(root);
  const candidate = path.resolve(resolvedRoot, ...relativePath.split("/"));
  const relation = path.relative(resolvedRoot, candidate);
  if (relation === "" || relation.startsWith("..") || path.isAbsolute(relation)) {
    throw new Error(`workspace 밖의 경로는 사용할 수 없습니다: ${relativePath}`);
  }
  return candidate;
}

function createManifest(
  kind: "starter" | "submission",
  directories: readonly string[],
  files: readonly BundleFile[],
): Buffer {
  const document = {
    directories: [...directories],
    files: files.map((file) => ({
      executable: file.executable,
      path: file.relativePath,
      sha256: `sha256:${sha256Hex(file.content)}`,
      size: file.content.byteLength,
    })),
    kind,
    schema: BUNDLE_SCHEMA,
    totals: {
      expanded_bytes: files.reduce((total, file) => total + file.content.byteLength, 0),
      file_count: files.length,
    },
  };
  return Buffer.from(`${JSON.stringify(document)}\n`, "utf8");
}

function normalizedTarHeader(
  name: string,
  type: "file" | "directory",
  size: number,
  executable: boolean,
): tar.Headers {
  return {
    name,
    size,
    type,
    mode: type === "directory" || executable ? 0o755 : 0o644,
    mtime: new Date(0),
    uid: 0,
    gid: 0,
    uname: "",
    gname: "",
  };
}

async function collectReadable(stream: AsyncIterable<unknown>): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of stream) {
    const bytes = Buffer.from(chunk as Uint8Array);
    chunks.push(bytes);
    total += bytes.byteLength;
  }
  return Buffer.concat(chunks, total);
}

function writePackEntry(
  pack: tar.Pack,
  header: tar.Headers,
  content?: Uint8Array,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const callback = (error?: Error | null): void => error ? reject(error) : resolve();
    if (content === undefined) {
      pack.entry(header, callback);
    } else {
      pack.entry(header, Buffer.from(content), callback);
    }
  });
}

function compareUtf8(left: string, right: string): number {
  return Buffer.compare(Buffer.from(left, "utf8"), Buffer.from(right, "utf8"));
}

function compareByDepthThenUtf8(left: string, right: string): number {
  const depthDifference = left.split("/").length - right.split("/").length;
  return depthDifference || compareUtf8(left, right);
}

async function exists(candidate: string): Promise<boolean> {
  try {
    await lstat(candidate);
    return true;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") {
      return false;
    }
    throw error;
  }
}

export function sha256Hex(bytes: Uint8Array): string {
  return createHash("sha256").update(bytes).digest("hex");
}

function gzipBytes(bytes: Uint8Array): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    gzip(Buffer.from(bytes), { level: 9 }, (error, result) => {
      if (error) {
        reject(error);
      } else {
        resolve(result);
      }
    });
  });
}

function gunzipBytes(bytes: Uint8Array, maxOutputLength: number): Promise<Buffer> {
  return new Promise((resolve, reject) => {
    gunzip(Buffer.from(bytes), { maxOutputLength }, (error, result) => {
      if (error) {
        reject(error);
      } else {
        resolve(result);
      }
    });
  });
}

function isBufferLimitError(error: unknown): boolean {
  return error instanceof Error && (
    (error as NodeJS.ErrnoException).code === "ERR_BUFFER_TOO_LARGE" ||
    error.message.toLowerCase().includes("larger than") ||
    error.message.toLowerCase().includes("maxoutputlength")
  );
}

function asError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error));
}

function printablePath(value: string): string {
  return JSON.stringify(value).slice(0, 300);
}

function formatMiB(bytes: number): string {
  return `${Math.floor(bytes / (1024 * 1024))} MiB`;
}
