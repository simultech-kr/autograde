import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, link, writeFile } from "node:fs/promises";
import { tmpdir, hostname } from "node:os";
import * as path from "node:path";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { randomUUID } from "node:crypto";
import { ContentApi } from "../contentApi";
import { ApiError, AutogradeClient, RequestCancelledError, type ApiTransport, type RequestOptions, type SessionTokenStore } from "../api";
import { contentCanonicalJson, contentSha256, parseContentContext, parseContentCatalog, parseContentManifest, validContentPath,
  syncContent, exportContent, contentErrorMessage, type ContentContext, type ContentManifest, type ContentSource, type ContentSyncOptions } from "../contentSync";

const context: ContentContext = {schema: "autograde.content.context.v1", course_key: "come2201", subject_scope: "a".repeat(64),
  limits: {max_file_bytes: 26214400, max_release_bytes: 104857600, max_files: 1000, max_manifest_bytes: 524288}};
const collectionId = `cc_${"1".repeat(32)}`;
function manifest(files: Record<string, string>, generation = 1, scope = context): ContentManifest {
  const raw = {schema: "autograde.content.manifest.v1", collection_id: collectionId, release_id: `cr_${String(generation).padStart(32, "0")}`,
    generation, policy_revision: generation, title: "강의자료", change_note: "변경 안내", instructions: "복사해서 사용하세요.",
    opens_at: "2026-10-24T00:00:00Z", closes_at: null, course_key: scope.course_key, subject_scope: scope.subject_scope,
    files: Object.entries(files).sort(([a], [b]) => Buffer.compare(Buffer.from(a), Buffer.from(b))).map(([filename, data]) => ({
      path: filename, sha256: contentSha256(data), size_bytes: Buffer.byteLength(data), media_type: "text/plain",
    }))};
  const digest = contentSha256(contentCanonicalJson(raw));
  return parseContentManifest({...raw, manifest_id: `cm_${digest}`, manifest_sha256: digest}, scope);
}
class Source implements ContentSource {
  public reads: string[] = [];
  public verifies = 0;
  public data = new Map<string, Buffer>();
  public beforeObject = () => {};
  public beforeVerify = () => {};
  public add(files: Record<string, string>) {for (const data of Object.values(files)) this.data.set(contentSha256(data), Buffer.from(data));}
  public async object(_manifest: ContentManifest, file: {sha256: string}) {
    this.reads.push(file.sha256); this.beforeObject();
    const bytes = this.data.get(file.sha256); if (!bytes) throw new Error("network failed"); return bytes;
  }
  public async verify() {this.verifies++; this.beforeVerify();}
}
async function fixture() {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-content-sync-"));
  const source = new Source();
  const options: ContentSyncOptions = {root: path.join(root, "materials"), origin: "https://test.invalid:20000", context, assertCurrent: () => {}};
  return {root, source, options, close: () => rm(root, {recursive: true, force: true})};
}

test("canonical manifest hashes include unicode and policy fields; paths and scope fail closed", () => {
  const good = manifest({"readme.md": "강의자료", "src/main.c": "int main() {}"});
  assert.equal(parseContentContext(context), context);
  assert.equal(parseContentCatalog({course_key: context.course_key, subject_scope: context.subject_scope, collections: [good]}, context).collections.length, 1);
  assert.equal(parseContentManifest(good, context), good);
  assert.throws(() => parseContentManifest({...good, title: "changed"}, context), /무결성/);
  assert.throws(() => parseContentManifest({...good, subject_scope: "b".repeat(64)}, context), /무결성/);
  assert.throws(() => parseContentContext({...context, limits: {...context.limits, max_files: 1001}}), /무결성/);
  assert.throws(() => parseContentCatalog({course_key: context.course_key, subject_scope: context.subject_scope, collections: [good, good]}, context));
  for (const filename of ["../x.txt", "/x.txt", "a\\x.txt", ".git/config", ".vscode/tasks.json", "a/CON.txt", "a./b.txt", "NUL.md",
    "a/../b.txt", "a:b.txt", "a\u0000.txt", "a//b.txt", "a. ", "test.exe", "readme", "e\u0301.txt", "\ud800.txt", `${"a/".repeat(12)}b.txt`, `${"a".repeat(237)}.txt`]) {
    assert.equal(validContentPath(filename), false, filename);
    assert.throws(() => manifest({[filename]: "x"}), /무결성/);
  }
  for (const filename of ["강의/readme.md", "chapter01/main.cpp", "empty.txt", "handout.PDF"]) assert.equal(validContentPath(filename), true);
  assert.throws(() => manifest({"A.txt": "1", "a.txt": "2"}));
  assert.throws(() => manifest({"straße.txt": "1", "STRASSE.txt": "2"}));
  assert.throws(() => manifest({"a.txt": "1", "A.txt/b.txt": "2"}));
});

