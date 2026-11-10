import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { buildStateFilename } from './build-resume.mjs';
import { requiredBaselineStages } from './windows-quick-baseline.mjs';
import { discoverWindowsBuilds, newQuickOutput } from './windows-quick-discovery.mjs';

const write = (file, value) => {
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(file, typeof value === 'string' ? value : `${JSON.stringify(value)}\n`);
};
const host = {hostname: 'same-windows-pc', platform: 'win32', architecture: 'x64', node: '26.13.0'};
const pin = {repository: 'https://github.com/microsoft/vscode.git', tag: '1.143.0', commit: 'a'.repeat(40), node: host.node};

function fixture(t) {
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(os.tmpdir(), 'ag-quick-discovery-')));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const repository = path.join(root, 'repository');
  fs.mkdirSync(repository);
  const options = {repository, host, pin};
  const make = (relative = 'dist/completed', {sourceName = 'source', time = 1700000000000, candidate = false} = {}) => {
    const source = path.join(root, sourceName), buildOutput = path.join(repository, relative);
    fs.mkdirSync(source, {recursive: true}); fs.mkdirSync(buildOutput, {recursive: true});
    const report = {schema: 1, status: candidate ? 'release-candidate' : 'local-build', buildMode: candidate ? 'candidate' : 'local',
      buildTarget: 'windows-x64', includeWsl: false, sourceRevision: 'b'.repeat(40), sourceDirty: false, extensionVersion: '0.7.0', upstream: pin,
      host: {platform: host.platform, architecture: host.architecture, node: host.node},
      acceptance: {productionReady: false, institution: 'not-tested', windowsWsl2: 'not-tested'},
      checks: [...requiredBaselineStages, 'windows-packaged-cli-version', 'windows-native-job-acl-msvc-checks'].map(name => ({name, result: 'passed'}))};
    const state = {schema: 1, source, output: buildOutput,
      identity: {...host, target: 'windows-x64', upstream: pin, local: !candidate, includeWsl: false},
      steps: Object.fromEntries(requiredBaselineStages.map(name => [name, {status: 'passed', signature: 'c'.repeat(64), token: `token-${name}`, dependencies: {}, value: {}}]))};
    const reportFile = path.join(buildOutput, 'build-windows-x64.json'), stateFile = path.join(buildOutput, buildStateFilename);
    const save = () => {write(reportFile, report); write(stateFile, state); fs.utimesSync(reportFile, new Date(time), new Date(time));};
    save();
    return {source, buildOutput, report, state, reportFile, stateFile, save, completedAt: time};
  };
  return {root, repository, options, make, discover: (overrides = {}) => discoverWindowsBuilds({...options, ...overrides})};
}

function snapshot(directory) {
  const found = [];
  function walk(current) {
    for (const entry of fs.readdirSync(current, {withFileTypes: true}).sort((left, right) => left.name.localeCompare(right.name))) {
      const file = path.join(current, entry.name), info = fs.lstatSync(file);
      found.push([path.relative(directory, file), info.mode, info.size, info.mtimeMs,
        info.isSymbolicLink() ? fs.readlinkSync(file) : info.isFile() ? fs.readFileSync(file).toString('base64') : null]);
      if (info.isDirectory() && !info.isSymbolicLink()) walk(file);
    }
  }
  walk(directory);
  return found;
}

test('discovery orders completed full builds by report mtime with stable path ties and makes no changes', t => {
  const f = fixture(t), older = f.make('dist/older'), newest = f.make('build/newest', {time: 1800000000000}),
    tiedB = f.make('.cache/b', {time: 1750000000000}), tiedA = f.make('.cache/a', {time: 1750000000000, candidate: true});
  fs.utimesSync(older.stateFile, new Date(1900000000000), new Date(1900000000000));
  const before = snapshot(f.root), result = f.discover();
  assert.deepEqual(result.candidates, [newest, tiedA, tiedB, older].map(({source, buildOutput, completedAt}) => ({source, buildOutput, completedAt})));
  assert.deepEqual(result.rejected, []);
  assert.deepEqual(snapshot(f.root), before, 'no proof, artifact, lock, or directory is created or modified');
});

test('scan includes root records and records at the depth limit without reading artifacts', t => {
  const f = fixture(t), root = f.make('dist'), nested = f.make('build/group/year/completed', {time: 1800000000000});
  assert.deepEqual(f.discover().candidates.map(value => value.buildOutput), [nested.buildOutput, root.buildOutput]);
});

test('scan stops at completed or interrupted builds instead of searching their payloads', t => {
  const f = fixture(t), completed = f.make('dist/completed'), interrupted = f.make('dist/interrupted');
  const nested = f.make('dist/completed/payload/nested', {time: 1800000000000});
  f.make('dist/interrupted/payload/nested', {time: 1800000000000});
  fs.unlinkSync(interrupted.reportFile);
  const result = f.discover();
  assert.deepEqual(result.candidates.map(value => value.buildOutput), [completed.buildOutput]);
  assert.deepEqual(result.rejected.map(value => value.directory), [interrupted.buildOutput]);
  assert.equal(f.discover({buildOutput: nested.buildOutput}).candidates[0].buildOutput, nested.buildOutput);
});

