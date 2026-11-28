import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {JSDOM} from "jsdom";
import {act, createElement, type ComponentType} from "react";
import ts from "typescript";
import {parseBootstrap, StudentContractError, type Course} from "../src/features/student/model.ts";

const key = "20260001", courseKey = "come2201", csrf = "synthetic_csrf";
const stamp = (offset = 0) => new Date(Date.now() + offset).toISOString();
const course = {course_key:courseKey, code:"COME2201", name:"설계 실습", section:"01", status:"matched", request_status:"matched", enrolled:true, eligible:true, can_withdraw:false};
const anonymous = () => ({ok:true, result:{authenticated:false, csrf, expires_at:null, server_time:stamp(), student:null, courses:[], api_base_url:"https://example.test:20000"}});
const authenticated = () => ({ok:true, result:{authenticated:true, auth_method:"course", csrf, expires_at:stamp(600000), server_time:stamp(), student:{student_key:key, verified:true}, courses:[course], api_base_url:"https://example.test:20000"}});
const courseResult = () => ({ok:true, result:{schema_version:1, student_key:key, course, csrf, server_time:stamp(), api_base_url:"https://example.test:20000", links:{assignments:`/courses/${courseKey}/assignments`, materials:`/courses/${courseKey}/materials`}, overview:{assignments:[{assignment_id:"assignment_01", assignment_key:"hello", title:"Hello 실습", opens_at:null, due_at:stamp(86400000), accepted_at:null}], latest:[{submission_id:"submission_02", assignment_id:"assignment_01", assignment_key:"hello", title:"Hello 실습", received_at:stamp(-10000), state:"published", score:5, max_score:10, published_at:stamp(-5000), code_version:"b".repeat(64), version:2, previous_best_score:10, previous_best_max_score:10, previous_best_id:"submission_01", previous_best_at:stamp(-20000)}], history:[], assignments_truncated:false, latest_truncated:false, history_truncated:false, limit:100}}});
const homeResult = () => ({ok:true,result:{schema_version:1,student_key:key,server_time:stamp(),courses_truncated:false,
  courses:[{course_key:courseKey,overview:courseResult().result.overview}]}});
const materials = () => ({items:[{id:"mat_hello",kind:"material",title:"Hello 실습 자료",filename:"20260001.pdf",format:"pdf",size_bytes:4,
  updated_at:stamp(),opens_at:stamp(-10000),closes_at:stamp(86400000),availability:"open",revision:1,file_revision:1,edition:1,
  instructions:"제공한 예제로 실습하세요.",change_note:"",requested:false,download_path:`/student/api/course/${courseKey}/materials/mat_hello/download`}],truncated:false});
const privacy = () => ({ok:true, result:{student_key:key, csrf, policy:Object.fromEntries(["version","title","purpose","items","retention","choice","withdrawal","scope","contact"].map(field=>[field,`synthetic ${field}`])), consents:[]}});

test("course login contract is explicit and cannot include enrollment privileges or an unverified identity", () => {
  assert.equal(parseBootstrap(authenticated()).auth_method, "course");
  assert.equal(parseBootstrap(anonymous()).auth_method, "member");
  for (const result of [
    {...authenticated().result, auth_method:"admin"},
    {...authenticated().result, student:{student_key:key, verified:false}},
    {...authenticated().result, courses:[{...course, enrolled:false}]},
    {...authenticated().result, courses:[{...course, can_withdraw:true}]},
    {...anonymous().result, auth_method:"course"},
  ]) assert.throws(()=>parseBootstrap({ok:true,result}), StudentContractError);
});

