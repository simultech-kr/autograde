import assert from "node:assert/strict";
import test from "node:test";

import { shouldDiscardTokensAfterRefreshError } from "../authPolicy";
import {
  isAssignmentDownloadable,
  isBundleAssignment,
  isInsecureHttpPilotUrl,
  isRfc1918Ipv4Host,
  isSupportedWorkspacePlatform,
  normalizeAssignments,
  normalizeGradeResult,
  normalizeClaimCode,
  normalizeRepositoryLocator,
  normalizeServiceBaseUrl,
  normalizeTargetRef,
  repositoryMatches,
  resolveAssignmentDiagnosticPath,
  resolveRepositoryRelativePath,
  safeRepositoryDirectoryName,
  safeAssignmentDirectoryName,
  selectRepositoryCloneUrl,
  targetRefMatches,
} from "../helpers";

test("credentials are discarded only after definitive refresh rejection", () => {
  assert.equal(shouldDiscardTokensAfterRefreshError(401, "invalid_grant"), true);
  assert.equal(shouldDiscardTokensAfterRefreshError(400, "invalid_token"), true);
  assert.equal(shouldDiscardTokensAfterRefreshError(401, "reauthentication_required"), true);
  assert.equal(shouldDiscardTokensAfterRefreshError(0, undefined), false);
  assert.equal(shouldDiscardTokensAfterRefreshError(503, "temporarily_unavailable"), false);
  assert.equal(shouldDiscardTokensAfterRefreshError(401, undefined), false);
});

test("service URL keeps external HTTP off unless the private-LAN pilot is explicit", () => {
  assert.equal(normalizeServiceBaseUrl("https://grade.example.edu/"), "https://grade.example.edu");
  assert.equal(normalizeServiceBaseUrl("http://127.0.0.1:18080/"), "http://127.0.0.1:18080");
  assert.throws(() => normalizeServiceBaseUrl("http://192.168.50.34:18081"), /기본적으로 차단/);
  assert.equal(
    normalizeServiceBaseUrl("http://192.168.50.34:18081/", true),
    "http://192.168.50.34:18081",
  );
  assert.equal(normalizeServiceBaseUrl("http://10.20.30.40", true), "http://10.20.30.40");
  assert.equal(normalizeServiceBaseUrl("http://172.31.255.254", true), "http://172.31.255.254");
  assert.throws(() => normalizeServiceBaseUrl("http://grade.example.edu", true), /RFC1918/);
  assert.throws(() => normalizeServiceBaseUrl("http://0.0.0.0:18080", true), /RFC1918/);
  assert.throws(() => normalizeServiceBaseUrl("http://8.8.8.8", true), /RFC1918/);
  assert.throws(() => normalizeServiceBaseUrl("http://169.254.1.1", true), /RFC1918/);
  assert.throws(() => normalizeServiceBaseUrl("http://0xc0a83222", true), /RFC1918/);
  assert.throws(() => normalizeServiceBaseUrl("http://192.168.050.034", true), /RFC1918/);
  assert.throws(() => normalizeServiceBaseUrl("https://grade.example.edu/api"), /path/);
  assert.throws(() => normalizeServiceBaseUrl("http://192.168.50.34/api", true), /path/);
  assert.throws(() => normalizeServiceBaseUrl("https://user:pass@grade.example.edu"), /인증 정보/);
});

test("insecure pilot URL recognition is limited to canonical RFC1918 IPv4", () => {
  assert.equal(isInsecureHttpPilotUrl("http://192.168.1.20:18080/activate?code=ABC"), true);
  assert.equal(isInsecureHttpPilotUrl("https://192.168.1.20:18080"), false);
  assert.equal(isInsecureHttpPilotUrl("http://192.168.1.20.example"), false);
  assert.equal(isRfc1918Ipv4Host("172.16.0.1"), true);
  assert.equal(isRfc1918Ipv4Host("172.32.0.1"), false);
  assert.equal(isRfc1918Ipv4Host("192.168.999.1"), false);
});

