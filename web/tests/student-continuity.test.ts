import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {JSDOM} from "jsdom";
import {act, createElement, type ComponentType} from "react";
import ts from "typescript";
import {ResumeBarrier, canResumePrivate, copyHandoffValue, usableClaim} from "../src/features/student/continuity.ts";
import type {Bootstrap} from "../src/features/student/model.ts";

const studentKey = "20260001", code = "SYNTHETIC-CLAIM", courseKey = "come2201";
const now = Date.parse("2026-10-30T03:00:00Z");
const stamp = (milliseconds: number) => new Date(now + milliseconds).toISOString();
const course = {course_key: courseKey, code: "COME2201", name: "소프트웨어 설계", section: "01", status: "matched", request_status: "matched", enrolled: true, eligible: true, can_withdraw: false};
const bootstrap = () => ({ok: true, result: {authenticated: true, csrf: "synthetic_csrf", expires_at: stamp(600000), server_time: stamp(0), student: {student_key: studentKey, verified: true}, api_base_url: "https://example.test:20000", courses: [{...course}]}});
const courseResult = () => ({ok: true, result: {student_key: studentKey, course, csrf: "synthetic_csrf", server_time: stamp(0), api_base_url: "https://example.test:20000", links: {assignments: `/courses/${courseKey}/assignments`}, overview: {assignments: [{assignment_id: "assignment_01", assignment_key: "week09", title: "Iterator 실습", opens_at: null, due_at: stamp(86400000), accepted_at: stamp(-86400000)}], latest: [], history: [], history_truncated: false, latest_truncated: false, assignments_truncated: false, limit: 100}}});
const claimResult = (expiry = stamp(180000)) => ({ok: true, result: {student_key: studentKey, claim: {assignment_id: "assignment_01", title: "Iterator 실습", code, expires_at: expiry, api_url: "https://example.test:20000"}}});

test("only the same unexpired identity and active enrollment can resume private context", () => {
  const value = bootstrap().result as Bootstrap;
  assert.equal(canResumePrivate(studentKey, value, courseKey, now), true);
  for (const changed of [
    {...value, student: {student_key: "20260002", verified: true}},
    {...value, student: {...value.student!, verified: false}},
    {...value, courses: [{...value.courses[0], enrolled: false}]},
    {...value, authenticated: false, student: null, expires_at: null},
    {...value, expires_at: stamp(-1)},
  ]) assert.equal(canResumePrivate(studentKey, changed, courseKey, now), false);
  const claim = claimResult().result.claim;
  assert.equal(usableClaim(claim, "assignment_01", now), true);
  assert.equal(usableClaim(claim, "assignment_02", now), false);
  assert.equal(usableClaim(claim, "assignment_01", now + 180000), false);
});

test("focus waits for outstanding mutations exactly once without replay", () => {
  const barrier = new ResumeBarrier(); barrier.begin(); barrier.begin();
  assert.equal(barrier.request(), false); assert.equal(barrier.request(), false);
  assert.equal(barrier.finish(), false); assert.equal(barrier.finish(), true);
  assert.equal(barrier.finish(), false); assert.equal(barrier.waiting, false);
});

test("copy reports manual fallback when the browser denies clipboard access", async () => {
  assert.equal(await copyHandoffValue(code), "manual");
  assert.equal(await copyHandoffValue(code, {writeText: async () => {throw new Error("denied");}}), "manual");
});

// Execute the actual component with React DOM. Only TSX/CSS loading is adapted for
// Node's test runner; requests, focus events, effects and form submissions are real.
function loadPortal(): ComponentType {
  const require = createRequire(import.meta.url), modules = new Map<string, {exports: unknown}>();
  function load(filename: string): unknown {
    if (modules.has(filename)) return modules.get(filename)!.exports;
    if (filename.endsWith(".css")) return {default: new Proxy({}, {get: (_object, key) => key}), __esModule: true};
    const module = {exports: {}}; modules.set(filename, module);
    const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true}}).outputText;
    new Function("require", "module", "exports", compiled)((specifier: string) => specifier.startsWith(".") ? load(resolve(dirname(filename), /\.(tsx?|css)$/.test(specifier) ? specifier : `${specifier}.ts`)) : require(specifier), module, module.exports);
    return module.exports;
  }
  return (load(fileURLToPath(new URL("../src/features/student/StudentPortal.tsx", import.meta.url))) as {StudentPortal: ComponentType}).StudentPortal;
}

