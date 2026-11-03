import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { checkSource } from '../../ide/code-oss/manage.mjs';
import { nodeCompatibilityMarker } from '../../ide/code-oss/node-compatibility.mjs';

const gitFiles = (root, args) => execFileSync('git', ['-C', root, 'ls-files', '-z', ...args],
  { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 }).split('\0').filter(Boolean);
const present = file => {
  try { return fs.lstatSync(file); }
  catch (error) { if (error.code === 'ENOENT' || error.code === 'ENOTDIR') return undefined; throw error; }
};
const relativePaths = values => [...new Set(values.map(value => {
  if (typeof value !== 'string' || !value || path.isAbsolute(value) || path.win32.isAbsolute(value)
      || value.includes('\0')) throw new Error('Snapshot paths must be relative');
  const normalized = value.split(path.sep).join('/').replace(/\/$/, '');
  if (normalized !== '.' && normalized.split('/').some(part => !part || part === '.' || part === '..')) {
    throw new Error(`Unsafe snapshot path: ${value}`);
  }
  return normalized;
}))].sort();

function fileDigest(file, buffer) {
  const hash = createHash('sha256');
  const fd = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  try {
    if (!fs.fstatSync(fd).isFile()) throw new Error(`Regular snapshot file required: ${file}`);
    let count;
    while ((count = fs.readSync(fd, buffer, 0, buffer.length, null))) hash.update(buffer.subarray(0, count));
  } finally { fs.closeSync(fd); }
  return hash.digest('hex');
}

function fingerprint(root, paths, exclude, recursive) {
  if (!fs.lstatSync(root).isDirectory()) throw new Error('Snapshot root must be a real directory');
  const hash = createHash('sha256'), buffer = Buffer.alloc(1024 * 1024);
  const record = value => hash.update(`${JSON.stringify(value)}\n`);
  const excluded = relative => exclude.some(item => item === '.' || relative === item || relative.startsWith(`${item}/`));
  function visit(relative) {
    if (excluded(relative)) return;
    const file = path.join(root, relative), info = present(file);
    if (!info) { record([relative, 'missing']); return; }
    const mode = info.mode & 0o777;
    if (info.isSymbolicLink()) record([relative, 'symlink', mode, fs.readlinkSync(file, { encoding: 'buffer' }).toString('base64')]);
    else if (info.isFile()) record([relative, 'file', mode, fileDigest(file, buffer)]);
    else if (info.isDirectory()) {
      record([relative, 'directory', mode]);
      if (recursive) for (const name of fs.readdirSync(file).sort()) visit(relative === '.' ? name : `${relative}/${name}`);
    } else throw new Error(`Unsupported snapshot file type: ${file}`);
  }
  for (const relative of paths) {
    if (excluded(relative)) continue;
    // An explicitly requested child may have a symlink ancestor. Record that
    // boundary instead of following it to bytes outside the selected tree.
    const parts = relative.split('/');
    let blocked = false;
    for (let index = 1; index < parts.length; index++) {
      const ancestor = parts.slice(0, index).join('/'), info = present(path.join(root, ancestor));
      if (!info?.isDirectory()) {
        record([relative, 'blocked-by', ancestor]); visit(ancestor); blocked = true; break;
      }
    }
    if (!blocked) visit(relative);
  }
  return hash.digest('hex');
}

export function inputFingerprint(repository, pathspecs) {
  const files = relativePaths(gitFiles(repository, ['--cached', '--others', '--exclude-standard', '--', ...pathspecs]));
  return fingerprint(repository, files, [], false);
}

export function snapshotPaths(root, paths, { exclude = [] } = {}) {
  paths = relativePaths(paths); exclude = relativePaths(exclude);
  return { paths, exclude, digest: fingerprint(root, paths, exclude, true) };
}

export function snapshotMatches(root, snapshot) {
  try {
    return typeof snapshot?.digest === 'string'
      && snapshotPaths(root, snapshot.paths, { exclude: snapshot.exclude }).digest === snapshot.digest;
  } catch { return false; }
}

export function dependencyPaths(source) {
  const candidates = new Set(['node_modules']);
  for (const lock of gitFiles(source, ['--cached', '--', '*package-lock.json'])) {
    if (path.posix.basename(lock) === 'package-lock.json') candidates.add(path.posix.join(path.posix.dirname(lock), 'node_modules'));
  }
  return [...candidates].sort().filter(relative => {
    const parts = relative.split('/');
    return parts.every((_, index) => present(path.join(source, ...parts.slice(0, index + 1)))?.isDirectory());
  });
}

export function assertReusableSource(source, pin) {
  source = checkSource(source, pin);
  const overlayFile = path.join(source, '.autograde-ide-overlay.json');
  const overlay = present(overlayFile) ? JSON.parse(fs.readFileSync(overlayFile, 'utf8')) : undefined;
  const nodeOwned = Boolean(present(path.join(source, nodeCompatibilityMarker)));
  const allowed = new Set([nodeCompatibilityMarker, '.autograde-ide-overlay.json', ...Object.keys(overlay?.policySources ?? {})]);
  const unknown = gitFiles(source, ['--others', '--exclude-standard']).filter(relative => {
    if (allowed.has(relative)) return false;
    if (overlay && /^extensions\/autograde(?:-ide-profile)?\//.test(relative)) return false;
    const backup = /^\.autograde-(node-stage|stage|policy-build)-[A-Za-z0-9]+(?:\/|$)/.exec(relative);
    if (backup && (backup[1] === 'node-stage' ? nodeOwned : overlay)
        && present(path.join(source, relative.split('/')[0]))?.isDirectory()) return false;
    return true;
  });
  if (unknown.length) throw new Error(`Refusing untracked upstream source: ${unknown.join(', ')}; preserve edits and use a clean checkout`);
  return source;
}
