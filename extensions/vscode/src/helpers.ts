import * as path from "node:path";
import { isIP } from "node:net";

import type {
  Assignment,
  AssignedRepository,
  GradeResult,
  ResultDiagnostic,
  RubricItem,
  SubmissionSummary,
} from "./types";

type JsonRecord = Record<string, unknown>;

export function isRecord(value: unknown): value is JsonRecord {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function firstString(record: JsonRecord, keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim() !== "") {
      return value;
    }
  }
  return undefined;
}

function firstNumber(record: JsonRecord, keys: readonly string[]): number | undefined {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return undefined;
}

function firstByteSize(record: JsonRecord, keys: readonly string[]): number | undefined {
  const value = firstNumber(record, keys);
  return value !== undefined && Number.isSafeInteger(value) && value >= 0 ? value : undefined;
}

function firstBoolean(record: JsonRecord, keys: readonly string[]): boolean | undefined {
  for (const key of keys) {
    if (typeof record[key] === "boolean") {
      return record[key];
    }
  }
  return undefined;
}

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "[::1]"]);
const MAX_CLAIM_CODE_INPUT_LENGTH = 256;
const CLAIM_CODE = /^AK1([23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{12})$/;

/** Canonicalize a human-transcribed one-time assignment claim code. */
export function normalizeClaimCode(value: string): string | undefined {
  if (value.length > MAX_CLAIM_CODE_INPUT_LENGTH || /[^\x00-\x7f]/.test(value)) {
    return undefined;
  }
  const compact = value.toUpperCase().replace(/[\t\n\v\f\r -]/g, "");
  const matched = CLAIM_CODE.exec(compact);
  if (!matched) {
    return undefined;
  }
  const payload = matched[1] as string;
  return `AK1-${payload.slice(0, 4)}-${payload.slice(4, 8)}-${payload.slice(8, 12)}`;
}

function canonicalIpv4Hostname(rawUrl: string, parsed: URL): string | undefined {
  // WHATWG URL parsing accepts legacy numeric spellings such as 0xc0a80114.
  // Requiring the original authority to contain canonical dotted decimal keeps
  // the pilot allow-list small and understandable to students and instructors.
  const authorityMatch = /^https?:\/\/([^/?#]+)(?:[/?#]|$)/i.exec(rawUrl.trim());
  const authority = authorityMatch?.[1];
  if (!authority || authority.includes("@") || authority.startsWith("[")) {
    return undefined;
  }
  const colonIndex = authority.lastIndexOf(":");
  const rawHostname = colonIndex >= 0 ? authority.slice(0, colonIndex) : authority;
  if (rawHostname !== parsed.hostname) {
    return undefined;
  }
  const octets = rawHostname.split(".");
  if (
    octets.length !== 4 ||
    octets.some((octet) => !/^(?:0|[1-9]\d{0,2})$/.test(octet) || Number(octet) > 255)
  ) {
    return undefined;
  }
  return rawHostname;
}

export function isRfc1918Ipv4Host(hostname: string): boolean {
  const octets = hostname.split(".").map(Number);
  if (
    octets.length !== 4 ||
    octets.some((octet) => !Number.isInteger(octet) || octet < 0 || octet > 255)
  ) {
    return false;
  }
  const first = octets[0]!;
  const second = octets[1]!;
  return (
    first === 10 ||
    (first === 172 && second >= 16 && second <= 31) ||
    (first === 192 && second === 168)
  );
}

/** True only for canonical, private IPv4 HTTP URLs used by the LAN pilot. */
export function isInsecureHttpPilotUrl(raw: string): boolean {
  let parsed: URL;
  try {
    parsed = new URL(raw.trim());
  } catch {
    return false;
  }
  if (parsed.protocol !== "http:" || parsed.username || parsed.password) {
    return false;
  }
  const hostname = canonicalIpv4Hostname(raw, parsed);
  return hostname !== undefined && isRfc1918Ipv4Host(hostname);
}

export function normalizeServiceBaseUrl(
  raw: string,
  allowInsecureHttpPilot = false,
): string {
  let parsed: URL;
  try {
    parsed = new URL(raw.trim());
  } catch {
    throw new Error("Autograde 서비스 URL이 올바르지 않습니다.");
  }

  if (
    parsed.username ||
    parsed.password ||
    parsed.pathname !== "/" ||
    parsed.search ||
    parsed.hash
  ) {
    throw new Error("서비스 URL에는 인증 정보, path, query 또는 fragment를 넣을 수 없습니다.");
  }

  if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
    throw new Error("Autograde 서비스는 HTTPS를 사용해야 합니다.");
  }
  validateOptionalPort(parsed.port || undefined);

  const destinationHostname = parsed.hostname.replace(/^\[|]$/g, "");
  if (isIP(destinationHostname) === 4 && canonicalIpv4Hostname(raw, parsed) === undefined) {
    throw new Error("IPv4 주소는 203.0.113.10과 같은 표준 점 표기로 입력하세요.");
  }
  validateServiceDestination(parsed);

  if (parsed.protocol === "http:" && !LOOPBACK_HOSTS.has(parsed.hostname.toLowerCase())) {
    if (!allowInsecureHttpPilot) {
      throw new Error(
        "외부 HTTP 접속은 기본적으로 차단됩니다. 신뢰된 사설 LAN 파일럿에서만 별도 허용하세요.",
      );
    }
    if (!isInsecureHttpPilotUrl(raw)) {
      throw new Error(
        "외부 HTTP 파일럿 주소는 표준 표기의 RFC1918 사설 IPv4 주소만 사용할 수 있습니다.",
      );
    }
  }

  parsed.pathname = parsed.pathname.replace(/\/+$/, "");
  return parsed.toString().replace(/\/$/, "");
}

/**
 * Normalize the address syntax accepted by the service-address input box.
 *
 * A missing scheme means HTTPS, except for an explicit loopback address where
 * the local-development HTTP default remains convenient. IPv6 with a port must
 * use the standard bracket form, for example `[2001:db8::10]:20000`.
 */
export function normalizeServiceAddressInput(
  raw: string,
  allowInsecureHttpPilot = false,
): string {
  const value = raw.trim();
  if (!value) {
    throw new Error("Autograde 서비스 주소를 입력하세요.");
  }

  if (/^[a-z][a-z\d+.-]*:\/\//i.test(value)) {
    return normalizeServiceBaseUrl(value, allowInsecureHttpPilot);
  }
  if (/\s/.test(value)) {
    throw new Error("Autograde 서비스 주소에는 공백을 넣을 수 없습니다.");
  }

  const authority = normalizeAddressAuthority(value);
  const candidate = `https://${authority}`;
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error("Autograde 서비스 주소가 올바르지 않습니다.");
  }

  if (LOOPBACK_HOSTS.has(parsed.hostname.toLowerCase())) {
    parsed.protocol = "http:";
  }
  return normalizeServiceBaseUrl(parsed.toString(), allowInsecureHttpPilot);
}

function normalizeAddressAuthority(value: string): string {
  if (isIP(value) === 6) {
    return `[${value}]`;
  }

  if (value.startsWith("[")) {
    const matched = /^\[([^\]]+)](?::(\d+))?$/.exec(value);
    if (!matched?.[1] || isIP(matched[1]) !== 6) {
      throw new Error("IPv6 주소와 port는 [2001:db8::10]:20000 형식으로 입력하세요.");
    }
    validateOptionalPort(matched[2]);
    return value;
  }

  const colonCount = [...value].filter((character) => character === ":").length;
  if (colonCount > 1) {
    throw new Error("IPv6 주소에 port를 붙일 때는 [2001:db8::10]:20000 형식을 사용하세요.");
  }

  const [hostname = "", port] = value.split(":", 2);
  if (!hostname || hostname.includes("/") || hostname.includes("?") || hostname.includes("#") || hostname.includes("@")) {
    throw new Error("서비스 주소에는 host 또는 IP와 선택적인 port만 입력하세요.");
  }
  if (/^[\d.]+$/.test(hostname) && isIP(hostname) !== 4) {
    throw new Error("IPv4 주소는 203.0.113.10과 같은 표준 점 표기로 입력하세요.");
  }
  validateOptionalPort(port);
  return value;
}

