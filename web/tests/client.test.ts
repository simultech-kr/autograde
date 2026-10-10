import test from "node:test";
import assert from "node:assert/strict";
import { ApiError, bootstrap, entryURL, resultsURL, terminalError } from "../src/lib/api/client.ts";
import { defaultQuery, filterKey, pageURL, parseQuery, pollDelay } from "../src/features/results/query.ts";
import { availableModules } from "../src/features/registry.ts";
import { sampleBootstrap } from "./fixtures.ts";

test("entry stays in Basic protection space and never accepts a return URL", () => {
  assert.equal(entryURL("come2201"), "/instructor/api/v1/entry?screen=results&course_key=come2201");
});
test("results query pins snapshot for paging and omits it only on explicit refresh", () => {
  const url = resultsURL("come2201", { ...defaultQuery, page: 2, student: "가상 & 학생" }, "a".repeat(64));
  const p = new URL(url, "https://example.test").searchParams;
  assert.equal(p.get("version"), "a".repeat(64)); assert.equal(p.get("student"), "가상 & 학생"); assert.equal(p.get("page"), "2");
  assert.ok(!resultsURL("come2201", defaultQuery).includes("version="));
});
test("URL filter parsing is bounded and page changes share a scope", () => {
  const q = parseQuery(new URLSearchParams({ student: "x".repeat(101), page: "NaN", status: "bad", sort: "bad", assignment_id: "gone" }));
  assert.equal(q.student.length, 100); assert.equal(q.page, 1); assert.equal(q.assignment, "gone"); assert.equal(q.status, "all");
  assert.equal(filterKey(q), filterKey({ ...q, page: 2 }));
  assert.ok(pageURL("come2201", q).includes("assignment_id=gone"));
});
test("terminal authorization errors stop; transient errors back off without busy retries", () => {
  for (const status of [401, 403, 404]) assert.equal(terminalError(new ApiError(status, "denied", "denied")), true);
  for (const status of [409, 429, 503]) assert.equal(terminalError(new ApiError(status, "retry", "retry")), false);
  assert.equal(pollDelay(0, 0), 5000); assert.equal(pollDelay(1, 0), 10000); assert.equal(pollDelay(8, 1), 60500);
});
test("API fetch uses same origin, no-store, abort signal and no Authorization", async () => {
  const original = globalThis.fetch; const signal = new AbortController().signal;
  globalThis.fetch = async (input, init) => {
    assert.equal(input, "/instructor/api/v1/bootstrap"); assert.equal(init?.credentials, "same-origin");
    assert.equal(init?.cache, "no-store"); assert.equal(init?.redirect, "error"); assert.equal(init?.signal, signal);
    assert.deepEqual(init?.headers, { Accept: "application/json" }); return Response.json(sampleBootstrap);
  };
  try { assert.deepEqual(await bootstrap(signal), sampleBootstrap); } finally { globalThis.fetch = original; }
});
test("invalid JSON failure never falls back to fixture or empty success", async () => {
  const original = globalThis.fetch; globalThis.fetch = async () => new Response("<html>proxy error</html>", { status: 503 });
  try { await assert.rejects(() => bootstrap(new AbortController().signal), e => e instanceof ApiError && e.status === 503); } finally { globalThis.fetch = original; }
});
test("trusted registry exposes only implemented, permitted features", () => {
  assert.deepEqual(availableModules([]), []);
  assert.deepEqual(availableModules(["results.read", "arbitrary-remote-plugin"]).map(m => m.id), ["courses", "results"]);
});
