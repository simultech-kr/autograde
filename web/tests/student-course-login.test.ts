import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {JSDOM} from "jsdom";
import {act, createElement, type ComponentType} from "react";
import ts from "typescript";
import {parseBootstrap, StudentContractError} from "../src/features/student/model.ts";

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

async function mount(initiallyAuthenticated=false, query="") {
  const dom=new JSDOM("<!doctype html><div id='root'></div>",{url:`https://example.test/app/student${query}`,pretendToBeVisual:true});
  const originals=new Map<string,PropertyDescriptor|undefined>();
  const bind=(name:string,value:unknown)=>{originals.set(name,Object.getOwnPropertyDescriptor(globalThis,name));Object.defineProperty(globalThis,name,{configurable:true,writable:true,value});};
  Object.defineProperty(dom.window.document,"visibilityState",{value:"visible"});
  dom.window.document.hasFocus=()=>true;
  for(const [name,value] of Object.entries({window:dom.window,document:dom.window.document,navigator:dom.window.navigator,HTMLElement:dom.window.HTMLElement,FormData:dom.window.FormData,IS_REACT_ACT_ENVIRONMENT:true,matchMedia:()=>({matches:false}),requestAnimationFrame:(callback:FrameRequestCallback)=>{callback(0);return 1;},setInterval:()=>1,clearInterval:()=>{}})) bind(name,value);
  let logged=initiallyAuthenticated, authMethod="course";
  const snapshot=()=>{const value=authenticated();value.result.auth_method=authMethod;return value;};
  const requests:{url:string;fields:URLSearchParams}[]=[];
  bind("fetch",async(url:string,options?:RequestInit)=>{
    const fields=new URLSearchParams(String(options?.body||"")); requests.push({url,fields});
    if(url==="/student/api/login") {assert.equal(fields.get("student_key"),key);assert.equal(fields.get("password"),"123456");assert.equal(fields.get("csrf"),csrf);logged=true;return Response.json(snapshot());}
    if(url==="/student/api/bootstrap") return Response.json(logged ? snapshot() : anonymous());
    if(url==="/student/api/home") return Response.json(homeResult());
    if(url==="/student/api/privacy") return Response.json(privacy());
    if(url===`/student/api/course/${courseKey}/claims`) return Response.json({ok:true,result:{student_key:key,claim:{assignment_id:"assignment_01",title:"Hello 실습",code:"SYNTHETIC-CODE",expires_at:stamp(180000),api_url:"https://example.test:20000"}}});
    if(url===`/student/api/course/${courseKey}`) {const value=courseResult();return Response.json({...value,result:{...value.result,materials:materials()}});}
    throw new Error(`Unexpected request ${url}`);
  });
  const {createRoot}=await import("react-dom/client");
  const root=createRoot(dom.window.document.getElementById("root")!);
  await act(async()=>root.render(createElement(loadPortal())));
  return {document:dom.window.document,requests,
    async click(label:string){const button=Array.from(dom.window.document.querySelectorAll("button")).find(node=>node.textContent?.trim()===label);assert.ok(button,`button ${label}`);await act(async()=>button.click());},
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
    const buttons=Array.from(app.document.querySelectorAll("button")).map(button=>button.textContent?.trim());
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
    assert.ok(Array.from(app.document.querySelectorAll("button")).some(button=>button.textContent?.trim()==="수강 선택"));
  } finally {await app.dispose();}
});

test("React: a task deep link focuses its assignment after initial authentication", async()=>{
  const app=await mount(true,`?course_key=${courseKey}&assignment=assignment_01`);
  try {
    assert.equal(app.document.activeElement?.getAttribute("data-assignment"),"assignment_01");
    assert.equal(app.document.querySelectorAll("[data-parent-link]").length,1);
  } finally {await app.dispose();}
});