type Handler = (url: string, options: RequestInit | undefined) => Promise<Response>;
async function mount(handler?: Handler, query = `?course_key=${courseKey}`) {
  const dom = new JSDOM("<!doctype html><div id='root'></div>", {url: `https://example.test/app/student${query}`, pretendToBeVisual: true});
  const originals = new Map<string, PropertyDescriptor | undefined>();
  const bind = (key: string, value: unknown) => {originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key)); Object.defineProperty(globalThis, key, {configurable: true, writable: true, value});};
  let visible = true, focused = true;
  Object.defineProperty(dom.window.document, "visibilityState", {get: () => visible ? "visible" : "hidden"});
  dom.window.document.hasFocus = () => focused;
  const intervals = new Map<number, () => void>(); let timer = 0;
  for (const [key, value] of Object.entries({window: dom.window, document: dom.window.document, navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement, FormData: dom.window.FormData, IS_REACT_ACT_ENVIRONMENT: true, matchMedia: () => ({matches: false}), requestAnimationFrame: (callback: FrameRequestCallback) => {callback(0); return 1;}, setInterval: (callback: () => void) => {intervals.set(++timer, callback); return timer;}, clearInterval: (id: number) => intervals.delete(id)})) bind(key, value);
  const requests: {url: string; options?: RequestInit}[] = [];
  bind("fetch", async (url: string, options?: RequestInit) => {requests.push({url, options}); return handler ? handler(url, options) : Response.json(url.endsWith("/claims") ? claimResult() : url.endsWith("/bootstrap") ? bootstrap() : courseResult());});
  const {createRoot} = await import("react-dom/client");
  const root = createRoot(dom.window.document.getElementById("root")!);
  await act(async () => root.render(createElement(loadPortal())));
  async function settle() {await act(async () => {await new Promise(resolve => setTimeout(resolve, 0));});}
  const button = (label: string) => {const match = Array.from(dom.window.document.querySelectorAll("button")).find(node => node.textContent === label); assert.ok(match, `button ${label}`); return match;};
  return {
    document: dom.window.document, requests, button, settle,
    surface: () => dom.window.document.querySelector("[data-theme] > div") as HTMLElement,
    code: () => dom.window.document.querySelector('[aria-label="발급한 수령 코드"]'),
    async click(label: string) {await act(async () => button(label).click());},
    async leave() {await act(async () => {visible = false; focused = false; dom.window.dispatchEvent(new dom.window.Event("blur")); dom.window.document.dispatchEvent(new dom.window.Event("visibilitychange"));});},
    async return() {await act(async () => {visible = true; focused = true; dom.window.document.dispatchEvent(new dom.window.Event("visibilitychange")); dom.window.dispatchEvent(new dom.window.Event("focus"));});},
    async tick() {await act(async () => {for (const callback of Array.from(intervals.values())) callback();});},
    async dispose() {await act(async () => root.unmount()); dom.window.close(); for (const [key, descriptor] of originals) {if (descriptor) Object.defineProperty(globalThis, key, descriptor); else Reflect.deleteProperty(globalThis, key);}},
  };
}

test("React: IDE switch masks data, preserves the same valid code, and never automatically reissues", async () => {
  const app = await mount();
  try {
    await app.click("이 실습 연결 코드 발급"); assert.equal(app.code()?.textContent, code);
    assert.match(app.document.body.textContent!, /이전 수락일 수 있으므로/);
    await app.leave(); assert.equal(app.surface().hidden, true);
    await app.return(); assert.equal(app.surface().hidden, false); assert.equal(app.code()?.textContent, code);
    await app.click("발급한 코드 보기"); assert.equal(app.requests.filter(r => r.url.endsWith("/claims")).length, 1);
    await app.click("수령 코드 복사"); assert.match(app.document.body.textContent!, /자동 복사가 허용되지 않았습니다/);
    assert.equal(app.document.defaultView!.localStorage.length, 0); assert.equal(app.document.defaultView!.sessionStorage.length, 0);
  } finally {await app.dispose();}
});

