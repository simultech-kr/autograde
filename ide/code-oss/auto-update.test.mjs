import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import {generateKeyPairSync} from 'node:crypto';
import {spawn} from 'node:child_process';
import {pathToFileURL} from 'node:url';
import {canonical, digest, createRelease, signDocument, stageRelease, activateRelease, authorize} from './release-channel.mjs';
import {checkForUpdate, getUpdateState, preparePendingUpdate, failUpdateTrial, acquireChannelLease, updateSettings, cleanupInterruptedDownloads} from './auto-update.mjs';
import {runChannel} from './channel-launcher.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(process.platform === 'darwin' ? '/private/tmp' : fs.realpathSync(os.tmpdir()), 'ag-u-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = path.join(root, 'store'), sessions = path.join(root, 'seat'), trace = path.join(root, 'runs.txt');
  fs.mkdirSync(sessions);
  const pair = () => {const p = generateKeyPairSync('ed25519'); return {key: p.privateKey.export({format:'pem',type:'pkcs8'}), public: p.publicKey.export({format:'pem',type:'spki'})};};
  const publisher = pair(), school = pair(), profile = {institutionId: 'school'};
  const context = {institutionId:'school',channel:'pilot',platform:process.platform,architecture:process.arch,serverRelease:'server-1',extensionVersion:'0.5.22',
    extensionVersionPolicy:'institution-approved',institutionProfileSha256:digest(canonical(profile)),publisherKeys:{p:publisher.public},institutionKeys:{s:school.public},sessionRoots:[sessions],
    updates:{feedUrl:'https://school.invalid/v1/ide-updates/pilot/feed.json',checkIntervalSeconds:60}};
  const contextFile = path.join(root, 'context.json'), configFile = path.join(root, 'channel.json');
  fs.writeFileSync(contextFile, JSON.stringify(context), {mode:0o600});
  fs.writeFileSync(configFile, JSON.stringify({schema:1,store,context:contextFile}), {mode:0o600});
  const release = (id, sequence, body, node = false) => {
    const payload = path.join(root, id); fs.mkdirSync(payload);
    fs.writeFileSync(path.join(payload, 'institution.json'), JSON.stringify(profile));
    if (node) fs.copyFileSync(process.execPath, path.join(payload, 'runner'));
    else fs.writeFileSync(path.join(payload, 'runner'), '#!/bin/sh\nexec /bin/sh "$@"\n');
    fs.chmodSync(path.join(payload, 'runner'), 0o755);
    const script = {darwin:'mac-launcher.mjs',linux:'linux-launcher.mjs'}[process.platform] ?? 'mac-launcher.mjs';
    fs.writeFileSync(path.join(payload, script), body ?? `echo ${id} >> '${trace}'\nprintf '{"event":"ide-started","pid":%s}\\n' "$$"\nsleep 0.15\nprintf '%s\\n' '{"event":"session-cleaned"}'\n`);
    const document = createRelease(payload,{releaseId:id,sequence,platform:process.platform,architecture:process.arch,ideVersion:'1.143.0',extensionVersion:`0.5.${21+sequence}`,
      serverReleases:['server-1'],createdAt:'2026-10-30T00:00:00Z',entrypoint:'runner',entrypointScript:script,nativeSigningRoot:'runner',institutionProfilePath:'institution.json',institutionProfileSha256:context.institutionProfileSha256});
    const releaseEnvelope = signDocument(document,publisher.key,'p'), approvalEnvelope = signDocument({kind:'autograde.institution-approval.v1',institutionId:'school',releaseSha256:digest(canonical(document)),channel:'pilot',notBefore:'2020-01-01T00:00:00Z',expiresAt:'2099-01-01T00:00:00Z'},school.key,'s');
    return {payload,releaseEnvelope,approvalEnvelope};
  };
  const seed = item => {stageRelease({store,context,...item});activateRelease({store,context,releaseId:item.releaseEnvelope.document.releaseId});};
  const download = item => async ({destination,signal}) => {signal?.throwIfAborted();fs.mkdirSync(destination);const payload=path.join(destination,'payload');fs.cpSync(item.payload,payload,{recursive:true});return {...item,payload};};
  return {root,store,sessions,trace,context,contextFile,configFile,release,seed,download};
}
const noNetwork = async () => {};

