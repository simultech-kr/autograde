import assert from "node:assert/strict";
import test from "node:test";
import { RemoteLearningController, parseRuntimeInfo, parseRuntimeSession, parseRuntimeResult, parseRuntimeClosed, validateRuntimeRun,
  runtimeErrorMessage, MAX_RUNTIME_OUTPUT, MAX_RUNTIME_STDIN, type LearningRuntimeApi,
  type LearningRuntimeResult, type LearningRuntimeSession, type RuntimeRunRequest } from "../remoteLearning";
import { defaultRuntimeSources, renderRemoteLearning } from "../remoteLearningMarkup";
import type { LearningCheckpointScope, SavedLearningSnapshot } from "../learningCheckpoint";

const scope: LearningCheckpointScope = {baseUrl:"https://school.test",generation:1,assignmentId:"a1",rootPath:"/private/a1",scheme:"file",authority:""};
const session = (checkpoint = "cp_1"): LearningRuntimeSession => ({id:"space_one", status:"ready", checkpoint_id:checkpoint, created_at:"2026-10-28T10:00:00Z",expires_at:"2026-10-28T11:00:00Z"});
const snapshot = (number = 1): SavedLearningSnapshot => ({checkpoint:{checkpoint_id:`cp_${number}`,assignment_id:"a1",archive_sha256:"a".repeat(64),created_at:"2026-10-28T10:00:00Z",file_count:5,source_bytes:99,version:number},filePaths:["main.cpp","helper.cpp","main.c","main.py","helper.py"]});
const result = (checkpoint = "cp_1"): LearningRuntimeResult => ({session:session(checkpoint),checkpoint_id:checkpoint,status:"completed",compile:{exit_code:0,stdout:"",stderr:""},run:{exit_code:0,stdout:"Hello\n",stderr:""}});
const input: RuntimeRunRequest = {language:"cpp",sources:["main.cpp","helper.cpp"],stdin:"42\n"};

function fixture() {
  const state = {scope:scope as LearningCheckpointScope | undefined, next:1, saveFails:false, enabled:true,
    session:null as LearningRuntimeSession | null, calls:[] as string[], onCreate:async (_signal:AbortSignal) => {},
    onRun:async (_signal:AbortSignal) => {}, onSave:async()=>{}, closeFails:false, getFails:false, python:true};
  const api:LearningRuntimeApi = {
    async getLearningRuntime() {state.calls.push("get"); if(state.getFails) throw {status:503};return {enabled:state.enabled,provider:"remote_linux",languages:state.python?["c","cpp","python"]:["c","cpp"],session:state.session};},
    async createLearningRuntime(_scope,id,signal) {state.calls.push(`create:${id}`);await state.onCreate(signal);return state.session=session(id);},
    async runLearningRuntime(_scope,_session,id,_request,signal) {state.calls.push(`run:${id}`);await state.onRun(signal);return result(id);},
    async closeLearningRuntime(_scope,_session,id) {state.calls.push(`close:${id}`);if(state.closeFails)throw {status:503};return {status:"closed",checkpoint_id:id,cleanup_verified:true};},
  };
  const controller = new RemoteLearningController(api,{currentScope:()=>state.scope,
    saveSnapshot:async()=>{state.calls.push(`save:${state.next}`);if(state.saveFails)throw new Error("현재 코드의 서버 저장을 확인하지 못했습니다.");await state.onSave();return snapshot(state.next++);},onState:()=>{}});
  return {controller,state};
}

