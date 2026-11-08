import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { prepare } from '../../ide/code-oss/manage.mjs';
import { policyHelperSources, policySources } from '../../ide/code-oss/extension-install-policy.mjs';
import { dependencyPaths, inputFingerprint, runtimeCoreFingerprint, snapshotPaths } from './build-inputs.mjs';
import { builtExtensionPaths } from './build-runtime.mjs';
import { buildStateFilename } from './build-resume.mjs';
import { coreConfigurationPaths, quickRuntimeFilename, requiredBaselineStages, validateBaseline } from './windows-quick-baseline.mjs';

const actualRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const write = (file, value) => { fs.mkdirSync(path.dirname(file), {recursive: true}); fs.writeFileSync(file, typeof value === 'string' ? value : `${JSON.stringify(value, null, 2)}\n`); };
const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const git = (directory, ...args) => execFileSync('git', ['-C', directory, ...args], {encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe']}).trim();
const commit = directory => {
  git(directory, 'add', '.');
  git(directory, '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '-qm', 'synthetic fixture');
  return git(directory, 'rev-parse', 'HEAD');
};

function pristinePolicyFixtures() {
  // Collect only pristine anchor strings, then exercise the real generator on
  // them. No checkout, download or compiler is needed for baseline-state tests.
  const source = policySources.toString(), start = source.indexOf('  function edit('), end = source.indexOf('  function importBefore');
  assert.ok(start > 0 && end > start);
  const recording = source.slice(0, start) + `  function edit(file, before) {
    const previous = raw.get(file) || '// synthetic upstream\\n';
    if (!previous.includes(before)) raw.set(file, previous + '\\n' + before + '\\n');
  }\n` + source.slice(end);
  const raw = new Map();
  new Function('policyHelperSources', 'raw', `return (${recording})(() => '');`)(policyHelperSources, raw);
  policySources(file => raw.get(file));
  return raw;
}

const dependencies = {
  'extension-dependencies': [], 'extension-compile': ['extension-dependencies'],
  'extension-platform-tests': ['extension-compile'], 'launcher-and-policy-tests': [],
  'upstream-commit-and-node': [], 'managed-node-build-compatibility': ['upstream-commit-and-node'],
  'windows-electron-header-preinstall': ['managed-node-build-compatibility'],
  'native-code-oss-dependencies': ['managed-node-build-compatibility', 'windows-electron-header-preinstall'],
  'teacher-only-policy-overlay': ['extension-platform-tests', 'native-code-oss-dependencies'],
  'generate-api-proposal-names': ['teacher-only-policy-overlay'], 'generate-extension-point-names': ['generate-api-proposal-names'],
  'code-oss-source-typecheck': ['generate-extension-point-names'], 'code-oss-client-compile': ['code-oss-source-typecheck'],
  'code-oss-extension-compile': ['code-oss-client-compile'], 'code-oss-extension-media': ['code-oss-extension-compile'],
  'code-oss-monaco-typecheck': ['code-oss-extension-media'], 'native-electron-download': ['native-code-oss-dependencies'],
  'overlay-verification': ['code-oss-monaco-typecheck', 'native-electron-download'],
  'package-windows': ['overlay-verification', 'launcher-and-policy-tests'],
};

function fixture(t, {dirty = false, coreFingerprint = false} = {}) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ag-quick-baseline-')));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const repository = path.join(root, 'repository'), source = path.join(root, 'source'), buildOutput = path.join(root, 'completed build');
  for (const directory of [repository, source, buildOutput]) fs.mkdirSync(directory);
  for (const relative of coreConfigurationPaths) fs.cpSync(path.join(actualRoot, relative), path.join(repository, relative), {recursive: true});
  const extensionRoot = path.join(repository, 'extensions/vscode'), profileRoot = path.join(repository, 'ide/code-oss/profile');
  write(path.join(extensionRoot, 'package.json'), {publisher: 'autograde', name: 'autograde-vscode', version: '0.7.0', main: './out/extension.js'});
  write(path.join(extensionRoot, 'package-lock.json'), {lockfileVersion: 3, packages: {}});
  write(path.join(extensionRoot, 'tsconfig.json'), {});
  write(path.join(extensionRoot, 'src/extension.ts'), 'export const version = 1;');
  write(path.join(extensionRoot, 'out/extension.js'), 'exports.version = 1;');
  write(path.join(extensionRoot, 'media/icon.svg'), '<svg/>');
  write(path.join(repository, 'scripts/ide/pipeline.mjs'), '// synthetic pipeline');
  git(repository, 'init', '-q'); const sourceRevision = commit(repository);

  write(path.join(source, '.gitignore'), 'node_modules/\nout/\ndist/\nmedia/\n.build/\n');
  write(path.join(source, 'package.json'), {version: '1.143.0'});
  write(path.join(source, 'product.json'), {nameShort: 'Code - OSS', applicationName: 'code-oss'});
  write(path.join(source, '.nvmrc'), `${process.versions.node}\n`);
  write(path.join(source, 'LICENSE.txt'), 'Synthetic license');
  write(path.join(source, 'extensions/existing/package.json'), {name: 'existing'});
  write(path.join(source, 'extensions/existing/package-lock.json'), {});
  write(path.join(source, 'resources/core.txt'), 'resource bytes');
  for (const [file, bytes] of pristinePolicyFixtures()) write(path.join(source, file), bytes);
  git(source, 'init', '-q');
  const pin = {repository: 'https://github.com/microsoft/vscode.git', tag: '1.143.0', commit: commit(source), node: process.versions.node};
  const overlay = () => prepare(source, {upstream: pin, extensionRoot, profileRoot, compile: () => {}});
  overlay();
  for (const [relative, bytes] of Object.entries({
    'out/main.js': 'compiled editor', 'extensions/existing/out/main.js': 'compiled builtin',
    'extensions/existing/media/bundle.js': 'compiled media', 'node_modules/native/index.node': 'native fixture',
    'extensions/existing/node_modules/nested/index.js': 'builtin dependency',
    '.build/typings/electron.d.ts': 'electron declarations', '.build/electron/Autograde IDE.exe': 'electron fixture',
  })) write(path.join(source, relative), bytes);
  const marker = read(path.join(source, '.autograde-ide-overlay.json'));
  const policyOutputs = Object.keys(marker.policySources).map(file => file.replace(/^src\//, 'out/').replace(/\.ts$/, '.js'));
  for (const file of policyOutputs) write(path.join(source, file), '// separately emitted managed policy');
  const artifactBytes = 'synthetic archived Windows package';
  const artifact = {target: 'windows-native', file: 'Autograde-IDE-0.7.0-local-windows-native-x64.zip', bytes: artifactBytes.length, sha256: hash(artifactBytes)};
  write(path.join(buildOutput, artifact.file), artifactBytes);
  write(path.join(buildOutput, `${artifact.file}.sha256`), `${artifact.sha256}  ${artifact.file}\n`);
  const host = {hostname: 'fixture-host', platform: 'win32', architecture: 'x64', node: process.versions.node};
  const report = {schema: 1, buildTarget: 'windows-x64', status: 'local-build', buildMode: 'local', sourceRevision, sourceDirty: dirty,
    includeWsl: false, extensionVersion: '0.7.0', upstream: pin, host,
    buildInputs: {ide: inputFingerprint(repository, ['ide/code-oss']), pipeline: inputFingerprint(repository, ['scripts/ide'])},
    acceptance: {productionReady: false, institution: 'not-tested', windowsWsl2: 'not-tested'},
    checks: [...requiredBaselineStages, 'windows-packaged-cli-version', 'windows-native-job-acl-msvc-checks'].map(name => ({name, result: 'passed'})), artifacts: [artifact]};
  if (coreFingerprint) report.buildInputs.runtimeCore = runtimeCoreFingerprint(repository);
  const state = {schema: 1, source, output: buildOutput, identity: {...host, target: 'windows-x64', upstream: pin, local: true, includeWsl: false}, steps: {}};
  for (const name of requiredBaselineStages) state.steps[name] = {status: 'passed', signature: hash(name), token: `token-${name}`,
    dependencies: Object.fromEntries(dependencies[name].map(dependency => [dependency, `token-${dependency}`])), value: {snapshot: null}};
  const snapshot = (stage, paths, exclude = []) => {state.steps[stage].value.snapshot = snapshotPaths(source, paths, {exclude});};
  snapshot('native-code-oss-dependencies', [...dependencyPaths(source), '.build/typings/electron.d.ts']);
  snapshot('code-oss-client-compile', ['out'], policyOutputs);
  snapshot('code-oss-extension-compile', builtExtensionPaths(source));
  snapshot('code-oss-extension-media', ['extensions/existing/media', 'extensions/autograde/media']);
  snapshot('native-electron-download', ['.build/electron']);
  state.steps['package-windows'].value = {artifact, checks: ['package-windows', 'windows-packaged-cli-version', 'windows-native-job-acl-msvc-checks']};
  const reportFile = path.join(buildOutput, 'build-windows-x64.json'), stateFile = path.join(buildOutput, buildStateFilename);
  const save = () => {write(reportFile, report); write(stateFile, state);}; save();
  const options = {repository, source, buildOutput, pin, host};
  return {root, repository, source, buildOutput, extensionRoot, overlay, report, state, reportFile, stateFile, artifact, save, options,
    validate: () => validateBaseline(options)};
}

test('full baseline permits repeated extension and launcher repackaging while retaining authoritative evidence', t => {
  const f = fixture(t), originalReport = fs.readFileSync(f.reportFile), originalState = fs.readFileSync(f.stateFile);
  const first = f.validate(); first.verifyCore();
  assert.equal(first.baseline.reportSha256, hash(originalReport));
  for (const version of [2, 3]) {
    write(path.join(f.extensionRoot, 'src/extension.ts'), `export const version = ${version};`);
    write(path.join(f.extensionRoot, 'out/extension.js'), `exports.version = ${version};`);
    write(path.join(f.repository, 'ide/code-oss/windows/NativeLauncher.cs'), `// launcher version ${version}`);
    f.overlay(); first.verifyCore(); f.validate().verifyCore();
  }
  assert.deepEqual(fs.readFileSync(f.reportFile), originalReport);
  assert.deepEqual(fs.readFileSync(f.stateFile), originalState);
  const proof = read(path.join(f.buildOutput, quickRuntimeFilename));
  assert.ok(proof.snapshot.exclude.includes('extensions/autograde'));
  assert.ok(proof.snapshot.exclude.includes('extensions/autograde-ide-profile'));
  assert.ok(proof.snapshot.paths.includes('extensions'));
  assert.equal(proof.snapshot.paths.includes('extensions/existing/node_modules'), false, 'covered descendants are hashed once');
  fs.appendFileSync(path.join(f.source, 'extensions/existing/node_modules/nested/index.js'), 'changed');
  assert.throws(first.verifyCore, /runtime changed/, 'parent snapshot still protects nested dependencies');
});

for (const relative of ['out/main.js', 'extensions/existing/out/main.js', 'extensions/existing/media/bundle.js',
  'node_modules/native/index.node', 'extensions/existing/node_modules/nested/index.js',
  '.build/typings/electron.d.ts', '.build/electron/Autograde IDE.exe']) {
  test(`first baseline rejects changed ${relative}`, t => {
    const f = fixture(t); fs.appendFileSync(path.join(f.source, relative), 'changed');
    assert.throws(f.validate, /runtime output changed/);
    assert.equal(fs.existsSync(path.join(f.buildOutput, quickRuntimeFilename)), false);
  });
}

test('cached proof detects changed and newly introduced runtime files after an extension refresh', t => {
  const f = fixture(t), verified = f.validate();
  for (const relative of ['out/inserted.js', 'extensions/existing/out/extra.js', 'extensions/existing/another-output/file.js', '.build/electron/extra.dll']) {
    write(path.join(f.source, relative), 'unexpected runtime');
    assert.throws(verified.verifyCore, /runtime changed|untracked upstream/);
    assert.throws(f.validate, /runtime changed|untracked upstream/);
    fs.unlinkSync(path.join(f.source, relative));
    // Remove the new directories too: directory entries are part of the proof.
    if (relative.includes('another-output/')) fs.rmdirSync(path.dirname(path.join(f.source, relative)));
    verified.verifyCore();
  }
});

for (const mutation of ['failed-stage', 'missing-check', 'stale-token', 'missing-edge', 'wrong-host', 'wrong-source', 'wrong-node', 'wrong-pin', 'wrong-zip']) {
  test(`invalid baseline is rejected: ${mutation}`, t => {
    const f = fixture(t);
    if (mutation === 'failed-stage') f.state.steps['code-oss-source-typecheck'].status = 'failed';
    if (mutation === 'missing-check') f.report.checks = f.report.checks.filter(item => item.name !== 'code-oss-monaco-typecheck');
    if (mutation === 'stale-token') f.state.steps['code-oss-client-compile'].dependencies['code-oss-source-typecheck'] = 'stale';
    if (mutation === 'missing-edge') f.state.steps['code-oss-client-compile'].dependencies = {};
    if (mutation === 'wrong-host') f.options.host = {...f.options.host, hostname: 'another-machine'};
    if (mutation === 'wrong-source') f.state.source = path.join(f.root, 'another-source');
    if (mutation === 'wrong-node') f.options.host = {...f.options.host, node: '0.0.0'};
    if (mutation === 'wrong-pin') f.report.upstream = {...f.report.upstream, commit: 'a'.repeat(40)};
    if (mutation === 'wrong-zip') write(path.join(f.buildOutput, f.artifact.file), 'tampered archive');
    f.save(); assert.throws(f.validate, /baseline|full-build/);
  });
}

test('clean baseline accepts unrelated code but rejects changed core configuration', t => {
  const f = fixture(t); f.validate();
  write(path.join(f.repository, 'ide/code-oss/manage.mjs'), '// new quick verifier helper');
  write(path.join(f.repository, 'scripts/ide/build-runtime.mjs'), '// unrelated packaging helper');
  f.validate();
  for (const relative of coreConfigurationPaths.filter(relative => !relative.endsWith('/profile'))) {
    const file = path.join(f.repository, relative), before = fs.readFileSync(file);
    fs.appendFileSync(file, '\n// core changed');
    assert.throws(f.validate, /core configuration changed/);
    fs.writeFileSync(file, before);
  }
  write(path.join(f.repository, 'ide/code-oss/profile/new-policy.json'), '{}');
  assert.throws(f.validate, /core configuration changed/);
});

test('dirty baseline requires exact original IDE and pipeline fingerprints', t => {
  const f = fixture(t, {dirty: true}); f.validate();
  write(path.join(f.extensionRoot, 'src/extension.ts'), 'export const unrelated = 2;'); f.validate();
  for (const relative of ['ide/code-oss/windows/new.cs', 'scripts/ide/new.mjs']) {
    write(path.join(f.repository, relative), 'new');
    assert.throws(f.validate, /Dirty baseline.*cannot be proven/);
    fs.unlinkSync(path.join(f.repository, relative));
  }
});

test('new dirty baselines permit launcher changes using the captured runtime core fingerprint', t => {
  const f = fixture(t, {dirty: true, coreFingerprint: true}); f.validate();
  write(path.join(f.repository, 'ide/code-oss/windows/NativeLauncher.cs'), '// newer launcher');
  write(path.join(f.repository, 'scripts/ide/new.mjs'), '// newer packaging');
  f.validate().verifyCore();
  write(path.join(f.repository, 'ide/code-oss/product.json'), '{}');
  assert.throws(f.validate, /core configuration changed/);
});

test('malformed runtime core fingerprint cannot fall back to looser legacy evidence', t => {
  const f = fixture(t, {coreFingerprint: true});
  f.report.buildInputs.runtimeCore = null; f.save();
  assert.throws(f.validate, /core configuration changed/);
});

test('mutated full-build evidence and proof bindings fail closed without replacing files', t => {
  const f = fixture(t), verified = f.validate(), cache = path.join(f.buildOutput, quickRuntimeFilename);
  const original = fs.readFileSync(cache), report = fs.readFileSync(f.reportFile);
  fs.appendFileSync(f.reportFile, '\n');
  assert.throws(verified.verifyCore, /evidence changed/);
  assert.throws(f.validate, /proof does not match/);
  assert.deepEqual(fs.readFileSync(cache), original);
  fs.writeFileSync(f.reportFile, report);
  const proof = read(cache); proof.baseline.checkpointSha256 = '0'.repeat(64); write(cache, proof);
  assert.throws(f.validate, /proof does not match/);
});

test('missing checkpoint and hard-linked proof are never adopted', t => {
  const f = fixture(t); f.validate();
  const cache = path.join(f.buildOutput, quickRuntimeFilename), alias = path.join(f.root, 'proof-alias');
  fs.linkSync(cache, alias); assert.throws(f.validate, /Invalid baseline file/); fs.unlinkSync(alias);
  fs.unlinkSync(f.stateFile); assert.throws(f.validate, /required baseline file.*full Windows build/);
});

test('dangling proof symlink is rejected without replacing the directory entry', {skip: process.platform === 'win32'}, t => {
  const f = fixture(t), cache = path.join(f.buildOutput, quickRuntimeFilename), missing = path.join(f.root, 'absent');
  fs.symlinkSync(missing, cache);
  assert.throws(f.validate, /Invalid baseline file/);
  assert.equal(fs.readlinkSync(cache), missing);
});
