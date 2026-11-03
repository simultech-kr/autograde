import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { openBuildJournal, buildStateFilename } from './build-resume.mjs';

const identity = { target: 'linux-x64', platform: 'linux', arch: 'x64', node: '26.13.0',
  upstream: { commit: 'a'.repeat(40) }, local: true, includeWsl: false };
const moduleUrl = new URL('./build-resume.mjs', import.meta.url).href;

function fixture(t) {
  const directory = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-build-resume-')));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  const source = path.join(directory, 'source'), output = path.join(directory, 'output');
  return { directory, source, output, identity, statePath: path.join(output, buildStateFilename) };
}
const readState = fixture => JSON.parse(fs.readFileSync(fixture.statePath, 'utf8'));
const lockPath = directory => path.join(path.dirname(directory), `.${path.basename(directory)}.autograde-build.lock`);
function open(t, fixture, options = {}) {
  const journal = openBuildJournal({ ...fixture, ...options });
  t.after(() => journal.close());
  return journal;
}
function child(fixture, body) {
  return execFileSync(process.execPath, ['--input-type=module', '-e',
    `import fs from 'node:fs'; import { openBuildJournal } from ${JSON.stringify(moduleUrl)};
     const config = ${JSON.stringify(fixture)}; ${body}`], { encoding: 'utf8' });
}

test('a failed stage persists and a separate process resumes only unfinished work', t => {
  const f = fixture(t), journal = open(t, f);
  const events = path.join(f.directory, 'events');
  const record = event => fs.appendFileSync(events, `${event}\n`);
  assert.equal(journal.step('dependencies', { key: 'lock-v1' }, () => { record('dependencies'); return { installed: true }; }).installed, true);
  assert.throws(() => journal.step('compile', { key: 'src-v1', deps: ['dependencies'] }, () => {
    assert.equal(readState(f).steps.compile.status, 'running');
    record('compile-failed'); throw new Error('compiler stopped');
  }), /compiler stopped/);
  const failed = readState(f);
  assert.equal(failed.steps.dependencies.status, 'passed');
  assert.equal(failed.steps.compile.status, 'failed');
  assert.equal(failed.steps.compile.error, 'compiler stopped');
  assert.equal(failed.steps.compile.token, undefined);
  journal.close();
  const stdout = child(f, `
    const journal = openBuildJournal({ ...config, resume: true });
    try {
      const value = journal.step('dependencies', { key: 'lock-v1', validate: value => value.installed }, () => { throw new Error('should skip'); });
      if (!value.installed) throw new Error('missing previous value');
      journal.step('compile', { key: 'src-v1', deps: ['dependencies'] }, () => fs.appendFileSync(${JSON.stringify(events)}, 'compile-passed\\n'));
    } finally { journal.close(); }
  `);
  assert.match(stdout, /Skipping passed stage: dependencies/);
  assert.equal(fs.readFileSync(events, 'utf8'), 'dependencies\ncompile-failed\ncompile-passed\n');
  assert.equal(readState(f).steps.compile.status, 'passed');
  assert.equal(fs.existsSync(lockPath(f.source)), false);
  assert.equal(fs.existsSync(lockPath(f.output)), false);
});

test('changed inputs invalidate the stage and its dependents while other stages remain reusable', t => {
  const f = fixture(t), first = open(t, f);
  for (const [name, deps] of [['install', []], ['compile', ['install']], ['independent', []]]) {
    first.step(name, { key: { version: 1 }, deps }, () => name);
  }
  const before = readState(f); first.close();
  const second = open(t, f, { resume: true }), ran = [];
  second.step('install', { key: { version: 2 } }, () => { ran.push('install'); });
  second.step('compile', { key: { version: 1 }, deps: ['install'] }, () => { ran.push('compile'); });
  second.step('independent', { key: { version: 1 } }, () => { throw new Error('should skip'); });
  const after = readState(f);
  assert.deepEqual(ran, ['install', 'compile']);
  assert.notEqual(before.steps.install.token, after.steps.install.token);
  assert.notEqual(before.steps.compile.token, after.steps.compile.token);
  assert.equal(before.steps.independent.token, after.steps.independent.token);
});