test("runtime info and receipts reject malformed, stale or unverified responses",()=>{
  assert.equal(parseRuntimeInfo({enabled:true,provider:"remote_linux",languages:["c","cpp"],session:session()}).session?.id,"space_one");
  for(const value of [null,{}, {enabled:true,provider:"local",languages:["c"],session:null},
    {enabled:true,provider:"remote_linux",languages:["cpp","cpp"],session:null},
    {enabled:false,provider:"remote_linux",languages:[],session:session()}]) assert.throws(()=>parseRuntimeInfo(value));
  for(const value of [{...session(),id:"../private"},{...session(),status:"unknown"},{...session(),expires_at:"bad"}]) assert.throws(()=>parseRuntimeSession(value));
  assert.throws(()=>parseRuntimeSession(session(),"cp_other")); assert.throws(()=>parseRuntimeSession(session(),undefined,"other"));
  assert.throws(()=>parseRuntimeClosed({status:"closed",checkpoint_id:"cp_1",cleanup_verified:false},"cp_1"));
  assert.throws(()=>parseRuntimeClosed({status:"closed",checkpoint_id:"cp_old",cleanup_verified:true},"cp_1"));
  assert.equal(parseRuntimeClosed({status:"closed",checkpoint_id:"cp_1",cleanup_verified:true},"cp_1").cleanup_verified,true);
});

test("run results bind current session and source version and bound output",()=>{
  assert.equal(parseRuntimeResult(result(),"space_one","cp_1").run?.stdout,"Hello\n");
  assert.throws(()=>parseRuntimeResult(result(),"space_other","cp_1"));
  assert.throws(()=>parseRuntimeResult(result("cp_old"),"space_one","cp_1"));
  assert.throws(()=>parseRuntimeResult({...result(),run:{exit_code:1,stdout:"",stderr:""}},"space_one","cp_1"));
  assert.throws(()=>parseRuntimeResult({...result(),compile:{exit_code:0,stdout:"a".repeat(MAX_RUNTIME_OUTPUT+1),stderr:""}},"space_one","cp_1"));
  assert.throws(()=>parseRuntimeResult({...result(),run:{exit_code:0,stdout:"가".repeat(MAX_RUNTIME_OUTPUT),stderr:""}},"space_one","cp_1"));
});

test("compile inputs cannot contain paths outside the immutable source or arbitrary flags",()=>{
  assert.deepEqual(validateRuntimeRun(input),input);
  for(const source of ["../other.cpp","/tmp/main.cpp","-include.cpp","src/../../x.cpp","a\\b.cpp","a\n.cpp","C:/x.cpp","a//b.cpp","./main.cpp","main.C","main.c"])
    assert.throws(()=>validateRuntimeRun({...input,sources:[source]}),source);
  assert.throws(()=>validateRuntimeRun({...input,sources:[]}));
  assert.throws(()=>validateRuntimeRun({...input,sources:["main.cpp","main.cpp"]}));
  assert.throws(()=>validateRuntimeRun({...input,stdin:"a".repeat(MAX_RUNTIME_STDIN+1)}));
  assert.throws(()=>validateRuntimeRun({...input,stdin:"\0"}));
  assert.throws(()=>validateRuntimeRun({...input,language:"sh"}));
});

test("Python support accepts a single safe entry point and rejects arbitrary interpreters or multiple entries",()=>{
  assert.deepEqual(parseRuntimeInfo({enabled:true,provider:"remote_linux",languages:["c","cpp","python"],session:null}).languages,["c","cpp","python"]);
  assert.throws(()=>parseRuntimeInfo({enabled:true,provider:"remote_linux",languages:["python","sh"],session:null}));
  const python={language:"python",sources:["src/main.py"],stdin:"안녕\n"};
  assert.deepEqual(validateRuntimeRun(python),python);
  for(const sources of [["main.py","helper.py"],["../main.py"],["main.cpp"],["-m.py"],["main.pyc"]])
    assert.throws(()=>validateRuntimeRun({...python,sources}));
  assert.deepEqual(defaultRuntimeSources(["helper.py","src/main.py"],"python"),["src/main.py"]);
  assert.deepEqual(defaultRuntimeSources(["helper.py","pkg/__main__.py"],"python"),["pkg/__main__.py"]);
});