test("claim codes canonicalize case, hyphens and ASCII whitespace only", () => {
  assert.equal(normalizeClaimCode(" ak1 2345-6789-abcd "), "AK1-2345-6789-ABCD");
  assert.equal(normalizeClaimCode("AK1--2345\t6789\nABCD"), "AK1-2345-6789-ABCD");
  assert.equal(normalizeClaimCode("AK1-2345-6789-ABCI"), undefined);
  assert.equal(normalizeClaimCode("AK1-2345-6789-ABC0"), undefined);
  assert.equal(normalizeClaimCode("AK1-2345-6789-ABC\u00a0D"), undefined);
  assert.equal(normalizeClaimCode("AK1-2345-6789-ABC\u017f"), undefined);
});

test("workspace platform policy allows Linux, macOS and Windows WSL2", () => {
  assert.equal(isSupportedWorkspacePlatform("linux", undefined), true);
  assert.equal(isSupportedWorkspacePlatform("darwin", undefined), true);
  assert.equal(isSupportedWorkspacePlatform("linux", "wsl"), true);
  assert.equal(isSupportedWorkspacePlatform("win32", "wsl"), true);
  assert.equal(isSupportedWorkspacePlatform("win32", undefined), false);
  assert.equal(isSupportedWorkspacePlatform("freebsd", undefined), false);
});

test("clone directory names are single safe path components", () => {
  assert.equal(safeRepositoryDirectoryName("lab03-student.git"), "lab03-student");
  assert.equal(safeRepositoryDirectoryName("../student"), undefined);
  assert.equal(safeRepositoryDirectoryName("nested/student"), undefined);
  assert.equal(safeRepositoryDirectoryName(".."), undefined);
});

test("repository locator handles SSH and HTTPS without exposing credentials", () => {
  assert.equal(
    normalizeRepositoryLocator("git@github.com:School-CSE101/Lab03-Student.git"),
    "github.com/school-cse101/lab03-student",
  );
  assert.equal(
    normalizeRepositoryLocator("https://token@github.com/School-CSE101/Lab03-Student.git"),
    "github.com/school-cse101/lab03-student",
  );
  assert.equal(normalizeRepositoryLocator("/tmp/local-repo"), undefined);
});

test("repository matching requires an assigned canonical URL", () => {
  assert.equal(repositoryMatches("git@github.com:org/student.git", {
    githubRepositoryId: 42,
    cloneUrl: "https://github.com/org/student.git",
  }), true);
  assert.equal(repositoryMatches("git@github.example.edu:org/student.git", {
    githubRepositoryId: 42,
    fullName: "org/student",
  }), false);
  assert.equal(repositoryMatches("git@github.com:org/other.git", {
    githubRepositoryId: 42,
    fullName: "org/student",
  }), false);
});

test("target refs normalize branch names and reject non-branch refs", () => {
  assert.equal(normalizeTargetRef("main"), "refs/heads/main");
  assert.equal(normalizeTargetRef("submission/lab01"), "refs/heads/submission/lab01");
  assert.equal(normalizeTargetRef("refs/heads/main"), "refs/heads/main");
  assert.equal(normalizeTargetRef("refs/tags/v1"), undefined);
  assert.equal(normalizeTargetRef("bad..branch"), undefined);
  assert.equal(targetRefMatches("refs/heads/submission/lab01", "submission/lab01"), true);
  assert.equal(targetRefMatches("refs/heads/main", "submission/lab01"), false);
});

test("clone URL falls back to SSH when HTTPS clone URL is absent or invalid", () => {
  assert.equal(selectRepositoryCloneUrl({
    githubRepositoryId: 42,
    sshUrl: "git@github.com:org/student.git",
  }), "git@github.com:org/student.git");
  assert.equal(selectRepositoryCloneUrl({
    githubRepositoryId: 42,
    cloneUrl: "/invalid/local/path",
    sshUrl: "git@github.com:org/student.git",
  }), "git@github.com:org/student.git");
  assert.equal(selectRepositoryCloneUrl({
    githubRepositoryId: 42,
    cloneUrl: "https://access-token@github.com/org/student.git",
    sshUrl: "git@github.com:org/student.git",
  }), "git@github.com:org/student.git");
  assert.equal(selectRepositoryCloneUrl({
    githubRepositoryId: 42,
    cloneUrl: "https://github.com/org/student.git?token=secret",
  }), undefined);
  assert.equal(selectRepositoryCloneUrl({
    githubRepositoryId: 42,
    sshUrl: "ssh://git@github.com/org/student.git",
  }), "ssh://git@github.com/org/student.git");
});

