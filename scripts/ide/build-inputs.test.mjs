import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { inputFingerprint, snapshotPaths, snapshotMatches, dependencyPaths, assertReusableSource } from './build-inputs.mjs';
import { prepareNodeCompatibility } from '../../ide/code-oss/node-compatibility.mjs';

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-build-inputs-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const write = (relative, bytes) => { const file = path.join(root, relative); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, bytes); };
  const git = (...args) => execFileSync('git', ['-C', root, ...args], { encoding: 'utf8' }).trim();
  git('init', '--quiet'); git('config', 'user.name', 'Fixture'); git('config', 'user.email', 'fixture@example.invalid');
  git('config', 'commit.gpgsign', 'false'); git('config', 'core.autocrlf', 'false');
  git('config', 'core.hooksPath', path.join(root, '.git', 'empty-hooks'));
  const commit = () => { git('add', '.'); git('commit', '--quiet', '-m', 'fixture'); return git('rev-parse', 'HEAD'); };
  return { root, write, git, commit };
}

test('input fingerprint reads actual tracked, staged, missing and new inputs while ignoring build output', t => {
  const f = fixture(t);
  f.write('.gitignore', 'node_modules/\nout/\n'); f.write('extensions/vscode/src/a.ts', 'original'); f.commit();
  const digest = () => inputFingerprint(f.root, ['extensions/vscode']);
  const first = digest();
  f.write('extensions/vscode/node_modules/large.bin', 'ignored'); f.write('unrelated.txt', 'ignored by pathspec');
  assert.equal(digest(), first);
  f.write('extensions/vscode/src/a.ts', 'staged'); f.git('add', 'extensions/vscode/src/a.ts');
  const staged = digest(); assert.notEqual(staged, first);
  f.write('extensions/vscode/src/a.ts', 'working'); assert.notEqual(digest(), staged);
  fs.unlinkSync(path.join(f.root, 'extensions/vscode/src/a.ts')); const missing = digest();
  f.write('extensions/vscode/src/new.ts', 'new'); assert.notEqual(digest(), missing);
  f.write('extensions/vscode/src/a.ts', 'original'); fs.unlinkSync(path.join(f.root, 'extensions/vscode/src/new.ts'));
  assert.equal(digest(), first, 'the current bytes, not the index bytes, determine the fingerprint');
});

test('snapshots detect content changes even with identical size and timestamps, additions and missing output', t => {
  const f = fixture(t); f.write('out/main.js', 'first'); f.write('out/nested/entry.js', 'nested');
  const snapshot = snapshotPaths(f.root, ['out', 'absent']);
  assert.equal(snapshotMatches(f.root, snapshot), true);
  const file = path.join(f.root, 'out/main.js'), stat = fs.statSync(file);
  f.write('out/main.js', 'other'); fs.utimesSync(file, stat.atime, stat.mtime);
  assert.equal(snapshotMatches(f.root, snapshot), false);
  f.write('out/main.js', 'first'); assert.equal(snapshotMatches(f.root, snapshot), true);
  f.write('out/new.js', 'new'); assert.equal(snapshotMatches(f.root, snapshot), false);
  fs.unlinkSync(path.join(f.root, 'out/new.js')); fs.unlinkSync(file);
  assert.equal(snapshotMatches(f.root, snapshot), false);
  f.write('out/main.js', 'first'); f.write('absent', 'created'); assert.equal(snapshotMatches(f.root, snapshot), false);
});

test('snapshots normalize ordering and exclude only selected paths and subtrees', t => {
  const f = fixture(t); f.write('out/main.js', 'main'); f.write('out/policy/a.js', 'generated'); f.write('out/policy-extra.js', 'retained');
  const a = snapshotPaths(f.root, ['out', 'absent', 'out'], { exclude: ['out/policy'] });
  const b = snapshotPaths(f.root, ['absent', 'out'], { exclude: ['out/policy'] });
  assert.deepEqual(a, b);
  f.write('out/policy/new.js', 'new'); assert.equal(snapshotMatches(f.root, a), true);
  f.write('out/policy-extra.js', 'changed'); assert.equal(snapshotMatches(f.root, a), false);
  assert.throws(() => snapshotPaths(f.root, ['../escape']), /Unsafe/);
  assert.equal(snapshotMatches(f.root, { paths: ['../escape'], digest: a.digest }), false);
});

