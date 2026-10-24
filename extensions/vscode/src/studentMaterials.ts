import { createHash } from "node:crypto";
import { lstat, open, realpath, unlink } from "node:fs/promises";
import * as path from "node:path";

export const MAX_STUDENT_MATERIAL_BYTES = 25 * 1024 * 1024;
export const MAX_STUDENT_MATERIAL_PAGE_SIZE = 30;
export const MAX_STUDENT_MATERIALS = 1000;

export interface StudentMaterial {
  readonly id: string;
  readonly courseKey: string;
  readonly title: string;
  readonly instructions: string;
  readonly format: "pdf" | "zip";
  readonly sizeBytes: number;
  readonly sha256: string;
  readonly fileRevision: number;
  readonly edition: number;
  readonly availability: "scheduled" | "open" | "closed";
  readonly opensAt: string;
  readonly closesAt: string;
  readonly updated: boolean;
}

export interface StudentMaterialsCatalog {
  readonly courseKey: string;
  readonly materials: readonly StudentMaterial[];
}

export interface StudentMaterialsPage extends StudentMaterialsCatalog {
  readonly nextCursor: string | null;
}

const record = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);
const identifier = (value: unknown): value is string =>
  typeof value === "string" && /^[A-Za-z0-9_-]{1,128}$/.test(value);
const date = (value: unknown): value is string =>
  typeof value === "string" && /(?:Z|[+-]\d{2}:\d{2})$/.test(value) && Number.isFinite(Date.parse(value));
const positive = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) > 0;

/** Parse only the current student's course-scoped metadata; never trust a download URL or filename. */
export function parseStudentMaterials(payload: unknown): StudentMaterialsPage {
  const invalid = () => new Error("배포 자료 목록의 형식이 올바르지 않습니다. 교수자에게 서버 확인을 요청하세요.");
  if (!record(payload) || !identifier(payload.course_key) || !Array.isArray(payload.materials) ||
      payload.materials.length > MAX_STUDENT_MATERIAL_PAGE_SIZE ||
      !(payload.next_cursor === null || typeof payload.next_cursor === "string" &&
        /^[a-f0-9]{64}\.mat_[a-f0-9]{32}$/.test(payload.next_cursor))) {
    throw invalid();
  }
  const courseKey = payload.course_key;
  const seen = new Set<string>();
  const materials = payload.materials.map((item: unknown): StudentMaterial => {
    if (!record(item) || !identifier(item.material_id) || seen.has(item.material_id) || item.course_key !== courseKey ||
        typeof item.title !== "string" || !item.title.trim() || item.title.length > 500 ||
        typeof item.instructions !== "string" || item.instructions.length > 10000 ||
        !["pdf", "zip"].includes(String(item.format)) ||
        !positive(item.size_bytes) || item.size_bytes > MAX_STUDENT_MATERIAL_BYTES ||
        typeof item.sha256 !== "string" || !/^[a-f0-9]{64}$/.test(item.sha256) ||
        !positive(item.file_revision) || !positive(item.edition) ||
        !["scheduled", "open", "closed"].includes(String(item.availability)) ||
        !date(item.opens_at) || !date(item.closes_at) || Date.parse(item.closes_at) <= Date.parse(item.opens_at) ||
        typeof item.file_updated !== "boolean") throw invalid();
    seen.add(item.material_id);
    return {id: item.material_id, courseKey, title: item.title, instructions: item.instructions,
      format: item.format as "pdf" | "zip", sizeBytes: item.size_bytes, sha256: item.sha256,
      fileRevision: item.file_revision, edition: item.edition, availability: item.availability as StudentMaterial["availability"],
      opensAt: item.opens_at, closesAt: item.closes_at, updated: item.file_updated};
  });
  return {courseKey, materials, nextCursor: payload.next_cursor};
}

export function studentMaterialFilename(material: StudentMaterial): string {
  // No student number, server-advertised path, title, URL, or executable suffix enters this filename.
  if (!identifier(material.id) || !identifier(material.courseKey) || !positive(material.fileRevision) ||
      !["pdf", "zip"].includes(material.format)) throw new Error("자료 파일 정보를 확인할 수 없습니다.");
  return `${material.courseKey}-${material.id}-v${material.fileRevision}.${material.format}`;
}