function validateOptionalPort(port: string | undefined): void {
  if (port === undefined) {
    return;
  }
  if (!/^\d+$/.test(port)) {
    throw new Error("port는 1부터 65535 사이의 숫자로 입력하세요.");
  }
  const numericPort = Number(port);
  if (!Number.isSafeInteger(numericPort) || numericPort < 1 || numericPort > 65_535) {
    throw new Error("port는 1부터 65535 사이의 숫자로 입력하세요.");
  }
}

function validateServiceDestination(parsed: URL): void {
  const hostname = parsed.hostname.replace(/^\[|]$/g, "").toLowerCase();
  if (hostname.includes("%")) {
    throw new Error("IPv6 zone 식별자가 포함된 서비스 주소는 사용할 수 없습니다.");
  }

  if (isIP(hostname) === 4) {
    const octets = hostname.split(".").map(Number);
    const first = octets[0] as number;
    if (first === 0 || hostname === "255.255.255.255" || first >= 224) {
      throw new Error("미지정, broadcast 또는 multicast IP는 서비스 주소로 사용할 수 없습니다.");
    }
    return;
  }

  if (isIP(hostname) === 6) {
    if (
      hostname === "::" ||
      /^ff/i.test(hostname) ||
      /^fe[89ab]/i.test(hostname)
    ) {
      throw new Error("미지정, link-local 또는 multicast IPv6 주소는 사용할 수 없습니다.");
    }
  }
}

