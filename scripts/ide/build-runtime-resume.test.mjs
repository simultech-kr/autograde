import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { parseOptions, publishArchive, validateArtifact, builtExtensionPaths, nativeEnvironmentFingerprint } from './build-runtime.mjs';
import { openBuildJournal, buildStateFilename } from './build-resume.mjs';
import { snapshotPaths, snapshotMatches } from './build-inputs.mjs';

const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const cli = ['--target', 'linux-x64', '--source', '/build/source', '--output', '/build/output'];
function fixture(t) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-runtime-resume-')));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  return {directory, source: path.join(directory, 'source'), output: path.join(directory, 'output')};
}
function archive(directory, name = 'Autograde-IDE-local.tar.gz', contents = 'synthetic package bytes') {
  fs.mkdirSync(directory, {recursive: true});
  const file = path.join(directory, name);
  fs.writeFileSync(file, contents);
  fs.writeFileSync(`${file}.sha256`, `${hash(contents)}  ${name}\n`);
  return file;
}

test('resume parsing defaults false and combines with local and WSL flags', () => {
  assert.deepEqual(parseOptions(cli), {target: 'linux-x64', source: '/build/source', output: '/build/output', includeWsl: false, local: false, resume: false});
  for (const resume of [false, true]) for (const local of [false, true]) for (const includeWsl of [false, true]) {
    const flags = [...(resume ? ['--resume'] : []), ...(local ? ['--local'] : []), ...(includeWsl ? ['--include-wsl'] : [])];
    const options = parseOptions([...flags, ...cli]);
    assert.deepEqual({resume: options.resume, local: options.local, includeWsl: options.includeWsl}, {resume, local, includeWsl});
  }
  for (const flag of ['--resume', '--local', '--include-wsl']) assert.throws(() => parseOptions([...cli, flag, flag]), /Usage/);
  for (const extra of [['--resume', 'false'], ['--resume=true'], ['--unknown'], ['--source', '/other'], ['--output'], ['--resume', '--resume']]) {
    assert.throws(() => parseOptions([...cli, ...extra]), /Usage/);
  }
});

test('archive publication verifies checksums before writing and preserves all previous files', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  const staged = archive(path.join(f.directory, 'attempt'));
  const old = path.join(f.output, path.basename(staged)); fs.writeFileSync(old, 'older archive');
  fs.writeFileSync(`${old}.sha256`, 'older checksum');
  fs.writeFileSync(`${staged}.sha256`, `${'0'.repeat(64)}  ${path.basename(staged)}\n`);
  const before = fs.readdirSync(f.output).sort();
  assert.throws(() => publishArchive(staged, f.output, 'linux-native'), /checksum mismatch/);
  assert.deepEqual(fs.readdirSync(f.output).sort(), before);
  assert.equal(fs.readFileSync(old, 'utf8'), 'older archive');
  assert.equal(fs.readFileSync(`${old}.sha256`, 'utf8'), 'older checksum');
  assert.equal(fs.readFileSync(staged, 'utf8'), 'synthetic package bytes');
});

test('republication uses unique filenames with matching sidecars for ZIP and TAR archives', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  for (const name of ['Autograde-IDE-local.tar.gz', 'Autograde-IDE-local.app.zip']) {
    const staged = archive(path.join(f.directory, 'attempt'), name);
    const first = publishArchive(staged, f.output, 'native'), second = publishArchive(staged, f.output, 'native');
    assert.notEqual(first.file, second.file);
    assert.match(first.file, /-build-[a-f0-9-]+\.(?:tar\.gz|zip)$/);
    assert.equal(first.bytes, Buffer.byteLength('synthetic package bytes'));
    assert.equal(first.sha256, hash('synthetic package bytes'));
    for (const artifact of [first, second]) {
      assert.equal(validateArtifact(f.output, artifact), true);
      assert.equal(fs.readFileSync(path.join(f.output, `${artifact.file}.sha256`), 'utf8'), `${artifact.sha256}  ${artifact.file}\n`);
    }
    assert.equal(fs.readFileSync(staged, 'utf8'), 'synthetic package bytes');
  }
});