test("diagnostic paths cannot escape the repository", () => {
  assert.equal(resolveRepositoryRelativePath("/work/repo", "src/main.py"), "/work/repo/src/main.py");
  assert.equal(resolveRepositoryRelativePath("/work/repo", "../secret"), undefined);
  assert.equal(resolveRepositoryRelativePath("/work/repo", "/etc/passwd"), undefined);
});

test("diagnostic paths are resolved below an assignment subpath", () => {
  assert.equal(
    resolveAssignmentDiagnosticPath("/work/repo", "assignments/lab01", "src/main.py"),
    "/work/repo/assignments/lab01/src/main.py",
  );
  assert.equal(
    resolveAssignmentDiagnosticPath("/work/repo", "../outside", "answer.txt"),
    undefined,
  );
});

test("assignment envelope is normalized with numeric repository identity", () => {
  const assignments = normalizeAssignments({
    assignments: [{
      assignment_id: "asn_1",
      assignment_key: "lab03",
      title: "Lab 03",
      assignment_path: "assignments/lab03",
      course: { course_key: "cse101-2026f" },
      repository: {
        github_repository_id: 123,
        full_name: "school/lab03-student",
        clone_url: "https://github.com/school/lab03-student.git",
        target_ref: "submission/lab03",
        state: "ready",
      },
      latest_submission: {
        submission_id: "sub_1",
        state: "published",
        score: 8.5,
        max_score: 10,
      },
    }],
  });
  assert.equal(assignments.length, 1);
  assert.equal(assignments[0]?.id, "asn_1");
  assert.equal(assignments[0]?.courseLabel, "cse101-2026f");
  assert.equal(assignments[0]?.assignmentPath, "assignments/lab03");
  assert.equal(assignments[0]?.repository?.githubRepositoryId, 123);
  assert.equal(assignments[0]?.repository?.targetRef, "refs/heads/submission/lab03");
  assert.equal(assignments[0]?.latestSubmission?.score, 8.5);
});

test("bundle assignment projection is normalized without a repository", () => {
  const assignments = normalizeAssignments({
    assignments: [{
      assignment_id: "lab01-v2",
      assignment_key: "lab01",
      title: "Lab 01",
      delivery_mode: "bundle",
      starter: {
        url: "/v1/assignments/lab01-v2/starter",
        sha256: `sha256:${"c".repeat(64)}`,
        size_bytes: 1234,
      },
      status: "open",
      ready: true,
      latest_submission: {
        submission_id: "sub_2",
        state: "queued",
        source_sha256: `sha256:${"a".repeat(64)}`,
      },
    }],
  });
  const assignment = assignments[0];
  assert.ok(assignment);
  assert.equal(isBundleAssignment(assignment), true);
  assert.equal(isAssignmentDownloadable(assignment), true);
  assert.equal(assignment.starterUrl, "/v1/assignments/lab01-v2/starter");
  assert.equal(assignment.starterSha256, "c".repeat(64));
  assert.equal(assignment.starterSizeBytes, 1234);
  assert.equal(assignment.repository, undefined);
  assert.equal(assignment.latestSubmission?.sourceDigest, `sha256:${"a".repeat(64)}`);
  assert.equal(safeAssignmentDirectoryName(assignment), "lab01");
});

test("draft bundle assignments are not downloadable until ready", () => {
  const assignment = {
    id: "asn_1",
    title: "Lab",
    deliveryMode: "bundle",
    status: "draft",
  };
  assert.equal(isAssignmentDownloadable(assignment), false);
  assert.equal(isAssignmentDownloadable({ ...assignment, ready: true }), true);
  assert.equal(isAssignmentDownloadable({ ...assignment, status: "open", ready: false }), false);
  assert.equal(isAssignmentDownloadable({ ...assignment, status: "unavailable" }), false);
});

test("result rubric accepts server-side mapping projections", () => {
  const result = normalizeGradeResult({
    result: {
      commit_sha: "a".repeat(40),
      source_digest: "b".repeat(64),
      score: 8,
      max_score: 10,
      rubric: {
        correctness: { score: 6, max_score: 8, feedback: "경계 조건을 확인하세요." },
        style: 2,
      },
      diagnostics: [],
    },
  });
  assert.equal(result?.rubric.length, 2);
  assert.equal(result?.sourceDigest, "b".repeat(64));
  assert.equal(result?.rubric[0]?.name, "correctness");
  assert.equal(result?.rubric[1]?.score, 2);
});
