import assert from "node:assert/strict";
import test from "node:test";
import { createHash } from "node:crypto";
import { mkdtemp, mkdir, readFile, readdir, rm, symlink, writeFile } from "node:fs/promises";
import * as path from "node:path";
import { tmpdir } from "node:os";
import { MAX_STUDENT_MATERIAL_BYTES, materialDirectory, materialErrorMessage, parseStudentMaterials,
  saveStudentMaterial, studentMaterialFilename, verifyStudentMaterial } from "../studentMaterials";
import { ApiError, AutogradeClient, HttpTransport, type ApiTransport, type RequestOptions, type SessionTokenStore } from "../api";

const pdf = Buffer.from("%PDF-1.7\nSynthetic student material\n%%EOF");
const metadata = {material_id: "mat_test", course_key: "come2201", title: "개인 실습 자료", instructions: "직접 저장해서 확인하세요.",
  format: "pdf", size_bytes: pdf.length, sha256: createHash("sha256").update(pdf).digest("hex"), file_revision: 2, edition: 1,
  availability: "open", opens_at: "2026-10-23T00:00:00Z", closes_at: "2026-12-23T00:00:00Z", file_updated: true};
const catalog = () => ({course_key: "come2201", materials: [{...metadata}], next_cursor: null});
const material = () => parseStudentMaterials(catalog()).materials[0]!;

test("material parser scopes every item and ignores server paths and download URLs", () => {
  const parsed = parseStudentMaterials({...catalog(), materials: [{...metadata, filename: "../../evil.exe", download_url: "https://evil.invalid/steal"}]});
  assert.equal(parsed.courseKey, "come2201");
  assert.equal(studentMaterialFilename(parsed.materials[0]!), "come2201-mat_test-v2.pdf");
  assert.deepEqual(parseStudentMaterials({course_key: "come2201", materials: [], next_cursor: null}).materials, []);
  for (const patch of [{course_key: "other"}, {material_id: "../../a"}, {size_bytes: MAX_STUDENT_MATERIAL_BYTES + 1},
    {sha256: "bad"}, {size_bytes: 0}, {format: "exe"}, {file_revision: 0}, {edition: 0}, {availability: "new"},
    {closes_at: "2026-10-22T00:00:00Z"}, {opens_at: "2026-10-23T00:00:00"}, {file_updated: 1}]) {
    assert.throws(() => parseStudentMaterials({...catalog(), materials: [{...metadata, ...patch}]}), /형식/);
  }
  assert.throws(() => parseStudentMaterials({...catalog(), materials: [metadata, metadata]}), /형식/);
  assert.throws(() => parseStudentMaterials({...catalog(), materials: Array(1001).fill(metadata)}), /형식/);
  assert.throws(() => parseStudentMaterials({...catalog(), materials: Array(31).fill(metadata)}), /형식/);
  for (const next_cursor of [undefined, "", "https://evil.invalid", "a".repeat(1024), 1]) {
    assert.throws(() => parseStudentMaterials({...catalog(), next_cursor}), /형식/);
  }
});

test("material verification rejects byte count, digest and file signature mismatches", () => {
  verifyStudentMaterial(pdf, material());
  assert.throws(() => verifyStudentMaterial(pdf.subarray(1), material()), /크기/);
  assert.throws(() => verifyStudentMaterial(Buffer.alloc(pdf.length), material()), /SHA-256/);
  assert.throws(() => verifyStudentMaterial(pdf, {...material(), format: "zip"}), /파일 형식/);
  const zip = Buffer.from([0x50, 0x4b, 0x03, 0x04, 1]);
  verifyStudentMaterial(zip, {...material(), format: "zip", sizeBytes: zip.length, sha256: createHash("sha256").update(zip).digest("hex")});
});

