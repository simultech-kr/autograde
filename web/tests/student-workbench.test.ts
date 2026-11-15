import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import {createRequire} from "node:module";
import {dirname, resolve} from "node:path";
import {fileURLToPath} from "node:url";
import {JSDOM} from "jsdom";
import {act, createElement, type ComponentType} from "react";
import ts from "typescript";
import {StudentContractError, type CourseData, type Receipt} from "../src/features/student/model.ts";
import {criterionVerdict, fetchMaterial, parseMaterialCatalog, parseSubmissionFeedback, type FeedbackCriterion, type MaterialItem} from "../src/features/student/workbench.ts";

const course = "come2201", student = "20260001", stamp = "2026-11-15T02:00:00Z";
const material = (): MaterialItem => ({id:"mat_one",kind:"material",title:"개별 피드백",filename:"20260001.pdf",format:"pdf",size_bytes:4,updated_at:stamp,opens_at:stamp,closes_at:"2026-12-01T02:00:00Z",availability:"open",revision:3,file_revision:2,edition:1,instructions:"파일을 확인하세요.",change_note:"설명 수정",requested:false,download_path:`/student/api/course/${course}/materials/mat_one/download`});
const feedback = () => ({ok:true,result:{schema_version:1,student_key:student,course_key:course,assignment_id:"a1",submission_id:"bsub_one",feedback_available:true,result:{submission_id:"bsub_one",state:"published",published_at:stamp,score:3,max_score:10,rubric:{compile:{score:3,max_score:3,status:"passed",feedback:"컴파일 성공"},output:{score:0,max_score:7,status:"failed",feedback:"줄바꿈이 다릅니다.",hint:"마지막 개행을 추가하세요.",path:"main.cpp",line:12}},diagnostics:[]}}});
const parseFeedback = (value:unknown) => parseSubmissionFeedback(value,course,student,"bsub_one","a1");

test("material catalog binds file metadata and download endpoint to the current course",()=>{
  const parsed=parseMaterialCatalog({items:[material()],truncated:false},course)!;
  assert.equal(parsed.items[0].download_path,material().download_path);
  assert.equal(parseMaterialCatalog(undefined,course),undefined);
  const closed={...material(),availability:"closed",filename:null,size_bytes:null,download_path:null};
  assert.equal(parseMaterialCatalog({items:[closed],truncated:false},course)!.items[0].availability,"closed");
  for(const patch of [{download_path:"https://evil.test/download"},{download_path:"/student/api/course/other/materials/mat_one/download"},{filename:"../secret.pdf"},{filename:"C:\\secret.pdf"},{size_bytes:26*1024*1024},{availability:"scheduled"},{revision:0},{kind:"exam",format:"zip"}]) {
    assert.throws(()=>parseMaterialCatalog({items:[{...material(),...patch}],truncated:false},course),StudentContractError);
  }
  assert.throws(()=>parseMaterialCatalog({items:[material(),material()],truncated:false},course),StudentContractError);
});

test("feedback rejects mismatched students, submissions, assignments and unpublished payloads",()=>{
  const result=parseFeedback(feedback());
  assert.equal(result.criteria.length,2);assert.equal(result.criteria[1].location,"main.cpp:12");
  for(const patch of [{student_key:"20269999"},{course_key:"other"},{submission_id:"bsub_other"},{assignment_id:"a2"},{feedback_available:false}]) {
    const value=feedback();assert.throws(()=>parseFeedback({...value,result:{...value.result,...patch}}),StudentContractError);
  }
  for(const state of ["queued","graded","failed"]) {const value=feedback();value.result.result.state=state;assert.throws(()=>parseFeedback(value),StudentContractError);}
  const scoreOnly=feedback();scoreOnly.result.feedback_available=false;scoreOnly.result.result.rubric={} as typeof scoreOnly.result.result.rubric;
  assert.equal(parseFeedback(scoreOnly).criteria.length,0);
});

test("criterion status never turns a lower score or blocked evaluation into success",()=>{
  const base:FeedbackCriterion={key:"test",title:"테스트",score:2,max_score:5,status:"passed",feedback:"",hint:""};
  assert.deepEqual(criterionVerdict(base),{status:"partial",label:"부분 충족 · 수정 필요 · 점수·판정 확인 필요",mismatch:true,needsWork:true,rank:0});
  assert.equal(criterionVerdict({...base,status:"blocked",score:0}).status,"blocked");
  assert.equal(criterionVerdict({...base,status:undefined,score:null,max_score:null}).status,"unknown");
  assert.equal(criterionVerdict({...base,score:5}).status,"passed");
});