/** Returns a credential-free host/path identity suitable for preflight comparison. */
export function normalizeRepositoryLocator(raw: string): string | undefined {
  const value = raw.trim();
  if (!value) {
    return undefined;
  }

  const scpMatch = /^(?:[^@/\s]+@)?([^:/\s]+):(.+)$/.exec(value);
  if (scpMatch?.[1] && scpMatch[2] && !value.includes("://")) {
    return normalizeHostPath(scpMatch[1], scpMatch[2]);
  }

  try {
    const parsed = new URL(value);
    if (!["https:", "http:", "ssh:", "git:"].includes(parsed.protocol)) {
      return undefined;
    }
    return normalizeHostPath(parsed.hostname, parsed.pathname);
  } catch {
    return undefined;
  }
}

function normalizeHostPath(host: string, repositoryPath: string): string | undefined {
  const cleanPath = repositoryPath
    .replace(/^\/+/, "")
    .replace(/\/+$/, "")
    .replace(/\.git$/i, "")
    .toLowerCase();
  if (!host || !cleanPath || !cleanPath.includes("/")) {
    return undefined;
  }
  return `${host.toLowerCase()}/${cleanPath}`;
}

export function repositoryMatches(
  remoteUrl: string,
  repository: AssignedRepository,
): boolean {
  const actual = normalizeRepositoryLocator(remoteUrl);
  if (!actual) {
    return false;
  }

  const urls = [repository.cloneUrl, repository.sshUrl, repository.htmlUrl];
  if (urls.some((candidate) => candidate && normalizeRepositoryLocator(candidate) === actual)) {
    return true;
  }

  // A full name without a server host is not enough to distinguish GitHub.com,
  // GitHub Enterprise, or an attacker-controlled host. Require a canonical URL.
  return false;
}

export function isAssignedRepositoryReady(repository: AssignedRepository): boolean {
  return repository.ready === true || repository.state?.toLowerCase() === "ready";
}

export function isBundleAssignment(assignment: Assignment): boolean {
  return assignment.deliveryMode?.trim().toLowerCase() === "bundle";
}

export function isSupportedWorkspacePlatform(
  platform: NodeJS.Platform,
  remoteName: string | undefined,
): boolean {
  return platform === "linux" || platform === "darwin" || (platform === "win32" && remoteName === "wsl");
}