test("React: blur during claim POST does not abort it; focus revalidates after the result", async () => {
  let complete: (response: Response) => void = () => {};
  const app = await mount(async url => url.endsWith("/claims") ? new Promise<Response>(resolve => {complete = resolve;}) : Response.json(url.endsWith("/bootstrap") ? bootstrap() : courseResult()));
  try {
    await app.click("이 실습 연결 코드 발급"); const before = app.requests.length;
    await app.leave(); await app.return(); assert.equal(app.requests.length, before);
    assert.equal(app.requests.find(r => r.url.endsWith("/claims"))?.options?.signal?.aborted, false);
    assert.equal(app.surface().hidden, true);
    await act(async () => complete(Response.json(claimResult()))); await app.settle();
    assert.equal(app.code()?.textContent, code); assert.equal(app.surface().hidden, false);
    assert.equal(app.requests.filter(r => r.url.endsWith("/claims")).length, 1);
  } finally {await app.dispose();}
});

test("React: failed revalidation remains masked, then revoked membership clears the code", async () => {
  let mode = "normal";
  const app = await mount(async url => {
    if (url.endsWith("/bootstrap")) {if (mode === "offline") throw new Error("offline"); const body = bootstrap(); if (mode === "revoked") body.result.courses[0].enrolled = false; return Response.json(body);}
    return Response.json(url.endsWith("/claims") ? claimResult() : courseResult());
  });
  try {
    await app.click("이 실습 연결 코드 발급"); mode = "offline"; await app.leave(); await app.return();
    assert.equal(app.surface().hidden, true); assert.match(app.document.body.textContent!, /로그인 상태를 다시 확인/);
    mode = "revoked"; await app.click("다시 확인"); assert.equal(app.surface().hidden, false); assert.equal(app.code(), null);
    assert.match(app.document.body.textContent!, /이 수업을 아직 이용할 수 없습니다/);
  } finally {await app.dispose();}
});

test("React: another account and an expired code never inherit the previous handoff", async () => {
  let switched = false;
  const app = await mount(async url => {
    if (url.endsWith("/bootstrap")) {const body = bootstrap(); if (switched) body.result.student.student_key = "20260002"; return Response.json(body);}
    if (url.endsWith("/claims")) return Response.json(claimResult(stamp(-1000)));
    const body = courseResult(); if (switched) body.result.student_key = "20260002"; return Response.json(body);
  });
  try {
    await app.click("이 실습 연결 코드 발급"); assert.equal(app.code(), null); assert.match(app.document.body.textContent!, /수령 코드가 만료되었습니다/);
    switched = true; await app.leave(); await app.return(); assert.equal(app.code(), null);
    assert.match(app.document.body.textContent!, /20260002/); assert.doesNotMatch(app.document.body.textContent!, /20260001/);
  } finally {await app.dispose();}
});

test("React: server revocation and logout discard the previous account's valid code", async () => {
  let mode = "normal";
  const anonymous = () => ({...bootstrap(), result: {...bootstrap().result, authenticated: false, student: null, expires_at: null}});
  const app = await mount(async url => {
    if (url.endsWith("/logout")) {mode = "anonymous"; return Response.json(anonymous());}
    if (url.endsWith("/bootstrap")) {
      if (mode === "revoked") return Response.json({ok: false, error: {code: "login_required", message: "다시 로그인하세요."}}, {status: 403});
      return Response.json(mode === "anonymous" ? anonymous() : bootstrap());
    }
    return Response.json(url.endsWith("/claims") ? claimResult() : courseResult());
  });
  try {
    await app.click("이 실습 연결 코드 발급"); mode = "revoked"; await app.leave(); await app.return();
    assert.equal(app.code(), null); assert.equal(app.surface().hidden, true);
    mode = "normal"; await app.click("다시 확인"); assert.equal(app.code(), null);
    await app.click("이 실습 연결 코드 발급"); assert.equal(app.code()?.textContent, code);
    await app.click("로그아웃"); assert.equal(app.code(), null); assert.match(app.document.body.textContent!, /학생 로그인/);
  } finally {await app.dispose();}
});

