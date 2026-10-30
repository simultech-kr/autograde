#!/usr/bin/env node
// Operator-side release transport. Never replaces a running application in place.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { createHash, createPrivateKey, createPublicKey, sign, verify, randomUUID } from 'node:crypto';
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const HASH = /^[a-f0-9]{64}$/;
const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;
const fail = message => { throw new Error(message); };
export const digest = bytes => createHash('sha256').update(bytes).digest('hex');
function fileDigest(file) {
  const hash = createHash('sha256'), buffer = Buffer.allocUnsafe(1024 * 1024), fd = fs.openSync(file, 'r');
  try { let count; while ((count = fs.readSync(fd, buffer, 0, buffer.length, null))) hash.update(buffer.subarray(0, count)); }
  finally { fs.closeSync(fd); }
  return hash.digest('hex');
}
export function canonical(value) {
  if (value === null || ['boolean', 'string'].includes(typeof value)) return JSON.stringify(value);
  if (typeof value === 'number' && Number.isSafeInteger(value)) return JSON.stringify(value);
  if (Array.isArray(value)) return `[${value.map(canonical).join(',')}]`;
  if (value && Object.getPrototypeOf(value) === Object.prototype) return `{${Object.keys(value).sort().map(key => `${JSON.stringify(key)}:${canonical(value[key])}`).join(',')}}`;
  return fail('Manifest contains an unsupported value');
}
const within = (root, candidate) => { const relative = path.relative(root, candidate); return relative === '' || !path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`); };
function relativeFile(value) {
  if (typeof value !== 'string' || !value || value.length > 1024 || value.includes('\\') || value.includes('\0') || value.startsWith('/')
    || value.split('/').some(part => !part || part === '.' || part === '..' || part.includes(':'))) fail('Invalid relative release path');
  return value;
}
function realDirectory(root) {
  const info = fs.lstatSync(root);
  if (!info.isDirectory() || info.isSymbolicLink()) fail('A real release directory is required');
  let current = path.resolve(root);
  for (;;) { const item = fs.lstatSync(current); if (!item.isDirectory() || item.isSymbolicLink()) fail('Linked release ancestor refused'); const parent = path.dirname(current); if (parent === current) break; current = parent; }
}
// A packaged Mac app has >70k inventory entries. Its signed compact feed is
// bounded separately at 32 MiB; the operator/staged pretty JSON can be larger.
function regular(file, limit = 64 * 1024 * 1024) {
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > limit) fail('Expected a bounded regular file');
  return fs.readFileSync(file);
}
function json(file) { return JSON.parse(regular(file).toString('utf8')); }
function exclusiveJson(file, value) { fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`, {flag: 'wx', mode: 0o600}); }
function atomicJson(file, value) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  try { const fd = fs.openSync(temporary, 'wx', 0o600); try { fs.writeFileSync(fd, `${JSON.stringify(value)}\n`); fs.fsyncSync(fd); } finally { fs.closeSync(fd); } fs.renameSync(temporary, file); syncDirectory(path.dirname(file)); }
  finally { fs.rmSync(temporary, {force: true}); }
}
export function syncDirectory(directory) {
  if (process.platform === 'win32') return;
  const fd = fs.openSync(directory, 'r'); try { fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
}

/** Includes executable bits and internal symlink targets (Electron frameworks need them). */
export function inventory(root) {
  root = path.resolve(root); realDirectory(root);
  const entries = [];
  function walk(directory) {
    for (const name of fs.readdirSync(directory).sort()) {
      const full = path.join(directory, name), info = fs.lstatSync(full), relative = relativeFile(path.relative(root, full).split(path.sep).join('/'));
      if (info.isSymbolicLink()) {
        const target = fs.readlinkSync(full);
        if (path.isAbsolute(target) || target.includes('\\') || !within(root, path.resolve(directory, target)) || !within(root, fs.realpathSync(full))) fail('External or dangling release link refused');
        entries.push({path: relative, type: 'link', target});
      } else if (info.isDirectory()) { entries.push({path: relative, type: 'directory'}); walk(full); }
      else if (info.isFile() && info.nlink === 1 && info.size <= 4 * 1024 ** 3) {
        entries.push({path: relative, type: 'file', size: info.size, executable: Boolean(info.mode & 0o111), sha256: fileDigest(full)});
      } else fail('Unsupported release file');
      if (entries.length > 150000) fail('Release file count exceeds limit');
    }
  }
  walk(root); return entries;
}
function validateRelease(release) {
  if (release?.kind !== 'autograde.ide-release.v1' || !ID.test(release.releaseId) || !Number.isSafeInteger(release.sequence) || release.sequence < 1
    || !['darwin', 'win32', 'linux'].includes(release.platform) || !['arm64', 'x64'].includes(release.architecture)
    || !/^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$/.test(release.ideVersion) || !/^\d+\.\d+\.\d+$/.test(release.extensionVersion)
    || !Array.isArray(release.serverReleases) || !release.serverReleases.length || release.serverReleases.some(value => !ID.test(value))
    || !Array.isArray(release.files) || !release.files.length || release.files.length > 150000
    || !HASH.test(release.institutionProfileSha256 ?? '') || !Number.isFinite(Date.parse(release.createdAt))) fail('Invalid release manifest');
  relativeFile(release.entrypoint); relativeFile(release.nativeSigningRoot); relativeFile(release.institutionProfilePath);
  if (!release.files.some(file => file.type === 'file' && file.path === release.entrypoint)) fail('Entrypoint must be a signed regular file');
  if (release.entrypointScript !== undefined) {
    relativeFile(release.entrypointScript);
    if (!release.files.some(file => file.type === 'file' && file.path === release.entrypointScript)) fail('Launch script must be a signed regular file');
    if (['channel-launcher.mjs', 'release-channel.mjs'].includes(path.posix.basename(release.entrypointScript))) fail('Recursive channel entrypoint refused');
  }
  if (!release.files.some(file => file.type === 'file' && file.path === release.institutionProfilePath)) fail('Institution profile must be included in the signed payload');
  const seen = new Set();
  for (const file of release.files) {
    relativeFile(file.path); if (seen.has(file.path)) fail('Duplicate release path'); seen.add(file.path);
    if (file.type === 'file' && (!HASH.test(file.sha256) || typeof file.executable !== 'boolean' || !Number.isSafeInteger(file.size) || file.size < 0)
      || !['file', 'link', 'directory'].includes(file.type) || file.type === 'link' && typeof file.target !== 'string') fail('Invalid release inventory');
  }
  return release;
}
export function createRelease(payload, metadata) {
  const release = validateRelease({...metadata, kind: 'autograde.ide-release.v1', files: inventory(payload)});
  verifyProfile(payload, release); return release;
}
export function signDocument(document, privatePem, keyId) {
  if (!ID.test(keyId)) fail('Invalid signing key ID');
  const key = createPrivateKey(privatePem); if (key.asymmetricKeyType !== 'ed25519') fail('Ed25519 signing key required');
  return {document, keyId, algorithm: 'Ed25519', signature: sign(null, Buffer.from(canonical(document)), key).toString('base64')};
}
export function verifyDocument(envelope, trustedKeys) {
  if (!envelope || !ID.test(envelope.keyId ?? '') || envelope.algorithm !== 'Ed25519' || typeof envelope.signature !== 'string'
    || !/^[A-Za-z0-9+/]{86}==$/.test(envelope.signature) || !Object.hasOwn(trustedKeys, envelope.keyId)) fail('Unknown or invalid signing key');
  const key = createPublicKey(trustedKeys[envelope.keyId]);
  if (key.asymmetricKeyType !== 'ed25519' || !verify(null, Buffer.from(canonical(envelope.document)), key, Buffer.from(envelope.signature, 'base64'))) fail('Release signature verification failed');
  return envelope.document;
}
export function authorize(releaseEnvelope, approvalEnvelope, context, now = Date.now()) {
  const release = validateRelease(verifyDocument(releaseEnvelope, context.publisherKeys));
  const approval = verifyDocument(approvalEnvelope, context.institutionKeys);
  if (approval?.kind !== 'autograde.institution-approval.v1' || approval.institutionId !== context.institutionId
    || approval.releaseSha256 !== digest(canonical(release)) || !['pilot', 'production'].includes(approval.channel)
    || approval.channel !== context.channel || !Number.isFinite(Date.parse(approval.expiresAt)) || Date.parse(approval.expiresAt) <= now
    || !Number.isFinite(Date.parse(approval.notBefore)) || Date.parse(approval.notBefore) > now) fail('Institution approval missing, expired or mismatched');
  if (release.platform !== context.platform || release.architecture !== context.architecture
    || !release.serverReleases.includes(context.serverRelease) || !extensionCompatible(release, context)
    || release.institutionProfileSha256 !== context.institutionProfileSha256) fail('Release is incompatible with institution, OS, server or extension');
  return release;
}
function extensionCompatible(release, context) {
  // A version upgrade still requires both signatures, the exact approved profile,
  // platform and server compatibility. Legacy contexts retain their exact pin.
  return context.extensionVersionPolicy === 'institution-approved'
    || context.extensionVersionPolicy === undefined && release.extensionVersion === context.extensionVersion;
}
export function verifyPayload(payload, release) {
  if (canonical(inventory(payload)) !== canonical(release.files)) fail('Release file digest, mode or inventory mismatch');
  verifyProfile(payload, release);
}
function verifyProfile(payload, release) {
  if (digest(canonical(json(path.join(payload, relativeFile(release.institutionProfilePath))))) !== release.institutionProfileSha256) fail('Packaged institution profile digest differs from approved profile');
}
export function nativeSignatureGate(payload, release, context) {
  if (context.channel !== 'production') return {status: 'pilot-unverified', deploymentReady: false};
  if (release.platform !== process.platform) fail('Native signature gate must run on the target operating system');
  const target = path.join(payload, relativeFile(release.nativeSigningRoot));
  if (release.platform === 'darwin') {
    if (!/^[A-Z0-9]{10}$/.test(context.nativeSigner ?? '')) fail('Production requires the institution-approved Apple Team ID');
    execFileSync('/usr/bin/codesign', ['--verify', '--deep', '--strict', target], {stdio: 'pipe'});
    execFileSync('/usr/sbin/spctl', ['--assess', '--type', 'execute', '--verbose=2', target], {stdio: 'pipe'});
    const details = spawnDetails('/usr/bin/codesign', ['-d', '--verbose=4', target]);
    if (!details.split(/\r?\n/).includes(`TeamIdentifier=${context.nativeSigner}`)) fail('Apple signer identity mismatch');
  } else if (release.platform === 'win32') {
    if (!/^[A-Fa-f0-9]{40}$/.test(context.nativeSigner ?? '')) fail('Production requires the approved Authenticode certificate thumbprint');
    // Base64 literals cannot inject PowerShell syntax, including quote characters in install paths.
    const encodedTarget = Buffer.from(target).toString('base64');
    const script = `$p=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('${encodedTarget}')); $s=Get-AuthenticodeSignature -LiteralPath $p; if($s.Status -ne 'Valid' -or $s.SignerCertificate.Thumbprint -ne '${context.nativeSigner}') { exit 1 }`;
    execFileSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-EncodedCommand', Buffer.from(script, 'utf16le').toString('base64')], {stdio: 'pipe'});
  }
  return {status: release.platform === 'linux' ? 'release-signature-verified' : 'native-signature-verified', deploymentReady: false};
}
function spawnDetails(command, args) {
  // codesign prints metadata on stderr even on success.
  const result = spawnSync(command, args, {encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']});
  if (result.error || result.status !== 0) fail('Cannot read native signing identity');
  return `${result.stdout}\n${result.stderr}`;
}
function managedStore(store) {
  store = path.resolve(store);
  if (!fs.existsSync(store)) fs.mkdirSync(store, {mode: 0o700});
  realDirectory(store); const stat = fs.statSync(store);
  if (process.platform !== 'win32' && (stat.uid !== process.getuid() || stat.mode & 0o077)) fail('Release store must be operator-owned and private (0700)');
  const releases = path.join(store, 'releases'); if (!fs.existsSync(releases)) fs.mkdirSync(releases, {mode: 0o700}); realDirectory(releases);
  return store;
}
function locked(store, operation) {
  store = managedStore(store); const lock = path.join(store, '.update-lock.json');
  exclusiveJson(lock, {pid: process.pid, createdAt: new Date().toISOString()});
  try { return operation(store); } finally { fs.unlinkSync(lock); }
}
export function recoverDeadLock(file) {
  if (!fs.existsSync(file)) return;
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1
    || process.platform !== 'win32' && (stat.uid !== process.getuid() || stat.mode & 0o077)) fail('Unsafe interrupted launcher lock');
  const lease = json(file);
  if (!Number.isSafeInteger(lease.pid) || lease.pid <= 0) fail('Unverifiable interrupted launcher lock');
  try { process.kill(lease.pid, 0); }
  catch (error) { if (error.code === 'ESRCH') { fs.unlinkSync(file); return; } }
  // A live or inaccessible process still owns this lease. Never terminate it.
}
function sessionGate(context) {
  if (!Array.isArray(context.sessionRoots) || !context.sessionRoots.length || context.sessionRoots.some(root => !path.isAbsolute(root))) fail('Explicit shared-session roots are required');
  for (const root of context.sessionRoots) {
    if (fs.existsSync(root)) realDirectory(root);
    if (fs.existsSync(path.join(root, '.autograde-shared-lock.json')) || fs.existsSync(path.join(root, 'current'))) fail('Update deferred: teaching session or cleanup is still pending');
  }
}
function stagePath(store, id) { if (!ID.test(id ?? '')) fail('Invalid release ID'); return path.join(store, 'releases', id); }
function loadStaged(store, id, context, now) {
  const root = stagePath(store, id); realDirectory(root);
  const release = authorize(json(path.join(root, 'release.json')), json(path.join(root, 'approval.json')), context, now);
  if (release.releaseId !== id) fail('Staged release identity mismatch');
  const payload = path.join(root, 'payload'); verifyPayload(payload, release); return {root, payload, release};
}
export function stageRelease({store, payload, releaseEnvelope, approvalEnvelope, context, now}) {
  if (within(path.resolve(payload), path.resolve(store))) fail('Release store must be outside the source payload');
  return locked(store, root => {
    const release = authorize(releaseEnvelope, approvalEnvelope, context, now); verifyPayload(payload, release);
    const target = stagePath(root, release.releaseId); if (fs.existsSync(target)) fail('Release already staged; immutable releases cannot be overwritten');
    const temporary = path.join(root, 'releases', `.stage-${randomUUID()}`); fs.mkdirSync(temporary, {mode: 0o700});
    try {
      fs.cpSync(payload, path.join(temporary, 'payload'), {recursive: true, dereference: false, verbatimSymlinks: true, errorOnExist: true});
      verifyPayload(path.join(temporary, 'payload'), release);
      exclusiveJson(path.join(temporary, 'release.json'), releaseEnvelope); exclusiveJson(path.join(temporary, 'approval.json'), approvalEnvelope);
      fs.renameSync(temporary, target);
    } finally { fs.rmSync(temporary, {recursive: true, force: true}); }
    return {releaseId: release.releaseId, status: 'staged', restartRequired: true, deploymentReady: false};
  });
}
export function activateRelease({store, releaseId, context, rollback = false, now, nativeGate = nativeSignatureGate, beforeSwitch, afterSwitch}) {
  return locked(store, root => {
    sessionGate(context); const stateFile = path.join(root, 'active.json');
    if (fs.existsSync(path.join(root, '.launch-lock.json'))) fail('Update deferred: managed IDE process is active');
    const previous = fs.existsSync(stateFile) ? json(stateFile) : undefined;
    const staged = loadStaged(root, releaseId, context, now);
    if (previous && !rollback && staged.release.sequence <= previous.sequence) fail('Update sequence must increase');
    if (rollback && previous?.previousReleaseId !== releaseId) fail('Rollback is restricted to the previously active verified release');
    nativeGate(staged.payload, staged.release, context);
    const active = {schema: 1, releaseId, sequence: staged.release.sequence, previousReleaseId: previous?.releaseId ?? null, activatedAt: new Date().toISOString()};
    try {
      beforeSwitch?.(staged);
      atomicJson(stateFile, active);
      verifyPayload(staged.payload, staged.release); afterSwitch?.(staged);
    } catch (error) {
      if (previous) atomicJson(stateFile, previous); else fs.rmSync(stateFile, {force: true});
      throw new Error(`Activation failed; previous selection restored: ${error.message}`);
    }
    return {...active, status: rollback ? 'rolled-back' : 'ready-for-next-launch', restartRequired: true, deploymentReady: false};
  });
}
/** Actual supervised launch of the approved executable; never runs manifest shell commands. */
export async function launchActive({store, context, nativeGate = nativeSignatureGate, requiredScript, onEvent}) {
  if (process.env.AUTOGRADE_RELEASE_CHANNEL_ACTIVE === '1') fail('Recursive release channel launch refused');
  const launch = locked(store, root => {
    const active = json(path.join(root, 'active.json')); const staged = loadStaged(root, active.releaseId, context);
    if (requiredScript && path.posix.basename(staged.release.entrypointScript ?? '') !== requiredScript) fail('Release must use the original shared-session launcher script');
    nativeGate(staged.payload, staged.release, context);
    const lock = path.join(root, '.launch-lock.json'); exclusiveJson(lock, {pid: process.pid, releaseId: active.releaseId});
    return {...staged, lock};
  });
  try {
    return await new Promise((resolve, reject) => {
      const args = launch.release.entrypointScript ? [path.join(launch.payload, launch.release.entrypointScript)] : [];
      const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(?:NODE_|ELECTRON_|VSCODE_|AUTOGRADE_|DYLD_|LD_)/.test(key)));
      env.AUTOGRADE_RELEASE_CHANNEL_ACTIVE = '1';
      const child = spawn(path.join(launch.payload, launch.release.entrypoint), args, {cwd: launch.payload, stdio: onEvent ? ['ignore', 'pipe', 'ignore'] : 'inherit', env});
      let pending = '';
      if (onEvent) child.stdout.on('data', chunk => {
        pending += chunk.toString('utf8');
        if (pending.length > 16384) { pending = ''; return; }
        let end;
        while ((end = pending.indexOf('\n')) !== -1) {
          const line = pending.slice(0, end); pending = pending.slice(end + 1);
          try {
            const value = JSON.parse(line);
            if (value.event === 'ide-started' && Number.isSafeInteger(value.pid) && value.pid > 0) onEvent({event: value.event, pid: value.pid});
            else if (value.event === 'session-cleaned') onEvent({event: value.event});
            else if (value.event === 'error') onEvent({event: 'error', message: 'IDE 실행 또는 실습 파일 정리를 완료하지 못했습니다. 담당자에게 확인을 요청하세요.'});
          } catch { /* Arbitrary child logs must never become persistent student data. */ }
        }
      });
      const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
      const stop = () => { if (child.pid && child.exitCode === null) child.kill('SIGTERM'); };
      const unregister = () => { for (const signal of signals) process.removeListener(signal, stop); };
      for (const signal of signals) process.on(signal, stop);
      child.once('error', error => { unregister(); reject(error); });
      child.once('close', (code, signal) => { unregister(); resolve({code: code ?? 1, signal}); });
    });
  } finally { fs.unlinkSync(launch.lock); }
}
// Shared by the automatic updater; all mutations keep the same store lock and
// activation/cleanup gates as manual operator commands.
export { locked as withReleaseStore, sessionGate as assertSessionIdle, loadStaged as inspectStagedRelease };
async function main(argv) {
  const [command, ...pairs] = argv, args = {};
  if (pairs.length % 2) fail('Arguments must be --name value pairs');
  for (let index = 0; index < pairs.length; index += 2) { const key = pairs[index].slice(2); if (!/^--[a-z-]+$/.test(pairs[index]) || key in args) fail('Invalid option'); args[key] = pairs[index + 1]; }
  const context = args.context && json(args.context);
  let result;
  if (command === 'manifest') result = createRelease(args.payload, json(args.metadata));
  else if (command === 'fingerprint') result = {sha256: digest(canonical(json(args.document)))};
  else if (command === 'approval') {
    const release = validateRelease(verifyDocument(json(args.release), context.publisherKeys));
    result = {kind: 'autograde.institution-approval.v1', institutionId: context.institutionId, releaseSha256: digest(canonical(release)),
      channel: context.channel, notBefore: args['not-before'], expiresAt: args.expires};
    // Validate dates and all compatibility constraints before handing the document to the institution signer.
    if (!Number.isFinite(Date.parse(result.notBefore)) || !Number.isFinite(Date.parse(result.expiresAt)) || Date.parse(result.notBefore) >= Date.parse(result.expiresAt)) fail('Approval validity dates are required');
    if (release.platform !== context.platform || release.architecture !== context.architecture || !release.serverReleases.includes(context.serverRelease)
      || !extensionCompatible(release, context) || release.institutionProfileSha256 !== context.institutionProfileSha256) fail('Cannot approve an incompatible release');
  }
  else if (command === 'sign') {
    const info = fs.lstatSync(args.key);
    if (process.platform !== 'win32' && (info.uid !== process.getuid() || info.mode & 0o077)) fail('Signing key must be owned by this operator and private (0600)');
    result = signDocument(json(args.document), regular(args.key, 16384), args['key-id']);
  }
  else if (command === 'verify') result = authorize(json(args.release), json(args.approval), context);
  else if (command === 'stage') result = stageRelease({store: args.store, payload: args.payload, releaseEnvelope: json(args.release), approvalEnvelope: json(args.approval), context});
  else if (command === 'activate' || command === 'rollback') result = activateRelease({store: args.store, releaseId: args.id, context, rollback: command === 'rollback'});
  else if (command === 'launch') result = await launchActive({store: args.store, context});
  else fail('Use manifest, fingerprint, approval, sign, verify, stage, activate, rollback or launch');
  if (args.output) exclusiveJson(args.output, result); else process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main(process.argv.slice(2)).catch(error => { process.stderr.write(`${error.message}\n`); process.exitCode = 1; });
