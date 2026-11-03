import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {JSDOM} from "jsdom";
import {act, createElement, type ComponentType} from "react";
import ts from "typescript";
import {parsePrivacyOverview} from "../src/features/student/privacy.ts";

const student = "20260001", session = "synthetic_csrf", course = "come2201";
const policy = Object.fromEntries(["version", "title", "purpose", "items", "retention", "choice", "withdrawal", "scope", "contact"].map(key => [key, "synthetic " + key]));
const stamp = "2026-11-03T00:00:00Z";
function consent(decision = "unanswered", revision = 0, key = course) {
  return {student_key: student, course_key: key, course_name: key === course ? "소프트웨어 설계" : "시스템 프로그래밍", policy: {...policy}, decision, revision, prompt_dismissed: false,
    updated_at: revision ? stamp : null, research_allowed: decision === "granted", history: revision ? [{decision, policy_version: policy.version, created_at: stamp}] : []};
}
const overview = (rows = [consent()], csrf = session) => ({ok: true, result: {student_key: student, csrf, policy: {...policy}, consents: rows}});

test("overview only accepts the signed-in student's unique, consistent course choices", () => {
  assert.equal(parsePrivacyOverview(overview(), student).consents.length, 1);
  assert.equal(parsePrivacyOverview(overview([]), student).consents.length, 0);
  for (const body of [overview([consent(), consent()]), overview([{...consent(), student_key: "20260002"}]),
    overview([{...consent(), course_key: "../escape"}]), overview([{...consent(), policy: {...policy, version: "old"}}]),
    overview([], ""), overview([], "unsafe\ncsrf"), {...overview(), result: {...overview().result, student_key: "20260002"}}]) {
    assert.throws(() => parsePrivacyOverview(body, student));
  }
});

type Props = {studentKey: string; sessionKey: string; visible: boolean; settings: boolean; preferredCourse?: string; onOpenSettings: () => void; onMutationStart?: () => void; onMutationEnd?: () => void};
function loadComponent(): ComponentType<Props> {
  const require = createRequire(import.meta.url), modules = new Map<string, {exports: unknown}>();
  function load(filename: string): unknown {
    if (modules.has(filename)) return modules.get(filename)!.exports;
    if (filename.endsWith(".css")) return {default: new Proxy({}, {get: (_object, key) => key}), __esModule: true};
    const module = {exports: {}}; modules.set(filename, module);
    const compiled = ts.transpileModule(readFileSync(filename, "utf8"), {compilerOptions: {jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, esModuleInterop: true}}).outputText;
    new Function("require", "module", "exports", compiled)((specifier: string) => specifier.startsWith(".") ? load(resolve(dirname(filename), /\.(tsx?|css)$/.test(specifier) ? specifier : `${specifier}.ts`)) : require(specifier), module, module.exports);
    return module.exports;
  }
  return (load(fileURLToPath(new URL("../src/features/student/StudentPrivacy.tsx", import.meta.url))) as {StudentPrivacy: ComponentType<Props>}).StudentPrivacy;
}
type Handler = (url: string, options: RequestInit | undefined) => Promise<Response>;
async function mount(handler: Handler, initial: Partial<Props> = {}) {
  const dom = new JSDOM("<!doctype html><button id='before'>실습 열기</button><div id='root'></div>", {url: "https://example.test/app/student", pretendToBeVisual: true});
  const originals = new Map<string, PropertyDescriptor | undefined>();
  const bind = (key: string, value: unknown) => {originals.set(key, Object.getOwnPropertyDescriptor(globalThis, key)); Object.defineProperty(globalThis, key, {configurable: true, writable: true, value});};
  for (const [key, value] of Object.entries({window: dom.window, document: dom.window.document, navigator: dom.window.navigator, HTMLElement: dom.window.HTMLElement, IS_REACT_ACT_ENVIRONMENT: true})) bind(key, value);
  const requests: {url: string; options?: RequestInit}[] = []; let started = 0, ended = 0, opened = 0;
  bind("fetch", async (url: string, options?: RequestInit) => {requests.push({url, options}); return handler(url, options);});
  const {createRoot} = await import("react-dom/client"), root = createRoot(dom.window.document.getElementById("root")!), Component = loadComponent();
  let props: Props = {studentKey: student, sessionKey: session, visible: true, settings: false, onOpenSettings: () => {opened++;}, onMutationStart: () => {started++;}, onMutationEnd: () => {ended++;}, ...initial};
  dom.window.document.getElementById("before")!.focus();
  await act(async () => root.render(createElement(Component, props)));
  const button = (label: string) => {const value = Array.from(dom.window.document.querySelectorAll("button")).find(node => node.textContent === label || node.getAttribute("aria-label") === label); assert.ok(value, label); return value;};
  return {document: dom.window.document, requests, button, counters: () => ({started, ended, opened}),
    dialog: () => dom.window.document.querySelector('[role="dialog"]'),
    async click(label: string) {await act(async () => button(label).click());},
    async render(next: Partial<Props>) {props = {...props, ...next}; await act(async () => root.render(createElement(Component, props)));},
    async key(key: string, shiftKey = false) {await act(async () => dom.window.document.dispatchEvent(new dom.window.KeyboardEvent("keydown", {key, shiftKey, bubbles: true, cancelable: true})));},
    async settle() {await act(async () => {await new Promise(resolve => setTimeout(resolve, 0));});},
    async dispose() {await act(async () => root.unmount()); dom.window.close(); for (const [key, value] of originals) {if (value) Object.defineProperty(globalThis, key, value); else Reflect.deleteProperty(globalThis, key);}},
  };
}

