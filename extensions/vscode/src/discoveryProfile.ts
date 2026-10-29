/** Public machine configuration only. Student secrets never belong in this file. */
import * as fs from "node:fs";
import * as path from "node:path";
import { randomBytes, createHash } from "node:crypto";
import { isIP } from "node:net";

export interface TrustedDiscoveryServer {
  readonly institutionId: string;
  readonly displayName: string;
  readonly apiUrl: string;
  readonly portalUrl: string;
}
export interface DiscoveryProfile {
  readonly schema: "autograde.ide-discovery.v1";
  readonly trustedServers: readonly TrustedDiscoveryServer[];
  readonly selectedInstitutionId?: string;
}

const fail = (): never => { throw new Error("기관 자동 검색 설정을 확인할 수 없습니다. 담당자에게 설정 파일과 접근 권한 확인을 요청하세요."); };
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
const keysAre = (value: Record<string, unknown>, keys: readonly string[]): boolean => Object.keys(value).every(key => keys.includes(key));
const identifier = (value: unknown): value is string => typeof value === "string" && /^[a-z0-9][a-z0-9-]{0,63}$/.test(value);

function origin(value: unknown): string {
  if (typeof value !== "string" || !value || value.length > 2048 || /[^\x21-\x7e]/.test(value)) return fail();
  let url: URL;
  try { url = new URL(value); } catch { return fail(); }
  if (url.protocol !== "https:" || url.username || url.password || url.search || url.hash || url.pathname !== "/" || url.port === "0"
    || isIP(url.hostname) || url.hostname.includes(":") || url.hostname.endsWith(".") || !url.hostname.includes(".")
    || url.hostname.length > 253 || url.hostname.split(".").some(label => !/^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label))) return fail();
  return url.origin;
}

export function validateDiscoveryProfile(value: unknown): DiscoveryProfile {
  if (!object(value) || !keysAre(value, ["schema", "trustedServers", "selectedInstitutionId"])
    || value.schema !== "autograde.ide-discovery.v1" || !Array.isArray(value.trustedServers)
    || value.trustedServers.length < 1 || value.trustedServers.length > 32) return fail();
  const ids = new Set<string>();
  const trustedServers = value.trustedServers.map((server: unknown): TrustedDiscoveryServer => {
    if (!object(server) || !keysAre(server, ["institutionId", "displayName", "apiUrl", "portalUrl"])
      || !identifier(server.institutionId) || ids.has(server.institutionId)
      || typeof server.displayName !== "string" || !server.displayName.trim() || server.displayName.length > 100
      || /[\u0000-\u001f\u007f]/.test(server.displayName)) return fail();
    ids.add(server.institutionId);
    const apiUrl = origin(server.apiUrl), portalUrl = origin(server.portalUrl);
    if (apiUrl === portalUrl) return fail();
    return {institutionId: server.institutionId, displayName: server.displayName, apiUrl, portalUrl};
  });
  if (value.selectedInstitutionId !== undefined && (!identifier(value.selectedInstitutionId) || !ids.has(value.selectedInstitutionId))) return fail();
  return {schema: "autograde.ide-discovery.v1", trustedServers,
    ...(typeof value.selectedInstitutionId === "string" ? {selectedInstitutionId: value.selectedInstitutionId} : {})};
}

function checkPath(file: string): fs.Stats {
  if (!path.isAbsolute(file) || path.normalize(file) !== file || !process.getuid) return fail();
  let current = path.parse(file).root;
  for (const name of path.relative(current, path.dirname(file)).split(path.sep).filter(Boolean)) {
    current = path.join(current, name);
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) return fail();
  }
  const parent = fs.lstatSync(path.dirname(file));
  if (parent.uid !== process.getuid() || (parent.mode & 0o077)) return fail();
  return parent;
}

interface Snapshot { profile: DiscoveryProfile; parent: fs.Stats; stat: fs.Stats; hash: string }
function readSnapshot(file: string): Snapshot | undefined {
  if (typeof file !== "string" || !file || !path.isAbsolute(file) || path.normalize(file) !== file) return fail();
  try { fs.lstatSync(file); } catch (error) { if ((error as NodeJS.ErrnoException).code === "ENOENT") return undefined; throw error; }
  const parent = checkPath(file);
  let fd: number | undefined;
  try {
    fd = fs.openSync(file, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW);
    const stat = fs.fstatSync(fd);
    if (!stat.isFile() || stat.nlink !== 1 || stat.uid !== process.getuid!() || (stat.mode & 0o077) || stat.size > 32768) return fail();
    const bytes = fs.readFileSync(fd);
    const profile = validateDiscoveryProfile(JSON.parse(bytes.toString("utf8")) as unknown);
    return {profile, parent, stat, hash: createHash("sha256").update(bytes).digest("hex")};
  } catch { return fail(); } finally { if (fd !== undefined) fs.closeSync(fd); }
}

export async function readDiscoveryProfile(file: string): Promise<DiscoveryProfile | undefined> {
  return readSnapshot(file)?.profile;
}

/** Only a choice from an existing operator-maintained allowlist is persisted. */
export async function selectDiscoveryInstitution(file: string, id: string): Promise<void> {
  const snapshot = readSnapshot(file);
  if (!snapshot || !snapshot.profile.trustedServers.some(server => server.institutionId === id)) return fail();
  const temporary = path.join(path.dirname(file), `.autograde-discovery-${randomBytes(12).toString("hex")}.tmp`);
  let fd: number | undefined, created = false;
  try {
    fd = fs.openSync(temporary, fs.constants.O_WRONLY | fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW, 0o600);
    created = true;
    fs.writeFileSync(fd, `${JSON.stringify({...snapshot.profile, selectedInstitutionId: id}, null, 2)}\n`);
    fs.fsyncSync(fd); fs.closeSync(fd); fd = undefined;
    const current = readSnapshot(file);
    if (!current || current.stat.dev !== snapshot.stat.dev || current.stat.ino !== snapshot.stat.ino || current.hash !== snapshot.hash
      || current.parent.dev !== snapshot.parent.dev || current.parent.ino !== snapshot.parent.ino) return fail();
    fs.renameSync(temporary, file);
    created = false;
  } catch { return fail(); } finally {
    if (fd !== undefined) fs.closeSync(fd);
    if (created) fs.unlinkSync(temporary);
  }
}
