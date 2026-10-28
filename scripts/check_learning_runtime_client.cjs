#!/usr/bin/env node
// Compiled IDE client against check_learning_runtime.py --serve.
// Only invented fixture identity 20260003 / 000003 is used. Never production.
// Run after compiling extensions/vscode; requires its normal dependencies.
'use strict';

const assert = require('node:assert/strict');
const {mkdtemp, mkdir, writeFile, readFile, rm} = require('node:fs/promises');
const {tmpdir} = require('node:os');
const path = require('node:path');
const {randomUUID} = require('node:crypto');
const {AutogradeClient, HttpTransport, TokenManager, ApiError} = require('../extensions/vscode/out/api.js');
const {RemoteLearningController} = require('../extensions/vscode/out/remoteLearning.js');
const {createSubmissionBundle, extractSubmissionBundle, writeWorkspaceMarker} = require('../extensions/vscode/out/bundle.js');

const COURSE = 'come2201', ASSIGNMENT = 'pilot_come2201_00';
const args = process.argv.slice(2);
const config = {api: 'http://127.0.0.1:18180', web: 'http://127.0.0.1:18181', report: 'build/learning-runtime-client-report.json'};
for (let i = 0; i < args.length; i += 2) {
  const key = args[i]?.replace(/^--/, '');
  if (!['api', 'web', 'report'].includes(key) || !args[i+1]) throw new Error('Use --api URL --web URL --report PATH');
  config[key] = args[i+1];
}
function loopbackOnly(value) {
  const url = new URL(value);
  if (url.protocol !== 'http:' || url.hostname !== '127.0.0.1' || url.username || url.password || url.pathname !== '/' || url.search || url.hash)
    throw new Error('This gate accepts only http://127.0.0.1:<port> fixture origins');
  return url.origin;
}
config.api = loopbackOnly(config.api); config.web = loopbackOnly(config.web);
const reportPath = path.resolve(config.report);
const checks = [], cookieJar = new Map();
let stage = 'startup', temporary, client, tokens, controller, scope;
function verify(name, condition) {
  if (!condition) throw new Error(`Check failed: ${name}`);
  checks.push(name);
}
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));

async function webRequest(endpoint, fields) {
  const response = await fetch(config.web + endpoint, {
    method: fields ? 'POST' : 'GET', redirect: 'manual', signal: AbortSignal.timeout(20_000),
    headers: {...(cookieJar.size ? {Cookie: [...cookieJar].map(([key,value]) => `${key}=${value}`).join('; ')} : {}),
      ...(fields ? {'Content-Type': 'application/x-www-form-urlencoded', Origin: config.web} : {})},
    ...(fields ? {body: new URLSearchParams(fields)} : {}),
  });
  // Do not follow redirects outside the fixture or print a response that could contain a code.
  assert.equal(response.status, 200, `Fixture web ${endpoint} should return 200`);
  for (const value of response.headers.getSetCookie()) {
    const pair = value.split(';', 1)[0], at = pair.indexOf('=');
    cookieJar.set(pair.slice(0,at), pair.slice(at+1));
  }
  const body = await response.text();
  assert.ok(Buffer.byteLength(body) <= 1024 * 1024, 'Fixture web response too large');
  return body;
}
function csrf(html) {
  const match = html.match(/name="csrf" value="([^"]+)"/);
  assert.ok(match, 'Fixture CSRF field missing'); return match[1];
}

async function authenticate() {
  stage = 'fixture_web_claim';
  const entry = await webRequest(`/courses/${COURSE}`);
  const selected = await webRequest(`/courses/${COURSE}/login`, {csrf:csrf(entry),student_key:'20260003',password:'000003'});
  const claimed = await webRequest(`/courses/${COURSE}/claims`, {csrf:csrf(selected),assignment_id:ASSIGNMENT});
  const code = claimed.match(/<code>(AK1-[A-Z0-9-]+)<\/code>/)?.[1];
  assert.ok(code, 'Fixture claim was not issued');
  stage = 'compiled_client_authentication';
  const transport = new HttpTransport(() => config.api);
  tokens = new TokenManager(transport); client = new AutogradeClient(transport,tokens);
  const signal = new AbortController().signal;
  const device = await client.createDeviceAuthorization('Synthetic Mac IDE runtime gate','0.5.18',signal,config.api,code);
  const accepted = await client.redeemAssignmentClaim(code,device.device_code,signal,config.api);
  assert.equal(accepted.assignmentId,ASSIGNMENT);
  const session = await client.exchangeDeviceCode(device.device_code,signal,config.api);
  await tokens.storeSession(session,config.api);
  cookieJar.clear();
  verify('claim_and_device_auth_through_compiled_client',true);
}