const invalidRecords = [
  ['failed report check', f => {f.report.checks[0].result = 'failed';}, /failed.*checks/],
  ['missing final CLI check', f => {f.report.checks = f.report.checks.filter(check => check.name !== 'windows-packaged-cli-version');}, /Missing successful.*cli-version/],
  ['duplicate report check', f => {f.report.checks.push(f.report.checks[0]);}, /Invalid.*checks/],
  ['failed checkpoint', f => {f.state.steps['code-oss-client-compile'].status = 'failed';}, /incomplete.*client-compile/],
  ['running checkpoint', f => {f.state.steps['package-windows'].status = 'running';}, /incomplete.*package-windows/],
  ['missing checkpoint', f => {delete f.state.steps['native-electron-download'];}, /incomplete.*native-electron-download/],
  ['another PC', f => {f.state.identity.hostname = 'other-pc';}, /different PC/],
  ['conflicting report PC', f => {f.report.host.hostname = 'other-pc';}, /different PC/],
  ['different Node', f => {f.state.identity.node = '24.18.0';}, /Node version/],
  ['different report Node', f => {f.report.host.node = '24.18.0';}, /Node version/],
  ['different architecture', f => {f.state.identity.architecture = 'arm64';}, /architecture/],
  ['different platform', f => {f.report.host.platform = 'linux';}, /different PC/],
  ['different report upstream', f => {f.report.upstream = {...pin, commit: 'd'.repeat(40)};}, /upstream pin/],
  ['different state upstream', f => {f.state.identity.upstream = {...pin, sourceNode: '24.18.0'};}, /upstream pin/],
  ['different output', f => {f.state.output = path.dirname(f.buildOutput);}, /canonical directory/],
  ['relative source', f => {f.state.source = '../source';}, /absolute.*paths/],
  ['missing source directory', f => {f.state.source = path.join(f.source, 'missing');}, /real directory/],
  ['quick build masquerading as full build', f => {f.report.status = 'local-fast-build'; f.report.buildMode = 'local-fast';}, /completed.*full-build/],
  ['wrong report schema', f => {f.report.schema = 2;}, /completed.*full-build/],
  ['wrong state schema', f => {f.state.schema = 2;}, /checkpoint schema/],
  ['WSL combined build', f => {f.report.includeWsl = true;}, /completed.*full-build/],
];
for (const [name, mutate, reason] of invalidRecords) {
  test(`discovery rejects ${name} and still finds an older completed build`, t => {
    const f = fixture(t), older = f.make('dist/older'), invalid = f.make('dist/latest', {time: 1800000000000});
    mutate(invalid); invalid.save();
    const result = f.discover();
    assert.deepEqual(result.candidates.map(value => value.buildOutput), [older.buildOutput]);
    assert.equal(result.rejected.length, 1);
    assert.equal(result.rejected[0].directory, invalid.buildOutput);
    assert.match(result.rejected[0].reason, reason);
  });
}

test('explicit build output derives source from its verified checkpoint outside the scan roots', t => {
  const f = fixture(t), build = f.make('custom/completed');
  assert.deepEqual(f.discover().candidates, []);
  const result = f.discover({buildOutput: build.buildOutput});
  assert.deepEqual(result.candidates, [{source: build.source, buildOutput: build.buildOutput, completedAt: build.completedAt}]);
  build.state.identity.hostname = 'another-pc'; build.save();
  const rejected = f.discover({buildOutput: build.buildOutput});
  assert.deepEqual(rejected.candidates, []);
  assert.match(rejected.rejected[0].reason, /different PC/);
});

test('source-only discovery filters by the canonical source directory', t => {
  const f = fixture(t), selected = f.make('dist/selected'), other = f.make('dist/other', {sourceName: 'other-source', time: 1800000000000});
  const result = f.discover({source: path.join(selected.source, '..', 'source')});
  assert.deepEqual(result.candidates.map(value => value.buildOutput), [selected.buildOutput]);
  assert.deepEqual(result.rejected, [{directory: other.buildOutput, reason: 'Full-build checkpoint belongs to a different source directory'}]);
});

test('explicit source and output preserve caller-relative paths for the locked verifier without reading records', t => {
  const f = fixture(t), source = path.relative(process.cwd(), path.join(f.root, 'explicit source')),
    buildOutput = path.relative(process.cwd(), path.join(f.root, 'explicit output'));
  const before = snapshot(f.root);
  assert.deepEqual(f.discover({source, buildOutput}), {candidates: [{source: path.resolve(source), buildOutput: path.resolve(buildOutput), completedAt: null}], rejected: []});
  assert.deepEqual(snapshot(f.root), before);
});

test('discovery skips hidden staging, legacy backups, runtime trees, links, and builds beyond its depth limit', t => {
  const f = fixture(t), valid = f.make('dist/valid');
  for (const relative of ['dist/.quick-attempt-abc/output', 'dist/.autograde-legacy-output-abc/output', 'dist/.git/old',
    'dist/node_modules/old', 'dist/editor/old', 'dist/runtime/old', 'dist/group/year/day/too-deep']) f.make(relative, {time: 1800000000000});
  const outside = f.make('elsewhere/full', {time: 1900000000000});
  fs.symlinkSync(outside.buildOutput, path.join(f.repository, 'dist', 'linked-output'), 'junction');
  assert.deepEqual(f.discover().candidates.map(value => value.buildOutput), [valid.buildOutput]);
});