// Run the production component; adapt only TSX/CSS module loading for Node.
function loadPortal(): ComponentType {
  const require=createRequire(import.meta.url), cache=new Map<string,{exports:unknown}>();
  function load(path:string):unknown {
    if (cache.has(path)) return cache.get(path)!.exports;
    if (path.endsWith(".css")) return {default:new Proxy({}, {get:(_object,key)=>key}), __esModule:true};
    const module={exports:{}}; cache.set(path,module);
    const javascript=ts.transpileModule(readFileSync(path,"utf8"),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
    new Function("require","module","exports",javascript)((specifier:string)=>specifier.startsWith(".") ? load(resolve(dirname(path),/\.(tsx?|css)$/.test(specifier) ? specifier : `${specifier}.ts`)) : require(specifier),module,module.exports);
    return module.exports;
  }
  return (load(fileURLToPath(new URL("../src/features/student/StudentPortal.tsx",import.meta.url))) as {StudentPortal:ComponentType}).StudentPortal;
}

async function mount(initiallyAuthenticated=false, query="", catalog?: {courses: Course[]}, getCourse=courseResult, historyState: unknown = null) {
  const dom=new JSDOM("<!doctype html><div id='root'></div>",{url:`https://example.test/app/student${query}`,pretendToBeVisual:true});
  const originals=new Map<string,PropertyDescriptor|undefined>();
  dom.window.history.replaceState(historyState, "");
  const bind=(name:string,value:unknown)=>{originals.set(name,Object.getOwnPropertyDescriptor(globalThis,name));Object.defineProperty(globalThis,name,{configurable:true,writable:true,value});};
  Object.defineProperty(dom.window.document,"visibilityState",{value:"visible"});
  dom.window.document.hasFocus=()=>true;
  for(const [name,value] of Object.entries({window:dom.window,document:dom.window.document,navigator:dom.window.navigator,HTMLElement:dom.window.HTMLElement,FormData:dom.window.FormData,IS_REACT_ACT_ENVIRONMENT:true,matchMedia:()=>({matches:false}),requestAnimationFrame:(callback:FrameRequestCallback)=>{callback(0);return 1;},setInterval:()=>1,clearInterval:()=>{}})) bind(name,value);
  let logged=initiallyAuthenticated, authMethod=catalog ? "member" : "course";
  const snapshot=()=>{const value=authenticated();return {...value,result:{...value.result,auth_method:authMethod,navigation_scope:(authMethod === "course" ? "a" : "b").repeat(64),courses:catalog?.courses ?? value.result.courses}};};
  const requests:{url:string;fields:URLSearchParams}[]=[];
  bind("fetch",async(url:string,options?:RequestInit)=>{
    const fields=new URLSearchParams(String(options?.body||"")); requests.push({url,fields});
    if(url==="/student/api/login") {assert.equal(fields.get("student_key"),key);assert.equal(fields.get("password"),"123456");assert.equal(fields.get("csrf"),csrf);logged=true;return Response.json(snapshot());}
    if(url==="/student/api/bootstrap") return Response.json(logged ? snapshot() : anonymous());
    if(url==="/student/api/enroll" && catalog) return Response.json(snapshot());
    if(url==="/student/api/home") return Response.json(homeResult());
    if(url==="/student/api/privacy") return Response.json(privacy());
    if(url===`/student/api/course/${courseKey}/claims`) return Response.json({ok:true,result:{student_key:key,claim:{assignment_id:"assignment_01",title:"Hello 실습",code:"SYNTHETIC-CODE",expires_at:stamp(180000),api_url:"https://example.test:20000"}}});
    if(url===`/student/api/course/${courseKey}`) {const value=getCourse();return Response.json({...value,result:{...value.result,materials:materials()}});}
    throw new Error(`Unexpected request ${url}`);
  });
  const {createRoot}=await import("react-dom/client");
  const root=createRoot(dom.window.document.getElementById("root")!);
  await act(async()=>root.render(createElement(loadPortal())));
  return {document:dom.window.document,requests,
    async click(label:string){const button=Array.from(dom.window.document.querySelectorAll<HTMLElement>("a, button")).find(node=>node.textContent?.trim()===label);assert.ok(button,`button ${label}`);await act(async()=>button.click());},
    async changeAuthentication(method:string){authMethod=method;await act(async()=>{dom.window.dispatchEvent(new dom.window.Event("focus"));});},
    async dispose(){await act(async()=>root.unmount());dom.window.close();for(const [name,descriptor] of originals){if(descriptor)Object.defineProperty(globalThis,name,descriptor);else Reflect.deleteProperty(globalThis,name);}}
  };
}

test("React: six-digit course password logs in without signup and retains claims, materials and own scores", async()=>{
  const app=await mount();
  try {
    assert.match(app.document.body.textContent!,/별도 회원가입은 필요하지 않습니다/);
    const form=app.document.querySelector("form")!;
    const password=form.querySelector<HTMLInputElement>('input[name="password"]')!;
    assert.equal(password.minLength,6);
    form.querySelector<HTMLInputElement>('input[name="student_key"]')!.value=key;
    password.value="123456";
    assert.equal(form.checkValidity(),true);
    await act(async()=>form.requestSubmit());
    assert.equal(password.value,"");
    assert.equal(app.requests.filter(request=>request.url==="/student/api/login").length,1);
    assert.equal(app.requests.some(request=>request.url==="/student/api/register"),false);
    const buttons=Array.from(app.document.querySelectorAll("button, a")).map(button=>button.textContent?.trim());
    for(const blocked of ["수강 선택","수업 추가","계정 활성화","신청 취소"]) assert.equal(buttons.includes(blocked),false);
    assert.match(app.document.body.textContent!,/지금 확인할 실습/);
    assert.equal(app.requests.filter(request=>request.url==="/student/api/home").length,1);
    await app.click("수업 열기 →");
    await app.click("수령 코드 발급");
    assert.equal(app.document.querySelector('[aria-label="발급한 수령 코드"]')?.textContent,"SYNTHETIC-CODE");
    const claim=app.requests.find(request=>request.url.endsWith("/claims"))!;
    assert.equal(claim.fields.get("assignment_id"),"assignment_01");
    assert.equal(claim.fields.has("student_key"),false);
    await app.click("학습 자료");
    assert.equal(app.document.querySelector('a[href="/courses/come2201/materials"]'),null);
    const material=app.document.querySelector('article[aria-label="Hello 실습 자료 자료"]')!;
    assert.match(material.textContent!,/20260001.pdf/);
    assert.equal(material.querySelector<HTMLButtonElement>('button[aria-label="Hello 실습 자료 PDF 다운로드"]')?.disabled,false);
    await app.click("내 점수");
    const scores=app.document.querySelector('[data-score-assignment="assignment_01"]')!;
    assert.match(scores.textContent!,/5 \/ 10점/);assert.match(scores.textContent!,/10 \/ 10점/);
    await app.click("내 계정");
    assert.match(app.document.body.textContent!,/수업 계정 · 수강 확인 완료/);
    assert.equal(app.document.querySelector<HTMLInputElement>('input[name="current_password"]')?.minLength,6);
    assert.equal(app.document.querySelector<HTMLInputElement>('input[name="new_password"]')?.minLength,8);
    assert.equal(app.document.defaultView!.localStorage.length,0);assert.equal(app.document.defaultView!.sessionStorage.length,0);
  } finally {await app.dispose();}
});

test("React: a course-session catalog deep link resolves to usable classes without enrollment controls",async()=>{
  const app=await mount(true,"?view=catalog");
  try {
    assert.equal(app.document.querySelector("main h1")?.textContent,"내 수업");
    assert.ok(Array.from(app.document.querySelectorAll("button")).some(button=>button.textContent?.trim()==="수업 열기 →"));
    assert.equal(app.document.querySelector('input[type="checkbox"]'),null);
    assert.equal(app.requests.some(request=>request.url.endsWith("/enroll")||request.url.endsWith("/activate")),false);
  } finally {await app.dispose();}
});

test("React: browser back to an old catalog entry normalizes a course session to home",async()=>{
  const app=await mount(true);
  try {
    const browser=app.document.defaultView!;
    // Reproduce an entry left in browser history by an earlier member session.
    browser.history.replaceState(null,"","/app/student?view=catalog");
    await app.click("내 계정");
    assert.equal(browser.location.search,"?view=account");
    await act(async()=>{
      const navigated=new Promise<void>(resolve=>browser.addEventListener("popstate",()=>resolve(),{once:true}));
      browser.history.back();
      await navigated;
    });
    assert.equal(app.document.querySelector("main h1")?.textContent,"내 수업");
    assert.equal(browser.location.search,"");
    assert.match(app.document.body.textContent!,/지금 확인할 실습/);
    assert.equal(app.document.querySelector('input[type="checkbox"]'),null);
    assert.equal(app.requests.some(request=>request.url.endsWith("/enroll")||request.url.endsWith("/activate")),false);
  } finally {await app.dispose();}
});

test("React: same student changing authentication source cannot inherit a prior claim",async()=>{
  const app=await mount(true,`?course_key=${courseKey}`);
  try {
    await app.click("수령 코드 발급");
    assert.equal(app.document.querySelector('[aria-label="발급한 수령 코드"]')?.textContent,"SYNTHETIC-CODE");
    await app.changeAuthentication("member");
    assert.equal(app.document.querySelector('[aria-label="발급한 수령 코드"]'),null);
    assert.equal(app.requests.filter(request=>request.url.endsWith("/claims")).length,1);
    assert.ok(Array.from(app.document.querySelectorAll("a")).some(link=>link.textContent?.trim()==="수강 선택"));
  } finally {await app.dispose();}
});

test("React: a task deep link focuses its assignment after initial authentication", async()=>{
  const app=await mount(true,`?course_key=${courseKey}&assignment=assignment_01`);
  try {
    assert.equal(app.document.activeElement?.getAttribute("data-assignment"),"assignment_01");
    assert.equal(app.document.querySelectorAll("[data-parent-link]").length,1);
  } finally {await app.dispose();}
});

test("React: opening a published result keeps focus on its score after the next frame", async () => {
  const app = await mount(true, `?course_key=${courseKey}`);
  const originalFrame = globalThis.requestAnimationFrame;
  const frames: FrameRequestCallback[] = [];
  globalThis.requestAnimationFrame = callback => frames.push(callback);
  const paint = async () => { await act(async () => { for (const frame of frames.splice(0)) frame(0); }); };
  try {
    await app.click("평가 결과 보기");
    await paint();
    const card = app.document.querySelector<HTMLElement>('[data-score-assignment="assignment_01"]')!;
    assert.equal(app.document.activeElement?.getAttribute("data-score-assignment"), "assignment_01");
    assert.equal(card.querySelector("dl dd")?.textContent, "5 / 10점");

    const gradebookButton = Array.from(app.document.querySelectorAll("button")).find(button => button.textContent === "시험·출석 성적")!;
    gradebookButton.focus();
    await app.click("상태 새로고침");
    assert.ok(app.document.activeElement === gradebookButton, "a data refresh must not repeat navigation");

    await app.click("내 점수");
    await paint();
    assert.ok(app.document.activeElement === app.document.querySelector("main h1"));

    const browser = app.document.defaultView!;
    await act(async () => {
      const navigated = new Promise<void>(resolve => browser.addEventListener("popstate", () => resolve(), {once: true}));
      browser.history.back();
      await navigated;
    });
    await paint();
    assert.equal(app.document.activeElement?.getAttribute("data-score-assignment"), "assignment_01");

    const historyButton = Array.from(app.document.querySelectorAll("button")).find(button => button.textContent === "시험·출석 성적")!;
    historyButton.focus();
    await act(async () => {
      browser.history.pushState(null, "", "#history-submission_02");
      browser.dispatchEvent(new browser.PopStateEvent("popstate"));
    });
    assert.ok(app.document.activeElement === historyButton, "a history fragment owns its own focus");
  } finally {
    globalThis.requestAnimationFrame = originalFrame;
    await app.dispose();
  }
});

const catalogCourse = (section: string): Course => ({
  ...course, course_key: `class_${section}`, code: "DES100", name: "설계 기초", term: "2026학년도 2학기", section,
  enrolled: false, status: "available", request_status: null,
});

test("React: enrollment keeps selected sections across searches and requires review when a selection becomes unavailable", async () => {
  const first = catalogCourse("01"), second = catalogCourse("02"), catalog = {courses: [first, second]};
  const app = await mount(true, "?view=catalog", catalog);
  try {
    const browser = app.document.defaultView!;
    const search = app.document.querySelector<HTMLInputElement>('input[placeholder="교과목명·코드·학기·분반"]')!;
    const checkbox = (section: string) => {
      const label = Array.from(app.document.querySelectorAll("label")).find(label => label.textContent?.includes(`${section}분반`));
      assert.ok(label);
      return label.querySelector<HTMLInputElement>('input[type="checkbox"]')!;
    };
    async function filter(value: string) {
      await act(async () => {
        Object.getOwnPropertyDescriptor(browser.HTMLInputElement.prototype, "value")!.set!.call(search, value);
        search.dispatchEvent(new browser.Event("input", {bubbles: true}));
      });
    }

    await act(async () => checkbox("01").click());
    await filter("02분반");
    assert.equal(app.document.querySelectorAll('input[type="checkbox"]').length, 1);
    await act(async () => checkbox("02").click());
    assert.match(app.document.body.textContent!, /현재 검색 결과 밖의 선택 1개/);
    await filter("없는 수업");
    assert.match(app.document.body.textContent!, /검색한 수업이 없습니다/);
    assert.match(app.document.body.textContent!, /현재 검색 결과 밖의 선택 2개/);
    await app.click("수업 검색 초기화");
    assert.equal(app.document.activeElement, search);
    assert.equal(checkbox("01").checked, true);
    assert.equal(checkbox("02").checked, true);

    catalog.courses = [{...first, status: "pending", request_status: "pending", can_withdraw: true}, second];
    await app.click("상태 새로고침");
    assert.equal(checkbox("01").disabled, true);
    assert.match(app.document.body.textContent!, /수강 상태가 바뀐 수업/);
    const submit = Array.from(app.document.querySelectorAll("button")).find(node => node.textContent === "선택한 2개 수업 신청")!;
    assert.equal(submit.disabled, true);
    await act(async () => submit.click());
    assert.equal(app.requests.some(request => request.url.endsWith("/enroll")), false);
    const remove = app.document.querySelector<HTMLButtonElement>('button[aria-label*="01분반 선택 해제"]')!;
    await act(async () => remove.click());
    assert.equal(app.document.activeElement, search);
    await app.click("선택한 1개 수업 신청");
    const post = app.requests.filter(request => request.url.endsWith("/enroll"));
    assert.equal(post.length, 1);
    assert.deepEqual(JSON.parse(post[0].fields.get("course_keys")!), [second.course_key]);
  } finally {await app.dispose();}
});

test("React: the course selection permits up to the server limit and replacing the last choice", async () => {
  const catalog = {courses: Array.from({length: 21}, (_, index) => catalogCourse(String(index + 1)))};
  const app = await mount(true, "?view=catalog", catalog);
  try {
    const boxes = Array.from(app.document.querySelectorAll<HTMLInputElement>('input[type="checkbox"]'));
    for (const box of boxes.slice(0, 20)) await act(async () => box.click());
    assert.equal(boxes[20].disabled, true);
    assert.match(app.document.body.textContent!, /최대 20개/);
    await act(async () => boxes[0].click());
    assert.equal(boxes[20].disabled, false);
    await act(async () => boxes[20].click());
    await app.click("선택한 20개 수업 신청");
    const post = app.requests.filter(request => request.url.endsWith("/enroll"));
    assert.equal(post.length, 1);
    assert.deepEqual(JSON.parse(post[0].fields.get("course_keys")!), catalog.courses.slice(1).map(course => course.course_key));
  } finally {await app.dispose();}
});

test("assignment filter shows the number of matches and restores all assignments with keyboard focus", async () => {
  const value = courseResult();
  value.result.overview.latest.length = 0;
  const app = await mount(true, `?course_key=${courseKey}`, undefined, () => value);
  try {
    const filter = app.document.querySelector<HTMLSelectElement>("label select")!;
    await act(async () => {
      filter.value = "completed";
      filter.dispatchEvent(new app.document.defaultView!.Event("change", {bubbles: true}));
    });
    assert.match(app.document.body.textContent!, /전체 1개 중 0개 표시/);
    assert.match(app.document.body.textContent!, /아직 결과가 공개된 과제가 없습니다/);
    assert.equal(app.document.querySelector("[data-assignment]"), null);
    await app.click("전체 과제 보기");
    assert.equal(filter.value, "all");
    assert.equal(app.document.activeElement, filter);
    assert.equal(app.document.querySelector("[data-assignment] h3")?.textContent, "Hello 실습");
  } finally {await app.dispose();}
});

test("an empty course and a partial projection have distinct recovery and counts", async () => {
  const value = courseResult();
  value.result.overview.latest.length = 0;
  value.result.overview.assignments_truncated = true;
  value.result.overview.latest_truncated = true;
  const partial = await mount(true, `?course_key=${courseKey}`, undefined, () => value);
  try {
    const filter = partial.document.querySelector<HTMLSelectElement>("label select")!;
    await act(async () => {
      filter.value = "open";
      filter.dispatchEvent(new partial.document.defaultView!.Event("change", {bubbles: true}));
    });
    assert.match(partial.document.body.textContent!, /조회한 1개 중 0개 표시/);
    assert.match(partial.document.body.textContent!, /조회한 범위에서 이 조건에 해당하는 과제가 없습니다/);
    assert.equal(partial.document.querySelector("[data-assignment]"), null);
  } finally {await partial.dispose();}
  value.result.overview.assignments.length = 0;
  value.result.overview.assignments_truncated = false;
  value.result.overview.latest_truncated = false;
  const empty = await mount(true, `?course_key=${courseKey}`, undefined, () => value);
  try {
    assert.match(empty.document.body.textContent!, /전체 0개/);
    assert.match(empty.document.body.textContent!, /교수자의 과제 공개를 기다려 주세요/);
    assert.doesNotMatch(empty.document.body.textContent!, /전체 과제 보기/);
    assert.equal(empty.document.querySelector<HTMLSelectElement>("label select")!.disabled, true);
  } finally {await empty.dispose();}
});

test("assignment card for a score-only receipt directs the student to the published total", async () => {
  const value = courseResult();
  Object.assign(value.result.overview.latest[0], {feedback_policy: "score_only"});
  const app = await mount(true, `?course_key=${courseKey}`, undefined, () => value);
  try {
    const card = app.document.querySelector("[data-assignment]")!;
    assert.match(card.textContent!, /내 점수에서 공개된 총점을 확인하세요/);
    assert.doesNotMatch(card.textContent!, /수정 힌트/);
  } finally {await app.dispose();}
});

test("React: student home and gradebook choices survive Back; an explicit result opens practice", async () => {
  const app = await mount(true);
  const browser = app.document.defaultView!;
  async function back() {
    await act(async () => {
      const ready = new Promise<void>(resolve => browser.addEventListener("popstate", () => resolve(), {once: true}));
      browser.history.back();
      await ready;
    });
  }
  try {
    const resultFilter = () => Array.from(app.document.querySelectorAll("button")).find(b => b.textContent?.startsWith("결과 확인"))!;
    await act(async () => resultFilter().click());
    await app.click("점수 확인");
    await app.click("시험·출석 성적");
    await app.click("학습 자료");
    await back();
    assert.equal(app.document.querySelector('[aria-label="성적 종류"] [aria-pressed=true]')?.textContent, "시험·출석 성적");
    await back();
    assert.equal(resultFilter().getAttribute("aria-pressed"), "true");

    const task = app.document.querySelector<HTMLButtonElement>('button[aria-label="Hello 실습 · 평가 결과 보기"]')!;
    await act(async () => task.click());
    assert.equal(app.document.querySelector('[aria-label="성적 종류"] [aria-pressed=true]')?.textContent, "실습 제출 결과");
    assert.equal(app.document.activeElement?.getAttribute("data-score-assignment"), "assignment_01");
    await app.click("시험·출석 성적");
    assert.equal(new URL(browser.location.href).searchParams.has("assignment"), false);
  } finally {await app.dispose();}
});

test("React: an instructor gradebook link opens the exam tab after login and can switch back to practice", async () => {
  const app = await mount(false, `?course_key=${courseKey}&view=scores&tab=gradebook`);
  try {
    const currentTab = () => app.document.querySelector('[aria-label="성적 종류"] [aria-pressed=true]')?.textContent;
    assert.equal(currentTab(), undefined);
    const form = app.document.querySelector("form")!;
    form.querySelector<HTMLInputElement>('input[name="student_key"]')!.value = key;
    form.querySelector<HTMLInputElement>('input[name="password"]')!.value = "123456";
    await act(async () => form.requestSubmit());
    assert.equal(currentTab(), "시험·출석 성적");
    await app.click("실습 제출 결과");
    assert.equal(currentTab(), "실습 제출 결과");
    assert.equal(new URL(app.document.defaultView!.location.href).searchParams.has("tab"), false);
  } finally { await app.dispose(); }
});

test("React: a material filter and sealed search resume only in the same course and session", async () => {
  const url = `?course_key=${courseKey}&view=materials`, query = "이름 20260001";
  let saved;
  const first = await mount(true, url);
  try {
    const browser = first.document.defaultView!;
    const input = first.document.querySelector<HTMLInputElement>('input[type="search"]')!;
    await act(async () => {
      Object.getOwnPropertyDescriptor(browser.HTMLInputElement.prototype, "value")!.set!.call(input, query);
      input.dispatchEvent(new browser.Event("input", {bubbles: true}));
    });
    const format = first.document.querySelector<HTMLSelectElement>('select')!;
    await act(async () => {format.value = "pdf"; format.dispatchEvent(new browser.Event("change", {bubbles: true}));});
    const until = Date.now() + 2000;
    while (!browser.history.state?.autogradeStudentList?.sealedQuery && Date.now() < until) {
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    saved = structuredClone(browser.history.state);
    assert.ok(saved.autogradeStudentList.sealedQuery);
    assert.doesNotMatch(JSON.stringify(saved) + browser.location.href, /20260001|synthetic_csrf|이름/);
    assert.equal(browser.localStorage.length + browser.sessionStorage.length, 0);
  } finally {await first.dispose();}

  for (const scenario of ["reload", "new session", "different course"]) {
    const state = structuredClone(saved);
    if (scenario === "different course") state.autogradeStudentList.page = "materials/other-course";
    const app = await mount(true, url, scenario === "new session" ? {courses: [course as Course]} : undefined, courseResult, state);
    try {
      const search = app.document.querySelector<HTMLInputElement>('input[type="search"]')!;
      if (scenario === "reload") {
        const until = Date.now() + 2000;
        while (!search.value && Date.now() < until) await act(async () => {await new Promise(resolve => setTimeout(resolve, 10));});
      }
      assert.equal(search.value, scenario === "reload" ? query : "");
      assert.equal(app.document.querySelector("select")?.value, scenario === "reload" ? "pdf" : "all");
    } finally {await app.dispose();}
  }
});

test("student course links preserve section, current page and a direct parent on deep entry", async () => {
  const context = {...course, section: "01분반", year: 2026, semester: "1"} as Course;
  const app = await mount(true, "?course_key=come2201&view=scores", {courses: [context, {...context, course_key: "another", year: 2027, semester: "summer"}]});
  try {
    const {document} = app;
    const location = () => document.querySelector('nav[aria-label="현재 위치"]')!;
    const parent = () => document.querySelector<HTMLAnchorElement>("a[data-parent-link]")!;
    const courseMenu = () => document.querySelector('nav[aria-label="수업 메뉴"]')!;
    const home = document.querySelector('nav[aria-label="학생 메뉴"] a[aria-current=location]')!;
    assert.equal(home.textContent, "내 수업");
    assert.equal(courseMenu().querySelector('a[aria-current=page]')?.textContent, "내 점수");
    assert.equal(location().querySelector('[aria-current=page]')?.textContent, "내 점수");
    const labels = Array.from(document.querySelectorAll<HTMLSelectElement>('select[aria-label="현재 수업"] option'), option => option.textContent);
    assert.ok(labels.includes("설계 실습 · 2026학년도 1학기 · 01분반 · COME2201"));
    assert.ok(labels.includes("설계 실습 · 2027학년도 여름학기 · 01분반 · COME2201"));
    assert.match(location().textContent!, /설계 실습 · 2026학년도 1학기 · 01분반 · COME2201/);
    assert.doesNotMatch(document.body.textContent!, /분반분반/);
    assert.equal(parent().getAttribute("href"), "/app/student?course_key=come2201");
    const link = courseMenu().querySelector<HTMLAnchorElement>('a[href$="view=materials"]')!;
    const win = document.defaultView!;
    let preservedNativeClick = false;
    win.addEventListener("click", event => {preservedNativeClick = !event.defaultPrevented; event.preventDefault();}, {once: true});
    link.dispatchEvent(new win.MouseEvent("click", {bubbles: true, cancelable: true, ctrlKey: true}));
    assert.ok(preservedNativeClick, "modified clicks can open a separate tab");
    assert.equal(location().querySelector('[aria-current=page]')?.textContent, "내 점수");
    await act(async () => link.click());
    assert.equal(location().querySelector('[aria-current=page]')?.textContent, "학습 자료");
    assert.equal(courseMenu().querySelector('a[aria-current=page]')?.textContent, "학습 자료");
    await act(async () => parent().click());
    assert.equal(courseMenu().querySelector('a[aria-current=page]')?.textContent, "실습 과제");
    assert.equal(parent().getAttribute("href"), "/app/student");
    assert.equal(win.location.search, "?course_key=come2201");
    assert.equal(app.requests.some(request => request.url.includes("/another")), false);
  } finally {await app.dispose();}
});

test("a same-name assignment keeps its distinguishing label from selection through claim and result", async () => {
  const tokenLabel = "마감 2035-04-03 13:50 KST · 과제 구분 assignment_01";
  function twoAssignments() {
    const data = courseResult();
    const first = {...data.result.overview.assignments[0], due_at: "2035-04-03T04:50:00Z", title_context: tokenLabel};
    const second = {...first, assignment_id: "assignment_02", title_context: "마감 2035-04-03 13:50 KST · 과제 구분 assignment_02"};
    return {...data, result: {...data.result, overview: {...data.result.overview, assignments: [first, second]}}};
  }
  const app = await mount(true, `?course_key=${courseKey}`, undefined, twoAssignments);
  try {
    const card = app.document.querySelector<HTMLElement>('[data-assignment="assignment_01"]')!;
    const issue = [...card.querySelectorAll("button")].find(button => button.textContent === "수령 코드 발급")!;
    assert.equal(app.document.getElementById(issue.getAttribute("aria-describedby")!)?.textContent, tokenLabel);
    let finish!: () => void;
    const hold = new Promise<void>(resolve => {finish = resolve;});
    const fetch = globalThis.fetch;
    globalThis.fetch = async (...args) => {
      if (String(args[0]).endsWith("/claims")) await hold;
      return fetch(...args);
    };
    await act(async () => issue.click());
    assert.equal(issue.textContent, "수령 코드 발급 중…");
    assert.equal(issue.getAttribute("aria-busy"), "true");
    assert.equal(app.document.querySelectorAll('button[aria-busy="true"]').length, 1);
    await act(async () => {finish();});
    const granted = app.document.querySelector('section[aria-label="과제 수령 코드"]')!;
    assert.ok(granted.textContent?.includes(tokenLabel));
    assert.ok(!granted.textContent?.includes("assignment_02"));
    await app.click("평가 결과 보기");
    const score = app.document.querySelector('[data-score-assignment="assignment_01"]')!;
    assert.ok(score.textContent?.includes(tokenLabel));
  } finally { await app.dispose(); }
});
