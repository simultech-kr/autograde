import { lstat, realpath } from "node:fs/promises";
import * as path from "node:path";

import { readWorkspaceMarker, type WorkspaceMarker } from "./bundle";

export interface LocalAssignmentRoot {
  readonly rootPath: string;
  readonly marker: WorkspaceMarker;
}

export function preferredSubmissionRoot<T extends { rootPath: string; containsActiveDocument: boolean }>(
  matches: readonly T[], selectedRoot?: string,
): T | undefined {
  // An explicit folder choice remains authoritative even if opening its preview failed.
  const selected = matches.find(candidate => candidate.rootPath === selectedRoot);
  if (selected) return selected;
  const active = matches.filter(candidate => candidate.containsActiveDocument);
  return active.length === 1 ? active[0] : matches.length === 1 ? matches[0] : undefined;
}

export function containsPath(parent: string, candidate: string): boolean {
  const relative = path.relative(path.resolve(parent), path.resolve(candidate));
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

/** Markers describe files, never grant access to a server assignment. */
export async function inspectLocalAssignment(rootPath: string): Promise<LocalAssignmentRoot | undefined> {
  const root = path.resolve(rootPath);
  const stat = await lstat(root).catch(() => undefined);
  if (!stat?.isDirectory() || stat.isSymbolicLink()) return undefined;
  const marker = await readWorkspaceMarker(root);
  return marker ? { rootPath: root, marker } : undefined;
}

export async function findContainingAssignment(candidatePath: string): Promise<LocalAssignmentRoot | undefined> {
  const boundary = await findAssignmentBoundary(candidatePath);
  if (!boundary) return undefined;
  const assignment = await inspectLocalAssignment(boundary);
  if (!assignment) throw new Error("현재 과제 폴더의 과제 정보가 손상되었습니다. 기존 코드를 보존하고 올바른 과제 폴더를 연결하세요.");
  return assignment;
}

async function findAssignmentBoundary(candidatePath: string): Promise<string | undefined> {
  let current = path.resolve(candidatePath);
  while (true) {
    try {
      await lstat(path.join(current, ".autograde"));
      return current;
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== "ENOENT") throw error;
    }
    const parent = path.dirname(current);
    if (parent === current) return undefined;
    current = parent;
  }
}

/** A directly opened assignment downloads its next assignment beside it. */
export async function courseDirectory(workspaceRoot: string): Promise<string> {
  let current = path.resolve(workspaceRoot);
  let boundary = await findAssignmentBoundary(current);
  while (boundary) {
    if (path.dirname(boundary) === boundary) throw new Error("파일 시스템 루트는 과제 저장 위치로 사용할 수 없습니다.");
    current = path.dirname(boundary);
    boundary = await findAssignmentBoundary(current);
  }
  return current;
}

export async function validateExistingAssignment(
  rootPath: string, serviceBaseUrl: string, assignmentId: string,
): Promise<LocalAssignmentRoot> {
  const local = await inspectLocalAssignment(rootPath);
  if (!local) throw new Error("선택한 폴더의 과제 정보가 없거나 손상되었습니다. 다운로드한 과제 폴더를 선택하세요. 기존 코드는 그대로 보존됩니다.");
  if (local.marker.serviceBaseUrl !== serviceBaseUrl) throw new Error("선택한 폴더는 다른 Autograde 서버의 과제입니다. 해당 서버 주소와 수령 코드로 연결하세요.");
  if (local.marker.assignmentId !== assignmentId) throw new Error("선택한 폴더와 현재 연결된 과제가 다릅니다. 해당 과제의 새 수령 코드를 입력한 뒤 기존 폴더를 여세요.");
  return local;
}

export async function requireAuthorizedActiveAssignment(
  activeDocumentPath: string | undefined, serviceBaseUrl: string, assignmentIds: ReadonlySet<string>,
): Promise<void> {
  if (!activeDocumentPath) return;
  const active = await findContainingAssignment(path.dirname(activeDocumentPath));
  if (active && (active.marker.serviceBaseUrl !== serviceBaseUrl || !assignmentIds.has(active.marker.assignmentId))) {
    throw new Error("현재 편집 중인 파일은 지금 연결된 과제가 아닙니다. 제출할 과제의 새 수령 코드를 입력하고 기존 과제 열기를 사용하세요.");
  }
}

/** Resolve collisions without touching any existing directory, even a damaged download. */
export async function freshAssignmentPath(courseRoot: string, directoryName: string): Promise<string> {
  const root = path.resolve(courseRoot);
  const actualRoot = await realpath(root);
  if (await findAssignmentBoundary(actualRoot)) throw new Error("과제 폴더 안에는 다른 과제를 받을 수 없습니다. 수업 폴더를 선택하세요.");
  if (!directoryName || path.basename(directoryName) !== directoryName || [".", ".."].includes(directoryName)) {
    throw new Error("과제 폴더 이름이 올바르지 않습니다.");
  }
  for (let version = 1; version <= 10000; version += 1) {
    const candidate = path.join(root, version === 1 ? directoryName : `${directoryName}-${version}`);
    try { await lstat(candidate); } catch (error) {
      if ((error as NodeJS.ErrnoException).code === "ENOENT") return candidate;
      throw error;
    }
  }
  throw new Error("같은 이름의 과제 폴더가 너무 많습니다. 다른 수업 폴더를 선택하세요.");
}
