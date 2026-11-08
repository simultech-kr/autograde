import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { parseOptions, publishArchive, validateArtifact, builtExtensionPaths, nativeEnvironmentFingerprint, preserveQuickRuntimeProof, runtimeWslPolicy } from './build-runtime.mjs';
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

test('resume defaults false while Linux WSL packaging is mandatory with or without the legacy flag', () => {
  assert.deepEqual(parseOptions(cli), {target: 'linux-x64', source: '/build/source', output: '/build/output', includeWsl: true, local: false, resume: false});
  for (const resume of [false, true]) for (const local of [false, true]) for (const includeWsl of [false, true]) {
    const flags = [...(resume ? ['--resume'] : []), ...(local ? ['--local'] : []), ...(includeWsl ? ['--include-wsl'] : [])];
    const options = parseOptions([...flags, ...cli]);
    assert.deepEqual({resume: options.resume, local: options.local, includeWsl: options.includeWsl}, {resume, local, includeWsl:true});
  }
  for (const flag of ['--resume', '--local', '--include-wsl']) assert.throws(() => parseOptions([...cli, flag, flag]), /Usage/);
  for (const extra of [['--resume', 'false'], ['--resume=true'], ['--unknown'], ['--source', '/other'], ['--output'], ['--resume', '--resume']]) {
    assert.throws(() => parseOptions([...cli, ...extra]), /Usage/);
  }
});

test('new Linux builds include WSL without making other native targets produce Linux payloads', t => {
  const f = fixture(t);
  for (const target of ['linux-x64', 'windows-x64', 'macos-arm64']) {
    assert.deepEqual(runtimeWslPolicy({target, output:f.output}), {
      includeWsl:target === 'linux-x64', checkpointIncludeWsl:target === 'linux-x64',
    });
    const args = ['--target', target, '--source', f.source, '--output', f.output];
    assert.deepEqual(parseOptions([...args, '--include-wsl']), parseOptions(args));
  }
  assert.equal(fs.existsSync(f.output), false);
});

for (const legacyIncludeWsl of [false, true]) {
  test(`mandatory WSL preserves historical checkpoint identity and compiler successes (${legacyIncludeWsl})`, t => {
    const f = fixture(t), identity = {target:'linux-x64', node:'26.13.0', local:true, includeWsl:legacyIncludeWsl};
    fs.mkdirSync(f.source);
    let compiled = 0, packaged = 0;
    const original = openBuildJournal({...f, identity});
    original.step('compile', {key:'source-v1'}, () => {compiled++; return 'verified-core';});
    original.close();
    const stateFile = path.join(f.output, buildStateFilename), before = fs.readFileSync(stateFile, 'utf8');
    const policy = runtimeWslPolicy({target:'linux-x64', output:f.output, resume:true});
    assert.deepEqual(policy, {includeWsl:true, checkpointIncludeWsl:legacyIncludeWsl});
    assert.equal(fs.readFileSync(stateFile, 'utf8'), before);
    const resumed = openBuildJournal({...f, identity:{...identity, includeWsl:policy.checkpointIncludeWsl}, resume:true});
    try {
      assert.equal(resumed.step('compile', {key:'source-v1'}, () => {compiled++;}), 'verified-core');
      resumed.step('package-wsl', {key:'wsl-v1', deps:['compile']}, () => {packaged++;});
    } finally {resumed.close();}
    assert.equal(compiled, 1);
    assert.equal(packaged, 1);
    assert.equal(JSON.parse(fs.readFileSync(stateFile, 'utf8')).identity.includeWsl, legacyIncludeWsl);
  });
}