test("React: a failed login keeps the student number and course but clears the password", async () => {
  const anonymous = {...bootstrap(), result: {...bootstrap().result, authenticated: false, student: null, expires_at: null}};
  const app = await mount(async url => url.endsWith("/login") ? Response.json({ok: false, error: {code: "login_denied", message: "학번과 비밀번호를 확인하세요."}}, {status: 401}) : Response.json(anonymous));
  try {
    const student = app.document.querySelector<HTMLInputElement>('input[name="student_key"]')!, password = app.document.querySelector<HTMLInputElement>('input[name="password"]')!;
    student.value = studentKey; password.value = "synthetic-password";
    await act(async () => student.form!.dispatchEvent(new app.document.defaultView!.Event("submit", {bubbles: true, cancelable: true})));
    assert.equal(student.value, studentKey); assert.equal(password.value, "");
    assert.equal(app.document.defaultView!.location.search, `?course_key=${courseKey}`);
    assert.match(app.document.body.textContent!, /학번과 비밀번호를 확인하세요/);
  } finally {await app.dispose();}
});

test("React: a valid code from another account is removed on return", async () => {
  let switched = false;
  const app = await mount(async url => {
    if (url.endsWith("/bootstrap")) {const body = bootstrap(); if (switched) body.result.student.student_key = "20260002"; return Response.json(body);}
    if (url.endsWith("/claims")) return Response.json(claimResult());
    const body = courseResult(); if (switched) body.result.student_key = "20260002"; return Response.json(body);
  });
  try {
    await app.click("이 실습 연결 코드 발급"); assert.equal(app.code()?.textContent, code);
    switched = true; await app.leave(); await app.return(); assert.equal(app.code(), null);
    assert.doesNotMatch(app.document.body.textContent!, /20260001/);
  } finally {await app.dispose();}
});

test("React: a late claim after navigation cannot restore a discarded code", async () => {
  let complete: (response: Response) => void = () => {};
  const app = await mount(async url => url.endsWith("/claims") ? new Promise<Response>(resolve => {complete = resolve;}) : Response.json(url.endsWith("/bootstrap") ? bootstrap() : courseResult()));
  try {
    await app.click("이 실습 연결 코드 발급"); await app.click("내 수업");
    await act(async () => complete(Response.json(claimResult()))); assert.equal(app.code(), null);
    assert.match(app.document.body.textContent!, /신청한 수업/);
  } finally {await app.dispose();}
});

test("React: stale enrollment plus denied course does not enter an automatic retry loop", async () => {
  let restored = false;
  const app = await mount(async url => url.endsWith("/bootstrap") ? Response.json(bootstrap()) : restored ? Response.json(courseResult()) : Response.json({ok: false, error: {code: "course_denied", message: "수강 상태를 확인하세요."}}, {status: 403}));
  try {
    await app.settle();
    assert.equal(app.requests.length, 2); assert.equal(app.surface().hidden, true);
    assert.match(app.document.body.textContent!, /로그인 상태를 다시 확인/);
    restored = true; await app.click("다시 확인"); assert.equal(app.surface().hidden, false);
    assert.match(app.document.body.textContent!, /Iterator 실습/);
  } finally {await app.dispose();}
});

