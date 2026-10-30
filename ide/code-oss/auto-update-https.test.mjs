import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import * as https from 'node:https';
import { generateKeyPairSync } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { canonical, digest, createRelease, signDocument, stageRelease, activateRelease } from './release-channel.mjs';
import { downloadRelease, publishUpdate } from './update-transport.mjs';
import { checkForUpdate, getUpdateState } from './auto-update.mjs';
import { runChannel } from './channel-launcher.mjs';

const keys = () => { const pair = generateKeyPairSync('ed25519'); return {key: pair.privateKey.export({format: 'pem', type: 'pkcs8'}), public: pair.publicKey.export({format: 'pem', type: 'spki'})}; };
const quote = value => `'${value.replaceAll("'", "'\\''")}'`;
const noNetwork = async () => {};
async function fixture(t) {
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'ag-update-https-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const store = path.join(root, 'store'), sessions = path.join(root, 'seat'), output = path.join(root, 'published'), trace = path.join(root, 'ran.txt'), ready = path.join(root, 'download-finished');
  fs.mkdirSync(sessions); fs.mkdirSync(output);
  fs.writeFileSync(path.join(root, 'openssl.cnf'), '[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n');
  execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-keyout', path.join(root, 'tls.key'), '-out', path.join(root, 'tls.crt'), '-config', path.join(root, 'openssl.cnf')], {stdio: 'pipe'});
  const requests = [], server = https.createServer({key: fs.readFileSync(path.join(root, 'tls.key')), cert: fs.readFileSync(path.join(root, 'tls.crt'))}, (req, res) => {
    requests.push(req.url);
    const file = path.join(output, path.basename(new URL(req.url, 'https://localhost').pathname));
    if (!fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
    res.writeHead(200, {'Content-Length': fs.statSync(file).size}); fs.createReadStream(file).pipe(res);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const httpsAgent = new https.Agent({ca: fs.readFileSync(path.join(root, 'tls.crt'))});
  t.after(async () => { httpsAgent.destroy(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
  const publisher = keys(), institution = keys(), profile = {institutionId: 'school'};
  const context = {institutionId: 'school', channel: 'pilot', platform: process.platform, architecture: process.arch, serverRelease: 'server-1',
    extensionVersion: '0.5.22', extensionVersionPolicy: 'institution-approved', institutionProfileSha256: digest(canonical(profile)),
    publisherKeys: {vendor: publisher.public}, institutionKeys: {school: institution.public}, sessionRoots: [sessions],
    updates: {feedUrl: `https://127.0.0.1:${server.address().port}/v1/ide-updates/pilot/${process.platform}/${process.arch}/feed.json`, checkIntervalSeconds: 60}};
  const contextFile = path.join(root, 'context.json'), configFile = path.join(root, 'channel.json');
  fs.writeFileSync(contextFile, JSON.stringify(context), {mode: 0o600});
  fs.writeFileSync(configFile, JSON.stringify({schema: 1, store, context: contextFile}), {mode: 0o600});
  const release = (id, sequence, {wait = false, broken = false} = {}) => {
    const payload = path.join(root, id); fs.mkdirSync(payload);
    fs.writeFileSync(path.join(payload, 'runner'), '#!/bin/sh\nexec /bin/sh "$@"\n', {mode: 0o755});
    fs.writeFileSync(path.join(payload, 'profile.json'), JSON.stringify(profile));
    const script = process.platform === 'linux' ? 'linux-launcher.mjs' : 'mac-launcher.mjs';
    // Small real supervised process fixture. The actual Mac supervisor privacy
    // lifecycle is independently exercised by auto-update.test.mjs.
    const current = path.join(sessions, 'current');
    const body = broken ? 'exit 7\n' : `set -eu
test ! -e ${quote(current)}
mkdir ${quote(current)}
printf student-code > ${quote(path.join(current, 'main.cpp'))}
printf synthetic-login > ${quote(path.join(current, 'login.json'))}
printf '{"event":"ide-started","pid":%s}\\n' "$$"
printf '%s\\n' ${quote(id)} >> ${quote(trace)}
${wait ? `n=0; while [ ! -e ${quote(ready)} ]; do n=$((n + 1)); if [ "$n" -gt 500 ]; then exit 8; fi; sleep 0.01; done` : ''}
rm ${quote(path.join(current, 'main.cpp'))} ${quote(path.join(current, 'login.json'))}
rmdir ${quote(current)}
printf '%s\\n' '{"event":"session-cleaned"}'
`;
    fs.writeFileSync(path.join(payload, script), body);
    const document = createRelease(payload, {releaseId: id, sequence, platform: process.platform, architecture: process.arch, ideVersion: '1.143.0', extensionVersion: `0.5.${21 + sequence}`,
      serverReleases: ['server-1'], createdAt: new Date().toISOString(), entrypoint: 'runner', entrypointScript: script, nativeSigningRoot: 'runner', institutionProfilePath: 'profile.json', institutionProfileSha256: context.institutionProfileSha256});
    const releaseEnvelope = signDocument(document, publisher.key, 'vendor'), approvalEnvelope = signDocument({kind: 'autograde.institution-approval.v1', institutionId: 'school',
      channel: 'pilot', releaseSha256: digest(canonical(document)), notBefore: '2020-01-01T00:00:00Z', expiresAt: '2099-01-01T00:00:00Z'}, institution.key, 'school');
    return {payload, releaseEnvelope, approvalEnvelope};
  };
  const seed = item => { stageRelease({store, context, ...item}); activateRelease({store, context, releaseId: item.releaseEnvelope.document.releaseId}); };
  const publish = item => publishUpdate({...item, context, output, institutionKey: institution.key, keyId: 'school'});
  const check = options => checkForUpdate({store, context, ...options, download: args => downloadRelease({...args, httpsAgent})});
  return {root, store, sessions, trace, ready, context, configFile, requests, release, seed, publish, check};
}

test('real HTTPS approved download during a running release applies next launch after student session cleanup', async t => {
  if (process.platform === 'win32') { t.skip('POSIX process integration'); return; }
  const f = await fixture(t); f.seed(f.release('one', 1, {wait: true})); await f.publish(f.release('two', 2));
  const events = []; let markStarted; const started = new Promise(resolve => { markStarted = resolve; });
  const report = event => { events.push(event); if (event.event === 'ide-started') markStarted(); };
  const watch = async options => {
    await Promise.race([started, new Promise((_, reject) => options.signal.addEventListener('abort', () => reject(new Error('IDE exited before fixture started')), {once: true}))]);
    assert.ok(fs.existsSync(path.join(f.sessions, 'current/login.json')));
    await f.check(options);
    assert.equal(getUpdateState(f.store).active.releaseId, 'one'); assert.equal(getUpdateState(f.store).pending.releaseId, 'two');
    fs.writeFileSync(f.ready, 'ready');
  };
  assert.equal(await runChannel(f.configFile, report, {watch}), 0);
  assert.equal(getUpdateState(f.store).active.releaseId, 'one'); assert.equal(fs.existsSync(path.join(f.sessions, 'current')), false);
  assert.equal(await runChannel(f.configFile, report, {watch: noNetwork}), 0);
  assert.equal(getUpdateState(f.store).active.releaseId, 'two'); assert.equal(getUpdateState(f.store).trial, undefined);
  assert.equal(fs.existsSync(path.join(f.sessions, 'current')), false);
  assert.equal(fs.readFileSync(f.trace, 'utf8'), 'one\ntwo\n');
  assert.deepEqual(fs.readdirSync(f.store).filter(name => name.startsWith('.download-')), []);
  assert.ok(f.requests.some(url => url.endsWith('.agupdate.gz'))); assert.ok(events.some(event => event.phase === 'ready-next-launch')); assert.ok(events.some(event => event.phase === 'applied'));
});

test('corrupt HTTPS artifact preserves the running version; verified bad launcher rolls back to the prior executable', async t => {
  if (process.platform === 'win32') { t.skip('POSIX process integration'); return; }
  const f = await fixture(t); f.seed(f.release('one', 1)); const next = f.release('two', 2), publication = await f.publish(next);
  const bytes = fs.readFileSync(publication.artifact); bytes[25] ^= 0xff; fs.writeFileSync(publication.artifact, bytes);
  const events = []; await f.check({report: event => events.push(event)});
  assert.equal(getUpdateState(f.store).pending, undefined); assert.equal(getUpdateState(f.store).active.releaseId, 'one');
  assert.ok(events.some(event => event.phase === 'kept-current'));
  assert.equal(await runChannel(f.configFile, event => events.push(event), {watch: noNetwork}), 0);
  assert.equal(fs.existsSync(path.join(f.sessions, 'current')), false);
  await f.publish(f.release('bad', 3, {broken: true})); await f.check({report: event => events.push(event)});
  assert.equal(getUpdateState(f.store).pending.releaseId, 'bad');
  assert.equal(await runChannel(f.configFile, event => events.push(event), {watch: noNetwork}), 0);
  const state = getUpdateState(f.store); assert.equal(state.active.releaseId, 'one'); assert.ok(state.failedReleaseIds.includes('bad')); assert.equal(state.trial, undefined);
  assert.ok(events.some(event => event.phase === 'rolled-back')); assert.equal(fs.readFileSync(f.trace, 'utf8'), 'one\none\n');
  assert.equal(fs.existsSync(path.join(f.sessions, 'current')), false); assert.deepEqual(fs.readdirSync(f.store).filter(name => name.startsWith('.download-')), []);
  const before = f.requests.length; await f.check({}); assert.equal(f.requests.length, before + 1); // quarantined feed does not redownload bytes
});