test('missing or invalid artifacts invalidate passed stages and their dependents', t => {
  const f = fixture(t), first = open(t, f), artifact = path.join(f.output, 'artifact');
  first.step('package', { key: 'same' }, () => { fs.writeFileSync(artifact, 'valid'); return artifact; });
  first.step('verify', { key: 'same', deps: ['package'] }, () => {});
  const token = readState(f).steps.verify.token;
  first.close(); fs.unlinkSync(artifact);
  const second = open(t, f, { resume: true });
  second.step('package', { key: 'same', validate: file => fs.readFileSync(file, 'utf8') === 'valid' }, () => {
    fs.writeFileSync(artifact, 'valid'); return artifact;
  });
  second.step('verify', { key: 'same', deps: ['package'] }, () => {});
  assert.notEqual(readState(f).steps.verify.token, token);
});

test('a killed process leaves a running checkpoint and dead local locks that are safely retried', t => {
  const f = fixture(t);
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', `
    import { openBuildJournal } from ${JSON.stringify(moduleUrl)};
    const journal = openBuildJournal(${JSON.stringify(f)});
    journal.step('compile', { key: 'v1' }, () => process.exit(42));
  `], { encoding: 'utf8' });
  assert.equal(result.status, 42, result.stderr);
  assert.equal(readState(f).steps.compile.status, 'running');
  assert.equal(fs.existsSync(lockPath(f.source)), true);
  const resumed = open(t, f, { resume: true });
  let called = false;
  resumed.step('compile', { key: 'v1' }, () => { called = true; });
  assert.equal(called, true);
  assert.equal(readState(f).steps.compile.status, 'passed');
});

test('source and output locks prevent concurrent builds and are both released', t => {
  const f = fixture(t), first = open(t, f);
  const before = fs.readFileSync(f.statePath, 'utf8');
  assert.throws(() => openBuildJournal({ ...f, resume: true }), /locked by process/);
  assert.throws(() => openBuildJournal({ ...f, output: path.join(f.directory, 'other-output') }), /locked by process/);
  assert.throws(() => openBuildJournal({ ...f, source: path.join(f.directory, 'other-source'), resume: true }), /locked by process/);
  assert.equal(fs.readFileSync(f.statePath, 'utf8'), before);
  assert.equal(fs.existsSync(lockPath(path.join(f.directory, 'other-source'))), false);
  assert.equal(fs.existsSync(lockPath(path.join(f.directory, 'other-output'))), false);
  first.close(); first.close();
  const second = open(t, f, { resume: true });
  assert.equal(second.resumed, true);
});

test('a second process cannot run against an active build', t => {
  const f = fixture(t); open(t, f);
  const result = child(f, `
    try { openBuildJournal({ ...config, resume: true }); process.exit(2); }
    catch (error) { if (!/locked by process/.test(error.message)) throw error; }
  `);
  assert.equal(result, '');
});

test('locks with unknown hosts or malformed owner data are preserved', t => {
  const f = fixture(t), file = lockPath(f.source);
  for (const contents of [JSON.stringify({ schema: 1, hostname: `${os.hostname()}-elsewhere`, pid: 999999 }), '{bad']) {
    fs.writeFileSync(file, contents);
    assert.throws(() => openBuildJournal(f), /lock owner cannot be verified/);
    assert.equal(fs.readFileSync(file, 'utf8'), contents);
    assert.equal(fs.existsSync(lockPath(f.output)), false);
    fs.unlinkSync(file);
  }
});

test('identity mismatch never rewrites the existing checkpoint or removes artifacts', t => {
  const f = fixture(t), first = open(t, f);
  first.step('install', { key: 'v1' }, () => {}); first.close();
  const artifact = path.join(f.output, 'keep.txt'); fs.writeFileSync(artifact, 'keep');
  const before = fs.readFileSync(f.statePath, 'utf8');
  for (const change of [{ node: '99.0.0' }, { local: false }, { includeWsl: true }, { upstream: { commit: 'b'.repeat(40) } }]) {
    assert.throws(() => openBuildJournal({ ...f, resume: true, identity: { ...identity, ...change } }), /identity/);
    assert.equal(fs.readFileSync(f.statePath, 'utf8'), before);
    assert.equal(fs.readFileSync(artifact, 'utf8'), 'keep');
  }
  assert.throws(() => openBuildJournal({ ...f, resume: true, source: path.join(f.directory, 'other-source') }), /paths/);
});

test('corrupt or structurally invalid journals are rejected without rewriting', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  for (const contents of ['{broken', JSON.stringify({ schema: 2 }), JSON.stringify({ schema: 1, source: f.source, output: f.output, identity,
    steps: { compile: { status: 'passed', signature: 'a'.repeat(64), dependencies: {} } } })]) {
    fs.writeFileSync(f.statePath, contents);
    assert.throws(() => openBuildJournal({ ...f, resume: true }), /checkpoint/);
    assert.equal(fs.readFileSync(f.statePath, 'utf8'), contents);
    assert.equal(fs.existsSync(lockPath(f.source)), false);
    assert.equal(fs.existsSync(lockPath(f.output)), false);
  }
});

test('resume preserves legacy output in a sibling backup without adopting prior successes', t => {
  const f = fixture(t); fs.mkdirSync(f.source); fs.mkdirSync(f.output);
  const sourceFile = path.join(f.source, 'unfinished-work'); fs.writeFileSync(sourceFile, 'source work');
  const outputFile = path.join(f.output, 'build-linux-x64.json'); fs.writeFileSync(outputFile, '{untrusted old report');
  fs.mkdirSync(path.join(f.output, 'partial package'));
  const payload = Buffer.from([0, 255, 1, 128]);
  fs.writeFileSync(path.join(f.output, 'partial package', '기존 파일.zip'), payload);
  assert.throws(() => openBuildJournal(f), /nonempty.*--resume/);
  assert.equal(fs.existsSync(f.statePath), false);
  const journal = open(t, f, {resume: true}), state = readState(f);
  assert.equal(journal.resumed, false);
  assert.deepEqual(state.steps, {});
  assert.equal(path.dirname(path.dirname(state.legacyOutput.backup)), f.directory);
  assert.notEqual(path.dirname(state.legacyOutput.backup), f.output);
  assert.deepEqual(fs.readdirSync(f.output), [buildStateFilename]);
  assert.equal(fs.readFileSync(sourceFile, 'utf8'), 'source work');
  assert.equal(fs.readFileSync(path.join(state.legacyOutput.backup, path.basename(outputFile)), 'utf8'), '{untrusted old report');
  assert.deepEqual(fs.readFileSync(path.join(state.legacyOutput.backup, 'partial package', '기존 파일.zip')), payload);
  let runs = 0;
  journal.step('compile', {key: 'same'}, () => { runs++; });
  journal.close();
  const second = open(t, f, {resume: true});
  second.step('compile', {key: 'same'}, () => { runs++; });
  assert.equal(runs, 1);
  assert.deepEqual(readState(f).legacyOutput, state.legacyOutput);
  assert.equal(second.resumed, true);
});

const backupOutputs = f => fs.readdirSync(f.directory)
  .filter(name => name.startsWith('.autograde-legacy-output-'))
  .map(name => path.join(f.directory, name, 'output'));

test('legacy output links are moved as entries without following or modifying their targets', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  const outside = path.join(f.directory, 'outside'); fs.mkdirSync(outside);
  const original = path.join(outside, 'keep.txt'); fs.writeFileSync(original, 'private contents');
  fs.symlinkSync(outside, path.join(f.output, 'directory-alias'), process.platform === 'win32' ? 'junction' : 'dir');
  fs.linkSync(original, path.join(f.output, 'hard-link'));
  open(t, f, {resume: true});
  const backup = readState(f).legacyOutput.backup;
  assert.equal(fs.lstatSync(path.join(backup, 'directory-alias')).isSymbolicLink(), true);
  assert.equal(fs.lstatSync(path.join(backup, 'hard-link')).nlink, 2);
  assert.equal(fs.readFileSync(original, 'utf8'), 'private contents');
  assert.deepEqual(fs.readdirSync(f.output), [buildStateFilename]);
});

test('a locked legacy output remains intact and releases build locks for retry', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  fs.writeFileSync(path.join(f.output, 'keep.zip'), 'keep');
  const rename = fs.renameSync;
  const mock = t.mock.method(fs, 'renameSync', (from, to) => {
    if (from === f.output) throw Object.assign(new Error('synthetic Windows sharing violation'), {code: 'EPERM'});
    return rename(from, to);
  });
  assert.throws(() => openBuildJournal({...f, resume: true}), /sharing violation/);
  assert.equal(fs.readFileSync(path.join(f.output, 'keep.zip'), 'utf8'), 'keep');
  assert.equal(fs.existsSync(f.statePath), false);
  for (const directory of [f.source, f.output]) assert.equal(fs.existsSync(lockPath(directory)), false);
  mock.mock.restore();
  open(t, f, {resume: true});
  assert.equal(fs.readFileSync(path.join(readState(f).legacyOutput.backup, 'keep.zip'), 'utf8'), 'keep');
});

test('failure recreating output after its atomic move preserves the backup and permits retry', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  fs.writeFileSync(path.join(f.output, 'keep.zip'), 'keep');
  const mkdir = fs.mkdirSync;
  const mock = t.mock.method(fs, 'mkdirSync', (directory, ...args) => {
    if (directory === f.output) throw new Error('synthetic output creation failure');
    return mkdir(directory, ...args);
  });
  assert.throws(() => openBuildJournal({...f, resume: true}), /output creation failure/);
  assert.equal(fs.existsSync(f.output), false);
  assert.equal(backupOutputs(f).length, 1);
  const backup = backupOutputs(f)[0];
  assert.equal(fs.readFileSync(path.join(backup, 'keep.zip'), 'utf8'), 'keep');
  mock.mock.restore();
  const journal = open(t, f, {resume: true});
  assert.equal(journal.resumed, false);
  assert.deepEqual(readState(f).steps, {});
  assert.equal(fs.readFileSync(path.join(backup, 'keep.zip'), 'utf8'), 'keep');
});

test('a process killed after moving legacy output can reopen its original path without data loss', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  fs.writeFileSync(path.join(f.output, 'keep.zip'), 'keep');
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', `
    import fs from 'node:fs'; import {openBuildJournal} from ${JSON.stringify(moduleUrl)};
    const config = ${JSON.stringify(f)}, rename = fs.renameSync;
    fs.renameSync = (from, to) => { rename(from, to); if (from === config.output) process.exit(42); };
    openBuildJournal({...config, resume: true});
  `], {encoding: 'utf8'});
  assert.equal(result.status, 42, result.stderr);
  assert.equal(fs.existsSync(f.output), false);
  const backups = backupOutputs(f);
  assert.equal(backups.length, 1);
  const journal = open(t, f, {resume: true});
  assert.equal(journal.resumed, false);
  assert.equal(fs.readFileSync(path.join(backups[0], 'keep.zip'), 'utf8'), 'keep');
  assert.deepEqual(readState(f).steps, {});
});

test('an active lock prevents migration of legacy output without changing any file', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  fs.writeFileSync(path.join(f.output, 'keep.zip'), 'keep');
  fs.writeFileSync(lockPath(f.output), JSON.stringify({schema: 1, hostname: os.hostname(), pid: process.pid}));
  assert.throws(() => openBuildJournal({...f, resume: true}), /locked by process/);
  assert.equal(fs.readFileSync(path.join(f.output, 'keep.zip'), 'utf8'), 'keep');
  assert.deepEqual(backupOutputs(f), []);
  assert.equal(fs.existsSync(f.statePath), false);
  assert.equal(fs.existsSync(lockPath(f.source)), false);
});

test('fresh and resume modes can initialize an empty output without changing source contents', t => {
  const f = fixture(t); fs.mkdirSync(f.source); fs.mkdirSync(f.output);
  const sourceFile = path.join(f.source, 'work'); fs.writeFileSync(sourceFile, 'work');
  const journal = open(t, f, { resume: true });
  assert.equal(journal.resumed, false);
  assert.equal(fs.readFileSync(sourceFile, 'utf8'), 'work');
  journal.close();
  assert.throws(() => openBuildJournal(f), /without --resume/);
});

test('symbolic and hard linked journals and lock files are refused', t => {
  const f = fixture(t); fs.mkdirSync(f.output);
  const original = path.join(f.directory, 'original'); fs.writeFileSync(original, 'untouched');
  for (const target of [f.statePath, lockPath(f.source)]) {
    for (const kind of ['hard', 'symbolic']) {
      if (kind === 'hard') fs.linkSync(original, target);
      else {
        try { fs.symlinkSync(original, target); }
        catch (error) { if (error.code === 'EPERM') continue; throw error; }
      }
      assert.throws(() => openBuildJournal({ ...f, resume: true }), /regular files without links/);
      assert.equal(fs.readFileSync(original, 'utf8'), 'untouched');
      fs.unlinkSync(target);
    }
  }
});

test('directory symlinks, files and overlapping source/output paths are refused', t => {
  const f = fixture(t), realDirectory = path.join(f.directory, 'real'); fs.mkdirSync(realDirectory);
  fs.symlinkSync(realDirectory, f.source, process.platform === 'win32' ? 'junction' : 'dir');
  assert.throws(() => openBuildJournal(f), /regular directory/);
  fs.unlinkSync(f.source); fs.writeFileSync(f.source, 'file');
  assert.throws(() => openBuildJournal(f), /regular directory/);
  fs.unlinkSync(f.source);
  assert.throws(() => openBuildJournal({ ...f, source: f.output }), /separate directories/);
  assert.throws(() => openBuildJournal({ ...f, source: path.join(f.output, 'nested') }), /separate directories/);
});

test('case aliases on case-insensitive filesystems use the canonical source and cannot hide overlapping output', t => {
  const f = fixture(t), source = path.join(f.directory, 'MixedSource'), alias = path.join(f.directory, 'mixedsource');
  fs.mkdirSync(source);
  if (!fs.existsSync(alias)) { t.skip('Filesystem is case-sensitive'); return; }
  const journal = open(t, { ...f, source: alias });
  assert.equal(journal.source, fs.realpathSync(source));
  journal.close();
  assert.throws(() => openBuildJournal({ ...f, source, output: path.join(alias, 'nested'), resume: true }), /separate directories/);
  assert.throws(() => openBuildJournal({ ...f, source: path.join(f.directory, 'missing-source'),
    output: path.join(f.directory, 'MISSING-SOURCE', 'nested'), resume: true }), /separate directories/);
});

test('checkpoint inputs and return values are JSON and cannot be mutated through aliases', t => {
  const f = fixture(t), journal = open(t, f), value = { count: 1 };
  const returned = journal.step('compile', { key: { z: 1, a: 2 } }, () => value);
  value.count = 2; returned.count = 3;
  assert.equal(readState(f).steps.compile.value.count, 1);
  assert.throws(() => journal.step('async-step', { key: 'v1' }, async () => 1), /serializable JSON/);
  assert.equal(readState(f).steps['async-step'].status, 'failed');
  journal.close();
  const resumed = open(t, f, { resume: true });
  assert.deepEqual(resumed.step('compile', { key: { a: 2, z: 1 } }, () => { throw new Error('should skip'); }), { count: 1 });
});

test('dependencies must be revalidated in the current run before downstream reuse', t => {
  const f = fixture(t), first = open(t, f);
  first.step('install', { key: 'v1' }, () => {});
  first.step('compile', { key: 'v1', deps: ['install'] }, () => {}); first.close();
  const resumed = open(t, f, { resume: true });
  assert.throws(() => resumed.step('compile', { key: 'v1', deps: ['install'] }, () => {}), /not passed in this run/);
  resumed.close();
  assert.throws(() => resumed.step('install', { key: 'v1' }, () => {}), /closed/);
});

test('failed dependencies cannot reuse downstream successes and a resumed retry invalidates them', t => {
  const f = fixture(t), first = open(t, f);
  first.step('install', { key: 'v1' }, () => {});
  first.step('compile', { key: 'v1', deps: ['install'] }, () => {});
  const compileToken = readState(f).steps.compile.token;
  first.close();
  const second = open(t, f, { resume: true });
  assert.throws(() => second.step('install', { key: 'v1', validate: () => false }, () => { throw new Error('install interrupted'); }), /install interrupted/);
  assert.throws(() => second.step('compile', { key: 'v1', deps: ['install'] }, () => {}), /not passed in this run/);
  assert.throws(() => second.step('install', { key: 'v1' }, () => {}), /already visited/);
  assert.equal(readState(f).steps.compile.token, compileToken);
  second.close();
  const third = open(t, f, { resume: true });
  third.step('install', { key: 'v1' }, () => {});
  let compiled = false;
  third.step('compile', { key: 'v1', deps: ['install'] }, () => { compiled = true; });
  assert.equal(compiled, true);
  assert.notEqual(readState(f).steps.compile.token, compileToken);
});

test('Object prototype stage names remain own properties and prototype mutation names are rejected', t => {
  const f = fixture(t), first = open(t, f);
  first.step('constructor', { key: JSON.parse('{"__proto__":{"polluted":true}}') }, () => 'constructor');
  first.step('toString', { key: 'v1', deps: ['constructor'] }, () => 'toString');
  first.step('hasOwnProperty', { key: 'v1', deps: ['toString'] }, () => 'hasOwnProperty');
  assert.throws(() => first.step('__proto__', { key: 'v1' }, () => {}), /Invalid build checkpoint stage/);
  assert.equal(Object.prototype.polluted, undefined);
  const state = readState(f);
  for (const name of ['constructor', 'toString', 'hasOwnProperty']) assert.equal(Object.hasOwn(state.steps, name), true);
  first.close();
  const second = open(t, f, { resume: true });
  assert.equal(second.step('constructor', { key: JSON.parse('{"__proto__":{"polluted":true}}') }, () => { throw new Error('should skip'); }), 'constructor');
  assert.equal(second.step('toString', { key: 'v1', deps: ['constructor'] }, () => { throw new Error('should skip'); }), 'toString');
  assert.equal(second.step('hasOwnProperty', { key: 'v1', deps: ['toString'] }, () => { throw new Error('should skip'); }), 'hasOwnProperty');
});

test('stage failures name the failed action, preserve their cause, and bound journal error text', t => {
  const f = fixture(t), journal = open(t, f), failure = new Error('x'.repeat(10_000));
  assert.throws(() => journal.step('compile', { key: 'v1' }, () => { throw failure; }), error => {
    assert.match(error.message, /^Build stage compile failed:/);
    assert.equal(error.cause, failure);
    return true;
  });
  assert.equal(readState(f).steps.compile.error.length, 4096);
});