test('historical WSL selection never authorizes a different source or compiler identity', t => {
  const f = fixture(t), identity = {target:'linux-x64', node:'26.13.0', local:true, includeWsl:false};
  const journal = openBuildJournal({...f, identity}); journal.close();
  const stateFile = path.join(f.output, buildStateFilename), before = fs.readFileSync(stateFile, 'utf8');
  const policy = runtimeWslPolicy({target:'linux-x64', output:f.output, resume:true});
  const restored = {...identity, includeWsl:policy.checkpointIncludeWsl};
  for (const change of [{identity:{...restored, node:'99.0.0'}}, {identity:{...restored, local:false}},
    {identity:restored, source:path.join(f.directory, 'different-source')}]) {
    assert.throws(() => openBuildJournal({...f, identity:restored, resume:true, ...change}), /identity|paths/);
    assert.equal(fs.readFileSync(stateFile, 'utf8'), before);
  }
  const corrupt = JSON.parse(before); corrupt.schema = 999;
  fs.writeFileSync(stateFile, JSON.stringify(corrupt));
  const invalidPolicy = runtimeWslPolicy({target:'linux-x64', output:f.output, resume:true});
  assert.throws(() => openBuildJournal({...f, identity:{...identity, includeWsl:invalidPolicy.checkpointIncludeWsl}, resume:true}), /Invalid build checkpoint/);
  assert.equal(fs.readFileSync(stateFile, 'utf8'), JSON.stringify(corrupt));
});

test('WSL identity compatibility does not follow linked checkpoint files', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  const checkpoint = path.join(f.output, buildStateFilename), outside = path.join(f.directory, 'outside.json');
  fs.writeFileSync(outside, JSON.stringify({identity:{target:'linux-x64', includeWsl:false}}));
  fs.linkSync(outside, checkpoint);
  assert.throws(() => runtimeWslPolicy({target:'linux-x64', output:f.output, resume:true}), /regular build file/);
  assert.equal(JSON.parse(fs.readFileSync(outside, 'utf8')).identity.includeWsl, false);
  const alias = path.join(f.directory, 'output-alias');
  fs.symlinkSync(f.output, alias, process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => runtimeWslPolicy({target:'linux-x64', output:alias, resume:true}), /regular directory/);
});

test('a completed full build preserves old quick proofs before publishing its new evidence', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  assert.equal(preserveQuickRuntimeProof(f.output), undefined);
  const proof = path.join(f.output, '.autograde-quick-runtime.json');
  const originals = [];
  for (const contents of ['{"schema":1,"old":"evidence"}', 'corrupt previous proof']) {
    fs.writeFileSync(proof, contents);
    const backup = preserveQuickRuntimeProof(f.output);
    assert.match(path.basename(backup), /^\.previous-quick-runtime-[a-f0-9-]+\.json$/);
    assert.equal(fs.readFileSync(backup, 'utf8'), contents);
    assert.equal(fs.existsSync(proof), false);
    originals.push(backup);
  }
  assert.notEqual(originals[0], originals[1]);
  fs.linkSync(originals[0], proof);
  assert.throws(() => preserveQuickRuntimeProof(f.output), /regular build file/);
  assert.equal(fs.readFileSync(proof, 'utf8'), fs.readFileSync(originals[0], 'utf8'));
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

for (const legacy of [false, true]) test(`a package failure retries only packaging and artifact tampering creates a new preserved publication (legacy output: ${legacy})`, t => {
  const f = fixture(t), identity = {target: 'linux-x64', node: process.versions.node, local: true};
  fs.mkdirSync(f.source);
  if (legacy) {
    fs.mkdirSync(f.output);
    fs.writeFileSync(path.join(f.output, 'build-linux-x64.json'), 'an unverified report from the previous builder');
    fs.writeFileSync(path.join(f.output, 'old.zip'), 'preserve legacy archive');
  }
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
  assert.throws(() => run(legacy), /Build stage package-linux failed: Package checksum mismatch/);
  assert.equal(compiled, 1); assert.equal(packaged, 1);
  assert.equal(fs.existsSync(attempts[0]), true);
  const state = JSON.parse(fs.readFileSync(path.join(f.output, buildStateFilename), 'utf8'));
  assert.equal(state.steps.compile.status, 'passed'); assert.equal(state.steps['package-linux'].status, 'failed');
  if (legacy) {
    assert.equal(fs.readFileSync(path.join(state.legacyOutput.backup, 'old.zip'), 'utf8'), 'preserve legacy archive');
    assert.equal(fs.readFileSync(path.join(state.legacyOutput.backup, 'build-linux-x64.json'), 'utf8'), 'an unverified report from the previous builder');
    assert.equal(fs.existsSync(path.join(f.output, 'build-linux-x64.json')), false);
  }
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