test('source parent aliases resolve to one source while linked directory entries and recorded aliases are rejected', t => {
  const f = fixture(t), build = f.make('dist/completed'), alias = path.join(f.root, 'parent-alias');
  fs.symlinkSync(f.root, alias, 'junction');
  assert.equal(f.discover({source: path.join(alias, 'source')}).candidates[0].source, build.source);
  assert.equal(f.discover({buildOutput: path.join(alias, 'repository/dist/completed')}).candidates[0].buildOutput, build.buildOutput);
  const directAlias = path.join(f.root, 'source-alias');
  fs.symlinkSync(build.source, directAlias, 'junction');
  const direct = f.discover({source: directAlias});
  assert.deepEqual(direct.candidates, []);
  assert.match(direct.rejected[0].reason, /symbolic link or junction/);
  build.state.source = path.join(alias, 'source'); build.save();
  assert.match(f.discover().rejected[0].reason, /canonical directory/);
});

for (const recordName of ['reportFile', 'stateFile']) {
  for (const kind of ['missing', 'malformed', 'array', 'hard-linked', 'oversized']) {
    test(`${recordName} is rejected when ${kind}`, t => {
      const f = fixture(t), build = f.make(), file = build[recordName];
      if (kind === 'missing') fs.unlinkSync(file);
      if (kind === 'malformed') fs.writeFileSync(file, '{');
      if (kind === 'array') fs.writeFileSync(file, '[]');
      if (kind === 'hard-linked') fs.linkSync(file, path.join(f.root, 'alias.json'));
      if (kind === 'oversized') fs.truncateSync(file, 32 * 1024 * 1024 + 1);
      const result = f.discover();
      assert.deepEqual(result.candidates, []);
      assert.equal(result.rejected.length, 1);
      assert.match(result.rejected[0].reason, /record|JSON/);
    });
  }
  test(`${recordName} symlink is not followed`, {skip: process.platform === 'win32'}, t => {
    const f = fixture(t), build = f.make(), file = build[recordName], target = path.join(f.root, 'target.json');
    fs.renameSync(file, target); fs.symlinkSync(target, file);
    assert.match(f.discover().rejected[0].reason, /no links/);
    assert.equal(fs.readlinkSync(file), target);
  });
}

test('missing scan roots return an empty shortlist without creating directories', t => {
  const f = fixture(t), before = snapshot(f.root);
  assert.deepEqual(f.discover(), {candidates: [], rejected: []});
  assert.deepEqual(snapshot(f.root), before);
});

const naming = {now: () => new Date(2026, 10, 9, 13, 14, 15), random: () => 'a1b2c3d4'};

test('fresh output has a timestamp and random suffix without creating dist or output', t => {
  const f = fixture(t), before = snapshot(f.root);
  assert.equal(newQuickOutput(f.repository, naming), path.join(f.repository, 'dist/windows-quick-20261109-131415-a1b2c3d4'));
  assert.deepEqual(snapshot(f.root), before);
  const outputs = new Set(Array.from({length: 16}, () => newQuickOutput(f.repository)));
  assert.equal(outputs.size, 16);
  assert.deepEqual(snapshot(f.root), before);
});

for (const kind of ['directory', 'zip', 'checksum', 'lock', 'broken-symlink']) {
  test(`fresh output retries a ${kind} collision without replacing it`, {skip: kind === 'broken-symlink' && process.platform === 'win32'}, t => {
    const f = fixture(t), first = newQuickOutput(f.repository, naming);
    fs.mkdirSync(path.dirname(first), {recursive: true});
    if (kind === 'directory') fs.mkdirSync(first);
    if (kind === 'zip') fs.writeFileSync(`${first}.zip`, 'preserved');
    if (kind === 'checksum') fs.writeFileSync(`${first}.zip.sha256`, 'preserved');
    if (kind === 'lock') fs.writeFileSync(path.join(path.dirname(first), `.${path.basename(first)}.autograde-build.lock`), 'preserved');
    if (kind === 'broken-symlink') fs.symlinkSync(path.join(f.root, 'missing'), first);
    const before = snapshot(f.root), suffixes = ['a1b2c3d4', 'deadbeef'];
    assert.equal(newQuickOutput(f.repository, {...naming, random: () => suffixes.shift()}), first.replace('a1b2c3d4', 'deadbeef'));
    assert.deepEqual(snapshot(f.root), before);
  });
}

test('output naming has bounded retries when every proposed destination collides', t => {
  const f = fixture(t), first = newQuickOutput(f.repository, naming);
  fs.mkdirSync(first, {recursive: true});
  let attempts = 0;
  assert.throws(() => newQuickOutput(f.repository, {...naming, random: () => {attempts++; return 'a1b2c3d4';}}), /128 attempts.*--output/);
  assert.equal(attempts, 128);
});