test('artifact validation rejects byte tampering, mismatched checksum names, missing files, and unsafe metadata', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  const artifact = publishArchive(archive(path.join(f.directory, 'attempt'), 'Autograde-IDE.zip', 'original'), f.output, 'native');
  const published = path.join(f.output, artifact.file), sidecar = `${published}.sha256`;
  fs.writeFileSync(published, 'tampered'); // Same length: the digest must detect this.
  assert.equal(validateArtifact(f.output, artifact), false);
  fs.writeFileSync(published, 'original');
  fs.writeFileSync(sidecar, `${artifact.sha256}  another.zip\n`);
  assert.equal(validateArtifact(f.output, artifact), false);
  fs.writeFileSync(sidecar, `${artifact.sha256}  ${artifact.file}\n`);
  assert.equal(validateArtifact(f.output, artifact), true);
  for (const invalid of [null, {}, {...artifact, file: '../outside.zip'}, {...artifact, file: '..\\outside.zip'},
    {...artifact, file: '/outside.zip'}, {...artifact, bytes: artifact.bytes + 1}, {...artifact, sha256: '0'.repeat(64)}]) {
    assert.equal(validateArtifact(f.output, invalid), false);
  }
  fs.unlinkSync(sidecar); assert.equal(validateArtifact(f.output, artifact), false);
  fs.unlinkSync(published); assert.equal(validateArtifact(f.output, artifact), false);
});

test('publication requires the staged sidecar to name its exact archive', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  const staged = archive(path.join(f.directory, 'attempt'));
  fs.writeFileSync(`${staged}.sha256`, `${hash('synthetic package bytes')}  wrong-name.tar.gz\n`);
  assert.throws(() => publishArchive(staged, f.output, 'linux-native'), /checksum mismatch/);
  assert.deepEqual(fs.readdirSync(f.output), []);
});

test('artifact validation rejects hard-linked published archives and sidecars', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  const artifact = publishArchive(archive(path.join(f.directory, 'attempt')), f.output, 'native');
  const published = path.join(f.output, artifact.file), alias = path.join(f.directory, 'alias');
  for (const file of [published, `${published}.sha256`]) {
    fs.linkSync(file, alias);
    assert.equal(validateArtifact(f.output, artifact), false);
    fs.unlinkSync(alias);
    assert.equal(validateArtifact(f.output, artifact), true);
  }
});

test('extension snapshots include full client/server and sibling runtime output trees', t => {
  const f = fixture(t);
  const outputs = [
    'extensions/css-language-features/client/out/node/cssClientMain.js',
    'extensions/css-language-features/client/out/common/client.js',
    'extensions/css-language-features/server/out/node/cssServerMain.js',
    'extensions/css-language-features/server/out/common/server.js',
    'extensions/markdown-language-features/dist/browser/extension.js',
    'extensions/plain/out/extension.js',
  ];
  const ignored = ['extensions/css-language-features/node_modules/dependency/out/index.js', 'extensions/plain/.git/out/internal.js'];
  for (const relative of [...outputs, ...ignored]) {
    const file = path.join(f.source, relative); fs.mkdirSync(path.dirname(file), {recursive: true}); fs.writeFileSync(file, relative);
  }
  // Upstream manifest entries commonly omit the .js suffix. Discovery must not
  // depend on resolving one entry point or omit its sibling runtime imports.
  fs.writeFileSync(path.join(f.source, 'extensions/css-language-features/package.json'), JSON.stringify({main: './client/out/node/cssClientMain'}));
  const selected = builtExtensionPaths(f.source);
  assert.deepEqual(selected, ['extensions/css-language-features/client/out', 'extensions/css-language-features/server/out',
    'extensions/markdown-language-features/dist', 'extensions/plain/out']);
  const snapshot = snapshotPaths(f.source, selected);
  for (const relative of outputs) {
    const file = path.join(f.source, relative);
    fs.writeFileSync(file, 'tampered output');
    assert.equal(snapshotMatches(f.source, snapshot), false, relative);
    fs.writeFileSync(file, relative);
    assert.equal(snapshotMatches(f.source, snapshot), true, relative);
  }
  for (const relative of ignored) fs.writeFileSync(path.join(f.source, relative), 'dependency or Git changes are handled separately');
  assert.equal(snapshotMatches(f.source, snapshot), true);
  fs.rmSync(path.join(f.source, 'extensions/css-language-features/server/out'), {recursive: true});
  assert.equal(snapshotMatches(f.source, snapshot), false);
});