test("download posts only CSRF and revisions, verifies type/size, and preserves the page",async()=>{
  const file=material();let calls=0;
  const fetcher=(async(path:RequestInfo|URL,options?:RequestInit)=>{
    calls++;assert.equal(path,file.download_path);assert.equal(options?.method,"POST");assert.equal(options?.credentials,"same-origin");assert.equal(options?.redirect,"error");
    const fields=new URLSearchParams(String(options?.body));assert.deepEqual(Object.fromEntries(fields),{csrf:"proof",revision:"3",file_revision:"2"});
    return new Response("%PDF",{headers:{"Content-Type":"application/pdf","Content-Length":"4"}});
  }) as typeof fetch;
  const blob=await fetchMaterial(file,course,"proof",new AbortController().signal,fetcher);
  assert.equal(await blob.text(),"%PDF");assert.equal(calls,1);
  for(const response of [new Response("login",{headers:{"Content-Type":"text/html"}}),new Response("%PDFmore",{headers:{"Content-Type":"application/pdf"}}),new Response("%P",{headers:{"Content-Type":"application/pdf"}})]) {
    await assert.rejects(fetchMaterial(file,course,"proof",new AbortController().signal,async()=>response),StudentContractError);
  }
  await assert.rejects(fetchMaterial(file,course,"proof",new AbortController().signal,async()=>new Response("",{status:409})),/자료가 수정되었습니다/);
  const cancelled=new AbortController();cancelled.abort();
  await assert.rejects(fetchMaterial(file,course,"proof",cancelled.signal,fetcher));
});

function loadComponent(name:"StudentScores"|"StudentMaterials"):ComponentType<{data:CourseData}> {
  const require=createRequire(import.meta.url),cache=new Map<string,{exports:unknown}>();
  function load(path:string):unknown {
    if(cache.has(path))return cache.get(path)!.exports;
    if(path.endsWith(".css"))return {default:new Proxy({}, {get:(_target,key)=>key}),__esModule:true};
    const module={exports:{}};cache.set(path,module);
    const javascript=ts.transpileModule(readFileSync(path,"utf8"),{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,esModuleInterop:true}}).outputText;
    new Function("require","module","exports",javascript)((specifier:string)=>specifier.startsWith(".")?load(resolve(dirname(path),/\.(tsx?|css)$/.test(specifier)?specifier:`${specifier}.ts`)):require(specifier),module,module.exports);
    return module.exports;
  }
  return (load(fileURLToPath(new URL(`../src/features/student/${name}.tsx`,import.meta.url))) as Record<string,ComponentType<{data:CourseData}>>)[name];
}
const receipt:Receipt={submission_id:"bsub_one",assignment_id:"a1",title:"출력 실습",assignment_key:"lab01",received_at:stamp,state:"published",code_version:"a".repeat(64),score:3,max_score:10,published_at:stamp,version:2,previous_best_id:"bsub_old",previous_best_score:9,previous_best_max_score:10,previous_best_at:"2026-11-14T02:00:00Z"};
const data=():CourseData=>({student_key:student,course:{course_key:course,name:"설계 실습",code:course,section:"01"},csrf:"proof",server_time:stamp,api_base_url:"https://example.test:20000",links:{assignments:`/courses/${course}/assignments`},overview:{assignments:[{assignment_id:"a1",assignment_key:"lab01",title:"출력 실습",opens_at:null,due_at:null,accepted_at:null}],latest:[{...receipt}],history:[{...receipt}],history_truncated:false,latest_truncated:false,assignments_truncated:false,limit:100},materials:{items:[material()],truncated:false},gradebook:{publication_id:"gbook_one",title:"중간 평가",published_at:stamp,version:1,items:[{key:"mid",label:"중간고사",value:85}]}});

async function mount(name:"StudentScores"|"StudentMaterials", initial:CourseData, fetcher:typeof fetch) {
  const dom=new JSDOM("<!doctype html><div id='root'></div>",{url:"https://example.test/app/student",pretendToBeVisual:true}),originals=new Map<string,PropertyDescriptor|undefined>();
  const bind=(key:string,value:unknown)=>{originals.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});};
  Object.defineProperty(dom.window.document,"visibilityState",{value:"visible"});dom.window.document.hasFocus=()=>true;
  for(const [key,value]of Object.entries({window:dom.window,document:dom.window.document,navigator:dom.window.navigator,HTMLElement:dom.window.HTMLElement,IS_REACT_ACT_ENVIRONMENT:true,fetch:fetcher}))bind(key,value);
  const {createRoot}=await import("react-dom/client"),root=createRoot(dom.window.document.getElementById("root")!),Component=loadComponent(name);
  await act(async()=>root.render(createElement(Component,{data:initial})));
  return {document:dom.window.document,
    async click(text:string){const button=Array.from(dom.window.document.querySelectorAll("button")).find(node=>node.textContent===text);assert.ok(button,`button: ${text}`);await act(async()=>button.click());},
    async render(next:CourseData){await act(async()=>root.render(createElement(Component,{data:next})));},
    async dispose(){await act(async()=>root.unmount());dom.window.close();for(const [key,descriptor]of originals){if(descriptor)Object.defineProperty(globalThis,key,descriptor);else Reflect.deleteProperty(globalThis,key);}}
  };
}