test("Python saves its immutable source and result language; older servers are refused before saving",async()=>{
  const f=fixture();await f.controller.prepare();
  assert.equal(await f.controller.run({language:"python",sources:["main.py"],stdin:""}),true);
  const state=f.controller.getState();assert.equal(state.resultLanguage,"python");assert.equal(state.result?.checkpoint_id,"cp_2");
  const html=renderRemoteLearning(state,input,false,true,"n");
  assert.match(html,/별도 컴파일 없음/);assert.doesNotMatch(html,/<h2>컴파일 /);
  const pythonHtml=renderRemoteLearning(state,{language:"python",sources:["main.py"],stdin:""},false,true,"n");
  assert.match(pythonHtml,/id="sources" size=/);assert.doesNotMatch(pythonHtml,/id="sources" multiple/);
  assert.match(pythonHtml,/현재 코드 저장 후 실행/);
  f.state.python=false;await f.controller.refresh();const before=f.state.calls.length;
  assert.equal(await f.controller.run({language:"python",sources:["main.py"],stdin:""}),false);
  assert.equal(f.state.calls.length,before);f.controller.dispose();
});

test("opening or refreshing reads status only; disabled environments never allocate or save",async()=>{
  const f=fixture();await f.controller.refresh(); assert.deepEqual(f.state.calls,["get"]);
  f.state.enabled=false;assert.equal(await f.controller.prepare(),true);assert.equal(f.controller.getState().phase,"disabled");
  assert.deepEqual(f.state.calls,["get","get"]); f.controller.dispose();
});

test("prepare, run and close each use a new acknowledged source snapshot, never a previous result",async()=>{
  const f=fixture();assert.equal(await f.controller.prepare(),true);assert.equal(f.controller.needsClose,true);
  assert.equal(await f.controller.run(input),true);assert.equal(f.controller.getState().result?.checkpoint_id,"cp_2");
  assert.equal(await f.controller.close(),true);assert.equal(f.controller.getState().snapshot?.checkpoint.checkpoint_id,"cp_3");
  assert.equal(f.controller.getState().cleanupVerified,true);assert.equal(f.controller.needsClose,false);
  assert.deepEqual(f.state.calls,["get","save:1","create:cp_1","save:2","run:cp_2","save:3","close:cp_3"]);
  await f.controller.close();assert.equal(f.state.calls.length,7,"a verified close is idempotent");f.controller.dispose();
});

test("failed save or new source absence never runs or closes stale code",async()=>{
  const f=fixture();await f.controller.prepare(); f.state.saveFails=true;
  assert.equal(await f.controller.run(input),false);assert.equal(await f.controller.close(),false);
  assert.equal(f.state.calls.some(call=>call.startsWith("run:")||call.startsWith("close:")),false);
  assert.equal(f.controller.needsClose,true);assert.notEqual(f.controller.getState().cleanupVerified,true);
  f.state.saveFails=false;assert.equal(await f.controller.run({...input,sources:["missing.cpp"]}),false);
  assert.equal(f.state.calls.some(call=>call.startsWith("run:")),false);f.controller.dispose();
});

test("logout, server switch and assignment change cancel a pending request and erase private results",async()=>{
  for(const changed of [undefined,{...scope,generation:2},{...scope,baseUrl:"https://other.test"},{...scope,assignmentId:"other"}]) {
    const f=fixture();await f.controller.prepare();let finish!:()=>void, signal!:AbortSignal;
    f.state.onRun=async value=>{signal=value;await new Promise<void>(resolve=>{finish=resolve;});};
    const pending=f.controller.run(input);for(let n=0;n<10;n++)await Promise.resolve();
    f.state.scope=changed;f.controller.reconcile();assert.equal(signal.aborted,true);
    finish();assert.equal(await pending,false);assert.deepEqual(f.controller.getState(),{phase:"idle"});f.controller.dispose();
  }
});

test("a request cannot proceed without trusted authenticated scope",async()=>{
  const f=fixture();f.state.scope=undefined;assert.equal(await f.controller.prepare(),false);assert.equal(await f.controller.run(input),false);
  assert.deepEqual(f.state.calls,[]);assert.match(f.controller.getState().message!,/신뢰/);f.controller.dispose();
});

test("editing while remote code runs marks its result as an older version",async()=>{
  const f=fixture();await f.controller.prepare();let finish!:()=>void;
  f.state.onRun=async()=>new Promise<void>(resolve=>{finish=resolve;});const pending=f.controller.run(input);
  for(let n=0;n<10;n++)await Promise.resolve();f.controller.markEdited();finish();await pending;
  assert.equal(f.controller.getState().editedSinceResult,true);assert.equal(f.controller.getState().result?.checkpoint_id,"cp_2");f.controller.dispose();
});

