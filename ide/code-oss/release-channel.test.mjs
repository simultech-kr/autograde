import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { generateKeyPairSync } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { authorize, canonical, createRelease, digest, signDocument, verifyDocument, verifyPayload, stageRelease, activateRelease, launchActive, nativeSignatureGate } from './release-channel.mjs';
import { applyInstitutionProfile, distributionInventory, institutionProfile, prepareDistribution } from './distribution-inventory.mjs';
import { parseOptions } from './package-macos.mjs';
import { parseWindowsOptions } from './package-windows.mjs';
import { parseLinuxOptions } from './package-linux.mjs';
import { parseWslOptions } from './package-wsl.mjs';

const pair = () => { const keys = generateKeyPairSync('ed25519'); return {private: keys.privateKey.export({format: 'pem', type: 'pkcs8'}), public: keys.publicKey.export({format: 'pem', type: 'spki'})}; };
function fixture(t) {
  const root = fs.mkdtempSync(path.join(fs.realpathSync(os.tmpdir()), 'ag-release-'));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const publisher = pair(), institution = pair(), payload = path.join(root, 'payload'), store = path.join(root, 'store'), sessions = path.join(root, 'seat');
  fs.mkdirSync(payload); fs.mkdirSync(sessions);
  fs.writeFileSync(path.join(payload, 'run.sh'), '#!/bin/sh\nexit 0\n', {mode: 0o755});
  fs.writeFileSync(path.join(payload, 'LICENSE'), 'Fixture license notice');
  const profile = {schema: 'autograde.ide-institution.v1', institutionId: 'school', apiUrl: 'https://school.invalid:20000', portalUrl: 'https://school.invalid:20010'};
  fs.writeFileSync(path.join(payload, 'institution-profile.json'), JSON.stringify(profile));
  const context = {publisherKeys: {publisher: publisher.public}, institutionKeys: {school: institution.public}, institutionId: 'school', channel: 'pilot',
    platform: 'linux', architecture: 'x64', extensionVersion: '0.5.20', serverRelease: 'server-1', institutionProfileSha256: digest(canonical(profile)), sessionRoots: [sessions]};
  const envelope = (id, sequence) => signDocument(createRelease(payload, {releaseId: id, sequence, platform: 'linux', architecture: 'x64', ideVersion: '1.143.0', extensionVersion: '0.5.20',
    serverReleases: ['server-1'], createdAt: '2026-10-29T00:00:00Z', institutionProfileSha256: context.institutionProfileSha256, institutionProfilePath: 'institution-profile.json', entrypoint: 'run.sh', nativeSigningRoot: 'run.sh'}), publisher.private, 'publisher');
  const approve = (release, overrides = {}) => signDocument({kind: 'autograde.institution-approval.v1', institutionId: 'school', releaseSha256: digest(canonical(release.document)),
    notBefore: '2020-01-01T00:00:00Z', expiresAt: '2099-01-01T00:00:00Z', channel: 'pilot', ...overrides}, institution.private, 'school');
  const stage = (id, sequence) => { const release = envelope(id, sequence), approval = approve(release); stageRelease({store, payload, releaseEnvelope: release, approvalEnvelope: approval, context}); return {release, approval}; };
  return {root, payload, store, sessions, publisher, institution, context, envelope, approve, stage};
}
test('real injected Ed25519 keys authenticate canonical release and distinct institution approval', t => {
  const f = fixture(t), release = f.envelope('r1', 1), approval = f.approve(release);
  assert.equal(authorize(release, approval, f.context).releaseId, 'r1');
  const reordered = {...release, document: Object.fromEntries(Object.entries(release.document).reverse())};
  assert.equal(verifyDocument(reordered, f.context.publisherKeys).releaseId, 'r1');
  assert.throws(() => authorize({...release, document: {...release.document, sequence: 99}}, approval, f.context), /signature/);
  assert.throws(() => authorize(release, {...approval, keyId: 'publisher'}, f.context), /key/);
  assert.throws(() => authorize(release, f.approve(release, {institutionId: 'other'}), f.context), /approval/);
  assert.throws(() => authorize(release, f.approve(release, {expiresAt: '2020-01-01T00:00:00Z'}), f.context), /expired/);
  assert.throws(() => authorize(release, f.approve(release, {notBefore: '2099-01-01T00:00:00Z'}), f.context), /approval/);
});
test('wrong platform, architecture, server, extension and institution profile are blocked', t => {
  const f = fixture(t), release = f.envelope('r1', 1), approval = f.approve(release);
  for (const patch of [{platform: 'win32'}, {architecture: 'arm64'}, {serverRelease: 'server-2'}, {extensionVersion: '0.5.19'}, {institutionProfileSha256: 'b'.repeat(64)}]) {
    assert.throws(() => authorize(release, approval, {...f.context, ...patch}), /incompatible/);
  }
  assert.throws(() => authorize(release, approval, {...f.context, channel: 'production'}), /approval/);
  assert.throws(() => createRelease(f.payload, {...release.document, institutionProfileSha256: '0'.repeat(64)}), /profile digest/);
});
test('all files, executable bits, extra files and internal links are verified', t => {
  const f = fixture(t); fs.symlinkSync('LICENSE', path.join(f.payload, 'notice-link'));
  const release = f.envelope('r1', 1); verifyPayload(f.payload, release.document);
  fs.writeFileSync(path.join(f.payload, 'extra'), 'extra'); assert.throws(() => verifyPayload(f.payload, release.document), /inventory/); fs.unlinkSync(path.join(f.payload, 'extra'));
  fs.chmodSync(path.join(f.payload, 'run.sh'), 0o644); assert.throws(() => verifyPayload(f.payload, release.document), /mode/); fs.chmodSync(path.join(f.payload, 'run.sh'), 0o755);
  fs.writeFileSync(path.join(f.payload, 'LICENSE'), 'tampered'); assert.throws(() => verifyPayload(f.payload, release.document), /digest/);
  fs.unlinkSync(path.join(f.payload, 'notice-link')); fs.symlinkSync('../outside', path.join(f.payload, 'notice-link'));
  assert.throws(() => f.envelope('r2', 2), /link/);
});
test('staging is immutable and allowed during teaching; activation defers until cleanup completes', t => {
  const f = fixture(t); fs.mkdirSync(path.join(f.sessions, 'current'));
  f.stage('r1', 1); assert.throws(() => f.stage('r1', 1), /immutable/);
  assert.throws(() => activateRelease({store: f.store, releaseId: 'r1', context: f.context}), /deferred/);
  assert.equal(fs.existsSync(path.join(f.store, 'active.json')), false);
  fs.rmdirSync(path.join(f.sessions, 'current'));
  assert.equal(activateRelease({store: f.store, releaseId: 'r1', context: f.context}).status, 'ready-for-next-launch');
  assert.throws(() => activateRelease({store: f.store, releaseId: 'r1', context: f.context}), /sequence/);
});
test('failed post-switch verification restores previous selection; explicit rollback re-verifies previous payload', t => {
  const f = fixture(t); f.stage('r1', 1); f.stage('r2', 2);
  activateRelease({store: f.store, releaseId: 'r1', context: f.context});
  const active = () => JSON.parse(fs.readFileSync(path.join(f.store, 'active.json')));
  assert.throws(() => activateRelease({store: f.store, releaseId: 'r2', context: f.context, afterSwitch: () => {throw new Error('synthetic post-switch failure');}}), /previous selection restored/);
  assert.equal(active().releaseId, 'r1');
  activateRelease({store: f.store, releaseId: 'r2', context: f.context});
  assert.throws(() => activateRelease({store: f.store, releaseId: 'r1', context: f.context}), /sequence/);
  assert.equal(activateRelease({store: f.store, releaseId: 'r1', context: f.context, rollback: true}).status, 'rolled-back');
  fs.writeFileSync(path.join(f.store, 'releases/r2/payload/LICENSE'), 'corrupt rollback');
  assert.throws(() => activateRelease({store: f.store, releaseId: 'r2', context: f.context, rollback: true}), /digest/);
  assert.equal(active().releaseId, 'r1');
});
test('missing real OS signing identity blocks production activation without changing current release', t => {
  const f = fixture(t), platform = process.platform;
  if (!['darwin', 'win32'].includes(platform)) { t.skip('Apple/Windows signing gate is exercised on the matching OS'); return; }
  assert.throws(() => nativeSignatureGate(f.payload, {...f.envelope('r1', 1).document, platform}, {...f.context, channel: 'production'}), /requires/);
});
test('production-sized signed metadata can be read back after stage and activated', t => {
  const f = fixture(t);
  // The actual 71k-entry Mac inventory exceeds the old 16 MiB JSON limit.
  // Padding signed metadata exercises that read boundary without 71k test files.
  const document = {...f.envelope('large-mac-manifest', 1).document, releaseNotes: 'x'.repeat(17 * 1024 * 1024)};
  const release = signDocument(document, f.publisher.private, 'publisher'), approval = f.approve(release);
  stageRelease({store: f.store, payload: f.payload, releaseEnvelope: release, approvalEnvelope: approval, context: f.context});
  assert.ok(fs.statSync(path.join(f.store, 'releases/large-mac-manifest/release.json')).size > 16 * 1024 * 1024);
  assert.equal(activateRelease({store: f.store, releaseId: 'large-mac-manifest', context: f.context}).status, 'ready-for-next-launch');
});
test('stale update and launch locks fail closed; managed launch runs the actual signed entrypoint', async t => {
  const f = fixture(t); f.stage('r1', 1);
  fs.writeFileSync(path.join(f.store, '.launch-lock.json'), '{}');
  assert.throws(() => activateRelease({store: f.store, releaseId: 'r1', context: f.context}), /active/);
  fs.unlinkSync(path.join(f.store, '.launch-lock.json'));
  activateRelease({store: f.store, releaseId: 'r1', context: f.context});
  if (process.platform !== 'win32') assert.equal((await launchActive({store: f.store, context: f.context})).code, 0);
  assert.equal(fs.existsSync(path.join(f.store, '.launch-lock.json')), false);
  fs.writeFileSync(path.join(f.store, '.update-lock.json'), '{}');
  assert.throws(() => activateRelease({store: f.store, releaseId: 'r1', context: f.context}), /EEXIST|Unsafe interrupted launcher lock/);
});
test('profile injection changes packaged defaults without secrets and inventory records license evidence', t => {
  const f = fixture(t), runtime = path.join(f.payload, 'autograde'); fs.mkdirSync(runtime);
  fs.writeFileSync(path.join(runtime, 'runtime-settings.json'), JSON.stringify({'autograde.serviceBaseUrl': 'https://old.invalid', 'update.mode': 'none'}));
  fs.writeFileSync(path.join(f.payload, 'package.json'), JSON.stringify({name: 'fixture-editor', version: '1.0.0', license: 'MIT'}));
  const profile = {schema: 'autograde.ide-institution.v1', institutionId: 'school', apiUrl: 'https://school.invalid:20000', portalUrl: 'https://school.invalid:20010'};
  const profileFile = path.join(f.root, 'institution.json'); fs.writeFileSync(profileFile, JSON.stringify(profile));
  const result = prepareDistribution(f.payload, runtime, {ideVersion: '1.143.0', extensionVersion: '0.5.20', nodeVersion: '24.18.0', upstreamCommit: 'a'.repeat(40)}, profileFile);
  assert.equal(result.institutionProfileSha256, digest(canonical(profile)));
  assert.equal(JSON.parse(fs.readFileSync(path.join(runtime, 'runtime-settings.json')))['autograde.serviceBaseUrl'], profile.apiUrl);
  const runtimeSettings = JSON.parse(fs.readFileSync(path.join(runtime, 'runtime-settings.json')));
  assert.equal(runtimeSettings['autograde.discovery.enabled'], true);
  assert.deepEqual(runtimeSettings['autograde.discovery.trustedServers'], [{institutionId: profile.institutionId,
    displayName: profile.institutionId, apiUrl: profile.apiUrl, portalUrl: profile.portalUrl}]);
  const sbom = JSON.parse(fs.readFileSync(path.join(runtime, 'sbom.cdx.json')));
  assert.equal(sbom.bomFormat, 'CycloneDX'); assert.ok(sbom.components.some(item => item.name === 'fixture-editor' && item.licenses[0].license.name === 'MIT'));
  assert.ok(sbom.properties.some(item => item.name === 'autograde:notice:LICENSE'));
  assert.equal(result.licenseReviewComplete, false);
  assert.throws(() => institutionProfile({...profile, reportToken: 'secret'}), /public/);
  assert.throws(() => institutionProfile({...profile, apiUrl: 'http://school.invalid'}), /HTTPS/);
  assert.throws(() => institutionProfile({...profile, apiUrl: 'https://user:secret@school.invalid'}), /credentials/);
  assert.throws(() => applyInstitutionProfile(runtime, profileFile), /EEXIST/);
  assert.equal(distributionInventory(f.payload, {ideVersion:'1.0.0',extensionVersion:'1.0.0',nodeVersion:'24.18.0',upstreamCommit:'a'.repeat(40)}).specVersion, '1.6');
});
test('all target package builders accept the same public institution profile option', () => {
  for (const parse of [parseOptions, parseWindowsOptions, parseLinuxOptions, parseWslOptions]) assert.equal(parse(['--institution-profile', '/operator/profile.json']).institutionProfile, '/operator/profile.json');
});
test('operator CLI signs with an injected private key and creates a verifiable distinct approval', t => {
  const f = fixture(t), tool = fileURLToPath(new URL('./release-channel.mjs', import.meta.url));
  const document = f.envelope('cli-1', 1).document;
  fs.writeFileSync(path.join(f.root, 'document.json'), JSON.stringify(document));
  fs.writeFileSync(path.join(f.root, 'publisher.pem'), f.publisher.private, {mode: 0o600});
  fs.writeFileSync(path.join(f.root, 'school.pem'), f.institution.private, {mode: 0o600});
  fs.writeFileSync(path.join(f.root, 'context.json'), JSON.stringify(f.context));
  const run = args => JSON.parse(execFileSync(process.execPath, [tool, ...args], {encoding: 'utf8', cwd: f.root}));
  const release = run(['sign', '--document', 'document.json', '--key', 'publisher.pem', '--key-id', 'publisher']);
  fs.writeFileSync(path.join(f.root, 'release.json'), JSON.stringify(release));
  const unsignedApproval = run(['approval', '--release', 'release.json', '--context', 'context.json', '--not-before', '2020-01-01T00:00:00Z', '--expires', '2099-01-01T00:00:00Z']);
  fs.writeFileSync(path.join(f.root, 'approval.unsigned.json'), JSON.stringify(unsignedApproval));
  const approval = run(['sign', '--document', 'approval.unsigned.json', '--key', 'school.pem', '--key-id', 'school']);
  assert.equal(authorize(release, approval, f.context).releaseId, 'cli-1');
  fs.writeFileSync(path.join(f.root, 'approval.json'), JSON.stringify(approval));
  assert.equal(run(['verify', '--release', 'release.json', '--approval', 'approval.json', '--context', 'context.json']).releaseId, 'cli-1');
});
