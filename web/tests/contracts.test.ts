import test from "node:test";
import assert from "node:assert/strict";
import { ContractError, parseBootstrap, parseResults } from "../src/lib/contracts/instructor.ts";
import { sampleBootstrap, sampleResults } from "./fixtures.ts";

test("bootstrap accepts only current version and explicit server capabilities", () => {
  assert.deepEqual(parseBootstrap(sampleBootstrap), sampleBootstrap);
  assert.throws(() => parseBootstrap({ ...sampleBootstrap, contract_version: "2" }), ContractError);
  assert.throws(() => parseBootstrap({ ...sampleBootstrap, courses: [sampleBootstrap.courses[0], sampleBootstrap.courses[0]] }), ContractError);
});

test("navigation contract rejects cross-course, remote, duplicate and action destinations", () => {
  const item = { id: "materials", label: "학생별 자료 배포", href: "/courses/come2201/instructor/materials" };
  const payload = (items: object[]) => ({ ...sampleBootstrap, courses: [{ ...sampleBootstrap.courses[0], navigation: [{ label: "학습자료", items }] }] });
  assert.equal(parseBootstrap(payload([item])).courses[0].navigation?.[0].items[0].href, item.href);
  for (const href of ["https://evil.test/", "/courses/other/instructor/materials", "/courses/come2201/instructor/materials/../settings", "/courses/come2201/instructor/status", "//evil.test/"]) {
    assert.throws(() => parseBootstrap(payload([{ ...item, href }])), ContractError);
  }
  assert.throws(() => parseBootstrap(payload([item, item])), ContractError);
  assert.throws(() => parseBootstrap({ ...sampleBootstrap, portal_navigation: [{ label: "계정", items: [{ ...item, href: "/instructor/logout" }] }] }), ContractError);
});
test("latest zero is retained independently of previous best 100 and pending null", () => {
  const r = parseResults(sampleResults);
  assert.equal(r.rows[0].score, 0); assert.equal(r.rows[0].previous_best?.score, 100);
  assert.equal(r.rows[1].score, null); assert.equal(r.rows[3].submission_id, null);
});

test("new portal navigation accepts only the authenticated course and fixed student entry", () => {
  const payload = (href: string) => ({ ...sampleBootstrap, courses: [{ ...sampleBootstrap.courses[0],
    navigation: [{ label: "과제·평가", items: [{id: "results", label: "제출·채점 현황", href}] }] }],
    portal_navigation: [{label: "다른 포털", items: [{id: "student", label: "학생 포털", href: "/app/student"}]}] });
  assert.equal(parseBootstrap(payload("/app?course_key=come2201")).courses[0].navigation?.[0].items[0].href, "/app?course_key=come2201");
  for (const href of ["/app?course_key=other", "/app?course_key=come2201&next=https://evil.test", "//evil.test/app"]) {
    assert.throws(() => parseBootstrap(payload(href)), ContractError);
  }
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