test("unanswered reminder traps keyboard focus, Escape records no consent, and same-session return does not nag", async () => {
  const app = await mount(async () => Response.json(overview()));
  try {
    assert.ok(app.dialog()); assert.equal(app.document.activeElement?.id, "research-dialog-heading");
    await app.key("Tab", true); assert.equal(app.document.activeElement?.textContent, "모든 수업의 설정 보기");
    await app.key("Tab"); assert.equal(app.document.activeElement?.getAttribute("aria-label"), "나중에 선택하고 닫기");
    await app.key("Escape"); assert.equal(app.dialog(), null); assert.equal(app.document.activeElement?.id, "before");
    await app.render({visible: false}); await app.render({visible: true}); assert.equal(app.dialog(), null);
    await app.render({settings: true}); assert.match(app.document.body.textContent!, /아직 선택하지 않았습니다/);
    assert.ok(app.button("동의합니다")); assert.ok(app.button("동의하지 않습니다"));
    assert.equal(app.requests.filter(row => row.options?.method === "POST" && !row.url.endsWith("/dismiss")).length, 0);
    assert.equal(app.document.defaultView!.localStorage.length, 0); assert.equal(app.document.defaultView!.sessionStorage.length, 0);
  } finally {await app.dispose();}
});

test("opt-in and opt-out are explicit, course-scoped choices with CAS and visible history", async () => {
  let row = consent("declined", 1);
  const app = await mount(async (_url, options) => {
    if (options?.method === "POST") {
      const values = new URLSearchParams(String(options.body));
      assert.equal(values.get("csrf"), session); assert.equal(values.get("revision"), String(row.revision));
      assert.equal(values.has("student_key"), false); assert.equal(values.has("course_key"), false);
      row = consent(values.get("decision")!, row.revision + 1);
      return Response.json({ok: true, result: row});
    }
    return Response.json(overview([row]));
  });
  try {
    assert.equal(app.dialog(), null); await app.render({settings: true});
    assert.equal(app.button("동의하지 않습니다").disabled, true);
    await app.click("동의합니다"); assert.equal(app.button("동의합니다").disabled, true);
    assert.ok(app.button("동의 철회")); assert.match(app.document.body.textContent!, /정보 활용에 동의했습니다/);
    await app.click("동의 철회"); assert.equal(app.button("동의하지 않습니다").disabled, true);
    assert.match(app.document.body.textContent!, /연구용 집계에서 내 정보를 제외/);
    assert.match(app.document.body.textContent!, /내 선택 변경 이력/);
    assert.deepEqual(app.counters(), {started: 2, ended: 2, opened: 0});
    assert.equal(app.requests.filter(row => row.options?.method === "POST" && !row.url.endsWith("/dismiss")).length, 2);
  } finally {await app.dispose();}
});

