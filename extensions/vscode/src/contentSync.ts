import { createHash, randomUUID } from "node:crypto";
import { constants } from "node:fs";
import { lstat, mkdir, open, readdir, realpath, rename, unlink, rmdir } from "node:fs/promises";
import * as path from "node:path";
import { hostname } from "node:os";
import { RequestCancelledError } from "./api";

export const MAX_CONTENT_FILE_BYTES = 25 * 1024 * 1024;
export const MAX_CONTENT_RELEASE_BYTES = 100 * 1024 * 1024;
export const MAX_MANIFEST_BYTES = 512 * 1024;
export const MAX_CONTENT_FILES = 1000;
export const MAX_CONTENT_CACHE_BYTES = 512 * 1024 * 1024;
export interface ContentContext {
  readonly schema: "autograde.content.context.v1";
  readonly course_key: string;
  readonly subject_scope: string;
  readonly limits: {readonly max_file_bytes: number; readonly max_release_bytes: number;
    readonly max_files: number; readonly max_manifest_bytes: number};
}
export interface ContentCollection {
  readonly collection_id: string; readonly release_id: string; readonly generation: number;
  readonly title: string; readonly change_note: string; readonly policy_revision: number;
}
export interface ContentCatalog {
  readonly course_key: string; readonly subject_scope: string; readonly collections: readonly ContentCollection[];
}
export interface ContentFile {
  readonly path: string; readonly sha256: string; readonly size_bytes: number; readonly media_type: string;
}
export interface ContentManifest extends ContentCollection {
  readonly schema: "autograde.content.manifest.v1"; readonly manifest_id: string; readonly manifest_sha256: string;
  readonly course_key: string; readonly subject_scope: string; readonly files: readonly ContentFile[];
}
export interface ContentSource {
  object(manifest: ContentManifest, file: ContentFile, signal?: AbortSignal): Promise<Uint8Array>;
  verify(manifest: ContentManifest, signal?: AbortSignal): Promise<void>;
}
export interface ContentSyncOptions {
  readonly root: string; readonly origin: string; readonly context: ContentContext;
  readonly assertCurrent: () => void; readonly assignmentDirectories?: readonly string[];
  /** Additional export-only boundaries; the temporary cache may be inside them. */
  readonly excludedExportDirectories?: readonly string[];
  readonly signal?: AbortSignal;
  readonly onProgress?: (completed: number, total: number) => void;
}
export interface ContentSyncResult {
  readonly manifest: ContentManifest; readonly downloadedFiles: number; readonly reusedFiles: number;
  readonly downloadedBytes: number; readonly cacheDirectory: string;
}
const record = (value: unknown): value is Record<string, unknown> => typeof value === "object" && value !== null && !Array.isArray(value);
const hash = (value: unknown): value is string => typeof value === "string" && /^[a-f0-9]{64}$/.test(value);
const positive = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;
const course = (value: unknown): value is string => typeof value === "string" && /^[a-z0-9][a-z0-9_-]{0,127}$/.test(value);
const invalid = () => new Error("콘텐츠 목록 형식이나 무결성이 올바르지 않습니다. 교수자에게 확인을 요청하세요.");
export const contentSha256 = (bytes: Uint8Array | string): string => createHash("sha256").update(bytes).digest("hex");

