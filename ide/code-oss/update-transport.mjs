#!/usr/bin/env node
// No executable archive headers: every destination, length and link comes from the signed inventory.
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as https from 'node:https';
import { createHash, randomUUID } from 'node:crypto';
import { createGzip, createGunzip } from 'node:zlib';
import { Transform, Readable } from 'node:stream';
import { pipeline } from 'node:stream/promises';
import { fileURLToPath } from 'node:url';
import { authorize, canonical, digest, signDocument, verifyDocument, verifyPayload } from './release-channel.mjs';

export const UPDATE_FORMAT = 'autograde-signed-files-gzip-v1';
export const UPDATE_LIMITS = Object.freeze({feed: 32 * 1024 ** 2, artifact: 2 * 1024 ** 3, unpacked: 8 * 1024 ** 3, file: 4 * 1024 ** 3});
const fail = message => { throw new Error(message); };
const within = (root, candidate) => { const relative = path.relative(root, candidate); return relative === '' || !path.isAbsolute(relative) && relative !== '..' && !relative.startsWith(`..${path.sep}`); };
const hashFile = file => { const hash = createHash('sha256'), buffer = Buffer.allocUnsafe(1024 ** 2), fd = fs.openSync(file, 'r'); try { let count; while ((count = fs.readSync(fd, buffer, 0, buffer.length, null))) hash.update(buffer.subarray(0, count)); } finally { fs.closeSync(fd); } return hash.digest('hex'); };
function realDirectory(directory) {
  for (let current = path.resolve(directory);;) {
    const stat = fs.lstatSync(current);
    if (!stat.isDirectory() || stat.isSymbolicLink()) fail('Update directory must have real directory ancestors');
    const parent = path.dirname(current); if (current === parent) break; current = parent;
  }
}
function regular(file, limit) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > limit) fail('Bounded regular update input required');
  return fs.readFileSync(file);
}
function validRelative(value) {
  if (typeof value !== 'string' || !value || value.length > 1024 || value.includes('\\') || /[\x00-\x1f\x7f]/.test(value) || value.startsWith('/')
    || value.split('/').some(part => !part || part === '.' || part === '..' || part.includes(':'))) fail('Unsafe signed update path');
  return value;
}
/** Validate all paths before creating even the first payload directory. */
export function validateUpdateInventory(release) {
  if (!Array.isArray(release.files) || !release.files.length || release.files.length > 150000) fail('Invalid update inventory');
  const byPath = new Map(), folded = new Set(); let total = 0;
  for (const item of release.files) {
    validRelative(item.path);
    // Mac default volumes fold case and Unicode normalization. Reject ambiguous archives everywhere.
    const key = item.path.normalize('NFD').toLowerCase();
    if (byPath.has(item.path) || folded.has(key)) fail('Duplicate or ambiguous signed update path');
    byPath.set(item.path, item); folded.add(key);
    if (item.type === 'file') {
      if (!Number.isSafeInteger(item.size) || item.size < 0 || item.size > UPDATE_LIMITS.file || !/^[a-f0-9]{64}$/.test(item.sha256) || typeof item.executable !== 'boolean') fail('Invalid signed update file');
      total += item.size; if (total > UPDATE_LIMITS.unpacked) fail('Update expands beyond size limit');
    } else if (item.type === 'link') {
      if (typeof item.target !== 'string' || !item.target || item.target.length > 1024 || path.posix.isAbsolute(item.target)
        || item.target.includes('\\') || /[\x00-\x1f\x7f:]/.test(item.target)) fail('Unsafe signed update link');
      const target = path.posix.normalize(path.posix.join(path.posix.dirname(item.path), item.target));
      if (target === '..' || target.startsWith('../') || path.posix.isAbsolute(target)) fail('External signed update link');
    } else if (item.type !== 'directory') fail('Unsupported signed update entry');
  }
  for (const item of release.files) {
    for (let parent = path.posix.dirname(item.path); parent !== '.'; parent = path.posix.dirname(parent)) {
      if (byPath.get(parent)?.type !== 'directory') fail('Signed update parent must be a declared real directory');
    }
  }
  return total;
}
export function validateFeedUrl(value) {
  if (typeof value !== 'string' || value !== value.trim() || value.includes('\\')) fail('A configured HTTPS update URL is required');
  const url = new URL(value);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) fail('Updates require HTTPS without credentials, query or fragment');
  return url;
}
function downloadResponse(url, {signal, httpsAgent} = {}) {
  return new Promise((resolve, reject) => {
    const request = https.get(url, {signal, agent: httpsAgent, headers: {'Accept-Encoding': 'identity', 'User-Agent': 'Autograde-IDE-Updater/1'}}, response => {
      if (response.statusCode !== 200 || response.headers['content-encoding'] && response.headers['content-encoding'] !== 'identity') {
        response.destroy(); reject(new Error('Update server returned a non-success response; redirects are not followed')); return;
      }
      resolve(response);
    });
    request.setTimeout(30000, () => request.destroy(new Error('Update connection timed out')));
    request.once('error', reject);
  });
}
function boundResponse(response, maximum, expected) {
  const value = response.headers['content-length'];
  if (value !== undefined && (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value)) || Number(value) > maximum || expected !== undefined && Number(value) !== expected)) {
    response.destroy(); fail('Update response size is invalid');
  }
}
async function readFeed(url, options) {
  const response = await downloadResponse(url, options); boundResponse(response, UPDATE_LIMITS.feed);
  const chunks = []; let size = 0;
  for await (const chunk of response) { size += chunk.length; if (size > UPDATE_LIMITS.feed) { response.destroy(); fail('Update feed exceeds size limit'); } chunks.push(chunk); }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
function validateFeed(envelope, context, feedUrl) {
  const feed = verifyDocument(envelope, context.institutionKeys);
  if (feed?.kind !== 'autograde.ide-update.v1' || feed.format !== UPDATE_FORMAT) fail('Unsupported update feed');
  const release = authorize(feed.releaseEnvelope, feed.approvalEnvelope, context);
  const unpacked = validateUpdateInventory(release), artifact = feed.artifact;
  if (!artifact || !/^[a-f0-9]{64}$/.test(artifact.sha256) || artifact.path !== `${artifact.sha256}.agupdate.gz`
    || !Number.isSafeInteger(artifact.size) || artifact.size < 1 || artifact.size > UPDATE_LIMITS.artifact
    || artifact.unpackedSize !== unpacked) fail('Invalid signed update artifact');
  const artifactUrl = new URL(artifact.path, feedUrl);
  if (artifactUrl.origin !== feedUrl.origin || artifactUrl.search || artifactUrl.hash) fail('Cross-origin update artifact refused');
  return {feed, release, artifactUrl};
}
/** Decompress only content bytes; file names, modes and links cannot be supplied by the stream. */
export async function unpackUpdate(artifact, payload, release, {signal} = {}) {
  const totalExpected = validateUpdateInventory(release); realDirectory(path.dirname(path.resolve(payload)));
  fs.mkdirSync(payload, {mode: 0o700});
  const directories = release.files.filter(item => item.type === 'directory').sort((a, b) => a.path.split('/').length - b.path.split('/').length || a.path.localeCompare(b.path));
  for (const item of directories) fs.mkdirSync(path.join(payload, item.path), {mode: 0o755});
  const files = release.files.filter(item => item.type === 'file');
  let index = 0, fd, hash, remaining = 0, total = 0;
  const openNext = () => {
    while (index < files.length) {
      const item = files[index]; fd = fs.openSync(path.join(payload, item.path), 'wx', item.executable ? 0o755 : 0o644);
      hash = createHash('sha256'); remaining = item.size;
      if (remaining) return;
      fs.closeSync(fd); fd = undefined;
      if (hash.digest('hex') !== item.sha256) fail('Empty update file digest mismatch');
      index++;
    }
  };
  const source = fs.createReadStream(artifact), gunzip = createGunzip();
  source.on('error', error => gunzip.destroy(error)); source.pipe(gunzip);
  const abort = () => { const error = new Error('Update download cancelled'); source.destroy(error); gunzip.destroy(error); };
  signal?.addEventListener('abort', abort, {once: true});
  try {
    signal?.throwIfAborted(); openNext();
    for await (const chunk of gunzip) {
      signal?.throwIfAborted(); total += chunk.length;
      if (total > totalExpected) fail('Update expands beyond signed length');
      let offset = 0;
      while (offset < chunk.length) {
        if (index >= files.length || fd === undefined) fail('Unexpected update content');
        const length = Math.min(remaining, chunk.length - offset), bytes = chunk.subarray(offset, offset + length);
        let written = 0; while (written < bytes.length) written += fs.writeSync(fd, bytes, written, bytes.length - written); hash.update(bytes); remaining -= length; offset += length;
        if (!remaining) {
          fs.closeSync(fd); fd = undefined;
          if (hash.digest('hex') !== files[index].sha256) fail('Update file digest mismatch');
          index++; openNext();
        }
      }
    }
    if (index !== files.length || total !== totalExpected) fail('Truncated update payload');
    for (const item of release.files.filter(item => item.type === 'link')) fs.symlinkSync(item.target, path.join(payload, item.path));
    verifyPayload(payload, release);
  } finally {
    signal?.removeEventListener('abort', abort); source.destroy(); gunzip.destroy();
    if (fd !== undefined) fs.closeSync(fd);
  }
}
/** httpsAgent is dependency injection for tests with a local CA; configuration cannot disable TLS checks. */
export async function downloadRelease({feedUrl, context, destination, signal, report = () => {}, minSequence = 0, excludeReleaseIds = [], httpsAgent}) {
  const url = validateFeedUrl(feedUrl);
  const combined = signal ? AbortSignal.any([signal, AbortSignal.timeout(30 * 60 * 1000)]) : AbortSignal.timeout(30 * 60 * 1000);
  combined.throwIfAborted(); report({event: 'update-checking'});
  const envelope = await readFeed(url, {signal: combined, httpsAgent});
  const {feed, release, artifactUrl} = validateFeed(envelope, context, url);
  if (!Number.isSafeInteger(minSequence) || minSequence < 0 || !Array.isArray(excludeReleaseIds)) fail('Invalid update selection policy');
  if (release.sequence <= minSequence || excludeReleaseIds.includes(release.releaseId)) return null;
  destination = path.resolve(destination); realDirectory(path.dirname(destination));
  fs.mkdirSync(destination, {mode: 0o700});
  const archive = path.join(destination, 'download.agupdate.gz'), payload = path.join(destination, 'payload');
  try {
    fs.writeFileSync(path.join(destination, '.download-owner.json'), JSON.stringify({schema: 1, pid: process.pid}), {flag: 'wx', mode: 0o600});
    report({event: 'update-downloading', releaseId: release.releaseId, sequence: release.sequence});
    const response = await downloadResponse(artifactUrl, {signal: combined, httpsAgent}); boundResponse(response, UPDATE_LIMITS.artifact, feed.artifact.size);
    let size = 0; const hash = createHash('sha256');
    const meter = new Transform({transform(chunk, _encoding, callback) {
      size += chunk.length;
      if (size > feed.artifact.size) return callback(new Error('Update download exceeds signed length'));
      hash.update(chunk); callback(null, chunk);
    }});
    await pipeline(response, meter, fs.createWriteStream(archive, {flags: 'wx', mode: 0o600}), {signal: combined});
    if (size !== feed.artifact.size || hash.digest('hex') !== feed.artifact.sha256) fail('Update artifact digest mismatch');
    report({event: 'update-verifying', releaseId: release.releaseId});
    await unpackUpdate(archive, payload, release, {signal: combined});
    fs.unlinkSync(archive);
    return {releaseEnvelope: feed.releaseEnvelope, approvalEnvelope: feed.approvalEnvelope, payload, feedDigest: digest(canonical(envelope))};
  } catch (error) { fs.rmSync(destination, {recursive: true, force: true}); throw error; }
}

async function* contentBytes(payload, release) {
  for (const item of release.files.filter(value => value.type === 'file')) {
    const file = path.join(payload, item.path), stat = fs.lstatSync(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size !== item.size) fail('Payload changed during publication');
    let count = 0; const hash = createHash('sha256');
    for await (const bytes of fs.createReadStream(file)) { count += bytes.length; if (count > item.size) fail('Payload grew during publication'); hash.update(bytes); yield bytes; }
    if (count !== item.size || hash.digest('hex') !== item.sha256) fail('Payload changed during publication');
  }
}
/** Operator action: one publisher signature and a separate institution approval are both required. */
export async function publishUpdate({payload, releaseEnvelope, approvalEnvelope, institutionKey, keyId, output, context}) {
  const release = authorize(releaseEnvelope, approvalEnvelope, context), unpackedSize = validateUpdateInventory(release);
  payload = path.resolve(payload); output = path.resolve(output);
  if (within(payload, output)) fail('Update publication must be outside the payload');
  verifyPayload(payload, release); realDirectory(output);
  const temporary = path.join(output, `.update-${randomUUID()}.tmp`), feedTemporary = path.join(output, `.feed-${randomUUID()}.tmp`);
  try {
    let size = 0; const hash = createHash('sha256');
    const meter = new Transform({transform(chunk, _encoding, callback) {
      size += chunk.length; if (size > UPDATE_LIMITS.artifact) return callback(new Error('Compressed update exceeds size limit'));
      hash.update(chunk); callback(null, chunk);
    }});
    await pipeline(Readable.from(contentBytes(payload, release)), createGzip({level: 6}), meter, fs.createWriteStream(temporary, {flags: 'wx', mode: 0o644}));
    verifyPayload(payload, release);
    const sha256 = hash.digest('hex'), name = `${sha256}.agupdate.gz`, artifact = path.join(output, name);
    const feed = signDocument({kind: 'autograde.ide-update.v1', format: UPDATE_FORMAT, releaseEnvelope, approvalEnvelope,
      artifact: {path: name, sha256, size, unpackedSize}}, institutionKey, keyId);
    verifyDocument(feed, context.institutionKeys); // Do not publish a feed that installed devices cannot trust.
    const json = `${JSON.stringify(feed)}\n`; if (Buffer.byteLength(json) > UPDATE_LIMITS.feed) fail('Signed update feed exceeds size limit');
    const archiveFd = fs.openSync(temporary, 'r'); try { fs.fsyncSync(archiveFd); } finally { fs.closeSync(archiveFd); }
    let existing;
    try { existing = fs.lstatSync(artifact); } catch (error) { if (error.code !== 'ENOENT') throw error; }
    if (existing) {
      const stat = existing;
      if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size !== size || hashFile(artifact) !== sha256) fail('Existing immutable update artifact differs');
      fs.unlinkSync(temporary);
    } else fs.renameSync(temporary, artifact);
    const fd = fs.openSync(feedTemporary, 'wx', 0o644); try { fs.writeFileSync(fd, json); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(feedTemporary, path.join(output, 'feed.json'));
    return {feed: path.join(output, 'feed.json'), artifact, releaseId: release.releaseId, sequence: release.sequence, bytes: size, format: UPDATE_FORMAT};
  } finally { fs.rmSync(temporary, {force: true}); fs.rmSync(feedTemporary, {force: true}); }
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [command, ...pairs] = process.argv.slice(2), args = {};
    if (command !== 'publish' || pairs.length % 2) fail('Use publish --payload DIR --release FILE --approval FILE --context FILE --key FILE --key-id ID --output EXISTING_DIR');
    for (let i = 0; i < pairs.length; i += 2) {
      if (!/^--(payload|release|approval|context|key|key-id|output)$/.test(pairs[i]) || Object.hasOwn(args, pairs[i].slice(2))) fail('Invalid publisher option');
      args[pairs[i].slice(2)] = pairs[i + 1];
    }
    const info = fs.lstatSync(args.key);
    if (process.platform !== 'win32' && (info.uid !== process.getuid() || info.mode & 0o077)) fail('Institution private key must be operator-owned and private (0600)');
    const read = (file, limit = UPDATE_LIMITS.feed) => JSON.parse(regular(file, limit).toString('utf8'));
    const result = await publishUpdate({payload: args.payload, releaseEnvelope: read(args.release), approvalEnvelope: read(args.approval),
      context: read(args.context, 1024 ** 2), institutionKey: regular(args.key, 16384), keyId: args['key-id'], output: args.output});
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
  } catch (error) { process.stderr.write(`${error.message}\n`); process.exitCode = 1; }
}