test("answering one prompt does not imply consent for other courses or open back-to-back prompts", async () => {
  let rows = [consent(), consent("unanswered", 0, "come3105")];
  const app = await mount(async (url, options) => {
    if (url.endsWith("/dismiss")) return Response.json({ok: true, result: {dismissed: true, policy_version: policy.version}});
    if (options?.method === "POST") {assert.equal(url, "/student/api/course/come3105/privacy"); rows[1] = consent("declined", 1, "come3105"); return Response.json({ok: true, result: rows[1]});}
    return Response.json(overview(rows));
  }, {preferredCourse: "come3105"});
  try {
    assert.equal((app.document.querySelector("select") as HTMLSelectElement).value, "come3105");
    await app.click("동의하지 않습니다"); assert.equal(app.dialog(), null); assert.equal(rows[0].decision, "unanswered");
    await app.render({visible: false}); await app.render({visible: true}); assert.equal(app.dialog(), null);
    await app.render({settings: true}); assert.match(app.document.body.textContent!, /소프트웨어 설계/); assert.match(app.document.body.textContent!, /아직 선택하지 않음/);
  } finally {await app.dispose();}
});

test("a new login can remind unanswered students, but an opt-out is not prompted", async () => {
  let csrf = session, row = consent();
  const app = await mount(async () => Response.json(overview([row], csrf)));
  try {
    await app.click("나중에 선택"); assert.equal(app.dialog(), null);
    csrf = "next_session"; await app.render({sessionKey: csrf}); assert.ok(app.dialog());
    await app.click("나중에 선택"); row = consent("declined", 1);
    await app.render({visible: false}); await app.render({visible: true}); assert.equal(app.dialog(), null);
  } finally {await app.dispose();}
});

test("a newly published policy is not suppressed by dismissing an older version", async () => {
  let version = policy.version;
  const app = await mount(async () => {
    const value = overview(); value.result.policy.version = version; value.result.consents[0].policy.version = version;
    return Response.json(value);
  });
  try {
    await app.click("나중에 선택"); assert.equal(app.dialog(), null);
    version = "new policy version"; await app.render({visible: false}); await app.render({visible: true});
    assert.ok(app.dialog()); assert.equal(app.requests.filter(row => row.options?.method === "POST" && !row.url.endsWith("/dismiss")).length, 0);
  } finally {await app.dispose();}
});

test("later dismissal is shared with reload and classic portal without becoming a consent choice", async () => {
  let dismissed = false, dismissWrites = 0;
  const handler: Handler = async (url, options) => {
    if (url.endsWith("/dismiss")) {
      assert.equal(options?.method, "POST");
      assert.deepEqual(Object.fromEntries(new URLSearchParams(String(options?.body))), {csrf: session, policy_version: policy.version});
      dismissed = true; dismissWrites++;
      return Response.json({ok: true, result: {dismissed: true, policy_version: policy.version}});
    }
    assert.equal(options?.method, "GET");
    return Response.json(overview([{...consent(), prompt_dismissed: dismissed}]));
  };
  const first = await mount(handler);
  try {
    assert.ok(first.dialog()); await first.click("나중에 선택"); await first.settle();
    assert.equal(dismissWrites, 1); assert.equal(first.dialog(), null);
  } finally {await first.dispose();}
  // A remount has no local reminder cache; the login-session flag alone keeps
  // the same result after reload or dismissal from the classic portal.
  const reloaded = await mount(handler);
  try {
    assert.equal(reloaded.dialog(), null); await reloaded.render({settings: true});
    assert.match(reloaded.document.body.textContent!, /아직 선택하지 않았습니다/);
    assert.equal(reloaded.button("동의합니다").disabled, false);
    assert.equal(reloaded.button("동의하지 않습니다").disabled, false);
    assert.equal(dismissWrites, 1);
  } finally {await reloaded.dispose();}
});