test("first sync downloads, repeat uses zero network objects, one changed file downloads once, rename reuses blob", async () => {
  const f = await fixture();
  try {
    const first = {"readme.md": "첫 자료", "sample/main.c": "int main() {}", "empty.txt": ""}; f.source.add(first);
    const release1 = manifest(first);
    const initial = await syncContent(f.source, release1, f.options);
    assert.equal(initial.downloadedFiles, 3); assert.equal(initial.reusedFiles, 0);
    const repeated = await syncContent(f.source, release1, f.options);
    assert.equal(repeated.downloadedFiles, 0); assert.equal(repeated.reusedFiles, 3); assert.equal(f.source.reads.length, 3);
    const updated = {...first, "readme.md": "수정 자료"}; f.source.add(updated);
    const second = await syncContent(f.source, manifest(updated, 2), f.options);
    assert.equal(second.downloadedFiles, 1); assert.equal(second.reusedFiles, 2);
    const renamed = {"guide.md": "수정 자료", "sample/main.c": first["sample/main.c"], "empty.txt": ""};
    const third = await syncContent(f.source, manifest(renamed, 3), f.options);
    assert.equal(third.downloadedFiles, 0); assert.equal(third.reusedFiles, 3); assert.equal(f.source.verifies, 4);
    assert.equal(JSON.parse(await readFile(path.join(third.cacheDirectory, "current", `${collectionId}.json`), "utf8")).generation, 3);
    assert.equal((await readdir(path.join(third.cacheDirectory, "snapshots"))).length, 3);
    assert.deepEqual(await readdir(path.join(third.cacheDirectory, "staging")), []);
    await assert.rejects(readFile(path.join(third.cacheDirectory, ".sync.lock")), {code: "ENOENT"});
  } finally { await f.close(); }
});

test("corrupt object is fetched again, invalid received bytes never activate, and previous pointer survives failed final verify", async () => {
  const f = await fixture();
  try {
    const first = {"readme.md": "first"}; f.source.add(first);
    const release = manifest(first), result = await syncContent(f.source, release, f.options);
    await writeFile(path.join(result.cacheDirectory, "objects", release.files[0]!.sha256), "bad");
    assert.equal((await syncContent(f.source, release, f.options)).downloadedFiles, 1);
    const second = {"readme.md": "second"}; const release2 = manifest(second, 2); f.source.add(second);
    f.source.beforeVerify = () => {throw new ApiError("changed", 409);};
    await assert.rejects(syncContent(f.source, release2, f.options), (error: unknown) => error instanceof ApiError && error.status === 409);
    assert.equal(JSON.parse(await readFile(path.join(result.cacheDirectory, "current", `${collectionId}.json`), "utf8")).generation, 1);
    f.source.beforeVerify = () => {};
    const recovery = await syncContent(f.source, release2, f.options);
    assert.equal(recovery.downloadedFiles, 0);
    const release3 = manifest({"readme.md": "third"}, 3);
    f.source.data.set(release3.files[0]!.sha256, Buffer.from("wrong"));
    await assert.rejects(syncContent(f.source, release3, f.options), /SHA-256/);
    assert.equal(JSON.parse(await readFile(path.join(result.cacheDirectory, "current", `${collectionId}.json`), "utf8")).generation, 2);
  } finally { await f.close(); }
});

