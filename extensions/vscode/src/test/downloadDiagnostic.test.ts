import assert from "node:assert/strict";
import test from "node:test";
import { ApiError, RequestCancelledError } from "../api";
import { DownloadDiagnostic, DownloadFailure, classifyDownloadError } from "../downloadDiagnostic";

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