export function isAssignmentDownloadable(assignment: Assignment): boolean {
  if (isBundleAssignment(assignment)) {
    if (assignment.ready !== undefined) {
      return assignment.ready;
    }
    const state = assignment.status?.trim().toLowerCase();
    return state === undefined || !new Set([
      "draft",
      "validating",
      "invalid",
      "disabled",
      "not_ready",
      "unavailable",
    ]).has(state);
  }
  return assignment.repository !== undefined && isAssignedRepositoryReady(assignment.repository);
}

export function safeAssignmentDirectoryName(assignment: Assignment): string | undefined {
  for (const candidate of [assignment.key, assignment.id]) {
    if (!candidate) {
      continue;
    }
    const safe = safeRepositoryDirectoryName(candidate);
    if (safe) {
      return safe;
    }
  }
  return undefined;
}

export function normalizeTargetRef(value: string | undefined): string | undefined {
  const candidate = value?.trim();
  if (!candidate) {
    return undefined;
  }
  const fullRef = candidate.startsWith("refs/heads/")
    ? candidate
    : candidate.startsWith("heads/")
      ? `refs/${candidate}`
      : candidate.startsWith("refs/")
        ? undefined
        : `refs/heads/${candidate}`;
  if (!fullRef) {
    return undefined;
  }
  const branch = fullRef.slice("refs/heads/".length);
  const components = branch.split("/");
  if (
    !branch ||
    branch === "HEAD" ||
    branch.startsWith("-") ||
    branch.endsWith(".") ||
    branch.includes("..") ||
    branch.includes("//") ||
    branch.includes("@{") ||
    /[\x00-\x20\x7f~^:?*[\]\\]/.test(branch) ||
    components.some((component) =>
      !component || component.startsWith(".") || component.endsWith(".lock")
    )
  ) {
    return undefined;
  }
  return fullRef;
}

export function targetRefMatches(currentUpstream: string, assignedTarget: string): boolean {
  const current = normalizeTargetRef(currentUpstream);
  const assigned = normalizeTargetRef(assignedTarget);
  return current !== undefined && current === assigned;
}

export function selectRepositoryCloneUrl(repository: AssignedRepository): string | undefined {
  for (const candidate of [repository.cloneUrl, repository.sshUrl]) {
    if (candidate && isSafeCloneUrl(candidate)) {
      return candidate.trim();
    }
  }
  return undefined;
}

/**
 * Git stores the clone URL in .git/config. Reject embedded HTTP credentials
 * and URL decorations that could leave a bearer secret on a shared machine.
 * An SSH username (including the common `git`) is an account label, not a
 * password; authentication must be delegated to the OS SSH agent.
 */
function isSafeCloneUrl(raw: string): boolean {
  const value = raw.trim();
  if (!normalizeRepositoryLocator(value)) {
    return false;
  }
  if (/^(?:[^@/\s]+@)?[^:/\s]+:.+$/.test(value) && !value.includes("://")) {
    return true;
  }
  try {
    const parsed = new URL(value);
    if (parsed.search || parsed.hash || parsed.password) {
      return false;
    }
    if (["http:", "https:", "git:"].includes(parsed.protocol) && parsed.username) {
      return false;
    }
    return true;
  } catch {
    return false;
  }
}

export function safeRepositoryDirectoryName(value: string): string | undefined {
  const candidate = value.trim().replace(/\.git$/i, "");
  if (
    candidate === "" ||
    candidate === "." ||
    candidate === ".." ||
    candidate.length > 100 ||
    !/^[A-Za-z0-9._-]+$/.test(candidate)
  ) {
    return undefined;
  }
  return candidate;
}

export function resolveRepositoryRelativePath(
  repositoryRoot: string,
  relativePath: string,
): string | undefined {
  if (!relativePath || path.isAbsolute(relativePath)) {
    return undefined;
  }
  const root = path.resolve(repositoryRoot);
  const candidate = path.resolve(root, relativePath);
  const relation = path.relative(root, candidate);
  if (relation === "" || (!relation.startsWith("..") && !path.isAbsolute(relation))) {
    return candidate;
  }
  return undefined;
}

export function resolveAssignmentDiagnosticPath(
  repositoryRoot: string,
  assignmentPath: string | undefined,
  diagnosticPath: string,
): string | undefined {
  const relativePath = assignmentPath && assignmentPath !== "."
    ? path.posix.join(assignmentPath, diagnosticPath)
    : diagnosticPath;
  return resolveRepositoryRelativePath(repositoryRoot, relativePath);
}