test("partial network failure preserves old snapshot, completed blobs can be reused on retry", async () => {
  const f = await fixture();
  try {
    f.source.add({"a.txt": "old"});
    const old = await syncContent(f.source, manifest({"a.txt": "old"}), f.options);
    const next = {"a.txt": "new", "b.txt": "second"}, release = manifest(next, 2);
    f.source.add({"a.txt": "new"});
    await assert.rejects(syncContent(f.source, release, f.options), /network failed/);
    assert.equal(JSON.parse(await readFile(path.join(old.cacheDirectory, "current", `${collectionId}.json`), "utf8")).generation, 1);
    f.source.add(next);
    const retried = await syncContent(f.source, release, f.options);
    assert.equal(retried.downloadedFiles, 1); assert.equal(retried.reusedFiles, 1);
  } finally { await f.close(); }
});

test("cancellation and identity change after receipt do not activate or leak into another student's scope", async () => {
  const f = await fixture();
  try {
    const files = {"readme.md": "hello"}; f.source.add(files);
    let current = true;
    const options = {...f.options, assertCurrent: () => {if (!current) throw new RequestCancelledError();}};
    f.source.beforeObject = () => {current = false;};
    await assert.rejects(syncContent(f.source, manifest(files), options), RequestCancelledError);
    current = true; f.source.beforeObject = () => {};
    const first = await syncContent(f.source, manifest(files), options);
    const otherContext = {...context, subject_scope: "b".repeat(64)};
    const second = await syncContent(f.source, manifest(files, 1, otherContext), {...options, context: otherContext});
    assert.notEqual(first.cacheDirectory, second.cacheDirectory); assert.equal(second.downloadedFiles, 1);
    const controller = new AbortController(); controller.abort();
    await assert.rejects(syncContent(f.source, manifest(files), {...options, signal: controller.signal}), RequestCancelledError);
  } finally { await f.close(); }
});

test("export is a new independent copy, preserves answers and prior copies, and requires online current verification", async () => {
  const f = await fixture();
  try {
    const files = {"readme.md": "original", "src/main.c": "int main() {}"}; f.source.add(files); const release = manifest(files);
    const cached = await syncContent(f.source, release, f.options);
    const output = path.join(f.root, "exports"), answer = path.join(f.root, "answers"); await mkdir(output); await mkdir(answer);
    await writeFile(path.join(answer, "main.c"), "my answer");
    const options = {...f.options, assignmentDirectories: [answer]};
    const one = await exportContent(f.source, release, options, output);
    await writeFile(path.join(one, "readme.md"), "my notes");
    const two = await exportContent(f.source, release, options, output);
    assert.notEqual(one, two); assert.equal(await readFile(path.join(two, "readme.md"), "utf8"), "original");
    assert.equal(await readFile(path.join(one, "readme.md"), "utf8"), "my notes");
    assert.equal(await readFile(path.join(cached.cacheDirectory, "objects", contentSha256("original")), "utf8"), "original");
    await assert.rejects(exportContent(f.source, release, options, answer), /과제 코드/);
    await assert.rejects(exportContent(f.source, release, options, cached.cacheDirectory), /내부 캐시/);
    assert.equal(await readFile(path.join(answer, "main.c"), "utf8"), "my answer");
    f.source.beforeVerify = () => {throw new ApiError("expired", 403);};
    await assert.rejects(exportContent(f.source, release, options, output), ApiError);
    assert.equal((await readdir(output)).length, 2);
  } finally { await f.close(); }
});

test("export-only exclusions allow disposable cache receipt but reject exports anywhere in that managed session", async () => {
  const f = await fixture();
  try {
    const files = {"notes.md": "keep these notes"}; f.source.add(files); const release = manifest(files);
    const managed = path.join(f.root, "managed"), output = path.join(f.root, "take-home");
    await mkdir(output);
    const options = {...f.options, root: path.join(managed, "materials"), excludedExportDirectories: [managed]};
    assert.equal((await syncContent(f.source, release, options)).downloadedFiles, 1);
    await assert.rejects(exportContent(f.source, release, options, managed), /과제 코드/);
    await assert.rejects(exportContent(f.source, release, options, options.root), /과제 코드/);
    const exported = await exportContent(f.source, release, options, output);
    assert.equal(await readFile(path.join(exported, "notes.md"), "utf8"), "keep these notes");
  } finally {await f.close();}
});

