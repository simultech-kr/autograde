import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import vm from 'node:vm';
import { execFileSync } from 'node:child_process';
import test from 'node:test';
import { nodeCompatibilitySource, nodeCompatibilityPath, nodeCompatibilityMarker, prepareNodeCompatibility, verifyNodeCompatibility } from './node-compatibility.mjs';

// The upstream guard's surrounding execution is kept deliberately small so the
// complete patched predicate runs against real version strings in isolation.
const original = `const [majorNodeVersion, minorNodeVersion, patchNodeVersion] = process.versions.node.split('.').map(Number);
const [requiredMajor, requiredMinor, requiredPatch] = '24.18.0'.split('.').map(Number);
\tif (majorNodeVersion !== requiredMajor ||
\t\tminorNodeVersion < requiredMinor ||
\t\t(minorNodeVersion === requiredMinor && patchNodeVersion < requiredPatch)) {
  throw new Error('Node guard rejected');
}
if (process.npmMajor >= 13) throw new Error('npm guard rejected');
if (!process.validToolchain) throw new Error('Toolchain guard rejected');
`;
const pin = { node: '26.13.0', sourceNode: '24.18.0' };

function fixture(t) {
  const source = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-node-compatibility-')));
  t.after(() => fs.rmSync(source, { recursive: true, force: true }));
  fs.mkdirSync(path.join(source, 'build/npm'), { recursive: true });
  fs.writeFileSync(path.join(source, nodeCompatibilityPath), original);
  fs.writeFileSync(path.join(source, '.nvmrc'), '24.18.0\n');
  fs.writeFileSync(path.join(source, '.gitattributes'), '* text=auto\n');
  fs.writeFileSync(path.join(source, 'build/.gitattributes'), '* text eol=lf\n');
  const git = (...args) => execFileSync('git', ['-C', source, ...args], { encoding: 'utf8' }).trim();
  git('init', '--quiet'); git('add', '.');
  git('-c', 'user.name=Autograde Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-m', 'fixture');
  return { source, pin: { ...pin, commit: git('rev-parse', 'HEAD') } };
}

test('guard accepts only the exact Node 26 addition and retains upstream Node 24 compatibility', () => {
  const source = nodeCompatibilitySource(original, pin);
  for (const node of ['24.18.0', '24.18.1', '24.22.2', '26.13.0']) {
    assert.doesNotThrow(() => vm.runInNewContext(source, { process: { versions: { node }, npmMajor: 11, validToolchain: true } }), node);
  }
  for (const node of ['24.17.9', '22.20.0', '25.9.0', '26.12.1', '26.13.1', '26.14.0', '27.0.0']) {
    assert.throws(() => vm.runInNewContext(source, { process: { versions: { node }, npmMajor: 11, validToolchain: true } }), /Node guard rejected/, node);
  }
  assert.throws(() => vm.runInNewContext(source, { process: { versions: { node: pin.node }, npmMajor: 13, validToolchain: true } }), /npm guard rejected/);
  assert.throws(() => vm.runInNewContext(source, { process: { versions: { node: pin.node }, npmMajor: 11, validToolchain: false } }), /Toolchain guard rejected/);
});

test('patch rejects unexpected or ambiguous upstream source and unvalidated version pairs', () => {
  assert.throws(() => nodeCompatibilitySource(original.replace('!== requiredMajor', '< requiredMajor'), pin), /guard changed/);
  assert.throws(() => nodeCompatibilitySource(original + original, pin), /guard changed/);
  assert.throws(() => nodeCompatibilitySource(original, { ...pin, node: '26.14.0' }), /Unsupported managed/);
});

test('managed patch is repeatable and preserves pristine .nvmrc and recoverable preinstall source', t => {
  const f = fixture(t);
  assert.equal(verifyNodeCompatibility(f.source, f.pin), false);
  assert.throws(() => verifyNodeCompatibility(f.source, f.pin, { required: true }), /missing/);
  assert.equal(prepareNodeCompatibility(f.source, f.pin), true);
  assert.equal(prepareNodeCompatibility(f.source, f.pin), true);
  assert.equal(verifyNodeCompatibility(f.source, f.pin, { required: true }), true);
  assert.equal(fs.readFileSync(path.join(f.source, '.nvmrc'), 'utf8'), '24.18.0\n');
  const stages = fs.readdirSync(f.source).filter(name => name.startsWith('.autograde-node-stage-'));
  assert.equal(stages.length, 1);
  assert.equal(fs.readFileSync(path.join(f.source, stages[0], 'previous-preinstall.ts'), 'utf8'), original);
});

test('tracked text attributes allow a clean Windows CRLF .nvmrc without weakening preinstall byte checks', t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.source, '.nvmrc'), '24.18.0\r\n');
  assert.equal(prepareNodeCompatibility(f.source, f.pin), true);
  assert.equal(fs.readFileSync(path.join(f.source, '.nvmrc'), 'utf8'), '24.18.0\r\n');
  const file = path.join(f.source, nodeCompatibilityPath);
  fs.writeFileSync(file, fs.readFileSync(file, 'utf8').replace(/\n/g, '\r\n'));
  assert.throws(() => verifyNodeCompatibility(f.source, f.pin), /was modified/);
});

test('staged attributes cannot silently change source-pin normalization', t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.source, '.gitattributes'), '* -text\n');
  execFileSync('git', ['-C', f.source, 'add', '.gitattributes']);
  assert.throws(() => prepareNodeCompatibility(f.source, f.pin), /attributes differ/);
  assert.equal(fs.readFileSync(path.join(f.source, nodeCompatibilityPath), 'utf8'), original);
});

test('unowned and post-prepare edits are refused without overwriting source', async t => {
  for (const managed of [false, true]) await t.test(String(managed), t => {
    const f = fixture(t);
    if (managed) prepareNodeCompatibility(f.source, f.pin);
    const edited = '// user edit\n' + fs.readFileSync(path.join(f.source, nodeCompatibilityPath), 'utf8');
    fs.writeFileSync(path.join(f.source, nodeCompatibilityPath), edited);
    assert.throws(() => prepareNodeCompatibility(f.source, f.pin), managed ? /was modified/ : /unowned/);
    assert.equal(fs.readFileSync(path.join(f.source, nodeCompatibilityPath), 'utf8'), edited);
  });
});

test('forged marker hashes, altered upstream pin and symlinks fail closed', async t => {
  for (const kind of ['marker', 'source-pin', 'nvmrc-whitespace', 'preinstall-link', 'marker-link']) await t.test(kind, t => {
    const f = fixture(t); prepareNodeCompatibility(f.source, f.pin);
    if (kind === 'marker') {
      const file = path.join(f.source, nodeCompatibilityMarker);
      const marker = JSON.parse(fs.readFileSync(file, 'utf8')); marker.patchedHash = '0'.repeat(64);
      fs.writeFileSync(file, JSON.stringify(marker));
    } else if (kind === 'source-pin') f.pin.sourceNode = '26.13.0';
    else if (kind === 'nvmrc-whitespace') fs.appendFileSync(path.join(f.source, '.nvmrc'), '\n');
    else {
      const file = path.join(f.source, kind === 'preinstall-link' ? nodeCompatibilityPath : nodeCompatibilityMarker);
      fs.renameSync(file, `${file}.saved`); fs.symlinkSync(`${file}.saved`, file);
    }
    assert.throws(() => prepareNodeCompatibility(f.source, f.pin), /marker|source Node pin|Regular file/);
  });
});