export function normalizeAssignments(payload: unknown): Assignment[] {
  const rawItems = Array.isArray(payload)
    ? payload
    : isRecord(payload) && Array.isArray(payload.assignments)
      ? payload.assignments
      : [];

  const assignments: Assignment[] = [];
  for (const raw of rawItems) {
    if (!isRecord(raw)) {
      continue;
    }
    const id = firstString(raw, ["id", "assignment_id"]);
    if (!id) {
      continue;
    }

    const course = isRecord(raw.course) ? raw.course : undefined;
    const delivery = isRecord(raw.delivery) ? raw.delivery : undefined;
    const starter = isRecord(raw.starter) ? raw.starter : undefined;
    const repositoryRaw = isRecord(raw.repository) ? raw.repository : undefined;
    const latestRaw = isRecord(raw.latest_submission) ? raw.latest_submission : undefined;
    let repository = repositoryRaw ? normalizeRepository(repositoryRaw) : normalizeRepository(raw);
    const assignmentReady = firstBoolean(raw, ["ready"]);
    if (repository && repository.ready === undefined && assignmentReady !== undefined) {
      repository = { ...repository, ready: assignmentReady };
    }
    const latestSubmission = latestRaw ? normalizeSubmission(latestRaw) : undefined;

    assignments.push({
      id,
      key: firstString(raw, ["assignment_key", "key"]),
      title: firstString(raw, ["title", "name", "assignment_key", "key"]) ?? id,
      courseLabel:
        (course && firstString(course, ["name", "title", "course_key", "key"])) ??
        firstString(raw, ["course_name", "course_key"]),
      status: firstString(raw, ["status", "state"]),
      ready: assignmentReady,
      deliveryMode:
        firstString(raw, ["delivery_mode"]) ??
        (delivery && firstString(delivery, ["mode", "delivery_mode"])),
      starterUrl:
        firstString(raw, ["starter_url"]) ??
        (starter && firstString(starter, ["url", "download_url", "starter_url"])),
      starterSha256:
        normalizeSha256Digest(firstString(raw, ["starter_sha256"])) ??
        normalizeSha256Digest(starter && firstString(starter, ["sha256", "digest"])),
      starterSizeBytes:
        firstByteSize(raw, ["starter_size_bytes"]) ??
        (starter && firstByteSize(starter, ["size_bytes", "compressed_bytes"])),
      dueAt: firstString(raw, ["due_at", "deadline", "deadline_at"]),
      submissionMode: firstString(raw, ["submission_mode"]),
      assignmentPath: firstString(raw, ["assignment_path"]),
      repository,
      latestSubmission,
    });
  }
  return assignments;
}

function normalizeRepository(raw: JsonRecord): AssignedRepository | undefined {
  const githubRepositoryId = firstNumber(raw, ["github_repository_id", "repository_id"]);
  const fullName = firstString(raw, ["full_name", "repository_full_name"]);
  const name = firstString(raw, ["name", "repository_name"]);
  const cloneUrl = firstString(raw, ["clone_url", "repository_clone_url"]);
  const sshUrl = firstString(raw, ["ssh_url", "repository_ssh_url"]);
  const htmlUrl = firstString(raw, ["html_url", "repository_html_url"]);
  const targetRef = normalizeTargetRef(firstString(raw, ["target_ref", "repository_target_ref"]));
  const state = firstString(raw, ["state", "lifecycle"]);
  const ready = firstBoolean(raw, ["ready"]);
  if (
    githubRepositoryId === undefined &&
    fullName === undefined &&
    cloneUrl === undefined &&
    sshUrl === undefined &&
    htmlUrl === undefined
  ) {
    return undefined;
  }
  return { githubRepositoryId, fullName, name, cloneUrl, sshUrl, htmlUrl, targetRef, state, ready };
}