test("failed reminder dismissal stays locally closed and is never retried automatically", async () => {
  let dismissWrites = 0;
  const app = await mount(async url => {
    if (url.endsWith("/dismiss")) {dismissWrites++; return Response.json({ok: false, error: {code: "request_failed", message: "synthetic failure"}}, {status: 503});}
    return Response.json(overview());
  });
  try {
    await app.click("나중에 선택"); await app.settle(); assert.equal(app.dialog(), null);
    await app.render({visible: false}); await app.render({visible: true});
    assert.equal(app.dialog(), null); assert.equal(dismissWrites, 1);
    assert.equal(app.requests.filter(row => row.options?.method === "POST" && !row.url.endsWith("/dismiss")).length, 0);
  } finally {await app.dispose();}
});

test("failed writes remove stale controls and require a fresh read without retrying consent", async () => {
  for (const status of [409, 500]) {
    let row = consent();
    const app = await mount(async (_url, options) => {
      if (options?.method === "POST") {row = consent("declined", 1); return Response.json({ok: false, error: {code: "consent_conflict", message: "현재 선택을 다시 확인하세요."}}, {status});}
      return Response.json(overview([row]));
    }, {settings: true});
    try {
      await app.click("동의합니다"); assert.match(app.document.body.textContent!, /현재 선택을 다시 확인/);
      assert.equal(Array.from(app.document.querySelectorAll("button")).some(button => button.textContent === "동의합니다"), false);
      await app.click("상태 새로고침"); assert.equal(app.button("동의하지 않습니다").disabled, true);
      assert.equal(app.requests.filter(row => row.options?.method === "POST" && !row.url.endsWith("/dismiss")).length, 1);
    } finally {await app.dispose();}
  }
});

test("summary failure is nonblocking and never invents an unanswered choice", async () => {
  for (const body of [overview([], "different_session"), {...overview(), result: {...overview().result, student_key: "20260002"}}]) {
    const app = await mount(async () => Response.json(body));
    try {
      assert.equal(app.dialog(), null); assert.match(app.document.body.textContent!, /실습은 계속할 수 있습니다/);
      await app.click("설정에서 확인"); assert.equal(app.counters().opened, 1);
      assert.equal(app.requests.filter(row => row.options?.method === "POST" && !row.url.endsWith("/dismiss")).length, 0);
    } finally {await app.dispose();}
  }
});

test("backgrounding hides the prompt immediately and resumes only after checking the current choice", async () => {
  let row = consent(), resolveRead: ((response: Response) => void) | undefined, pause = false;
  const app = await mount(async () => pause ? new Promise<Response>(resolve => {resolveRead = resolve;}) : Response.json(overview([row])));
  try {
    assert.ok(app.dialog()); await app.render({visible: false}); assert.equal(app.dialog(), null);
    pause = true; await app.render({visible: true}); assert.equal(app.dialog(), null);
    row = consent("declined", 1); await act(async () => resolveRead!(Response.json(overview([row]))));
    assert.equal(app.dialog(), null);
  } finally {await app.dispose();}
});

test("an in-flight choice survives a focus change and is never posted twice", async () => {
  let complete: ((response: Response) => void) | undefined, row = consent();
  const app = await mount(async (url, options) => options?.method === "POST" && !url.endsWith("/dismiss") ? new Promise<Response>(resolve => {complete = resolve;}) : Response.json(overview([row])));
  try {
    await app.click("동의합니다"); await app.render({visible: false}); await app.render({visible: true});
    assert.equal(app.dialog(), null); assert.equal(app.requests.find(item => item.options?.method === "POST")!.options!.signal!.aborted, false);
    row = consent("granted", 1); await act(async () => complete!(Response.json({ok: true, result: row}))); await app.settle();
    assert.equal(app.dialog(), null); assert.equal(app.requests.filter(item => item.options?.method === "POST" && !item.url.endsWith("/dismiss")).length, 1);
    assert.deepEqual(app.counters(), {started: 1, ended: 1, opened: 0});
  } finally {await app.dispose();}
});