test("material saves are exclusive, stay separate from assignment folders and never extract ZIP", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-material-"));
  try {
    const code = path.join(root, "code"), separate = path.join(root, "materials");
    await mkdir(code); await mkdir(separate); await mkdir(path.join(code, "nested"));
    await assert.rejects(materialDirectory(code, [code]), /코드 폴더/);
    await assert.rejects(materialDirectory(path.join(code, "nested"), [code]), /코드 폴더/);
    await symlink(code, path.join(root, "alias"));
    await assert.rejects(materialDirectory(path.join(root, "alias"), [code]), /코드 폴더/);
    const saved = await saveStudentMaterial(separate, material(), pdf, () => {}, [code]);
    assert.deepEqual(await readFile(saved), pdf);
    await assert.rejects(saveStudentMaterial(separate, material(), pdf, () => {}, [code]), {code: "EEXIST"});
    assert.deepEqual(await readFile(saved), pdf);
    const zip = Buffer.from([0x50, 0x4b, 0x03, 0x04, 1]);
    const zipped = {...material(), format: "zip" as const, sizeBytes: zip.length, sha256: createHash("sha256").update(zip).digest("hex")};
    await saveStudentMaterial(separate, zipped, zip, () => {}, [code]);
    assert.equal((await readdir(separate)).length, 2);
  } finally { await rm(root, {recursive: true, force: true}); }
});

test("stale sessions do not leave completed or partial files and cannot overwrite a symlink", async () => {
  const root = await mkdtemp(path.join(tmpdir(), "autograde-material-"));
  try {
    let calls = 0;
    await assert.rejects(saveStudentMaterial(root, material(), pdf, () => { if (++calls === 3) throw new Error("stale"); }), /stale/);
    assert.deepEqual(await readdir(root), []);
    const protectedFile = path.join(root, "existing");
    await writeFile(protectedFile, "keep");
    await symlink(protectedFile, path.join(root, studentMaterialFilename(material())));
    await assert.rejects(saveStudentMaterial(root, material(), pdf, () => {}), {code: "EEXIST"});
    assert.equal(await readFile(protectedFile, "utf8"), "keep");
  } finally { await rm(root, {recursive: true, force: true}); }
});

class Tokens implements SessionTokenStore {
  public generation = 1;
  public getSessionGeneration() { return this.generation; }
  public async hasSession() { return true; }
  public async getAccessToken() { return "test-token"; }
  public async storeSession() { this.generation++; }
  public async clear() { this.generation++; }
}
class Transport implements ApiTransport {
  public base = "https://test.invalid";
  public calls: {endpoint: string; init: RequestInit; token?: string; options?: RequestOptions}[] = [];
  public outcome: unknown = catalog();
  public action?: () => void;
  public getBaseUrl() { return this.base; }
  public async request<T>(endpoint: string, init: RequestInit, token?: string, options?: RequestOptions): Promise<T> {
    this.calls.push({endpoint, init, token, options}); this.action?.();
    if (this.outcome instanceof Error) throw this.outcome;
    return this.outcome as T;
  }
  public requestBytes(endpoint: string, init: RequestInit, token?: string, options?: RequestOptions) {
    return this.request<Uint8Array>(endpoint, init, token, options);
  }
}

test("material APIs reuse authenticated origin binding, SHA pin, size limit and precise MIME", async () => {
  const transport = new Transport();
  const client = new AutogradeClient(transport, new Tokens());
  assert.equal((await client.getStudentMaterials()).materials.length, 1);
  transport.outcome = pdf;
  assert.deepEqual(await client.getStudentMaterialFile(material()), pdf);
  assert.equal(transport.calls[0]?.endpoint, "/v1/materials");
  const download = transport.calls[1]!;
  assert.equal(download.endpoint, "/v1/materials/mat_test/download");
  assert.equal(new Headers(download.init.headers).get("X-Autograde-Expected-SHA256"), metadata.sha256);
  assert.equal(download.options?.maxResponseBytes, MAX_STUDENT_MATERIAL_BYTES);
  assert.deepEqual(download.options?.acceptedContentTypes, ["application/pdf"]);
  assert.ok(transport.calls.every(call => call.token === "test-token" && call.options?.expectedBaseUrl === transport.base));
  transport.outcome = new ApiError("missing", 404);
  await assert.rejects(client.getStudentMaterials(), (error: unknown) => error instanceof ApiError && error.code === "materials_unsupported");
  await assert.rejects(client.getStudentMaterialFile(material()), (error: unknown) => error instanceof ApiError && error.code !== "materials_unsupported");
});

test("late metadata and file responses are discarded on identity or server changes", async () => {
  for (const file of [false, true]) for (const change of ["session", "server"]) {
    const transport = new Transport(), tokens = new Tokens(), client = new AutogradeClient(transport, tokens);
    transport.outcome = file ? pdf : catalog();
    transport.action = () => { if (change === "session") tokens.generation++; else transport.base = "https://other.invalid"; };
    await assert.rejects(file ? client.getStudentMaterialFile(material()) : client.getStudentMaterials(),
      (error: unknown) => error instanceof ApiError && error.code === "login_required");
  }
});