export function normalizeSubmission(payload: unknown): SubmissionSummary | undefined {
  const raw = isRecord(payload) && isRecord(payload.submission) ? payload.submission : payload;
  if (!isRecord(raw)) {
    return undefined;
  }
  const id = firstString(raw, ["id", "submission_id", "request_id"]);
  if (!id) {
    return undefined;
  }
  const sourceSha = firstString(raw, ["source_sha"]);
  return {
    id,
    state: firstString(raw, ["state", "status"]) ?? "received",
    headSha:
      firstString(raw, ["head_sha", "commit_sha", "requested_sha"]) ??
      (sourceSha && /^[0-9a-f]{40}$/i.test(sourceSha) ? sourceSha : undefined),
    sourceDigest:
      firstString(raw, ["source_digest", "source_sha256", "bundle_sha256", "archive_sha256"]) ??
      (sourceSha && /^[0-9a-f]{64}$/i.test(sourceSha) ? sourceSha : undefined),
    score: firstNumber(raw, ["score"]),
    maxScore: firstNumber(raw, ["max_score"]),
  };
}

export function normalizeGradeResult(payload: unknown): GradeResult | undefined {
  const raw = isRecord(payload) && isRecord(payload.result) ? payload.result : payload;
  if (!isRecord(raw)) {
    return undefined;
  }
  const rubricRaw = raw.rubric;
  const diagnosticsRaw = Array.isArray(raw.diagnostics) ? raw.diagnostics : [];
  const sourceSha = firstString(raw, ["source_sha"]);
  return {
    state: firstString(raw, ["state", "status"]) ?? "published",
    headSha:
      firstString(raw, ["head_sha", "commit_sha"]) ??
      (sourceSha && /^[0-9a-f]{40}$/i.test(sourceSha) ? sourceSha : undefined),
    sourceDigest:
      firstString(raw, ["source_digest", "source_sha256", "bundle_sha256", "archive_sha256"]) ??
      (sourceSha && /^[0-9a-f]{64}$/i.test(sourceSha) ? sourceSha : undefined),
    score: firstNumber(raw, ["score"]),
    maxScore: firstNumber(raw, ["max_score"]),
    rubric: normalizeRubric(rubricRaw),
    diagnostics: diagnosticsRaw.flatMap((item): ResultDiagnostic[] => {
      if (!isRecord(item)) {
        return [];
      }
      const diagnosticPath = firstString(item, ["path", "file"]);
      const message = firstString(item, ["message"]);
      if (!diagnosticPath || !message) {
        return [];
      }
      return [{
        path: diagnosticPath,
        line: firstNumber(item, ["line"]),
        column: firstNumber(item, ["column"]),
        endLine: firstNumber(item, ["end_line"]),
        endColumn: firstNumber(item, ["end_column"]),
        severity: firstString(item, ["severity"]),
        message,
      }];
    }),
  };
}

function normalizeRubric(value: unknown): RubricItem[] {
  if (Array.isArray(value)) {
    return value.flatMap((item): RubricItem[] => {
      if (!isRecord(item)) {
        return [];
      }
      return [{
        name: firstString(item, ["name", "title", "criterion"]) ?? "항목",
        score: firstNumber(item, ["score"]),
        maxScore: firstNumber(item, ["max_score"]),
        feedback: firstString(item, ["feedback", "message"]),
      }];
    });
  }
  if (!isRecord(value)) {
    return [];
  }
  return Object.entries(value).flatMap(([name, item]): RubricItem[] => {
    if (typeof item === "number" && Number.isFinite(item)) {
      return [{ name, score: item }];
    }
    if (typeof item === "string") {
      return [{ name, feedback: item }];
    }
    if (!isRecord(item)) {
      return [];
    }
    return [{
      name: firstString(item, ["name", "title", "criterion"]) ?? name,
      score: firstNumber(item, ["score"]),
      maxScore: firstNumber(item, ["max_score"]),
      feedback: firstString(item, ["feedback", "message"]),
    }];
  });
}

function normalizeSha256Digest(value: string | undefined): string | undefined {
  const normalized = value?.trim().toLowerCase().replace(/^sha256:/, "");
  return normalized && /^[0-9a-f]{64}$/.test(normalized) ? normalized : undefined;
}