test("React: latest score comes first, teacher grades are explicit, feedback loads on request with failed criteria expanded",async()=>{
  let calls=0;const payload=feedback();payload.result.result.rubric.output.feedback='<img src=x onerror="alert(1)">';
  const app=await mount("StudentScores",data(),async(path)=>{assert.equal(path,`/student/api/course/${course}/submissions/bsub_one`);calls++;return Response.json(payload);});
  try {
    const card=app.document.querySelector('[data-score-assignment="a1"]')!;
    assert.match(card.textContent!,/3 \/ 10점/);assert.match(card.textContent!,/9 \/ 10점/);assert.equal(app.document.querySelector('[data-gradebook-item="mid"]'),null);assert.equal(calls,0);
    await app.click("평가 항목·수정 힌트 보기");assert.equal(calls,1);
    const criteria=Array.from(card.querySelectorAll('details[data-status]'));assert.equal(criteria[0].getAttribute("data-status"),"failed");assert.equal(criteria[0].hasAttribute("open"),true);
    assert.match(criteria[0].textContent!,/마지막 개행을 추가하세요/);assert.equal(card.querySelector("img"),null);assert.match(card.textContent!,/<img src=x/);
    await app.click("평가 항목 접기");await app.click("평가 항목·수정 힌트 보기");assert.equal(calls,1);
    await app.click("시험·출석 성적");assert.equal(app.document.querySelector('[data-gradebook-item="mid"]')?.textContent,"중간고사85");assert.equal(app.document.querySelector('[data-score-assignment="a1"]'),null);
  }finally{await app.dispose();}
});

test("React: queued latest result cannot show previous feedback or provisional score",async()=>{
  const value=data();value.overview.latest[0]={...receipt,state:"queued",score:8,published_at:null};
  const app=await mount("StudentScores",value,async()=>{throw new Error("pending feedback must not be requested");});
  try{const card=app.document.querySelector('[data-score-assignment="a1"]')!;assert.match(card.textContent!,/점수 미확정/);assert.match(card.textContent!,/9 \/ 10점/);assert.doesNotMatch(card.textContent!,/8 \/ 10점/);assert.equal(card.querySelector('[aria-controls^="feedback-"]'),null);}finally{await app.dispose();}
});

test("React: a late feedback response cannot replace a newly queued submission",async()=>{
  let finish!:(value:Response)=>void;
  const app=await mount("StudentScores",data(),()=>new Promise<Response>(resolve=>{finish=resolve;}));
  try{
    await app.click("평가 항목·수정 힌트 보기");
    const next=data();next.overview.latest[0]={...receipt,submission_id:"bsub_new",state:"queued",published_at:null,score:null,max_score:null,received_at:"2026-11-15T02:03:00Z",version:3};
    await app.render(next);
    await act(async()=>finish(Response.json(feedback())));
    assert.match(app.document.body.textContent!,/채점 대기/);assert.doesNotMatch(app.document.body.textContent!,/마지막 개행을 추가하세요|줄바꿈이 다릅니다/);
    assert.equal(app.document.querySelector('[id="feedback-bsub_one"]'),null);
  }finally{await app.dispose();}
});

test("React: changed published feedback asks for refresh instead of mixing two result versions",async()=>{
  const changed=feedback();changed.result.result.score=7;
  const app=await mount("StudentScores",data(),async()=>Response.json(changed));
  try{await app.click("평가 항목·수정 힌트 보기");assert.match(app.document.querySelector('[role="alert"]')!.textContent!,/채점 결과가 갱신되었습니다/);assert.equal(app.document.querySelector('details[data-status]'),null);}finally{await app.dispose();}
});

test("React: direct material row shows files, download errors stay inline, closed files remain disabled",async()=>{
  const value=data();value.materials!.items.push({...material(),id:"mat_closed",title:"지난 자료",availability:"closed",filename:null,size_bytes:null,download_path:null});
  let calls=0;const app=await mount("StudentMaterials",value,async()=>{calls++;return new Response("",{status:409});});
  try{assert.match(app.document.body.textContent!,/20260001.pdf/);assert.match(app.document.body.textContent!,/4 B/);assert.equal(app.document.querySelector('a[href*="/courses/"]'),null);
    const closed=app.document.querySelector('button[aria-label="지난 자료 PDF 다운로드"]') as HTMLButtonElement;assert.equal(closed.disabled,true);
    await app.click("다운로드");assert.equal(calls,1);assert.match(app.document.querySelector('[role="alert"]')!.textContent!,/자료가 수정되었습니다/);assert.equal(app.document.defaultView!.location.pathname,"/app/student");
  }finally{await app.dispose();}
});
