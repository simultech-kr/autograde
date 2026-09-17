import { lstat, readdir, realpath } from "node:fs/promises";
import * as path from "node:path";

import { readWorkspaceMarker } from "./bundle";

const SAFE_STARTER_FILE_NAMES = ["main.cpp", "main.c", "README.md", "README.txt", "README"] as const;
const MAX_PREVIEW_FILE_BYTES = 2 * 1024 * 1024;

export interface AuthenticatedBundleRoot {
  readonly rootPath: string;
  readonly assignmentId: string;
  readonly containsActiveDocument: boolean;
}

interface CandidateRoot {
  readonly rootPath: string;
  readonly workspaceRoot: string;
  containsActiveDocument: boolean;
}

/**
 * Find assignment roots without recursively walking an arbitrary course tree.
 * A marker is only considered when its assignment id came from the current,
 * authenticated server projection supplied by the caller.
 */
export async function discoverAuthenticatedBundleRoots(
  workspaceRoots: readonly string[],
  activeDocumentPath: string | undefined,
  serviceBaseUrl: string,
  authenticatedAssignmentIds: ReadonlySet<string>,
): Promise<readonly AuthenticatedBundleRoot[]> {
  if (authenticatedAssignmentIds.size === 0) {
    return [];
  }

  const candidates = new Map<string, CandidateRoot>();
  for (const rawWorkspaceRoot of workspaceRoots) {
    const workspaceRoot = path.resolve(rawWorkspaceRoot);
    const activePath = activeDocumentPath ? path.resolve(activeDocumentPath) : undefined;
    if (activePath && isAtOrBelow(workspaceRoot, activePath)) {
      let current = path.dirname(activePath);
      while (isAtOrBelow(workspaceRoot, current)) {
        addCandidate(candidates, current, workspaceRoot, true);
        if (samePath(current, workspaceRoot)) {
          break;
        }
        const parent = path.dirname(current);
        if (samePath(parent, current)) {
          break;
        }
        current = parent;
      }
    }

    addCandidate(candidates, workspaceRoot, workspaceRoot, false);
    let children;
    try {
      children = await readdir(workspaceRoot, { withFileTypes: true });
    } catch {
      continue;
    }
    children.sort((left, right) => Buffer.from(left.name).compare(Buffer.from(right.name)));
    for (const child of children) {
      if (child.isDirectory() && !child.isSymbolicLink()) {
        addCandidate(candidates, path.join(workspaceRoot, child.name), workspaceRoot, false);
      }
    }
  }

  const matches: AuthenticatedBundleRoot[] = [];
  for (const candidate of candidates.values()) {
    if (!(await isSafeCandidate(candidate.workspaceRoot, candidate.rootPath))) {
      continue;
    }
    const marker = await readWorkspaceMarker(candidate.rootPath);
    if (
      !marker ||
      marker.serviceBaseUrl !== serviceBaseUrl ||
      !authenticatedAssignmentIds.has(marker.assignmentId)
    ) {
      continue;
    }
    matches.push({
      rootPath: candidate.rootPath,
      assignmentId: marker.assignmentId,
      containsActiveDocument: candidate.containsActiveDocument,
    });
  }
  return matches.sort((left, right) => {
    if (left.containsActiveDocument !== right.containsActiveDocument) {
      return left.containsActiveDocument ? -1 : 1;
    }
    return Buffer.from(left.rootPath).compare(Buffer.from(right.rootPath));
  });
}

/** Return a small, regular top-level source/README without loading project tasks. */
export async function findSafeStarterPreview(assignmentRoot: string): Promise<string | undefined> {
  const root = path.resolve(assignmentRoot);
  const rootStat = await lstat(root).catch(() => undefined);
  if (!rootStat?.isDirectory() || rootStat.isSymbolicLink()) {
    return undefined;
  }
  const realRoot = await realpath(root).catch(() => undefined);
  if (!realRoot) {
    return undefined;
  }
  for (const fileName of SAFE_STARTER_FILE_NAMES) {
    const candidate = path.join(root, fileName);
    const stat = await lstat(candidate).catch(() => undefined);
    if (!stat?.isFile() || stat.isSymbolicLink() || stat.size > MAX_PREVIEW_FILE_BYTES) {
      continue;
    }
    const realCandidate = await realpath(candidate).catch(() => undefined);
    if (realCandidate && isAtOrBelow(realRoot, realCandidate)) {
      return candidate;
    }
  }
  return undefined;
}

function addCandidate(
  candidates: Map<string, CandidateRoot>,
  rootPath: string,
  workspaceRoot: string,
  containsActiveDocument: boolean,
): void {
  const resolved = path.resolve(rootPath);
  if (!isAtOrBelow(workspaceRoot, resolved)) {
    return;
  }
  const key = process.platform === "win32" ? resolved.toLowerCase() : resolved;
  const existing = candidates.get(key);
  if (existing) {
    existing.containsActiveDocument ||= containsActiveDocument;
    return;
  }
  candidates.set(key, { rootPath: resolved, workspaceRoot, containsActiveDocument });
}

async function isSafeCandidate(workspaceRoot: string, candidateRoot: string): Promise<boolean> {
  const candidateStat = await lstat(candidateRoot).catch(() => undefined);
  if (!candidateStat?.isDirectory() || candidateStat.isSymbolicLink()) {
    return false;
  }
  const [realWorkspace, realCandidate] = await Promise.all([
    realpath(workspaceRoot).catch(() => undefined),
    realpath(candidateRoot).catch(() => undefined),
  ]);
  return Boolean(realWorkspace && realCandidate && isAtOrBelow(realWorkspace, realCandidate));
}

function isAtOrBelow(parent: string, candidate: string): boolean {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  return relative === "" || (
    relative !== ".." &&
    !relative.startsWith(`..${path.sep}`) &&
    !path.isAbsolute(relative)
  );
}

function samePath(left: string, right: string): boolean {
  const normalizedLeft = path.resolve(left);
  const normalizedRight = path.resolve(right);
  return process.platform === "win32"
    ? normalizedLeft.toLowerCase() === normalizedRight.toLowerCase()
    : normalizedLeft === normalizedRight;
}