const pageCursor = (index: number) => `${"a".repeat(64)}.mat_${String(index).padStart(32, "0")}`;
const page = (start: number, count: number, more = false) => ({...catalog(),
  materials: Array.from({length: count}, (_, index) => ({...metadata, material_id: `mat_${start + index}`})),
  next_cursor: more ? pageCursor(start + count) : null});

test("materials fetch all bounded pages without exposing partial lists", async () => {
  const transport = new Transport();
  const pages = [page(0, 30, true), page(30, 30, true), page(60, 10)];
  transport.action = () => {transport.outcome = pages[transport.calls.length - 1];};
  const result = await new AutogradeClient(transport, new Tokens()).getStudentMaterials();
  assert.equal(result.materials.length, 70);
  assert.equal(new Set(result.materials.map(row => row.id)).size, 70);
  assert.equal(transport.calls.length, 3);
  assert.equal(transport.calls[1]?.endpoint, `/v1/materials?cursor=${pageCursor(30)}`);
  assert.ok(transport.calls.every(call => call.options?.expectedBaseUrl === transport.base));
});

test("material page loops, duplicate IDs, course changes and empty continuation pages fail closed", async () => {
  for (const scenario of ["repeated", "duplicate", "course", "empty", "changed"]) {
    const transport = new Transport();
    const second = scenario === "repeated" ? {...page(30, 30, true), next_cursor: pageCursor(30)}
      : scenario === "duplicate" ? page(0, 1)
      : scenario === "course" ? {...page(30, 1), course_key: "other", materials: [{...metadata, material_id: "mat_other", course_key: "other"}]}
      : scenario === "empty" ? page(30, 0, true) : new ApiError("changed", 409, "material_changed");
    transport.action = () => {transport.outcome = transport.calls.length === 1 ? page(0, 30, true) : second;};
    await assert.rejects(new AutogradeClient(transport, new Tokens()).getStudentMaterials(),
      (error: unknown) => error instanceof ApiError && error.code === (scenario === "changed" ? "material_changed" : "invalid_response"));
    assert.equal(transport.calls.length, 2);
  }
});

test("material pagination has a 1000 item cap and a single session guard across requests", async () => {
  const transport = new Transport();
  transport.action = () => {transport.outcome = page((transport.calls.length - 1) * 30, 30, true);};
  await assert.rejects(new AutogradeClient(transport, new Tokens()).getStudentMaterials(),
    (error: unknown) => error instanceof ApiError && error.code === "material_catalog_limit" && /1000/.test(error.message));
  assert.equal(transport.calls.length, 34);
  for (const change of ["session", "server", "cancel"]) {
    const transport = new Transport(), tokens = new Tokens(), cancellation = new AbortController();
    transport.action = () => {
      transport.outcome = page((transport.calls.length - 1) * 30, 30, transport.calls.length < 2 || change !== "cancel");
      if (transport.calls.length === 2) {
        if (change === "session") tokens.generation++;
        else if (change === "server") transport.base = "https://other.invalid";
        else cancellation.abort();
      }
    };
    await assert.rejects(new AutogradeClient(transport, tokens).getStudentMaterials(cancellation.signal));
    assert.equal(transport.calls.length, 2);
  }
});

test("real transport rejects incorrect MIME before writing a material", async () => {
  const transport = new HttpTransport(() => "https://test.invalid", async () => new Response(pdf,
    {headers: {"Content-Type": "text/html"}}));
  await assert.rejects(new AutogradeClient(transport, new Tokens()).getStudentMaterialFile(material()),
    (error: unknown) => error instanceof ApiError && error.code === "invalid_response");
});

test("material error guidance distinguishes login, old server, changed revision, access and disk errors", () => {
  assert.match(materialErrorMessage(new ApiError("", 401)), /로그인/);
  assert.match(materialErrorMessage(new ApiError("", 404, "materials_unsupported")), /지원하지/);
  assert.match(materialErrorMessage(new ApiError("", 409, "material_changed")), /최신 목록/);
  assert.match(materialErrorMessage(new ApiError("", 403)), /배포 기간/);
  assert.match(materialErrorMessage(new ApiError("", 404)), /철회/);
  assert.match(materialErrorMessage({code: "EEXIST"}), /덮어쓰지/);
  assert.match(materialErrorMessage({code: "ENOSPC"}), /공간/);
});