export function contentCanonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(contentCanonicalJson).join(",")}]`;
  if (record(value)) return `{${Object.keys(value).sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)))
    .map(key => `${JSON.stringify(key)}:${contentCanonicalJson(value[key])}`).join(",")}}`;
  return JSON.stringify(value);
}

export function parseContentContext(raw: unknown): ContentContext {
  if (!record(raw) || raw.schema !== "autograde.content.context.v1" || !course(raw.course_key) || !hash(raw.subject_scope) ||
      !record(raw.limits)) throw invalid();
  for (const [key, maximum] of Object.entries({max_file_bytes: MAX_CONTENT_FILE_BYTES, max_release_bytes: MAX_CONTENT_RELEASE_BYTES,
    max_files: MAX_CONTENT_FILES, max_manifest_bytes: MAX_MANIFEST_BYTES})) {
    if (!positive(raw.limits[key]) || Number(raw.limits[key]) > maximum) throw invalid();
  }
  return raw as unknown as ContentContext;
}
function collection(raw: unknown): raw is Record<string, unknown> & ContentCollection {
  return record(raw) && typeof raw.collection_id === "string" && /^cc_[a-f0-9]{32}$/.test(raw.collection_id) &&
    typeof raw.release_id === "string" && /^cr_[a-f0-9]{32}$/.test(raw.release_id) && positive(raw.generation) &&
    positive(raw.policy_revision) && typeof raw.title === "string" && raw.title.trim().length > 0 && raw.title.length <= 500 &&
    typeof raw.change_note === "string" && raw.change_note.length <= 10000;
}
export function parseContentCatalog(raw: unknown, context: ContentContext): ContentCatalog {
  if (!record(raw) || raw.course_key !== context.course_key || raw.subject_scope !== context.subject_scope ||
      !Array.isArray(raw.collections) || raw.collections.length > 1000) throw invalid();
  const seen = new Set<string>();
  for (const item of raw.collections) {
    if (!collection(item) || seen.has(item.collection_id)) throw invalid();
    seen.add(item.collection_id);
  }
  return raw as unknown as ContentCatalog;
}

/** Portable export paths: no hidden IDE config, executable hooks, traversal or Windows device aliases. */
export function validContentPath(value: unknown): value is string {
  if (typeof value !== "string" || !value || Buffer.byteLength(value) > 240 || value.normalize("NFC") !== value ||
      Buffer.from(value).toString("utf8") !== value ||
      /[\\\u0000-\u001f\u007f<>:"|?*]/.test(value) || value.startsWith("/") || value.endsWith("/")) return false;
  const segments = value.split("/");
  return /\.(txt|md|csv|json|pdf|zip|c|h|cpp|hpp|cc|cxx|png|jpg|jpeg)$/i.test(value) &&
    segments.length <= 12 && segments.every(segment => segment.length > 0 &&
    !segment.startsWith(".") && !/[. ]$/.test(segment) &&
    !/^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(segment));
}

export function parseContentManifest(raw: unknown, context: ContentContext): ContentManifest {
  if (!collection(raw) || raw.schema !== "autograde.content.manifest.v1" || raw.course_key !== context.course_key ||
      raw.subject_scope !== context.subject_scope || !hash(raw.manifest_sha256) || raw.manifest_id !== `cm_${raw.manifest_sha256}` ||
      !Array.isArray(raw.files) || raw.files.length > context.limits.max_files ||
      Buffer.byteLength(contentCanonicalJson(raw)) > context.limits.max_manifest_bytes) throw invalid();
  const paths = new Set<string>(), sizes = new Map<string, number>();
  let total = 0, previous: string | undefined;
  for (const item of raw.files) {
    if (!record(item) || !validContentPath(item.path) || !hash(item.sha256) || !Number.isSafeInteger(item.size_bytes) ||
        Number(item.size_bytes) < 0 || Number(item.size_bytes) > context.limits.max_file_bytes ||
        typeof item.media_type !== "string" || !/^[a-z0-9!#$&^_.+-]+\/[a-z0-9!#$&^_.+-]+$/i.test(item.media_type)) throw invalid();
    // Upper/lower expansion conservatively rejects sharp-s/ligature aliases as well as ASCII case collisions.
    const folded = item.path.toUpperCase().toLowerCase();
    if (paths.has(folded) || previous !== undefined && Buffer.compare(Buffer.from(previous), Buffer.from(item.path)) >= 0) throw invalid();
    // A file cannot also be a directory (including case-insensitive file systems).
    if ([...paths].some(existing => folded.startsWith(`${existing}/`) || existing.startsWith(`${folded}/`))) throw invalid();
    if (sizes.has(item.sha256) && sizes.get(item.sha256) !== item.size_bytes) throw invalid();
    paths.add(folded); sizes.set(item.sha256, Number(item.size_bytes)); previous = item.path;
    total += Number(item.size_bytes);
  }
  if (total > context.limits.max_release_bytes) throw invalid();
  const {manifest_id: _id, manifest_sha256: _digest, ...unsigned} = raw;
  if (contentSha256(contentCanonicalJson(unsigned)) !== raw.manifest_sha256) throw invalid();
  return raw as unknown as ContentManifest;
}

function inside(parent: string, child: string): boolean {
  const relative = path.relative(parent, child);
  return relative === "" || relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative);
}
const missing = (error: unknown) => (error as NodeJS.ErrnoException).code === "ENOENT";
async function statMaybe(filename: string) { try {return await lstat(filename);} catch (error) {if (missing(error)) return undefined; throw error;} }

/** Resolve the existing ancestor once; never follow links in the managed subtree. */
async function privateRoot(directory: string, guard: () => void): Promise<string> {
  const resolved = path.resolve(directory), info = await statMaybe(resolved);
  if (info) {
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("자료 캐시 폴더에 심볼릭 링크를 사용할 수 없습니다.");
    return realpath(resolved);
  }
  const parent = await privateRoot(path.dirname(resolved), guard);
  const target = path.join(parent, path.basename(resolved));
  guard();
  try { await mkdir(target, {mode: 0o700}); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
  await assertDirectory(target);
  return target;
}
async function assertDirectory(directory: string): Promise<void> {
  for (let cursor = path.resolve(directory); ; cursor = path.dirname(cursor)) {
    const info = await lstat(cursor);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error("자료 경로가 안전한 일반 폴더가 아닙니다.");
    if (path.dirname(cursor) === cursor) break;
  }
}
async function childDirectory(root: string, relative: string, guard: () => void): Promise<string> {
  await assertDirectory(root);
  let current = root;
  for (const segment of relative.split("/").filter(Boolean)) {
    current = path.join(current, segment);
    guard();
    try { await mkdir(current, {mode: 0o700}); } catch (error) { if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error; }
    await assertDirectory(current);
  }
  return current;
}
async function readPlain(filename: string, maximum: number): Promise<Buffer> {
  const info = await lstat(filename);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > maximum) throw new Error("자료 캐시 파일이 안전한 일반 파일이 아닙니다.");
  const handle = await open(filename, constants.O_RDONLY | (constants.O_NOFOLLOW ?? 0));
  try {
    const actual = await handle.stat();
    if (actual.ino !== info.ino || actual.dev !== info.dev || actual.nlink !== 1 || actual.size > maximum) throw new Error("자료 파일이 검증 도중 변경되었습니다.");
    const bytes = await handle.readFile();
    if (bytes.length > maximum) throw new Error("자료 파일이 허용 크기를 초과했습니다.");
    return bytes;
  } finally { await handle.close(); }
}
async function writeExclusive(filename: string, bytes: Uint8Array, guard: () => void): Promise<{ino: number; dev: number}> {
  guard(); await assertDirectory(path.dirname(filename)); guard();
  const handle = await open(filename, "wx", 0o600), identity = await handle.stat();
  try { guard(); await handle.writeFile(bytes); await handle.sync(); guard(); return {ino: identity.ino, dev: identity.dev}; }
  catch (error) {
    const current = await statMaybe(filename);
    if (current?.ino === identity.ino && current.dev === identity.dev) await unlink(filename).catch(() => undefined);
    throw error;
  } finally { await handle.close(); }
}
async function checkOutsideAssignments(root: string, assignments: readonly string[]): Promise<void> {
  for (const assignment of assignments) {
    let actual: string;
    try { actual = await realpath(assignment); } catch (error) { if (missing(error)) continue; throw error; }
    if (inside(actual, root)) throw new Error("강의자료는 과제 코드 폴더와 분리된 폴더에 저장하세요.");
  }
}
async function cacheRoot(options: ContentSyncOptions): Promise<string> {
  parseContentContext(options.context);
  const origin = new URL(options.origin);
  if (!["http:", "https:"].includes(origin.protocol) || origin.username || origin.password) throw invalid();
  // No local alias, student number, URL credentials, or server-provided filesystem paths enter the scope.
  // Check the planned location before creating any directory inside an assignment.
  let ancestor = path.resolve(options.root);
  const suffix: string[] = [];
  while (!await statMaybe(ancestor)) { suffix.unshift(path.basename(ancestor)); ancestor = path.dirname(ancestor); }
  const planned = path.join(await realpath(ancestor), ...suffix);
  await checkOutsideAssignments(planned, options.assignmentDirectories ?? []);
  const root = await privateRoot(options.root, options.assertCurrent);
  await checkOutsideAssignments(root, options.assignmentDirectories ?? []);
  return childDirectory(root, `.content/${contentSha256(options.origin)}/${options.context.subject_scope}/${options.context.course_key}`, options.assertCurrent);
}
function verifyBytes(bytes: Uint8Array, file: ContentFile): void {
  if (bytes.byteLength !== file.size_bytes || contentSha256(bytes) !== file.sha256) throw new Error("콘텐츠 파일의 크기 또는 SHA-256이 다릅니다. 기존 자료는 변경하지 않았습니다.");
}
async function cachedObject(objects: string, file: ContentFile): Promise<Buffer | undefined> {
  await assertDirectory(objects);
  const filename = path.join(objects, file.sha256);
  if (!await statMaybe(filename)) return undefined;
  const bytes = await readPlain(filename, MAX_CONTENT_FILE_BYTES);
  return bytes.byteLength === file.size_bytes && contentSha256(bytes) === file.sha256 ? bytes : undefined;
}

/** A crashed local extension host cannot leave a permanent lock; never steal an active/foreign lock. */
async function acquireLock(filename: string, guard: () => void): Promise<{ino: number; dev: number}> {
  const owner = Buffer.from(JSON.stringify({pid: process.pid, hostname: hostname(), nonce: randomUUID()}));
  for (let attempt = 0; attempt < 3; attempt++) {
    try { return await writeExclusive(filename, owner, guard); }
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "EEXIST") throw error;
      const identity = await lstat(filename);
      let stale = false;
      try {
        const previous = JSON.parse((await readPlain(filename, 4096)).toString("utf8")) as Record<string, unknown>;
        if (previous.hostname === hostname() && Number.isSafeInteger(previous.pid) && Number(previous.pid) > 0 && typeof previous.nonce === "string") {
          try { process.kill(Number(previous.pid), 0); }
          catch (probe) { stale = (probe as NodeJS.ErrnoException).code === "ESRCH"; }
        }
      } catch { /* Corrupt or foreign locks require deliberate operator recovery. */ }
      if (!stale) throw error;
      guard();
      const current = await lstat(filename);
      if (current.ino !== identity.ino || current.dev !== identity.dev || !current.isFile() || current.nlink !== 1) throw error;
      await unlink(filename);
    }
  }
  throw new Error("자료 동기화 잠금이 계속 변경되고 있습니다. 다른 IDE 창의 작업이 끝난 뒤 다시 시도하세요.");
}

/** A snapshot is a validated manifest referencing immutable blobs, never links to editable answers. */
export async function syncContent(source: ContentSource, manifest: ContentManifest, options: ContentSyncOptions): Promise<ContentSyncResult> {
  parseContentManifest(manifest, options.context);
  const assertCurrent = options.assertCurrent;
  const guard = () => { if (options.signal?.aborted) throw new RequestCancelledError(); assertCurrent(); };
  options = {...options, assertCurrent: guard};
  guard();
  const root = await cacheRoot(options);
  const objects = await childDirectory(root, "objects", guard);
  const snapshots = await childDirectory(root, "snapshots", guard);
  const staging = await childDirectory(root, "staging", guard);
  const currents = await childDirectory(root, "current", guard);
  const lock = path.join(root, ".sync.lock");
  const lockIdentity = await acquireLock(lock, guard);
  let downloadedFiles = 0, reusedFiles = 0, downloadedBytes = 0;
  try {
    // After acquiring the cross-process lock, remnants of an interrupted writer are no longer live.
    // Only our UUID-named ordinary staging files are reclaimed; no recursive deletion is used.
    for (const name of await readdir(staging)) {
      guard(); await assertDirectory(staging);
      if (!/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(name)) {
        throw new Error("임시 콘텐츠 폴더에 알 수 없는 파일이 있습니다. 교수자에게 캐시 확인을 요청하세요.");
      }
      const filename = path.join(staging, name), info = await lstat(filename);
      if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw new Error("안전하지 않은 임시 콘텐츠 파일을 발견했습니다.");
      guard(); await unlink(filename);
    }
    let cacheBytes = 0;
    for (const name of await readdir(objects)) {
      if (!hash(name)) throw new Error("캐시 저장소에 알 수 없는 파일이 있습니다.");
      const info = await lstat(path.join(objects, name));
      if (!info.isFile() || info.nlink !== 1) throw new Error("안전하지 않은 캐시 파일을 발견했습니다.");
      cacheBytes += info.size;
    }
    for (const directory of [snapshots, currents]) for (const name of await readdir(directory)) {
      guard();
      if (!(directory === snapshots ? /^cm_[a-f0-9]{64}\.json$/ : /^cc_[a-f0-9]{32}\.json$/).test(name)) throw new Error("캐시 메타데이터에 알 수 없는 파일이 있습니다.");
      const info = await lstat(path.join(directory, name));
      if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > MAX_MANIFEST_BYTES) throw new Error("캐시 메타데이터가 올바르지 않습니다.");
      cacheBytes += info.size;
    }
    // Reserve both the immutable manifest and replacement pointer (conservative for repeated sync).
    cacheBytes += Buffer.byteLength(contentCanonicalJson(manifest)) * 2;
    if (cacheBytes > MAX_CONTENT_CACHE_BYTES) throw new Error("자료 캐시의 512 MiB 한도에 도달했습니다. 교수자에게 캐시 정리를 문의하세요.");
    const done = new Set<string>();
    for (const file of manifest.files) {
      guard();
      if (done.has(file.sha256) || await cachedObject(objects, file)) { reusedFiles++; }
      else {
        if (cacheBytes + file.size_bytes > MAX_CONTENT_CACHE_BYTES) throw new Error("자료 캐시의 512 MiB 한도에 도달했습니다. 교수자에게 캐시 정리를 문의하세요. 기존 자료는 유지됩니다.");
        const bytes = await source.object(manifest, file, options.signal);
        guard(); verifyBytes(bytes, file);
        const temporary = path.join(staging, randomUUID());
        await writeExclusive(temporary, bytes, guard);
        try {
          guard(); await assertDirectory(objects); await assertDirectory(staging);
          const old = await statMaybe(path.join(objects, file.sha256));
          if (old && (!old.isFile() || old.isSymbolicLink() || old.nlink !== 1)) throw new Error("안전하지 않은 캐시 파일을 덮어쓸 수 없습니다.");
          guard(); await rename(temporary, path.join(objects, file.sha256));
          cacheBytes += file.size_bytes - (old?.size ?? 0);
        } finally { await unlink(temporary).catch(() => undefined); }
        downloadedFiles++; downloadedBytes += file.size_bytes;
      }
      done.add(file.sha256);
      options.onProgress?.(downloadedFiles + reusedFiles, manifest.files.length);
    }
    guard(); await source.verify(manifest, options.signal); guard();
    const currentFile = path.join(currents, `${manifest.collection_id}.json`);
    if (await statMaybe(currentFile)) {
      const current = JSON.parse((await readPlain(currentFile, MAX_MANIFEST_BYTES)).toString("utf8")) as Record<string, unknown>;
      if (Number(current.generation) > manifest.generation ||
          current.generation === manifest.generation && current.manifest_sha256 !== manifest.manifest_sha256) {
        throw new Error("더 최신인 자료가 캐시에 있습니다. 목록을 새로 확인하세요.");
      }
    }
    const serialized = Buffer.from(contentCanonicalJson(manifest)), snapshot = path.join(snapshots, `${manifest.manifest_id}.json`);
    if (await statMaybe(snapshot)) {
      const existing = await readPlain(snapshot, MAX_MANIFEST_BYTES);
      if (!existing.equals(serialized)) throw new Error("기존 콘텐츠 스냅샷이 손상되었습니다. 교수자에게 문의하세요.");
    } else await writeExclusive(snapshot, serialized, guard);
    const pending = path.join(staging, randomUUID());
    await writeExclusive(pending, serialized, guard);
    try { guard(); await assertDirectory(currents); await assertDirectory(staging); guard(); await rename(pending, currentFile); }
    finally { await unlink(pending).catch(() => undefined); }
    return {manifest, downloadedFiles, reusedFiles, downloadedBytes, cacheDirectory: root};
  } finally {
    const info = await statMaybe(lock);
    if (info?.ino === lockIdentity.ino && info.dev === lockIdentity.dev) await unlink(lock).catch(() => undefined);
  }
}

/** Online verification is mandatory in this MVP; local bytes are not DRM or an OS security boundary. */
export async function exportContent(source: ContentSource, manifest: ContentManifest, options: ContentSyncOptions, destination: string): Promise<string> {
  parseContentManifest(manifest, options.context);
  const assertCurrent = options.assertCurrent;
  const guard = () => { if (options.signal?.aborted) throw new RequestCancelledError(); assertCurrent(); };
  options = {...options, assertCurrent: guard};
  guard(); await source.verify(manifest, options.signal); guard();
  const root = await cacheRoot(options), objects = path.join(root, "objects");
  const parentInfo = await lstat(destination);
  if (!parentInfo.isDirectory() || parentInfo.isSymbolicLink()) throw new Error("자료를 저장할 일반 폴더를 선택하세요.");
  const parent = await realpath(destination);
  await checkOutsideAssignments(parent, [...(options.assignmentDirectories ?? []), ...(options.excludedExportDirectories ?? [])]);
  const managed = await realpath(options.root);
  if (inside(path.join(managed, ".content"), parent)) throw new Error("내부 캐시 밖의 자료 저장 폴더를 선택하세요.");
  const output = path.join(parent, `${manifest.collection_id}-v${manifest.generation}-${randomUUID().slice(0, 8)}`);
  guard(); await mkdir(output, {mode: 0o700});
  const outputIdentity = await lstat(output), files: {filename: string; ino: number; dev: number}[] = [], directories = new Set<string>();
  try {
    for (const file of manifest.files) {
      guard(); await assertDirectory(output);
      const bytes = await cachedObject(objects, file);
      if (!bytes) throw new Error("캐시 파일이 없거나 손상되었습니다. 먼저 변경분을 다시 받으세요.");
      const relativeParent = path.posix.dirname(file.path);
      if (relativeParent !== ".") {
        const parts = relativeParent.split("/");
        for (let i = 1; i <= parts.length; i++) directories.add(path.join(output, ...parts.slice(0, i)));
        await childDirectory(output, relativeParent, guard);
      }
      const filename = path.join(output, ...file.path.split("/"));
      const identity = await writeExclusive(filename, bytes, guard); files.push({filename, ...identity});
    }
    guard(); await source.verify(manifest, options.signal); guard();
    return output;
  } catch (error) {
    // Only files created by this operation are removed; never recursively delete an arbitrary selection.
    const current = await statMaybe(output);
    if (current?.ino === outputIdentity.ino && current.dev === outputIdentity.dev && current.isDirectory()) {
      for (const created of files.reverse()) {
        const existing = await statMaybe(created.filename);
        if (existing?.ino === created.ino && existing.dev === created.dev) await unlink(created.filename).catch(() => undefined);
      }
      for (const directory of [...directories].sort((a, b) => b.length - a.length)) await rmdir(directory).catch(() => undefined);
      await rmdir(output).catch(() => undefined);
    }
    throw error;
  }
}

export function contentErrorMessage(error: unknown): string {
  const value = error as {status?: number; code?: string};
  if (value?.status === 409) return "교수자가 자료 또는 배포 권한을 변경했습니다. 목록을 다시 확인하세요. 이전 캐시와 내보낸 파일은 변경하지 않았습니다.";
  if (value?.status === 401) return "학생 인증이 만료되었습니다. 새 수령 코드로 연결한 뒤 다시 시도하세요.";
  if (value?.status === 403) return "배포 기간이 종료되었거나 수강 권한이 없습니다. 교수자에게 문의하세요.";
  if (value?.status === 404 && value.code !== "content_unsupported") return "배포가 중단되었거나 현재 학생에게 제공되지 않는 자료입니다.";
  if (value?.code === "EEXIST") return "자료 동기화가 이미 진행 중이거나 이전 실행의 잠금이 남아 있습니다. 다른 IDE 작업을 마친 뒤 다시 시도하세요. 계속되면 교수자에게 캐시 확인을 요청하세요.";
  if (value?.code === "ENOSPC") return "저장 공간이 부족합니다. 여유 공간을 확보하세요. 이전 캐시는 유지됩니다.";
  if (["EPERM", "EACCES", "EROFS"].includes(value?.code ?? "")) return "자료 폴더에 저장할 권한이 없습니다. 본인의 쓰기 가능한 폴더를 선택하세요.";
  return error instanceof Error ? error.message : "자료 동기화에 실패했습니다. 서버 연결을 확인하세요.";
}