test('compiler and SDK environment changes invalidate native fingerprints while unrelated settings do not', () => {
  const baseline = {PATH: '/official/node/bin:/usr/bin', CC: 'clang', CXX: 'clang++', SDKROOT: '/SDK/one',
    MACOSX_DEPLOYMENT_TARGET: '13.0', CPLUS_INCLUDE_PATH: '/include', INCLUDE: 'C:\\headers', LIB: 'C:\\lib', TERM: 'xterm'};
  const original = nativeEnvironmentFingerprint(baseline);
  for (const [name, value] of Object.entries({PATH: '/other/bin', CC: 'gcc', CXX: 'g++', SDKROOT: '/SDK/two', MACOSX_DEPLOYMENT_TARGET: '14.0',
    CPLUS_INCLUDE_PATH: '/other/include', INCLUDE: 'C:\\other-headers', LIB: 'C:\\other-lib', CPPFLAGS: '-DCHANGED', CMAKE_TOOLCHAIN_FILE: '/toolchain.cmake',
    WindowsSdkDir: 'C:\\SDK', npm_config_build_from_source: 'true'})) {
    assert.notEqual(nativeEnvironmentFingerprint({...baseline, [name]: value}), original, name);
  }
  assert.equal(nativeEnvironmentFingerprint({...baseline, TERM: 'dumb', DISPLAY: ':1', CI_JOB_URL: 'https://example.invalid/job'}), original);
  assert.equal(nativeEnvironmentFingerprint(Object.fromEntries(Object.entries(baseline).reverse())), original);
});

test('excluding regenerated source maps preserves JS validation for Windows extension rebuilds', t => {
  const f = fixture(t), directory = path.join(f.source, 'out', 'nested'); fs.mkdirSync(directory, {recursive: true});
  const script = path.join(directory, 'extension.js'), map = `${script}.map`;
  fs.writeFileSync(script, 'compiled JavaScript'); fs.writeFileSync(map, 'first source map');
  const snapshot = snapshotPaths(f.source, ['out'], {exclude: ['out/nested/extension.js.map']});
  fs.writeFileSync(map, 'regenerated source map');
  assert.equal(snapshotMatches(f.source, snapshot), true);
  fs.unlinkSync(map);
  assert.equal(snapshotMatches(f.source, snapshot), true);
  fs.writeFileSync(script, 'changed JavaScript');
  assert.equal(snapshotMatches(f.source, snapshot), false);
});

test('a package failure retries only packaging and artifact tampering creates a new preserved publication', t => {
  const f = fixture(t), identity = {target: 'linux-x64', node: process.versions.node, local: true};
  fs.mkdirSync(f.source);
  let compiled = 0, packaged = 0, fail = true;
  const attempts = [], compileOutput = path.join(f.source, 'compiled.js');
  function run(resume) {
    const journal = openBuildJournal({...f, identity, resume});
    try {
      journal.step('compile', {key: 'source-v1', validate: value => hash(fs.readFileSync(compileOutput)) === value.hash}, () => {
        compiled++; fs.writeFileSync(compileOutput, 'compiled output'); return {hash: hash('compiled output')};
      });
      return journal.step('package-linux', {key: 'package-v1', deps: ['compile'], validate: value => validateArtifact(f.output, value.artifact)}, () => {
        packaged++;
        const attempt = fs.mkdtempSync(path.join(f.directory, '.output.package-attempt-')); attempts.push(attempt);
        const staged = archive(attempt);
        if (fail) fs.writeFileSync(`${staged}.sha256`, 'invalid checksum');
        const artifact = publishArchive(staged, f.output, 'linux-native');
        fs.rmSync(attempt, {recursive: true});
        return {artifact, checks: ['package-linux', 'linux-packaged-cli-version']};
      });
    } finally { journal.close(); }
  }
  assert.throws(() => run(false), /Build stage package-linux failed: Package checksum mismatch/);
  assert.equal(compiled, 1); assert.equal(packaged, 1);
  assert.equal(fs.existsSync(attempts[0]), true);
  const state = JSON.parse(fs.readFileSync(path.join(f.output, buildStateFilename), 'utf8'));
  assert.equal(state.steps.compile.status, 'passed'); assert.equal(state.steps['package-linux'].status, 'failed');
  fail = false;
  const first = run(true);
  assert.equal(compiled, 1); assert.equal(packaged, 2);
  assert.equal(fs.existsSync(attempts[0]), true);
  assert.equal(fs.existsSync(attempts[1]), false);
  assert.deepEqual(run(true), first);
  assert.equal(compiled, 1); assert.equal(packaged, 2);
  const previous = path.join(f.output, first.artifact.file); fs.writeFileSync(previous, 'preserve these changed bytes');
  const next = run(true);
  assert.equal(compiled, 1); assert.equal(packaged, 3);
  assert.notEqual(next.artifact.file, first.artifact.file);
  assert.equal(fs.readFileSync(previous, 'utf8'), 'preserve these changed bytes');
  assert.equal(validateArtifact(f.output, next.artifact), true);
});
