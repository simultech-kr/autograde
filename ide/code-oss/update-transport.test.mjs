import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import * as https from 'node:https';
import { execFileSync } from 'node:child_process';
import { generateKeyPairSync, randomBytes } from 'node:crypto';
import { gzipSync } from 'node:zlib';
import { fileURLToPath } from 'node:url';
import { authorize, canonical, createRelease, digest, signDocument, verifyPayload } from './release-channel.mjs';
import { downloadRelease, publishUpdate, unpackUpdate, validateFeedUrl, validateUpdateInventory, UPDATE_FORMAT, UPDATE_LIMITS } from './update-transport.mjs';

const pair = () => { const value = generateKeyPairSync('ed25519'); return {private: value.privateKey.export({type: 'pkcs8', format: 'pem'}), public: value.publicKey.export({type: 'spki', format: 'pem'})}; };
async function fixture(t, {serve = false} = {}) {
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'ag-update-transport-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const payload = path.join(root, 'payload'), output = path.join(root, 'published'); fs.mkdirSync(payload); fs.mkdirSync(output);
  fs.mkdirSync(path.join(payload, 'App/Contents/MacOS'), {recursive: true});
  fs.writeFileSync(path.join(payload, 'App/Contents/MacOS/runner'), '#!/bin/sh\nexit 0\n', {mode: 0o755});
  fs.writeFileSync(path.join(payload, 'App/Contents/data.bin'), randomBytes(140000));
  fs.writeFileSync(path.join(payload, 'App/Contents/empty'), '');
  fs.symlinkSync('Contents', path.join(payload, 'App/Current'));
  const profile = {institutionId: 'school', apiUrl: 'https://school.invalid:20000'};
  fs.writeFileSync(path.join(payload, 'profile.json'), JSON.stringify(profile));
  const publisher = pair(), institution = pair();
  const context = {institutionId: 'school', channel: 'pilot', platform: 'darwin', architecture: 'arm64', extensionVersion: '0.5.22', serverRelease: 'server-1',
    institutionProfileSha256: digest(canonical(profile)), publisherKeys: {publisher: publisher.public}, institutionKeys: {school: institution.public}};
  const releaseEnvelope = signDocument(createRelease(payload, {releaseId: 'r2', sequence: 2, ideVersion: '1.143.0', extensionVersion: context.extensionVersion,
    platform: context.platform, architecture: context.architecture, serverReleases: [context.serverRelease], createdAt: new Date().toISOString(),
    entrypoint: 'App/Contents/MacOS/runner', nativeSigningRoot: 'App', institutionProfilePath: 'profile.json', institutionProfileSha256: context.institutionProfileSha256}), publisher.private, 'publisher');
  const approve = (release, overrides = {}) => signDocument({kind: 'autograde.institution-approval.v1', institutionId: 'school', channel: 'pilot',
    releaseSha256: digest(canonical(release.document)), notBefore: '2020-01-01T00:00:00Z', expiresAt: '2099-01-01T00:00:00Z', ...overrides}, institution.private, 'school');
  const approvalEnvelope = approve(releaseEnvelope);
  const options = {payload, output, releaseEnvelope, approvalEnvelope, context, institutionKey: institution.private, keyId: 'school'};
  const publication = await publishUpdate(options);
  const getFeed = () => JSON.parse(fs.readFileSync(path.join(output, 'feed.json')));
  const replaceFeed = document => fs.writeFileSync(path.join(output, 'feed.json'), JSON.stringify(signDocument(document, institution.private, 'school')));
  const f = {root, payload, output, publisher, institution, context, releaseEnvelope, approvalEnvelope, approve, options, publication, getFeed, replaceFeed};
  if (serve) {
    fs.writeFileSync(path.join(root, 'openssl.cnf'), '[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\nbasicConstraints=critical,CA:TRUE\n');
    execFileSync('openssl', ['req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '1', '-keyout', path.join(root, 'tls.key'), '-out', path.join(root, 'tls.crt'), '-config', path.join(root, 'openssl.cnf')], {stdio: 'pipe'});
    const requests = [], server = https.createServer({key: fs.readFileSync(path.join(root, 'tls.key')), cert: fs.readFileSync(path.join(root, 'tls.crt'))}, (req, res) => {
      requests.push(req.url);
      if (f.handler?.(req, res)) return;
      const name = path.basename(new URL(req.url, 'https://localhost').pathname), file = path.join(output, name);
      if (!fs.existsSync(file)) { res.writeHead(404); res.end(); return; }
      res.writeHead(200, {'Content-Length': fs.statSync(file).size}); fs.createReadStream(file).pipe(res);
    });
    await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
    const agent = new https.Agent({ca: fs.readFileSync(path.join(root, 'tls.crt'))});
    t.after(async () => { agent.destroy(); server.closeAllConnections(); await new Promise(resolve => server.close(resolve)); });
    Object.assign(f, {requests, server, httpsAgent: agent, feedUrl: `https://127.0.0.1:${server.address().port}/v1/ide-updates/pilot/darwin/arm64/feed.json`});
    f.download = (overrides = {}) => downloadRelease({feedUrl: f.feedUrl, context, destination: path.join(root, `download-${Math.random()}`), httpsAgent: agent, ...overrides});
  }
  return f;
}

test('publisher and real HTTPS downloader round-trip all files, executable bits, empty files and Electron links', async t => {
  const f = await fixture(t, {serve: true}), events = [];
  const result = await f.download({minSequence: 1, report: event => events.push(event)});
  verifyPayload(result.payload, f.releaseEnvelope.document);
  assert.equal(fs.readlinkSync(path.join(result.payload, 'App/Current')), 'Contents');
  assert.equal(fs.statSync(path.join(result.payload, 'App/Contents/MacOS/runner')).mode & 0o111, 0o111);
  assert.equal(fs.existsSync(path.join(path.dirname(result.payload), 'download.agupdate.gz')), false);
  const owner = path.join(path.dirname(result.payload), '.download-owner.json');
  assert.deepEqual(JSON.parse(fs.readFileSync(owner)), {schema: 1, pid: process.pid});
  assert.equal(fs.statSync(owner).mode & 0o777, 0o600);
  assert.match(result.feedDigest, /^[a-f0-9]{64}$/);
  assert.deepEqual(events.map(event => event.event), ['update-checking', 'update-downloading', 'update-verifying']);
  assert.equal(authorize(result.releaseEnvelope, result.approvalEnvelope, f.context).releaseId, 'r2');
  assert.equal(f.getFeed().document.format, UPDATE_FORMAT);
});

test('same or older sequence and quarantined releases only request the feed', async t => {
  const f = await fixture(t, {serve: true});
  assert.equal(await f.download({minSequence: 2}), null);
  assert.equal(await f.download({minSequence: 3}), null);
  assert.equal(await f.download({excludeReleaseIds: ['r2']}), null);
  assert.equal(f.requests.length, 3); assert.ok(f.requests.every(url => url.endsWith('/feed.json')));
});

test('invalid feed signature and unapproved or expired institution release never download an artifact', async t => {
  const f = await fixture(t, {serve: true}), feed = f.getFeed();
  fs.writeFileSync(path.join(f.output, 'feed.json'), JSON.stringify({...feed, signature: 'A'.repeat(86) + '=='}));
  await assert.rejects(f.download(), /signature/);
  f.replaceFeed({...feed.document, releaseEnvelope: {...f.releaseEnvelope, document: {...f.releaseEnvelope.document, sequence: 99}}});
  await assert.rejects(f.download(), /signature/);
  f.replaceFeed({...feed.document, approvalEnvelope: f.approve(f.releaseEnvelope, {expiresAt: '2020-01-01T00:00:01Z'})});
  await assert.rejects(f.download(), /expired/);
  f.replaceFeed({...feed.document, approvalEnvelope: f.approve(f.releaseEnvelope, {institutionId: 'other'})});
  await assert.rejects(f.download(), /approval/);
  assert.ok(f.requests.every(url => url.endsWith('/feed.json')));
});

test('HTTPS trust, configured origin, redirects and credentials are enforced', async t => {
  const f = await fixture(t, {serve: true});
  await assert.rejects(f.download({httpsAgent: undefined}), /self.signed|certificate/i);
  for (const url of ['http://localhost/feed.json', 'https://user:secret@localhost/feed.json', 'https://localhost/feed.json?token=x', 'https://localhost/feed.json#x', 'https://local\\host/feed.json']) assert.throws(() => validateFeedUrl(url));
  const feed = f.getFeed(); f.replaceFeed({...feed.document, artifact: {...feed.document.artifact, path: 'https://other.invalid/update.gz'}});
  await assert.rejects(f.download(), /artifact/);
  f.handler = (_req, res) => { res.writeHead(302, {Location: 'https://other.invalid/feed.json'}); res.end(); return true; };
  await assert.rejects(f.download(), /redirects/);
});

test('corrupted and oversized downloads fail without retaining partial files', async t => {
  const f = await fixture(t, {serve: true}), destination = path.join(f.root, 'download');
  const bytes = fs.readFileSync(f.publication.artifact); bytes[20] ^= 0x40; fs.writeFileSync(f.publication.artifact, bytes);
  await assert.rejects(f.download({destination}), /digest/); assert.equal(fs.existsSync(destination), false);
  f.handler = (req, res) => { if (req.url.endsWith('/feed.json')) return false; res.writeHead(200, {'Content-Length': UPDATE_LIMITS.artifact + 1}); res.end('too large'); return true; };
  await assert.rejects(f.download({destination}), /size/); assert.equal(fs.existsSync(destination), false);
});

test('cancellation during a slow artifact transfer closes it and removes temporary content', async t => {
  const f = await fixture(t, {serve: true}), controller = new AbortController(), destination = path.join(f.root, 'cancelled');
  f.handler = (req, res) => { if (req.url.endsWith('/feed.json')) return false; res.writeHead(200); res.write('incomplete'); setImmediate(() => controller.abort()); return true; };
  await assert.rejects(f.download({destination, signal: controller.signal}), /abort|cancel/i);
  assert.equal(fs.existsSync(destination), false);
});

test('existing destination files are never removed or overwritten', async t => {
  const f = await fixture(t, {serve: true}), destination = path.join(f.root, 'existing'); fs.mkdirSync(destination);
  fs.writeFileSync(path.join(destination, 'student.txt'), 'keep');
  await assert.rejects(f.download({destination}), /EEXIST/);
  assert.equal(fs.readFileSync(path.join(destination, 'student.txt'), 'utf8'), 'keep');
  fs.symlinkSync(destination, path.join(f.root, 'linked'));
  await assert.rejects(f.download({destination: path.join(f.root, 'linked', 'new')}), /ancestors/);
  assert.equal(fs.existsSync(path.join(destination, 'new')), false);
});

test('signed path validation rejects traversal, ambiguous names, escaped links and links used as parents before extraction', async t => {
  const f = await fixture(t), release = f.releaseEnvelope.document;
  for (const entry of [
    {path: '../escape', type: 'directory'}, {path: '/absolute', type: 'directory'}, {path: 'a\\b', type: 'directory'},
    {path: 'colon:name', type: 'directory'}, {path: 'bad\0name', type: 'directory'},
    {path: 'App/current', type: 'directory'}, {path: 'bad', type: 'link', target: '../outside'},
    {path: 'bad', type: 'link', target: '/tmp/outside'}, {path: 'bad', type: 'link', target: '\\outside'},
    {path: 'bad', type: 'link', target: 'bad\0link'}, {path: 'App/Current/child', type: 'directory'},
    {path: 'not-declared/child', type: 'directory'}, {path: 'hard', type: 'hardlink', target: 'profile.json'},
    {path: 'huge', type: 'file', size: UPDATE_LIMITS.file + 1, sha256: 'a'.repeat(64), executable: false},
  ]) assert.throws(() => validateUpdateInventory({...release, files: [...release.files, entry]}), undefined, JSON.stringify(entry));
});

test('decompression enforces signed length and per-file digest without trusting archive metadata', async t => {
  const f = await fixture(t), release = f.releaseEnvelope.document;
  const bytes = Buffer.concat(release.files.filter(item => item.type === 'file').map(item => fs.readFileSync(path.join(f.payload, item.path))));
  const cases = [bytes.subarray(0, bytes.length - 1), Buffer.concat([bytes, Buffer.from('extra')]), Buffer.from(bytes)]; cases[2][0] ^= 0xff;
  for (const [index, data] of cases.entries()) {
    const artifact = path.join(f.root, `bad-${index}.gz`); fs.writeFileSync(artifact, gzipSync(data));
    await assert.rejects(unpackUpdate(artifact, path.join(f.root, `bad-${index}`), release), /Truncated|signed length|digest/);
  }
  const artifact = path.join(f.root, 'invalid.gz'); fs.writeFileSync(artifact, 'not gzip');
  await assert.rejects(unpackUpdate(artifact, path.join(f.root, 'invalid'), release));
});

test('publication is immutable, replaces only feed atomically, and rejects incompatible or altered inputs', async t => {
  const f = await fixture(t), oldFeed = fs.readFileSync(path.join(f.output, 'feed.json'));
  const again = await publishUpdate(f.options); assert.equal(again.artifact, f.publication.artifact);
  await assert.rejects(publishUpdate({...f.options, context: {...f.context, institutionId: 'other'}}), /approval/);
  assert.deepEqual(fs.readFileSync(path.join(f.output, 'feed.json')), oldFeed);
  fs.writeFileSync(f.publication.artifact, 'corrupt');
  await assert.rejects(publishUpdate(f.options), /immutable/);
  assert.deepEqual(fs.readFileSync(path.join(f.output, 'feed.json')), oldFeed);
  assert.equal(fs.readdirSync(f.output).some(name => name.endsWith('.tmp')), false);
  fs.appendFileSync(path.join(f.payload, 'profile.json'), 'tampered');
  await assert.rejects(publishUpdate(f.options), /digest|inventory/);
});

test('operator publication CLI uses only the institution private key and refuses a public-readable key', async t => {
  const f = await fixture(t), key = path.join(f.root, 'institution.key');
  fs.writeFileSync(key, f.institution.private, {mode: 0o600});
  for (const [name, object] of [['release', f.releaseEnvelope], ['approval', f.approvalEnvelope], ['context', f.context]]) fs.writeFileSync(path.join(f.root, `${name}.json`), JSON.stringify(object));
  const args = [fileURLToPath(new URL('./update-transport.mjs', import.meta.url)), 'publish', '--payload', f.payload,
    '--release', path.join(f.root, 'release.json'), '--approval', path.join(f.root, 'approval.json'), '--context', path.join(f.root, 'context.json'),
    '--key', key, '--key-id', 'school', '--output', f.output];
  const result = JSON.parse(execFileSync(process.execPath, args, {encoding: 'utf8'}));
  assert.equal(result.releaseId, 'r2'); assert.equal(f.getFeed().keyId, 'school');
  fs.chmodSync(key, 0o644);
  assert.throws(() => execFileSync(process.execPath, args, {encoding: 'utf8', stdio: 'pipe'}), /private/);
});