test('snapshots read large files in chunks and include executable mode', t => {
  const f = fixture(t); f.write('large', Buffer.alloc(2 * 1024 * 1024 + 17, 65));
  const snapshot = snapshotPaths(f.root, ['large']);
  const descriptor = fs.openSync(path.join(f.root, 'large'), 'r+');
  fs.writeSync(descriptor, Buffer.from('B'), 0, 1, 2 * 1024 * 1024); fs.closeSync(descriptor);
  assert.equal(snapshotMatches(f.root, snapshot), false);
  if (process.platform !== 'win32') {
    const changed = snapshotPaths(f.root, ['large']); fs.chmodSync(path.join(f.root, 'large'), 0o755);
    assert.equal(snapshotMatches(f.root, changed), false);
  }
});

test('symlinks and requested paths beneath them never read their targets', { skip: process.platform === 'win32' }, t => {
  const f = fixture(t); f.write('outside/data', 'first'); fs.mkdirSync(path.join(f.root, 'selected'));
  fs.symlinkSync('../outside', path.join(f.root, 'selected/link'));
  const tree = snapshotPaths(f.root, ['selected']), child = snapshotPaths(f.root, ['selected/link/data']);
  f.write('outside/data', 'other'); assert.equal(snapshotMatches(f.root, tree), true); assert.equal(snapshotMatches(f.root, child), true);
  fs.unlinkSync(path.join(f.root, 'selected/link')); fs.symlinkSync('../missing', path.join(f.root, 'selected/link'));
  assert.equal(snapshotMatches(f.root, tree), false); assert.equal(snapshotMatches(f.root, child), false);
});

test('dependency roots come from tracked lockfiles without scanning ignored dependency trees', t => {
  const f = fixture(t); f.write('.gitignore', 'node_modules/\n');
  for (const name of ['package-lock.json', 'build/package-lock.json', 'remote/package-lock.json', 'absent/package-lock.json']) f.write(name, '{}');
  f.commit();
  for (const name of ['node_modules/a/index.js', 'build/node_modules/a/index.js', 'remote/node_modules/a/index.js', 'node_modules/deep/package-lock.json', 'untracked/node_modules/a/index.js']) f.write(name, 'fixture');
  f.write('untracked/package-lock.json', '{}');
  assert.deepEqual(dependencyPaths(f.root), ['build/node_modules', 'node_modules', 'remote/node_modules']);
});

function sourceFixture(t) {
  const f = fixture(t);
  f.write('product.json', '{}'); f.write('package.json', '{"version":"1.143.0"}'); f.write('.nvmrc', '24.18.0\n');
  f.write('LICENSE.txt', 'fixture'); f.write('extensions/existing/package.json', '{}'); f.write('src/main.ts', '// original');
  f.write('.gitignore', 'node_modules/\n');
  f.write('build/npm/preinstall.ts', '\tif (majorNodeVersion !== requiredMajor ||\n\t\tminorNodeVersion < requiredMinor ||\n\t\t(minorNodeVersion === requiredMinor && patchNodeVersion < requiredPatch)) {\nthrow new Error();\n}\n');
  const pin = { commit: f.commit(), tag: '1.143.0', node: '26.13.0', sourceNode: '24.18.0' };
  return { ...f, pin };
}

test('reusable source accepts a pinned checkout and its owned Node patch without needing an overlay', t => {
  const f = sourceFixture(t); assert.equal(assertReusableSource(f.root, f.pin), f.root);
  prepareNodeCompatibility(f.root, f.pin); f.write('node_modules/partial/index.js', 'failed install');
  assert.equal(assertReusableSource(f.root, f.pin), f.root);
  assert.equal(fs.readFileSync(path.join(f.root, 'node_modules/partial/index.js'), 'utf8'), 'failed install');
});

test('reusable source rejects tracked and untracked edits while preserving every byte', t => {
  const f = sourceFixture(t);
  f.write('src/main.ts', '// user edit');
  assert.throws(() => assertReusableSource(f.root, f.pin), /modified upstream source/);
  assert.equal(fs.readFileSync(path.join(f.root, 'src/main.ts'), 'utf8'), '// user edit');
  f.write('src/main.ts', '// original'); f.write('src/new.ts', '// untracked edit');
  assert.throws(() => assertReusableSource(f.root, f.pin), /untracked upstream source/);
  assert.equal(fs.readFileSync(path.join(f.root, 'src/new.ts'), 'utf8'), '// untracked edit');
  assert.throws(() => assertReusableSource(f.root, { ...f.pin, commit: '0'.repeat(40) }), /commit does not match/);
});
