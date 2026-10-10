import test from "node:test";
import assert from "node:assert/strict";
import { ContractError, parseBootstrap, parseResults } from "../src/lib/contracts/instructor.ts";
import { sampleBootstrap, sampleResults } from "./fixtures.ts";

test("bootstrap accepts only current version and explicit server capabilities", () => {
  assert.deepEqual(parseBootstrap(sampleBootstrap), sampleBootstrap);
  assert.throws(() => parseBootstrap({ ...sampleBootstrap, contract_version: "2" }), ContractError);
  assert.throws(() => parseBootstrap({ ...sampleBootstrap, courses: [sampleBootstrap.courses[0], sampleBootstrap.courses[0]] }), ContractError);
});
test("latest zero is retained independently of previous best 100 and pending null", () => {
  const r = parseResults(sampleResults);
  assert.equal(r.rows[0].score, 0); assert.equal(r.rows[0].previous_best?.score, 100);
  assert.equal(r.rows[1].score, null); assert.equal(r.rows[3].submission_id, null);
});
test("malformed score, maximum, timestamp, version, and duplicate rows are rejected", () => {
  const check = (patch: object) => assert.throws(() => parseResults({ ...sampleResults, rows: [{ ...sampleResults.rows[0], ...patch }] }), ContractError);
  check({ score: "0" }); check({ score: Infinity }); check({ score: -1 }); check({ max_score: null });
  check({ score: 101 }); check({ max_score: 0 }); check({ received_at: "invalid" });
  check({ previous_best: { ...sampleResults.rows[0].previous_best, score: 101 } });
  assert.throws(() => parseResults({ ...sampleResults, version: "old" }), ContractError);
  assert.throws(() => parseResults({ ...sampleResults, rows: [sampleResults.rows[0], sampleResults.rows[0]] }), ContractError);
});
test("only scoped existing instructor detail links are accepted", () => {
  for (const detail_url of ["https://evil.example/", "//evil.example/", "/courses/other/instructor/submissions", "/courses/come2201/instructor/../login", "/courses/come2201/instructor/%2e%2e/login", "/courses/come2201/instructor/\\evil"]) {
    assert.throws(() => parseResults({ ...sampleResults, rows: [{ ...sampleResults.rows[0], detail_url }] }), ContractError);
  }
  const result = parseResults({ ...sampleResults, rows: [{ ...sampleResults.rows[0], detail_url: "/courses/come2201/instructor/students/SAMPLE-001/submissions?assignment_id=observer" }] });
  assert.match(result.rows[0].detail_url!, /^\/courses\/come2201\/instructor\//);
});