test("edits during upload or environment allocation cannot be relabeled as the latest result",async()=>{
  const f=fixture();f.state.onCreate=async()=>{f.controller.markEdited();};
  await f.controller.prepare();assert.equal(f.controller.getState().editedSinceResult,true);
  f.state.onSave=async()=>{f.controller.markEdited();};
  await f.controller.run(input);assert.equal(f.controller.getState().editedSinceResult,true);
  assert.equal(f.controller.getState().result?.checkpoint_id,"cp_2");
  f.state.onSave=async()=>{};await f.controller.run(input);assert.equal(f.controller.getState().editedSinceResult,false);
  assert.equal(f.controller.getState().result?.checkpoint_id,"cp_3");f.controller.dispose();
});

test("ambiguous create and failed cleanup retain the need to close; duplicate actions do not overlap",async()=>{
  const f=fixture();let finish!:()=>void;f.state.onCreate=async()=>new Promise<void>(resolve=>{finish=resolve;});
  const first=f.controller.prepare();for(let n=0;n<10;n++)await Promise.resolve();
  assert.equal(await f.controller.prepare(),false);finish();await first;
  f.state.closeFails=true;assert.equal(await f.controller.close(),false);assert.equal(f.controller.needsClose,true);
  assert.notEqual(f.controller.getState().cleanupVerified,true);
  f.state.closeFails=false;assert.equal(await f.controller.close(),true);f.controller.dispose();
});

test("unconfirmed create discovers and closes the remote session instead of silently forgetting it",async()=>{
  const f=fixture();f.state.onCreate=async()=>{f.state.session=session();throw {status:503};};
  assert.equal(await f.controller.prepare(),false);assert.equal(f.controller.needsClose,true);
  assert.equal(await f.controller.close(),true);assert.equal(f.controller.getState().cleanupVerified,true);f.controller.dispose();
});

test("closed and expired spaces are not active allocations",async()=>{
  const f=fixture();f.state.session={...session(),status:"closed"};await f.controller.refresh();assert.equal(f.controller.needsClose,false);
  f.state.session={...session(),status:"expired"};await f.controller.refresh();assert.equal(f.controller.needsClose,false);f.controller.dispose();
});

test("error guidance never displays raw exceptions containing credentials or local paths",()=>{
  assert.doesNotMatch(runtimeErrorMessage(new Error("secret token /private/file.cpp")),/secret|private/);
  assert.match(runtimeErrorMessage({status:401}),/인증/);assert.match(runtimeErrorMessage({status:429}),/잠시/);
  assert.match(runtimeErrorMessage({status:503}),/실행 관리자/);
});

test("source defaults use current language, renderer escapes output and binds a strict CSP",()=>{
  assert.deepEqual(defaultRuntimeSources(["help.cpp","src/main.cpp","main.c"],"cpp"),["src/main.cpp"]);
  const hostile={...result(),run:{exit_code:0,stdout:'<script>steal()</script>\x1b[31m',stderr:'<img onerror="x">'}};
  const html=renderRemoteLearning({phase:"ready",info:{enabled:true,provider:"remote_linux",languages:["c","cpp"],session:session()},session:session(),snapshot:snapshot(),result:hostile,editedSinceResult:true},input,false,true,"nonce-test");
  assert.ok(html.includes('&lt;script&gt;steal()&lt;/script&gt;'));assert.ok(!html.includes('<script>steal()'));
  assert.ok(!html.includes('\x1b'));assert.match(html,/default-src 'none'/);assert.match(html,/form-action 'none'/);
  assert.match(html,/실행 이후 코드가 변경/);assert.match(html,/코드 버전: cp_1/);assert.match(html,/max-height:360px/);
  assert.match(html,/표준입력/);assert.match(html,/컴파일/);assert.match(html,/프로그램 실행/);
  const disabled=renderRemoteLearning({phase:"disabled"},input,false,true,"n");assert.match(disabled,/data-action="run" disabled/);
});