test("React: logout clears private data before an unknown result and only explicit retry completes it", async () => {
  let rejectLogout: (error: Error) => void = () => {}, attempts = 0;
  const anonymous = () => ({...bootstrap(), result: {...bootstrap().result, authenticated: false, student: null, expires_at: null}});
  const app = await mount(async url => {
    if (url.endsWith("/logout")) {attempts++; return attempts === 1 ? new Promise<Response>((_resolve, reject) => {rejectLogout = reject;}) : Response.json(anonymous());}
    return Response.json(url.endsWith("/bootstrap") ? bootstrap() : url.endsWith("/claims") ? claimResult() : courseResult());
  });
  try {
    await app.click("이 실습 연결 코드 발급"); await app.click("로그아웃");
    assert.equal(app.code(), null); assert.equal(app.surface().hidden, true); assert.doesNotMatch(app.document.body.textContent!, /20260001/);
    await act(async () => rejectLogout(new Error("connection lost")));
    assert.equal(app.surface().hidden, true); assert.match(app.document.body.textContent!, /로그아웃 완료를 확인하지 못했습니다/);
    await app.leave(); await app.return(); assert.equal(app.surface().hidden, true); assert.equal(attempts, 1);
    assert.doesNotMatch(app.document.body.textContent!, /20260001/);
    await app.click("로그아웃 다시 시도"); assert.equal(app.surface().hidden, false); assert.match(app.document.body.textContent!, /학생 로그인/);
    assert.equal(attempts, 2);
  } finally {await app.dispose();}
});

test("React: logout rejection cannot revive a student and confirmed server logout returns to login", async () => {
  let ended = false;
  const app = await mount(async url => {
    if (url.endsWith("/logout")) {ended = true; return Response.json({ok: false, error: {code: "login_required", message: "로그인이 필요합니다."}}, {status: 401});}
    if (url.endsWith("/bootstrap")) return Response.json(ended ? {...bootstrap(), result: {...bootstrap().result, authenticated: false, student: null, expires_at: null}} : bootstrap());
    return Response.json(url.endsWith("/claims") ? claimResult() : courseResult());
  });
  try {
    await app.click("이 실습 연결 코드 발급"); await app.click("로그아웃");
    assert.equal(app.code(), null); assert.equal(app.surface().hidden, true);
    await app.click("다시 확인"); assert.equal(app.surface().hidden, false); assert.match(app.document.body.textContent!, /학생 로그인/);
  } finally {await app.dispose();}
});

test("React: authenticated action authorization and malformed responses fail closed", async () => {
  for (const status of [401, 403, 200]) {
    const pending = () => {const value = bootstrap(); value.result.courses[0] = {...value.result.courses[0], enrolled: false, status: "pending", request_status: "pending", can_withdraw: true}; return value;};
    const app = await mount(async url => url.endsWith("/withdraw") ? Response.json(status === 200 ? {ok: true, result: {bad: true}} : {ok: false, error: {code: "login_required", message: "다시 확인하세요."}}, {status}) : Response.json(pending()), "");
    try {
      await app.click("신청 취소"); assert.equal(app.surface().hidden, true); assert.doesNotMatch(app.document.body.textContent!, /20260001/);
      await app.click("다시 확인"); assert.equal(app.surface().hidden, false); assert.equal(app.button("신청 취소").disabled, false);
    } finally {await app.dispose();}
  }
});

test("React: enrollment refresh during POST waits for completion and never leaves busy stuck", async () => {
  let complete: (response: Response) => void = () => {};
  const pending = () => {const value = bootstrap(); value.result.courses[0] = {...value.result.courses[0], enrolled: false, status: "pending", request_status: "pending", can_withdraw: true}; return value;};
  const app = await mount(async url => url.endsWith("/withdraw") ? new Promise<Response>(resolve => {complete = resolve;}) : Response.json(pending()), "");
  try {
    await app.click("신청 취소"); await app.click("내 수업"); const count = app.requests.length;
    await app.click("명단 연결 다시 확인"); assert.equal(app.requests.length, count);
    assert.equal(app.requests.find(r => r.url.endsWith("/withdraw"))?.options?.signal?.aborted, false);
    await act(async () => complete(Response.json(pending())));
    assert.equal(app.button("신청 취소").disabled, false); assert.equal(app.button("로그아웃").disabled, false);
    assert.equal(app.requests.filter(r => r.url.endsWith("/withdraw")).length, 1);
    assert.equal(app.requests.filter(r => r.url.endsWith("/bootstrap")).length, 2);
  } finally {await app.dispose();}
});
