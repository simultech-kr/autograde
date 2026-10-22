import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, RequestCancelledError } from "../api";
import { DownloadDiagnostic, DownloadFailure, classifyDownloadError, downloadGuidance } from "../downloadDiagnostic";

for (const [native, code, advice] of [
  ["EACCES", "LOCAL-PERMISSION", /다른 폴더/],
  ["EPERM", "LOCAL-PERMISSION", /다른 폴더/],
  ["ENOSPC", "LOCAL-SPACE", /여유 공간/],
  ["EEXIST", "LOCAL-EXISTS", /덮어쓰지/],
  ["ENAMETOOLONG", "LOCAL-PATH", /문의번호/],
  ["ENOENT", "LOCAL-PATH", /문의번호/],
  ["EIO", "LOCAL-IO", /문의번호/],
] as const) {
  test(`local ${native} failure exposes a safe category and recovery guidance`, () => {
    const d = new DownloadDiagnostic(); d.stage = "installing";
    d.fail(Object.assign(new Error("private-path-and-secret"), { code: native }));
    assert.equal(d.code, `AG-DL-${code}`);
    assert.match(downloadGuidance(d.code), advice);
    assert.doesNotMatch(d.text, /private-path-and-secret/);
    assert.equal(d.outcome, "failed");
  });
}

for (const [status, code, advice] of [
  [401, "AUTH-EXPIRED", /새 수령 코드/],
  [403, "ACCESS-DENIED", /접근 권한/],
  [429, "HTTP-RATE-LIMIT", /잠시 후/],
  [503, "HTTP-SERVER", /잠시 후/],
] as const) {
  test(`HTTP ${status} download failure has actionable, non-secret diagnostics`, () => {
    const d = new DownloadDiagnostic(); d.stage = "requesting";
    d.fail(new ApiError("private-server-message", status));
    assert.equal(d.code, `AG-DL-${code}`);
    assert.equal(d.httpStatus, status);
    assert.match(downloadGuidance(d.code), advice);
    assert.doesNotMatch(d.text + JSON.stringify(d.payload("test", "linux")), /private-server-message/);
  });
}

test("download diagnostics never serialize raw exception messages, paths or credentials", () => {
  const d = new DownloadDiagnostic(); d.stage = "installing";
  d.fail(Object.assign(new Error("token-secret /Users/student/private.c"), {code:"EACCES"}));
  assert.equal(d.code, "AG-DL-LOCAL-PERMISSION");
  assert.equal(d.outcome, "failed");
  assert.doesNotMatch(d.text + JSON.stringify(d.payload("0.5.2", "linux", "wsl")), /token-secret|private\.c|\/Users/);
});
test("opening failures never undo verified file readiness", () => {
  const d = new DownloadDiagnostic(); d.stage = "opening"; d.outcome = "succeeded";
  d.fail(new Error("IDE unavailable"));
  assert.equal(d.outcome, "succeeded"); assert.equal(d.openOutcome, "open_failed");
});
test("cancel and timeout are distinct and typed integrity failures survive", () => {
  assert.equal(classifyDownloadError(new RequestCancelledError(), "requesting"), "AG-DL-USER-CANCELLED");
  assert.equal(classifyDownloadError(new ApiError("", 0, "request_timeout"), "requesting"), "AG-DL-NETWORK-TIMEOUT");
  assert.equal(classifyDownloadError(new DownloadFailure("AG-DL-INTEGRITY-HASH"), "verifying"), "AG-DL-INTEGRITY-HASH");
  assert.equal(classifyDownloadError(new Error("TLS maybe"), "requesting"), "AG-DL-UNKNOWN");
});
test("bounded environment fields and sequence are deterministic", () => {
  const d = new DownloadDiagnostic();
  const a = d.payload("0.5.2", "darwin", "secret-hostname") as Record<string, unknown>;
  const b = d.payload("0.5.2", "linux", "wsl") as Record<string, unknown>;
  assert.equal(a.remote_kind, "other"); assert.equal(a.os, "macos");
  assert.equal(b.remote_kind, "wsl"); assert.equal(b.seq, 1); assert.equal(a.attempt_id, b.attempt_id);
});