export function verifyStudentMaterial(bytes: Uint8Array, material: StudentMaterial): void {
  if (bytes.byteLength !== material.sizeBytes || bytes.byteLength < 1 || bytes.byteLength > MAX_STUDENT_MATERIAL_BYTES) {
    throw new Error("배포 자료의 크기가 일치하지 않습니다. 저장하지 않았습니다. 목록을 새로고침하세요.");
  }
  if (createHash("sha256").update(bytes).digest("hex") !== material.sha256) {
    throw new Error("배포 자료의 SHA-256이 일치하지 않습니다. 저장하지 않았습니다. 목록을 새로고침하세요.");
  }
  const data = Buffer.from(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const isPdf = data.subarray(0, 5).equals(Buffer.from("%PDF-"));
  const isZip = data.subarray(0, 4).equals(Buffer.from([0x50, 0x4b, 0x03, 0x04]));
  if (material.format === "pdf" ? !isPdf : !isZip) {
    throw new Error("배포 자료의 실제 파일 형식이 PDF/ZIP 정보와 다릅니다. 저장하지 않았습니다.");
  }
}

function contains(parent: string, child: string): boolean {
  const relative = path.relative(parent, child);
  return relative === "" || (!relative.startsWith(`..${path.sep}`) && relative !== ".." && !path.isAbsolute(relative));
}

/** Canonical paths reject a folder/symlink inside assignment code, protecting later submission bundles. */
export async function materialDirectory(directory: string, assignmentDirectories: readonly string[]): Promise<string> {
  const target = await realpath(directory);
  if (!(await lstat(target)).isDirectory()) throw new Error("자료를 저장할 폴더를 선택하세요.");
  for (const assignment of assignmentDirectories) {
    let code: string;
    try { code = await realpath(assignment); }
    catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") continue; throw error; }
    if (contains(code, target)) throw new Error("과제 코드 폴더 안에는 자료를 저장할 수 없습니다. 별도 자료 폴더를 선택하세요.");
  }
  return target;
}

/** Exclusive creation prevents overwrites and final-component symlinks; stale or failed writes are removed. */
export async function saveStudentMaterial(
  directory: string, material: StudentMaterial, bytes: Uint8Array,
  assertCurrent: () => void, assignmentDirectories: readonly string[] = [],
): Promise<string> {
  verifyStudentMaterial(bytes, material);
  const parent = await materialDirectory(directory, assignmentDirectories);
  assertCurrent();
  const destination = path.join(parent, studentMaterialFilename(material));
  const handle = await open(destination, "wx", 0o600);
  const identity = await handle.stat();
  try {
    assertCurrent();
    await handle.writeFile(bytes);
    await handle.sync();
    assertCurrent();
    return destination;
  } catch (error) {
    // Remove only the exact newly created inode, never a pre-existing or replaced path.
    const current = await lstat(destination).catch(() => undefined);
    if (current?.ino === identity.ino && current.dev === identity.dev) await unlink(destination).catch(() => undefined);
    throw error;
  } finally { await handle.close(); }
}

export function materialErrorMessage(error: unknown): string {
  const value = error as {status?: number; code?: string; message?: string};
  if (value?.status === 401 || value?.code === "login_required") return "학생 로그인이 필요합니다. 과제 수령 코드로 로그인한 뒤 배포 자료를 다시 여세요.";
  if (value?.code === "materials_unsupported") return "현재 서버는 IDE 자료 배포를 지원하지 않습니다. 학생 웹을 이용하거나 교수자에게 서버 업데이트를 요청하세요.";
  if (value?.status === 409) return "교수자가 자료를 수정했습니다. 배포 자료를 다시 열어 최신 목록에서 다운로드하세요. 기존 파일은 변경하지 않았습니다.";
  if (value?.status === 403) return "자료 배포 기간 또는 수강 권한을 확인하세요. 목록을 새로고침하고 교수자에게 문의하세요.";
  if (value?.status === 404) return "자료가 철회되었거나 현재 학생에게 배포되지 않았습니다. 배포 자료 목록을 다시 여세요.";
  if (value?.code === "EEXIST") return "같은 버전의 자료 파일이 이미 있습니다. 기존 파일은 덮어쓰지 않았습니다. 다른 폴더를 선택하세요.";
  if (["EACCES", "EPERM", "EROFS"].includes(value?.code ?? "")) return "선택한 폴더에 저장 권한이 없습니다. 학생 본인의 쓰기 가능한 자료 폴더를 선택하세요.";
  if (value?.code === "ENOSPC") return "저장 공간이 부족합니다. 여유 공간을 확보한 뒤 다시 다운로드하세요.";
  return error instanceof Error ? error.message : "자료를 다운로드하지 못했습니다. 서버 연결과 저장 폴더를 확인하세요.";
}
