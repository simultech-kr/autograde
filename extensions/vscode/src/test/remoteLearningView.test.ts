import assert from "node:assert/strict";
import test from "node:test";
import type { LearningCheckpointScope } from "../learningCheckpoint";
import type { LearningRuntimeApi, LearningRuntimeSession } from "../remoteLearning";

interface Loader {_load(name:string,parent:unknown,main:boolean):unknown}
const loader=require("node:module") as Loader,original=loader._load;
let trusted=true,html="",handler:((message:unknown)=>Promise<void>)|undefined,disposeHandler:(()=>void)|undefined;
let creation=0,options:Record<string,unknown>={};
const stub={ViewColumn:{One:1},workspace:{get isTrusted(){return trusted;}},window:{showInformationMessage:async()=>{},
  createWebviewPanel:(_type:string,_title:string,_column:number,given:Record<string,unknown>)=>{
    creation++;options=given;
    return {reveal(){},dispose(){disposeHandler?.();},webview:{set html(value:string){html=value;},get html(){return html;},
      onDidReceiveMessage(callback:typeof handler){handler=callback;return{dispose(){handler=undefined;}};}},
      onDidDispose(callback:()=>void){disposeHandler=callback;return{dispose(){disposeHandler=undefined;}};}};
  }}};
loader._load=function(name,parent,main){return name==="vscode"?stub:original.call(this,name,parent,main);};
const {RemoteLearningView}=require("../remoteLearningView") as typeof import("../remoteLearningView");
loader._load=original;
const initialScope:LearningCheckpointScope={baseUrl:"https://school.test",generation:1,assignmentId:"a1",rootPath:"/private/code",scheme:"file",authority:""};
const session:LearningRuntimeSession={id:"space_one",status:"ready",checkpoint_id:"cp_1",created_at:"2026-10-28T10:00:00Z",expires_at:"2026-10-28T11:00:00Z"};
const input={language:"cpp",sources:["main.cpp"],stdin:""};
function fixture(){
  trusted=true;html="";
  const state={scope:initialScope as LearningCheckpointScope|undefined,calls:[] as string[],failClose:false};
  const api:LearningRuntimeApi={
    async getLearningRuntime(){state.calls.push("get");return{enabled:true,provider:"remote_linux",languages:["c","cpp","python"],session:null};},
    async createLearningRuntime(){state.calls.push("create");return session;},
    async runLearningRuntime(){state.calls.push("run");return{session,checkpoint_id:"cp_1",status:"completed",compile:{exit_code:0,stdout:"",stderr:""},run:{exit_code:0,stdout:"ok",stderr:""}};},
    async closeLearningRuntime(){state.calls.push("close");if(state.failClose)throw{status:503};return{status:"closed",checkpoint_id:"cp_1",cleanup_verified:true};}
  };
  const view=new RemoteLearningView(api,{currentScope:()=>state.scope,saveSnapshot:async()=>{
    state.calls.push("save");return{checkpoint:{checkpoint_id:"cp_1",assignment_id:"a1",archive_sha256:"a".repeat(64),created_at:session.created_at,file_count:3,source_bytes:12,version:1},filePaths:["main.cpp","main.py","helper.py"]};},onStatus:()=>{}});
  return{view,state};
}

test("opening the remote panel reads only, is a singleton and disables command URLs and local resources",async()=>{
  const f=fixture(),before=creation;
  await f.view.show();await f.view.show();assert.equal(creation,before+1);assert.deepEqual(f.state.calls,["get","get"]);
  assert.equal(options.enableCommandUris,false);assert.deepEqual(options.localResourceRoots,[]);assert.equal(options.retainContextWhenHidden,false);
  assert.match(html,/개인|학생별/);f.view.dispose();
});

test("remote panel validates messages and rechecks trust independent of rendered buttons",async()=>{
  const f=fixture();await f.view.show();
  await handler?.({type:"remoteLearning",action:"prepare",input,command:"arbitrary"});
  await handler?.({type:"remoteLearning",action:"arbitrary",input});
  await handler?.({type:"remoteLearning",action:"prepare",input:{...input,token:"secret"}});
  trusted=false;await handler?.({type:"remoteLearning",action:"prepare",input});
  assert.deepEqual(f.state.calls,["get"]);assert.match(html,/과제 폴더/);
  f.view.dispose();
});

test("prepared panel displays captured sources, dispatches run and verifies close before exit",async()=>{
  const f=fixture();await f.view.show();await handler?.({type:"remoteLearning",action:"prepare",input});
  assert.match(html,/<option value="main.cpp" selected>/);
  await handler?.({type:"remoteLearning",action:"run",input:{...input,stdin:"123"}});
  assert.match(html,/실행 완료/);assert.match(html,/<pre>ok<\/pre>/);assert.ok(f.state.calls.includes("run"));
  f.state.failClose=true;assert.equal(await f.view.finishBeforeExit(),false);assert.doesNotMatch(html,/서버 저장·원격 공간 삭제 확인됨/);
  f.state.failClose=false;assert.equal(await f.view.finishBeforeExit(),true);assert.match(html,/서버 저장·원격 공간 삭제 확인됨/);
  const calls=f.state.calls.length;assert.equal(await f.view.finishBeforeExit(),true);assert.equal(f.state.calls.length,calls);
  f.view.dispose();
});

test("authentication change clears rendered source, output and stdin; disposal does not allocate cleanup",async()=>{
  const f=fixture();await f.view.show();await handler?.({type:"remoteLearning",action:"prepare",input});
  await handler?.({type:"remoteLearning",action:"input",input:{...input,stdin:"private text"}});
  await handler?.({type:"remoteLearning",action:"run",input:{...input,stdin:"private text"}});
  assert.match(html,/private text/);
  f.state.scope=undefined;f.view.reconcile();assert.doesNotMatch(html,/private text|<pre>ok|option value="main.cpp"/);
  const before=f.state.calls.length;f.view.dispose();assert.equal(f.state.calls.length,before);
});

test("Python selection dispatches one entry and keeps its result label after changing language",async()=>{
  const f=fixture();await f.view.show();await handler?.({type:"remoteLearning",action:"prepare",input});
  const python={language:"python",sources:["main.py"],stdin:"3\n"};
  await handler?.({type:"remoteLearning",action:"language",input:python});
  assert.match(html,/<option value="main.py" selected>/);assert.doesNotMatch(html,/id="sources" multiple/);
  await handler?.({type:"remoteLearning",action:"run",input:python});
  assert.match(html,/별도 컴파일 없음/);assert.match(html,/<pre>ok/);
  await handler?.({type:"remoteLearning",action:"language",input});
  assert.match(html,/별도 컴파일 없음/);assert.equal(f.view.controller.getState().resultLanguage,"python");
  f.view.dispose();
});