test("cache root and objects reject symlinks and hardlinks and never create directories inside answers", async () => {
  const f = await fixture();
  try {
    const files = {"a.txt": "hello"}; f.source.add(files); const release = manifest(files);
    const answer = path.join(f.root, "answer"); await mkdir(answer);
    await assert.rejects(syncContent(f.source, release, {...f.options, root: path.join(answer, "new-cache"), assignmentDirectories: [answer]}), /과제 코드/);
    assert.deepEqual(await readdir(answer), []);
    const alias = path.join(f.root, "alias"); await symlink(answer, alias);
    await assert.rejects(syncContent(f.source, release, {...f.options, root: alias}), /심볼릭/);
    const cached = await syncContent(f.source, release, f.options);
    const object = path.join(cached.cacheDirectory, "objects", release.files[0]!.sha256);
    await link(object, path.join(f.root, "hardlink.txt"));
    await assert.rejects(syncContent(f.source, release, f.options), /안전하지 않은/);
    await rm(path.join(f.root, "hardlink.txt")); await rm(object); await writeFile(path.join(answer, "original.txt"), "safe");
    await symlink(path.join(answer, "original.txt"), object);
    await assert.rejects(syncContent(f.source, release, f.options), /안전하지 않은/);
    assert.equal(await readFile(path.join(answer, "original.txt"), "utf8"), "safe");
  } finally { await f.close(); }
});

test("withdrawal during export removes only this attempt's partial copy", async () => {
  const f = await fixture();
  try {
    const files = {"a.txt": "hello", "sub/b.txt": "world"}; f.source.add(files); const release = manifest(files);
    await syncContent(f.source, release, f.options);
    const output = path.join(f.root, "exports"); await mkdir(output); await writeFile(path.join(output, "keep.txt"), "keep");
    let verification = 0;
    f.source.beforeVerify = () => {if (++verification === 2) throw new ApiError("withdrawn", 409);};
    await assert.rejects(exportContent(f.source, release, f.options, output), ApiError);
    assert.deepEqual(await readdir(output), ["keep.txt"]);
  } finally { await f.close(); }
});

test("an exited local host lock is recovered but a live process lock is never stolen", async () => {
  const f = await fixture();
  try {
    const files = {"a.txt": "hello"}; f.source.add(files); const release = manifest(files);
    const cached = await syncContent(f.source, release, f.options), lock = path.join(cached.cacheDirectory, ".sync.lock");
    await writeFile(lock, JSON.stringify({hostname: hostname(), pid: process.pid, nonce: "live"}));
    await assert.rejects(syncContent(f.source, release, f.options), {code: "EEXIST"});
    const child = spawn(process.execPath, ["-e", ""], {stdio: "ignore"}); await once(child, "exit");
    await writeFile(lock, JSON.stringify({hostname: hostname(), pid: child.pid, nonce: "crashed"}));
    assert.equal((await syncContent(f.source, release, f.options)).downloadedFiles, 0);
    await assert.rejects(readFile(lock), {code: "ENOENT"});
  } finally { await f.close(); }
});

test("abandoned UUID staging files are reclaimed but unrecognized files are never deleted", async () => {
  const f = await fixture();
  try {
    const files = {"a.txt": "hello"}; f.source.add(files); const release = manifest(files);
    const cached = await syncContent(f.source, release, f.options), staging = path.join(cached.cacheDirectory, "staging");
    await writeFile(path.join(staging, randomUUID()), "abandoned partial bytes");
    const next = await syncContent(f.source, release, f.options);
    assert.equal(next.downloadedFiles, 0); assert.deepEqual(await readdir(staging), []);
    await writeFile(path.join(staging, "keep.txt"), "user file");
    await assert.rejects(syncContent(f.source, release, f.options), /알 수 없는/);
    assert.equal(await readFile(path.join(staging, "keep.txt"), "utf8"), "user file");
  } finally {await f.close();}
});

test("deleted remote files disappear from new exports without deleting previous user copies", async () => {
  const f = await fixture();
  try {
    const files = {"a.txt": "hello", "b.txt": "removed later"}; f.source.add(files); const first = manifest(files);
    await syncContent(f.source, first, f.options);
    const output = path.join(f.root, "exports"); await mkdir(output);
    const previous = await exportContent(f.source, first, f.options, output);
    const next = manifest({"a.txt": "hello"}, 2), result = await syncContent(f.source, next, f.options);
    assert.equal(result.downloadedFiles, 0);
    const latest = await exportContent(f.source, next, f.options, output);
    assert.deepEqual(await readdir(latest), ["a.txt"]); assert.deepEqual(await readdir(previous), ["a.txt", "b.txt"]);
  } finally {await f.close();}
});

