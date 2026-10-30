import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { generateKeyPairSync } from 'node:crypto';
import { execFileSync, spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { canonical, digest, createRelease, signDocument, stageRelease, activateRelease, launchActive } from './release-channel.mjs';
import { channelBootstrapConfig, installMacChannel } from './install-channel-launcher.mjs';
import { loadChannelConfiguration, runChannel } from './channel-launcher.mjs';
import { channelDesktopTemplates } from './channel-desktop-templates.mjs';

function fixture(t) {
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'ag-channel-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const payload = path.join(root, 'payload'), store = path.join(root, 'releases'), sessions = path.join(root, 'seat');
  fs.mkdirSync(payload); fs.mkdirSync(sessions);
  const makeKey = () => { const p = generateKeyPairSync('ed25519'); return {private: p.privateKey.export({format:'pem',type:'pkcs8'}), public:p.publicKey.export({format:'pem',type:'spki'})}; };
  const publisher = makeKey(), school = makeKey();
  fs.writeFileSync(path.join(payload, 'institution.json'), JSON.stringify({institutionId:'test'}));
  const context = {institutionId:'test',channel:'pilot',platform:process.platform,architecture:process.arch,serverRelease:'test-server',extensionVersion:'0.5.21',
    institutionProfileSha256:digest(canonical({institutionId:'test'})),publisherKeys:{p:publisher.public},institutionKeys:{s:school.public},sessionRoots:[sessions]};
  const contextFile = path.join(root, 'context.json'); fs.writeFileSync(contextFile, JSON.stringify(context), {mode:0o600});
  const configFile = path.join(root, 'channel.json'); fs.writeFileSync(configFile, JSON.stringify({schema:1,store,context:contextFile}), {mode:0o600});
  const stage = (id, sequence, source) => {
    fs.writeFileSync(path.join(payload,'mac-launcher.mjs'), source);
    const document = createRelease(payload,{releaseId:id,sequence,platform:process.platform,architecture:process.arch,ideVersion:'1.143.0',extensionVersion:'0.5.21',serverReleases:['test-server'],
      createdAt:'2026-10-29T00:00:00Z',entrypoint:'node',entrypointScript:'mac-launcher.mjs',nativeSigningRoot:'node',institutionProfilePath:'institution.json',institutionProfileSha256:context.institutionProfileSha256});
    const releaseEnvelope = signDocument(document,publisher.private,'p'), approvalEnvelope = signDocument({kind:'autograde.institution-approval.v1',institutionId:'test',releaseSha256:digest(canonical(document)),channel:'pilot',notBefore:'2020-01-01T00:00:00Z',expiresAt:'2099-01-01T00:00:00Z'},school.private,'s');
    stageRelease({store,payload,releaseEnvelope,approvalEnvelope,context}); activateRelease({store,releaseId:id,context});
  };
  return {root,payload,store,sessions,context,contextFile,configFile,stage};
}
test('stable Mac icon configuration refuses placement inside releases or student working space', t => {
  if(process.platform!=='darwin'){t.skip('Mac operator icon configuration');return;}
  const f=fixture(t), output=path.join(f.root,'Autograde Channel.app');
  assert.equal(channelBootstrapConfig({store:f.store,context:f.contextFile,output}).store,f.store);
  assert.throws(()=>channelBootstrapConfig({store:f.store,context:f.contextFile,output:path.join(f.store,'Channel.app')}),/separate/);
  assert.throws(()=>channelBootstrapConfig({store:f.store,context:f.contextFile,output:path.join(f.sessions,'Channel.app')}),/student/);
  fs.chmodSync(f.configFile,0o666); assert.throws(()=>loadChannelConfiguration(f.configFile),/Unsafe/);
});
test('fixed channel follows newly approved releases on the next start and blocks recursive scripts', async t => {
  const f=fixture(t); fs.copyFileSync(process.execPath,path.join(f.payload,'node'));
  f.stage('one',1,"process.exitCode = 0;\n");
  assert.equal(await runChannel(f.configFile,()=>{}),0);
  f.stage('two',2,"process.exitCode = 7;\n");
  const events=[]; assert.equal(await runChannel(f.configFile,event=>events.push(event)),1); assert.equal(events[0].event,'error');
  activateRelease({store:f.store,releaseId:'one',context:f.context,rollback:true});
  assert.equal(await runChannel(f.configFile,()=>{}),0);
  const old=process.env.AUTOGRADE_RELEASE_CHANNEL_ACTIVE;
  try {process.env.AUTOGRADE_RELEASE_CHANNEL_ACTIVE='1';await assert.rejects(launchActive({store:f.store,context:f.context}),/Recursive/);}
  finally {if(old===undefined)delete process.env.AUTOGRADE_RELEASE_CHANNEL_ACTIVE;else process.env.AUTOGRADE_RELEASE_CHANNEL_ACTIVE=old;}
});
test('Mac icon builds with native Cocoa entry and its own pinned Node, without a student Node install', t => {
  if(process.platform!=='darwin'){t.skip('Native Mac build requires macOS');return;}
  const f=fixture(t), output=path.join(f.root,'Autograde Channel.app');
  const result=installMacChannel({store:f.store,context:f.contextFile,output,node:process.execPath});
  assert.equal(result.signing,'ad-hoc-candidate');assert.equal(result.deploymentReady,false);
  const resources=path.join(output,'Contents/Resources');
  assert.equal(execFileSync(path.join(resources,'runtime/bin/node'),['--version'],{encoding:'utf8'}).trim(),`v${process.versions.node}`);
  execFileSync('/usr/bin/codesign',['--verify','--strict',output],{stdio:'pipe'});
  assert.equal(loadChannelConfiguration(path.join(resources,'autograde/channel.json')).store,f.store);
  const plist=fs.readFileSync(path.join(output,'Contents/Info.plist'),'utf8');assert.match(plist,/info.cbchoi.autograde.channel/);
  for(const name of ['auto-update.mjs','update-transport.mjs']) assert.ok(fs.existsSync(path.join(resources,'autograde',name)));
  fs.copyFileSync(process.execPath,path.join(f.payload,'node'));
  f.stage('native-icon',1,"process.stdout.write(JSON.stringify({event:'ide-started',pid:process.pid})+'\\n');setTimeout(()=>process.stdout.write(JSON.stringify({event:'session-cleaned'})+'\\n'),100);\n");
  // Exercise the actual native icon executable with system Node absent from PATH.
  execFileSync(path.join(output,'Contents/MacOS/AutogradeLauncher'),[],{env:{...process.env,PATH:'/usr/bin:/bin'},stdio:'pipe',timeout:15000});
  assert.equal(fs.existsSync(path.join(f.store,'.channel-lock.json')),false);
  assert.throws(()=>installMacChannel({store:f.store,context:f.contextFile,output,node:process.execPath}),/new .app/);
});
test('closing the channel forwards shutdown to the original supervisor and waits for its cleanup receipt', async t => {
  if(process.platform==='win32'){t.skip('POSIX signal forwarding');return;}
  const f=fixture(t);fs.copyFileSync(process.execPath,path.join(f.payload,'node'));
  f.stage('shutdown',1,"process.on('SIGTERM',()=>{process.stdout.write('session-cleaned\\n');process.exit(0);});process.stdout.write('ready\\n');setInterval(()=>{},1000);\n");
  const tool=fileURLToPath(new URL('./release-channel.mjs',import.meta.url));
  const child=spawn(process.execPath,[tool,'launch','--store',f.store,'--context',f.contextFile],{stdio:['ignore','pipe','pipe']});
  let output='', sent=false;
  const timeout=setTimeout(()=>child.kill('SIGKILL'),10000);
  child.stdout.on('data',chunk=>{output+=chunk;if(!sent&&output.includes('ready')){sent=true;child.kill('SIGTERM');}});
  const code=await new Promise((resolve,reject)=>{child.once('error',reject);child.once('exit',resolve);});clearTimeout(timeout);
  assert.equal(code,0);assert.match(output,/session-cleaned/);assert.equal(fs.existsSync(path.join(f.store,'.launch-lock.json')),false);
});
test('Linux and Windows templates point to bundled runtime and clear inherited runtime injection variables', () => {
  const linux=channelDesktopTemplates('linux', "/opt/학교's Autograde");
  assert.match(linux['start-channel.sh'], /env -i/);assert.match(linux['start-channel.sh'], /runtime\/bin\/node/);
  assert.match(linux['autograde-channel.desktop'], /Terminal=false/);assert.match(linux['start-channel.sh'], /'\\''/);
  const windows=channelDesktopTemplates('win32',"C:\\Program Files\\학교's Autograde");
  assert.match(windows['start-channel.ps1'], /runtime\\node.exe/);assert.match(windows['start-channel.ps1'], /Remove-Item -LiteralPath/);
  assert.match(windows['install-shortcut.ps1'], /CreateShortcut/);assert.match(windows['install-shortcut.ps1'], /학교''s/);
  assert.throws(()=>channelDesktopTemplates('linux','/unsafe\npath'),/Invalid/);
  assert.throws(()=>channelDesktopTemplates('win32','relative'),/Absolute/);
});