test('extension upgrades require explicit institution-approved policy; exact pins stay enforced', t => {
  const f=fixture(t), r=f.release('two',2);
  assert.equal(authorize(r.releaseEnvelope,r.approvalEnvelope,f.context).extensionVersion,'0.5.23');
  const context={...f.context};delete context.extensionVersionPolicy;
  assert.throws(()=>authorize(r.releaseEnvelope,r.approvalEnvelope,context),/incompatible/);
  assert.throws(()=>authorize(r.releaseEnvelope,r.approvalEnvelope,{...context,extensionVersionPolicy:'anything'}),/incompatible/);
});
test('download during class leaves current release unchanged and applies only on next launch', async t => {
  const f=fixture(t);f.seed(f.release('one',1));const next=f.release('two',2), events=[];
  let checked=false;
  const watch=async options=>{
    await checkForUpdate({...options,download:f.download(next)});
    assert.equal(getUpdateState(f.store).active.releaseId,'one');
    assert.equal(getUpdateState(f.store).pending.releaseId,'two');checked=true;
  };
  assert.equal(await runChannel(f.configFile,e=>events.push(e),{watch}),0);assert.ok(checked);
  assert.equal(getUpdateState(f.store).active.releaseId,'one');
  assert.equal(await runChannel(f.configFile,e=>events.push(e),{watch:noNetwork}),0);
  assert.equal(getUpdateState(f.store).active.releaseId,'two');assert.equal(getUpdateState(f.store).trial,undefined);
  assert.equal(fs.readFileSync(f.trace,'utf8'),'one\ntwo\n');
  assert.ok(events.some(e=>e.phase==='ready-next-launch'));assert.ok(events.some(e=>e.phase==='applied'));
});
test('offline and verification failures run the existing executable without changing selection', async t => {
  const f=fixture(t);f.seed(f.release('one',1));
  for(const reason of ['offline','hash mismatch','insufficient disk']) {
    assert.equal(await runChannel(f.configFile,()=>{},{watch:options=>checkForUpdate({...options,download:async()=>{throw new Error(reason);}})}),0);
    assert.equal(getUpdateState(f.store).active.releaseId,'one');assert.equal(getUpdateState(f.store).pending,undefined);
  }
  assert.equal(fs.readFileSync(f.trace,'utf8'),'one\none\none\n');
  assert.ok(!fs.readFileSync(path.join(f.store,'update-state.json'),'utf8').includes('insufficient disk'));
});
test('new launcher failure restores verified previous executable in the same click and quarantines candidate', async t => {
  const f=fixture(t);f.seed(f.release('one',1));const bad=f.release('bad',2,"printf '%s\\n' '{\"event\":\"error\",\"message\":\"synthetic private content\"}'\nexit 7\n");
  await checkForUpdate({...f,download:f.download(bad)});const events=[];
  assert.equal(await runChannel(f.configFile,e=>events.push(e),{watch:noNetwork}),0);
  assert.equal(getUpdateState(f.store).active.releaseId,'one');assert.deepEqual(getUpdateState(f.store).failedReleaseIds,['bad']);
  assert.equal(fs.readFileSync(f.trace,'utf8'),'one\n');assert.ok(events.some(e=>e.phase==='rolled-back'));
  assert.ok(!events.some(e=>e.event==='error'));assert.ok(!JSON.stringify(events).includes('synthetic private content'));
  let selection;
  await checkForUpdate({...f,download:async options=>{selection=options;return null;}});
  assert.ok(selection.excludeReleaseIds.includes('bad'));assert.equal(selection.minSequence,2);
});
test('activation failure and modified downloaded payload preserve old version', async t => {
  const f=fixture(t);f.seed(f.release('one',1));const next=f.release('two',2);
  await checkForUpdate({...f,download:f.download(next)});
  fs.writeFileSync(path.join(f.store,'releases/two/payload/institution.json'),'{}');
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);
  assert.equal(getUpdateState(f.store).active.releaseId,'one');assert.ok(getUpdateState(f.store).failedReleaseIds.includes('two'));
});
test('pending cleanup defers activation, and failed new release cannot rollback across retained student data', async t => {
  const f=fixture(t);f.seed(f.release('one',1));await checkForUpdate({...f,download:f.download(f.release('two',2))});
  const current=path.join(f.sessions,'current');fs.mkdirSync(current);fs.writeFileSync(path.join(current,'login'),'synthetic');
  assert.equal(preparePendingUpdate(f),null);assert.equal(getUpdateState(f.store).active.releaseId,'one');
  assert.equal(fs.readFileSync(path.join(current,'login'),'utf8'),'synthetic');fs.rmSync(current,{recursive:true});
  assert.equal(preparePendingUpdate(f).releaseId,'two');fs.mkdirSync(current);
  assert.equal(failUpdateTrial(f).restored,false);assert.equal(getUpdateState(f.store).active.releaseId,'two');
  fs.rmdirSync(current);assert.equal(failUpdateTrial(f).restored,true);assert.equal(getUpdateState(f.store).active.releaseId,'one');
});
test('interrupted switch and rollback journal reconciles without reinstalling an unconfirmed release', async t => {
  const f=fixture(t);f.seed(f.release('one',1));await checkForUpdate({...f,download:f.download(f.release('two',2))});
  preparePendingUpdate(f);activateRelease({store:f.store,context:f.context,releaseId:'one',rollback:true});
  // Simulate power loss between rollback and deleting the trial journal.
  assert.equal(getUpdateState(f.store).trial.releaseId,'two');
  assert.equal(preparePendingUpdate(f),null);assert.equal(getUpdateState(f.store).trial,undefined);
});
test('closing IDE aborts unfinished network work and deletes only its temporary download', async t => {
  const f=fixture(t);f.seed(f.release('one',1));let cancelled=false;
  const watch=options=>checkForUpdate({...options,download:async({destination,signal})=>{
    fs.mkdirSync(destination);fs.writeFileSync(path.join(destination,'partial'),'incomplete');
    await new Promise((_,reject)=>signal.addEventListener('abort',()=>{cancelled=true;reject(new Error('aborted'));},{once:true}));
  }});
  assert.equal(await runChannel(f.configFile,()=>{},{watch}),0);assert.equal(cancelled,true);
  assert.deepEqual(fs.readdirSync(f.store).filter(x=>x.startsWith('.download-')),[]);
  assert.equal(getUpdateState(f.store).active.releaseId,'one');assert.equal(getUpdateState(f.store).pending,undefined);
});
test('concurrent icons refuse to share a release and dead supervised locks recover without deleting student roots', async t => {
  const f=fixture(t);f.seed(f.release('one',1));const release=acquireChannelLease(f.store);
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),1);release();
  const child=spawn('/bin/sh',['-c','exit 0']);await new Promise(resolve=>child.on('close',resolve));
  for(const name of ['.channel-lock.json','.launch-lock.json'])fs.writeFileSync(path.join(f.store,name),JSON.stringify({pid:child.pid}),{mode:0o600});
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);
});
test('update URL policy is HTTPS-only and is independent of discovery', t => {
  const f=fixture(t);
  for(const feedUrl of ['http://localhost/feed.json','https://user:secret@server/feed.json','https://server/feed.json?token=secret','https://server/feed.json#token'])assert.throws(()=>updateSettings({...f.context,updates:{feedUrl}}));
  assert.equal(updateSettings({...f.context,discoveredUpdateUrl:'https://evil.invalid/feed.json'}).feedUrl,f.context.updates.feedUrl);
});
test('expired previous approval never gets bypassed during automatic rollback', async t => {
  const f=fixture(t);f.seed(f.release('one',1));await checkForUpdate({...f,download:f.download(f.release('two',2))});preparePendingUpdate(f);
  fs.writeFileSync(path.join(f.store,'releases/one/approval.json'),'{}');
  assert.equal(failUpdateTrial(f).restored,false);assert.equal(getUpdateState(f.store).active.releaseId,'two');
});
test('disabling network updates does not skip recovery of an unconfirmed build', async t => {
  const f=fixture(t);f.seed(f.release('one',1));await checkForUpdate({...f,download:f.download(f.release('two',2))});preparePendingUpdate(f);
  fs.writeFileSync(f.contextFile,JSON.stringify({...f.context,updates:{feedUrl:'http://invalid/feed.json'}}));
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);
  assert.equal(getUpdateState(f.store).active.releaseId,'one');assert.equal(getUpdateState(f.store).trial,undefined);
  assert.equal(fs.readFileSync(f.trace,'utf8'),'one\n');
});
test('uncertain activation restoration keeps the trial journal and stops before running a student session', async t => {
  const f=fixture(t);f.seed(f.release('one',1));await checkForUpdate({...f,download:f.download(f.release('two',2))});
  assert.throws(()=>preparePendingUpdate({...f,activate:options=>{activateRelease(options);throw new Error('simulated restoration failure');}}),/Cannot confirm previous selection/);
  assert.equal(getUpdateState(f.store).active.releaseId,'two');assert.equal(getUpdateState(f.store).trial.releaseId,'two');
  assert.equal(fs.existsSync(f.trace),false);
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);assert.equal(getUpdateState(f.store).active.releaseId,'one');
});
test('a candidate silently exiting zero without starting IDE is not confirmed', async t => {
  const f=fixture(t);f.seed(f.release('one',1));await checkForUpdate({...f,download:f.download(f.release('empty',2,"printf '%s\\n' '{\"event\":\"session-cleaned\"}'\nexit 0\n"))});
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);assert.equal(getUpdateState(f.store).active.releaseId,'one');
  assert.ok(getUpdateState(f.store).failedReleaseIds.includes('empty'));
});
test('only owner-marked downloads of a dead supervisor are removed on next start', async t => {
  const f=fixture(t);f.seed(f.release('one',1));
  const child=spawn('/bin/sh',['-c','exit 0']);await new Promise(resolve=>child.on('close',resolve));
  const directories=[['a',child.pid],['b',process.pid],['c',null]].map(([letter,pid])=>{
    const directory=path.join(f.store,`.download-${letter.repeat(8)}-${letter.repeat(4)}-${letter.repeat(4)}-${letter.repeat(4)}-${letter.repeat(12)}`);
    fs.mkdirSync(directory,{mode:0o700});fs.writeFileSync(path.join(directory,'partial'),'download');
    if(pid)fs.writeFileSync(path.join(directory,'.download-owner.json'),JSON.stringify({schema:1,pid}),{mode:0o600});return directory;
  });
  cleanupInterruptedDownloads(f.store);
  assert.deepEqual(directories.map(directory=>fs.existsSync(directory)),[false,true,true]);
});
test('successful updates retain current and previous versions and prune only older automatic downloads', async t => {
  const f=fixture(t);f.seed(f.release('one',1));
  const manual=f.release('manual',2);stageRelease({...f,...manual});
  await checkForUpdate({...f,download:f.download(manual)});
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);
  for(let sequence=2;sequence<=4;sequence++){
    await checkForUpdate({...f,download:f.download(f.release(`r${sequence}`,sequence+1))});
    assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);
  }
  assert.deepEqual(fs.readdirSync(path.join(f.store,'releases')).sort(),['manual','one','r3','r4']);
  assert.deepEqual(getUpdateState(f.store).managedReleaseIds,['r3','r4']);
});
test('real shared Mac supervisor deletes code, course materials and login before updated/fallback starts', async t => {
  if(process.platform!=='darwin'){t.skip('Mac shared supervisor integration');return;}
  const f=fixture(t), source=path.join(f.root,'editor');fs.mkdirSync(source);
  const module=pathToFileURL(path.resolve('ide/code-oss/mac-launcher.mjs')).href;
  const sessionScript=id=>`import fs from 'node:fs';import path from 'node:path';import {spawn} from 'node:child_process';import {runMacSession} from ${JSON.stringify(module)};
const base=${JSON.stringify(f.sessions)}, trace=${JSON.stringify(f.trace)};
const bundle={manifest:{version:'1.143.0',policy:'standard',approvedBuiltins:['autograde']},source:${JSON.stringify(source)},executable:process.execPath,defaults:{}};
const result=await runMacSession(bundle,{runtimeBase:base,discoveryProfilePath:${JSON.stringify(path.join(f.root,'missing-profile.json'))},start:(_e,_a,options)=>spawn(process.execPath,['-e','setTimeout(()=>{},150)'],options),report:event=>{
 if(event.event==='ide-started'){const seat=path.join(base,'current');for(const name of ['workspace/main.cpp','materials/slides.pdf','r/user-data/User/login.json']){const file=path.join(seat,name);if(fs.existsSync(file))throw Error('previous student retained');fs.mkdirSync(path.dirname(file),{recursive:true});fs.writeFileSync(file,'synthetic student secret');}fs.appendFileSync(trace,${JSON.stringify(`${id}\n`)});}
 process.stdout.write(JSON.stringify(event)+'\\n');}});process.exitCode=result.code;
`;
  f.seed(f.release('one',1,sessionScript('one'),true));
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);assert.equal(fs.existsSync(path.join(f.sessions,'current')),false);
  await checkForUpdate({...f,download:f.download(f.release('two',2,sessionScript('two'),true))});
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);assert.equal(fs.existsSync(path.join(f.sessions,'current')),false);
  await checkForUpdate({...f,download:f.download(f.release('bad',3,'process.exitCode=7;',true))});
  assert.equal(await runChannel(f.configFile,()=>{},{watch:noNetwork}),0);assert.equal(fs.existsSync(path.join(f.sessions,'current')),false);
  assert.equal(fs.readFileSync(f.trace,'utf8'),'one\ntwo\ntwo\n');
  assert.equal(getUpdateState(f.store).active.releaseId,'two');
});