async function captureAndSave(selected) {
  const bundle = await createSubmissionBundle(selected.rootPath), key = `runtime-client-${randomUUID()}`;
  const signal = new AbortController().signal;
  for(let attempt=0; attempt<4; attempt++) {
    try {
      const checkpoint = await client.saveLearningCheckpoint(selected,bundle.archive,bundle.sha256,signal,key);
      assert.equal(checkpoint.received_archive_sha256 ?? checkpoint.archive_sha256,bundle.sha256);
      assert.equal(checkpoint.assignment_id,ASSIGNMENT);
      return {checkpoint,filePaths:bundle.filePaths};
    } catch(error) {
      if (!(error instanceof ApiError) || error.status !== 429 || attempt===3) throw error;
      // Keep the real server's checkpoint throttle and retry the same immutable bytes/key.
      await wait(Math.min(30_000,Math.max(11_000,error.retryAfterMs || 0)));
    }
  }
  throw new Error('Checkpoint save exhausted');
}

async function main() {
  await authenticate();
  temporary = await mkdtemp(path.join(tmpdir(),'autograde-client-runtime-'));
  const root = path.join(temporary,'source'); await mkdir(root,{mode:0o700});
  scope = {baseUrl:config.api,generation:tokens.getSessionGeneration(),assignmentId:ASSIGNMENT,rootPath:root,scheme:'file',authority:''};
  const c = '#include <stdio.h>\nint main(void){int n;if(scanf("%d",&n)!=1)return 2;printf("C:%d\\n",n*2);return 0;}\n';
  const cpp = '#include <iostream>\nint main(){std::string n;std::cin>>n;std::cout<<"Hello "<<n<<"\\n";}\n';
  const edited = '#include <iostream>\nint main(){std::string n;std::cin>>n;std::cout<<"Updated "<<n<<"\\n";}\n';
  await writeFile(path.join(root,'main.c'),c);await writeFile(path.join(root,'main.cpp'),cpp);
  await writeFile(path.join(root,'main.py'),"from helper import twice\nprint('Python:', twice(int(input())))\n");
  await writeFile(path.join(root,'helper.py'),"def twice(value): return value * 2\n");
  await writeWorkspaceMarker(root,{schemaVersion:1,serviceBaseUrl:config.api,assignmentId:ASSIGNMENT});
  const initial = await client.getSubmissionHistory(ASSIGNMENT);
  assert.equal(initial.submissions.length,0,'Use the clean synthetic fixture student 20260003');
  controller = new RemoteLearningController(client,{currentScope:()=>scope,saveSnapshot:captureAndSave,onState:()=>{}});
  stage = 'environment_preparation';
  assert.equal(await controller.prepare(),true,'Compiled controller prepare');
  const prepared = controller.getState();
  verify('owned_remote_linux_space_ready',prepared.info?.enabled===true&&prepared.session?.status==='ready');
  verify('prepare_uses_exact_saved_checkpoint',prepared.session.checkpoint_id===prepared.snapshot.checkpoint.checkpoint_id);
  stage = 'c_compile_and_run';
  assert.equal(await controller.run({language:'c',sources:['main.c'],stdin:'21\n'}),true);
  const cResult = controller.getState().result;
  verify('c17_compilation_and_stdin',cResult?.status==='completed'&&cResult.compile.exit_code===0&&cResult.run?.stdout==='C:42\n');
  stage = 'cpp_compile_and_run';
  assert.equal(await controller.run({language:'cpp',sources:['main.cpp'],stdin:'Autograde\n'}),true);
  const oldResult = controller.getState().result;
  verify('cpp17_compilation_and_stdin',oldResult?.status==='completed'&&oldResult.run?.stdout==='Hello Autograde\n');
  await writeFile(path.join(root,'main.cpp'),edited);controller.markEdited();
  verify('editing_marks_previous_run_as_older_code',controller.getState().editedSinceResult===true);
  stage = 'edited_code_rerun';
  assert.equal(await controller.run({language:'cpp',sources:['main.cpp'],stdin:'Student\n'}),true);
  const current = controller.getState();
  verify('latest_edited_code_runs',current.result?.run?.stdout==='Updated Student\n'&&current.result.status==='completed');
  verify('latest_result_bound_to_new_checkpoint',current.result.checkpoint_id===current.snapshot.checkpoint.checkpoint_id&&current.result.checkpoint_id!==oldResult.checkpoint_id);
  verify('current_run_clears_older_code_notice',current.editedSinceResult===false);
  stage = 'python_run';
  assert.equal(await controller.run({language:'python',sources:['main.py'],stdin:'21\n'}),true);
  const pythonResult = controller.getState();
  verify('python3_entry_and_local_module_stdin',pythonResult.result?.status==='completed'&&pythonResult.result.run?.stdout==='Python: 42\n');
  verify('python_result_language_and_saved_version',pythonResult.resultLanguage==='python'&&pythonResult.result.checkpoint_id===pythonResult.snapshot.checkpoint.checkpoint_id);
  verify('python_compile_phase_empty',pythonResult.result.compile.exit_code===0&&pythonResult.result.compile.stdout===''&&pythonResult.result.compile.stderr==='');
  stage = 'save_and_close';
  assert.equal(await controller.close(),true);
  const closed = controller.getState();
  verify('server_save_and_cleanup_verified',closed.cleanupVerified===true&&closed.session?.status==='closed'&&!controller.needsClose);
  assert.equal(await controller.close(),true);
  verify('client_close_idempotent',controller.getState().cleanupVerified===true);
  stage = 'checkpoint_restore_after_close';
  const bytes = await client.getLearningCheckpointBundle(scope,closed.snapshot.checkpoint,new AbortController().signal);
  const restored = path.join(temporary,'restored'); await extractSubmissionBundle(bytes,restored);
  verify('latest_source_restores_after_space_deletion',await readFile(path.join(restored,'main.cpp'),'utf8')===edited);
  verify('other_source_survives_checkpoint_restore',await readFile(path.join(restored,'main.c'),'utf8')===c);
  stage = 'resume_in_new_space';
  scope = {...scope,rootPath:restored};
  await writeWorkspaceMarker(restored,{schemaVersion:1,serviceBaseUrl:config.api,assignmentId:ASSIGNMENT});
  controller.reconcile();assert.equal(await controller.prepare(),true);
  verify('restored_work_gets_new_private_space',controller.getState().session.id!==closed.session.id);
  assert.equal(await controller.run({language:'cpp',sources:['main.cpp'],stdin:'Resume\n'}),true);
  verify('restored_code_runs_in_new_space',controller.getState().result?.run?.stdout==='Updated Resume\n');
  assert.equal(await controller.close(),true);
  verify('resumed_space_also_deleted',controller.getState().cleanupVerified===true);
  const final = await client.getSubmissionHistory(ASSIGNMENT);
  verify('practice_created_no_formal_submissions',final.submissions.length===0&&!final.hasMore);
  const sessions = await client.getLearningRuntime(scope,new AbortController().signal);
  verify('server_confirms_space_is_closed',sessions.session===null||['closed','expired'].includes(sessions.session.status));
  stage = 'complete';
  return {status:'passed',check_count:checks.length,checks,provider:'compiled-ide-client-and-real-linux-worker',
    node_version:process.version,production_data_used:false,formal_submissions_created:0,credentials_or_claims_recorded:false};
}

async function finish() {
  // A failure still tries a verified close through the owned controller, then drops all local test files.
  if(controller?.needsClose&&scope) await controller.close().catch(()=>false);
  controller?.dispose();
  if(client&&tokens) {try{await client.revokeCurrentSession();}catch{}await tokens.clear();}
  cookieJar.clear();scope=undefined;
  if(temporary)await rm(temporary,{recursive:true,force:true});
}

(async()=>{
  let report;
  try {report=await main();}
  catch(error) {
    report={status:'failed',stage,check_count:checks.length,checks,production_data_used:false,
      // Only bounded, non-secret categories are retained. Never print exception messages or responses.
      error_type:error instanceof ApiError?'ApiError':error?.name==='AssertionError'?'AssertionError':'Error',
      ...(error instanceof ApiError?{http_status:error.status}:{}),credentials_or_claims_recorded:false};
    process.exitCode=1;
  } finally {await finish();}
  await mkdir(path.dirname(reportPath),{recursive:true});await writeFile(reportPath,JSON.stringify(report,null,2)+'\n',{mode:0o600});
  process.stdout.write(JSON.stringify(report)+'\n');
})().catch(()=>{process.stderr.write('Synthetic runtime client gate failed during cleanup or report writing\n');process.exitCode=1;});