class Tokens implements SessionTokenStore {
  public generation = 1; public refreshes = 0;
  public getSessionGeneration() {return this.generation;}
  public async hasSession() {return true;}
  public async getAccessToken(force = false) {if (force) this.refreshes++; return "memory-token";}
  public async storeSession() {this.generation++;}
  public async clear() {this.generation++;}
}
class Transport implements ApiTransport {
  public base = "https://test.invalid"; public calls: {endpoint: string; init: RequestInit; token?: string; options?: RequestOptions}[] = [];
  public payload: unknown = context; public failure?: Error; public action = () => {};
  public getBaseUrl() {return this.base;}
  public async request<T>(): Promise<T> {throw new Error("unbounded JSON transport must not be used");}
  public async requestBytes(endpoint: string, init: RequestInit, token?: string, options?: RequestOptions): Promise<Uint8Array> {
    this.calls.push({endpoint, init, token, options}); this.action(); if (this.failure) throw this.failure;
    return Buffer.isBuffer(this.payload) ? this.payload : Buffer.from(JSON.stringify(this.payload));
  }
}
test("content API binds origin/identity, caps JSON bodies, pins object hash and verify payload", async () => {
  const transport = new Transport(), tokens = new Tokens(), api = new ContentApi(new AutogradeClient(transport, tokens));
  assert.equal((await api.context()).course_key, context.course_key);
  const release = manifest({"a.txt": "hello"}); transport.payload = release;
  assert.equal((await api.manifest(release.release_id, context)).manifest_id, release.manifest_id);
  transport.payload = Buffer.from("hello"); await api.object(release, release.files[0]!);
  transport.payload = {verified: true, manifest_id: release.manifest_id}; await api.verify(release);
  const json = transport.calls[0]!, object = transport.calls[2]!, verify = transport.calls[3]!;
  assert.equal(json.options?.maxResponseBytes, 524288); assert.deepEqual(json.options?.acceptedContentTypes, ["application/json"]);
  assert.equal(new Headers(json.init.headers).get("Accept"), "application/json");
  assert.equal(new Headers(verify.init.headers).get("Content-Type"), "application/json");
  assert.equal(new Headers(object.init.headers).get("X-Autograde-Manifest-SHA256"), release.manifest_sha256);
  assert.equal(object.options?.maxResponseBytes, 5);
  assert.deepEqual(JSON.parse(String(verify.init.body)), {expected_manifest_id: release.manifest_id, manifest_sha256: release.manifest_sha256,
    generation: release.generation, policy_revision: release.policy_revision});
  assert.ok(transport.calls.every(call => call.options?.expectedBaseUrl === transport.base && call.token === "memory-token"));
  transport.payload = context; transport.action = () => {tokens.generation++;};
  await assert.rejects(api.context(), RequestCancelledError);
});

test("content API retries one unauthorized request, distinguishes unsupported server, and rejects unverifiable success", async () => {
  const transport = new Transport(), tokens = new Tokens(), api = new ContentApi(new AutogradeClient(transport, tokens));
  transport.action = () => {transport.failure = transport.calls.length === 1 ? new ApiError("expired", 401) : undefined;};
  await api.context(); assert.equal(tokens.refreshes, 1); assert.equal(transport.calls.length, 2);
  transport.action = () => {}; transport.failure = new ApiError("missing", 404);
  await assert.rejects(api.context(), (error: unknown) => error instanceof ApiError && error.code === "content_unsupported");
  transport.failure = undefined; transport.payload = {verified: true, manifest_id: "wrong"};
  await assert.rejects(api.verify(manifest({"a.txt": "hello"})), /최종 권한 검증/);
  assert.match(contentErrorMessage(new ApiError("changed", 409)), /이전 캐시/);
  assert.match(contentErrorMessage(new ApiError("expired", 403)), /배포 기간/);
});
