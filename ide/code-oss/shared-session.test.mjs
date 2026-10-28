import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { spawn } from 'node:child_process';
import { EventEmitter, once } from 'node:events';
import test from 'node:test';
import { learnerEnvironment, learnerLayout, prepareLearner } from './learning-profile.mjs';
import { SESSION_MARKER, sharedSessionLayout, prepareSharedSession, markSessionLaunching,
  recordSessionProcess, cleanupSharedSession, superviseSharedChild, sharedSettings, processAlive } from './shared-session.mjs';

const defaults = { 'editor.fontSize': 16, 'files.hotExit': 'onExit' };
const dead = { isAlive: () => false, isGroupAlive: () => false };
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ag-shared-test-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const base = path.join(root, 'seat');
  return { root, base, layout: () => sharedSessionLayout(base, 'fixture-seat') };
}
function json(file, value) { fs.writeFileSync(file, JSON.stringify(value), { mode: 0o600 }); }
function marker(a) { return JSON.parse(fs.readFileSync(path.join(a.root, SESSION_MARKER), 'utf8')); }
function stale(a, phase = 'prepared', idePid = null) {
  const file = path.join(a.root, SESSION_MARKER), value = marker(a);
  json(file, { ...value, launcherPid: 99999999, phase, idePid });
  const lockFile = path.join(a.base, '.autograde-shared-lock.json');
  json(lockFile, { ...JSON.parse(fs.readFileSync(lockFile, 'utf8')), launcherPid: 99999999 });
}

test('shared sessions use a fixed owned root with fresh identities and do not adopt persistent student profiles', t => {
  const f = fixture(t);
  const old = prepareLearner(learnerLayout(f.base, 'fixture-seat'), defaults);
  fs.writeFileSync(path.join(old.workspace, 'answer.c'), 'old private answer');
  const a = prepareSharedSession(f.layout(), defaults);
  assert.equal(path.basename(a.root), 'current');
  assert.notEqual(a.root, old.root);
  assert.equal(a.preservedLegacyProfiles, 1);
  assert.deepEqual(fs.readdirSync(a.workspace), []);
  assert.deepEqual(fs.readdirSync(a.materials), []);
  const settings = JSON.parse(fs.readFileSync(path.join(a.runtime, 'user-data/User/settings.json')));
  for (const [key, value] of Object.entries(sharedSettings)) assert.deepEqual(settings[key], value);
  assert.equal(settings['editor.fontSize'], 16);
  cleanupSharedSession(a);
  assert.equal(fs.readFileSync(path.join(old.workspace, 'answer.c'), 'utf8'), 'old private answer');
  const b = prepareSharedSession(f.layout(), defaults);
  assert.equal(b.root, a.root);
  assert.notEqual(b.sessionId, a.sessionId);
  cleanupSharedSession(b);
});

for (const ending of ['cleanup', 'stale recovery']) test(`bundled service address survives ${ending} without retaining student data`, t => {
  const f = fixture(t);
  const bundledDefaults = JSON.parse(fs.readFileSync(new URL('./runtime-settings.json', import.meta.url), 'utf8'));
  const serviceAddress = 'https://ai.cbchoi.info:20000';
  const a = prepareSharedSession(f.layout(), bundledDefaults);
  const settingsPath = path.join(a.runtime, 'user-data/User/settings.json');
  const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
  assert.equal(settings['autograde.serviceBaseUrl'], serviceAddress);
  assert.equal(settings['autograde.studentPortalBaseUrl'], 'https://ai.cbchoi.info:20010');

  const privateFiles = ['workspace/answer.cpp', 'materials/slides.pdf',
    'r/user-data/User/globalStorage/autograde/session.json', 'r/user-data/Backups/unsaved-code'];
  for (const relative of privateFiles) {
    const file = path.join(a.root, relative);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, 'synthetic previous-student data');
  }
  // A student's session-local address choice must not preserve the whole profile.
  json(settingsPath, { ...settings, 'autograde.serviceBaseUrl': 'https://temporary.example:20000' });
  if (ending === 'cleanup') cleanupSharedSession(a);
  else stale(a, 'running', 99999998);

  const b = prepareSharedSession(f.layout(), bundledDefaults, dead);
  assert.equal(b.root, a.root);
  assert.notEqual(b.sessionId, a.sessionId);
  assert.equal(JSON.parse(fs.readFileSync(path.join(b.runtime, 'user-data/User/settings.json'), 'utf8'))['autograde.serviceBaseUrl'], serviceAddress);
  assert.equal(JSON.parse(fs.readFileSync(path.join(b.runtime, 'user-data/User/settings.json'), 'utf8'))['autograde.studentPortalBaseUrl'], 'https://ai.cbchoi.info:20010');
  assert.deepEqual(fs.readdirSync(b.workspace), []);
  assert.deepEqual(fs.readdirSync(b.materials), []);
  for (const relative of privateFiles) assert.equal(fs.existsSync(path.join(b.root, relative)), false, relative);
  assert.deepEqual(b.recovered, ending === 'stale recovery' ? [a.sessionId] : []);
  cleanupSharedSession(b);
  assert.equal(fs.existsSync(b.root), false);
});

test('session environment resets inherited ownership and exposes only this session', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  const env = learnerEnvironment(a, { AUTOGRADE_SESSION_ROOT: '/previous', AUTOGRADE_SESSION_ID: 'old', PATH: '/tools' });
  assert.equal(env.AUTOGRADE_LEARNING_LIFETIME, 'shared');
  assert.equal(env.AUTOGRADE_SESSION_ROOT, a.root);
  assert.equal(env.AUTOGRADE_SESSION_ID, a.sessionId);
  assert.equal(env.AUTOGRADE_LEARNER_WORKSPACE, a.workspace);
  assert.equal(env.AUTOGRADE_LEARNER_MATERIALS, a.materials);
  assert.equal(env.VSCODE_PORTABLE, a.runtime);
  assert.equal(env.PATH, '/tools');
  const legacy = learnerEnvironment(undefined, env);
  assert.equal(legacy.AUTOGRADE_LEARNING_LIFETIME, 'unmanaged');
  assert.equal(legacy.AUTOGRADE_SESSION_ROOT, '');
  assert.equal(legacy.AUTOGRADE_SESSION_ID, '');
  assert.equal(legacy.VSCODE_PORTABLE, '');
  cleanupSharedSession(a);
});

test('cleanup removes code, materials, editor history, cache and logs; external export survives', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  const outside = path.join(f.root, 'my-export.zip'); fs.writeFileSync(outside, 'fixture exported bytes');
  for (const relative of ['workspace/main.cpp', 'materials/slides.pdf', 'materials/.content/objects/a',
    'r/user-data/Backups/c', 'r/user-data/User/History/d', 'r/user-data/logs/x.log']) {
    const file = path.join(a.root, relative); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, 'private');
  }
  if (process.platform !== 'win32') fs.symlinkSync(outside, path.join(a.workspace, 'export-link'));
  cleanupSharedSession(a);
  assert.equal(fs.existsSync(a.root), false);
  assert.equal(fs.readFileSync(outside, 'utf8'), 'fixture exported bytes');
  assert.equal(fs.existsSync(path.join(a.base, '.autograde-shared-lock.json')), false);
});

test('a live launcher blocks every next student at the same seat', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  const b = sharedSessionLayout(f.base, 'another-student');
  assert.throws(() => prepareSharedSession(b, defaults), /Another shared IDE session is active/);
  assert.equal(fs.existsSync(b.root), true); // Fixed path still belongs to the previous session.
  cleanupSharedSession(a);
});

test('stale sessions are erased before the next workspace is created', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  fs.writeFileSync(path.join(a.workspace, 'answer.c'), 'previous student'); stale(a, 'running', 99999998);
  const b = prepareSharedSession(f.layout(), defaults, dead);
  assert.equal(b.root, a.root);
  assert.notEqual(marker(b).sessionId, a.sessionId);
  assert.deepEqual(b.recovered, [a.sessionId]);
  assert.deepEqual(fs.readdirSync(b.workspace), []);
  cleanupSharedSession(b);
});

test('an interrupted startup recovery retries the previous root under a fresh lease', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); stale(a);
  fs.writeFileSync(path.join(a.workspace, 'old.c'), 'previous student');
  const b = f.layout(), lockPath = path.join(f.base, '.autograde-shared-lock.json');
  assert.throws(() => prepareSharedSession(b, defaults, {
    isGroupAlive: () => false,
    isAlive: () => {
      if (JSON.parse(fs.readFileSync(lockPath)).sessionId === b.sessionId) throw new Error('simulated recovery interruption');
      return false;
    },
  }), /simulated recovery interruption/);
  const interrupted = JSON.parse(fs.readFileSync(lockPath));
  assert.equal(interrupted.phase, 'recovering');
  assert.equal(marker(a).sessionId, a.sessionId);
  json(lockPath, {...interrupted, launcherPid: 99999997});
  const c = prepareSharedSession(f.layout(), defaults, dead);
  assert.deepEqual(fs.readdirSync(c.workspace), []);
  assert.deepEqual(c.recovered, [a.sessionId]); cleanupSharedSession(c);
});

for (const live of [false, true]) test(`interrupted recovery claimant is ${live ? 'live and blocks' : 'dead and recoverable'}`, t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); stale(a);
  const claimant = 99999996;
  json(path.join(f.base, `.autograde-recovered-${a.sessionId}.json`), {schema: 2, sessionId: a.sessionId,
    recoveringPid: claimant, claimId: 'b'.repeat(24)});
  const options = {isAlive: pid => live && pid === claimant, isGroupAlive: () => false};
  if (live) {
    assert.throws(() => prepareSharedSession(f.layout(), defaults, options), /Another launcher is recovering/);
    assert.equal(marker(a).sessionId, a.sessionId); return;
  }
  const b = prepareSharedSession(f.layout(), defaults, options);
  assert.deepEqual(b.recovered, [a.sessionId]);
  assert.equal(fs.existsSync(path.join(f.base, `.autograde-recovery-${a.sessionId}-${'b'.repeat(24)}.json`)), true);
  cleanupSharedSession(b);
});

for (const state of ['empty', 'root-removed', 'nonempty', 'replaced']) test(`interrupted final cleanup receipt: ${state}`, t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); stale(a);
  const before = marker(a), receipt = path.join(f.base, `.autograde-cleanup-${a.sessionId}.json`);
  json(receipt, {schema: 1, rootName: 'current', marker: before});
  for (const name of fs.readdirSync(a.root)) fs.rmSync(path.join(a.root, name), {recursive: true});
  if (state === 'root-removed') fs.rmdirSync(a.root);
  if (state === 'nonempty') fs.writeFileSync(path.join(a.root, 'keep.c'), 'unowned');
  if (state === 'replaced') { fs.renameSync(a.root, `${a.root}-old`); fs.mkdirSync(a.root, {mode: 0o700}); }
  if (['nonempty', 'replaced'].includes(state)) {
    assert.throws(() => prepareSharedSession(f.layout(), defaults, dead), /not empty|identity changed/);
    assert.equal(fs.existsSync(receipt), true);
    if (state === 'nonempty') assert.equal(fs.readFileSync(path.join(a.root, 'keep.c'), 'utf8'), 'unowned');
    return;
  }
  const b = prepareSharedSession(f.layout(), defaults, dead);
  assert.deepEqual(b.recovered, [a.sessionId]);
  assert.equal(fs.existsSync(receipt), false);
  assert.deepEqual(fs.readdirSync(b.workspace), []); cleanupSharedSession(b);
});

test('legacy random roots are erased before creating the fixed workspace', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); stale(a);
  const legacyRoot = path.join(f.base, `e-${a.sessionId}`), oldMarker = marker(a);
  fs.writeFileSync(path.join(a.workspace, 'old.c'), 'previous'); fs.renameSync(a.root, legacyRoot);
  json(path.join(legacyRoot, SESSION_MARKER), {...oldMarker, root: legacyRoot});
  const lockPath = path.join(f.base, '.autograde-shared-lock.json'), lock = JSON.parse(fs.readFileSync(lockPath));
  delete lock.rootName; delete lock.phase; json(lockPath, lock);
  const b = prepareSharedSession(f.layout(), defaults, dead);
  assert.equal(fs.existsSync(legacyRoot), false);
  assert.equal(path.basename(b.root), 'current');
  assert.deepEqual(b.recovered, [a.sessionId]);
  assert.deepEqual(fs.readdirSync(b.workspace), []); cleanupSharedSession(b);
});

for (const liveKind of ['main', 'group']) test(`orphaned live IDE ${liveKind} blocks recovery without deleting files`, t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  stale(a, 'running', 12345);
  const b = f.layout();
  const states = { isAlive: pid => liveKind === 'main' && pid === 12345, isGroupAlive: pid => liveKind === 'group' && pid === 12345 };
  assert.throws(() => prepareSharedSession(b, defaults, states), /previous IDE is still active/);
  assert.equal(fs.existsSync(a.root), true);
  assert.equal(fs.existsSync(b.root), true); // Fixed path still belongs to the previous session.
});

test('unknown interrupted spawn fails closed instead of assuming no IDE is alive', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); stale(a, 'launching');
  const b = f.layout();
  assert.throws(() => prepareSharedSession(b, defaults, dead), /interrupted before its process was recorded/);
  assert.equal(fs.existsSync(a.root), true);
  assert.equal(fs.existsSync(b.root), true); // Fixed path still belongs to the previous session.
});

test('tampered ownership, unmarked folders and changed root inode never authorize deletion', async t => {
  for (const kind of ['marker', 'unmarked', 'inode']) await t.test(kind, t => {
    const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); stale(a);
    fs.writeFileSync(path.join(a.root, 'keep.txt'), 'keep');
    if (kind === 'marker') json(path.join(a.root, SESSION_MARKER), { ...marker(a), root: f.root });
    if (kind === 'unmarked') fs.unlinkSync(path.join(a.root, SESSION_MARKER));
    if (kind === 'inode') {
      const before = marker(a); fs.renameSync(a.root, `${a.root}-preserved`);
      fs.mkdirSync(a.root, { mode: 0o700 }); json(path.join(a.root, SESSION_MARKER), before);
      fs.writeFileSync(path.join(a.root, 'keep.txt'), 'keep');
    }
    const b = f.layout();
    assert.throws(() => prepareSharedSession(b, defaults, dead));
    assert.equal(fs.readFileSync(path.join(a.root, 'keep.txt'), 'utf8'), 'keep');
    assert.equal(fs.existsSync(b.root), true); // Fixed path still belongs to the previous session.
  });
});

test('symlinked roots and ancestors never traverse into external folders', async t => {
  if (process.platform === 'win32') return t.skip('POSIX symlink test');
  for (const kind of ['root', 'base']) await t.test(kind, t => {
    const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
    const outside = path.join(f.root, 'outside'); fs.mkdirSync(outside); fs.writeFileSync(path.join(outside, 'keep'), 'outside');
    const target = kind === 'root' ? a.root : a.base;
    fs.renameSync(target, `${target}-preserved`); fs.symlinkSync(outside, target);
    assert.throws(() => cleanupSharedSession(a));
    assert.equal(fs.readFileSync(path.join(outside, 'keep'), 'utf8'), 'outside');
  });
});

test('cleanup errors preserve the lease and block a new student until repaired', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  const before = marker(a); json(path.join(a.root, SESSION_MARKER), { ...before, rootIno: before.rootIno + 1 });
  assert.throws(() => cleanupSharedSession(a), /ownership mismatch/);
  assert.equal(fs.existsSync(path.join(a.base, '.autograde-shared-lock.json')), true);
  assert.throws(() => prepareSharedSession(f.layout(), defaults), /Another shared IDE session is active/);
  json(path.join(a.root, SESSION_MARKER), before); cleanupSharedSession(a);
});

test('cleanup refuses a recorded live process and an unrecorded spawn outcome', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  markSessionLaunching(a);
  assert.throws(() => cleanupSharedSession(a), /launch outcome unknown/);
  recordSessionProcess(a, process.pid);
  assert.throws(() => cleanupSharedSession(a), /IDE still running/);
  cleanupSharedSession(a, dead);
});

test('the real child completion deletes the owned session only after process close', async t => {
  if (process.platform === 'win32') return t.skip('Shared launcher currently targets macOS/Linux/WSL2');
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  fs.writeFileSync(path.join(a.workspace, 'main.c'), 'private'); markSessionLaunching(a);
  const child = spawn(process.execPath, ['-e', 'setTimeout(() => process.exit(0), 150)'], { detached: true, stdio: 'ignore' });
  const completion = superviseSharedChild(child, a);
  recordSessionProcess(a, child.pid);
  assert.equal(fs.existsSync(a.root), true);
  assert.equal((await completion).code, 0);
  assert.equal(fs.existsSync(a.root), false);
});

test('failed executable spawn cleans a known nonstarted session without leaving credentials', async t => {
  if (process.platform === 'win32') return t.skip('Shared launcher currently targets macOS/Linux/WSL2');
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); markSessionLaunching(a);
  const child = spawn(path.join(f.root, 'does-not-exist'), [], { detached: true, stdio: 'ignore' });
  await assert.rejects(superviseSharedChild(child, a), /ENOENT/);
  assert.equal(fs.existsSync(a.root), false);
});

test('IDE crash cleanup preserves a previously exported file', async t => {
  if (process.platform === 'win32') return t.skip('Shared launcher currently targets macOS/Linux/WSL2');
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  const exported = path.join(f.root, 'export.txt'); fs.writeFileSync(exported, 'my export'); markSessionLaunching(a);
  const child = spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)'], { detached: true, stdio: 'ignore' });
  const completion = superviseSharedChild(child, a); recordSessionProcess(a, child.pid);
  child.kill('SIGKILL'); await completion;
  assert.equal(fs.existsSync(a.root), false);
  assert.equal(fs.readFileSync(exported, 'utf8'), 'my export');
});

test('requested shutdown escalates before close when a real child ignores SIGTERM', async t => {
  if (process.platform === 'win32') return t.skip('POSIX detached process supervisor');
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  const before = ['SIGINT', 'SIGTERM', 'SIGHUP'].map(name => process.listenerCount(name));
  const shutdown = new AbortController(); markSessionLaunching(a);
  const child = spawn(process.execPath, ['-e', "process.on('SIGTERM',()=>{}); process.send('ready'); setInterval(()=>{},1000)"],
    { detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
  const completion = superviseSharedChild(child, a, { shutdownTimeoutMs: 100, shutdownSignal: shutdown.signal });
  void completion.catch(() => {}); recordSessionProcess(a, child.pid);
  try {
    await once(child, 'message', {signal: AbortSignal.timeout(5000)});
    shutdown.abort();
    assert.equal(fs.existsSync(a.root), true);
    assert.equal(processAlive(child.pid), true);
    const result = await Promise.race([completion, new Promise((_, reject) => {
      const timer = setTimeout(() => reject(new Error('Shutdown never completed')), 5000); timer.unref();
      void completion.finally(() => clearTimeout(timer)).catch(() => {});
    })]);
    assert.equal(result.signal, 'SIGKILL');
    assert.equal(processAlive(child.pid), false);
    assert.equal(fs.existsSync(a.root), false);
    assert.deepEqual(['SIGINT', 'SIGTERM', 'SIGHUP'].map(name => process.listenerCount(name)), before);
  } finally {
    if (processAlive(child.pid)) { process.kill(-child.pid, 'SIGKILL'); await once(child, 'close'); }
  }
});

test('a launcher signal bounds TERM-ignoring child shutdown without signalling the test process', async t => {
  if (process.platform === 'win32') return t.skip('POSIX detached process supervisor');
  const f = fixture(t), term = path.join(f.root, 'term-observed');
  const script = `
    import fs from 'node:fs'; import {spawn} from 'node:child_process';
    import {sharedSessionLayout, prepareSharedSession, markSessionLaunching, recordSessionProcess,
      superviseSharedChild} from ${JSON.stringify(new URL('./shared-session.mjs', import.meta.url).href)};
    const a = prepareSharedSession(sharedSessionLayout(${JSON.stringify(f.base)}, 'signal-fixture'), {});
    markSessionLaunching(a);
    const child = spawn(process.execPath, ['-e', ${JSON.stringify(`const fs=require('node:fs'); process.on('SIGTERM',()=>fs.writeFileSync(${JSON.stringify(term)},'observed')); process.send('ready'); setInterval(()=>{},1000);`)}],
      {detached:true,stdio:['ignore','ignore','ignore','ipc']});
    const completion = superviseSharedChild(child,a,{shutdownTimeoutMs:250}); recordSessionProcess(a,child.pid);
    child.once('message',()=>process.send({ready:true,childPid:child.pid,root:a.root}));
    completion.then(result=>process.send({completed:true,...result,rootExists:fs.existsSync(a.root),
      signalListeners:process.listenerCount('SIGTERM')},()=>process.disconnect()), error=>{
      process.exitCode=1; process.send({error:error.message},()=>process.disconnect());
    });
  `;
  const launcher = spawn(process.execPath, ['--input-type=module', '-e', script],
    { detached: true, stdio: ['ignore', 'ignore', 'ignore', 'ipc'] });
  let childPid;
  try {
    const [ready] = await once(launcher, 'message', {signal: AbortSignal.timeout(5000)});
    assert.equal(ready.ready, true); childPid = ready.childPid;
    const completed = once(launcher, 'message', {signal: AbortSignal.timeout(5000)});
    launcher.kill('SIGTERM');
    const until = Date.now() + 2000;
    while (!fs.existsSync(term) && Date.now() < until) await new Promise(done => setTimeout(done, 5));
    assert.equal(fs.existsSync(term), true, 'TERM reached the real child');
    assert.equal(fs.existsSync(ready.root), true, 'No cleanup while the child still ignores TERM');
    assert.equal(processAlive(childPid), true);
    const [result] = await completed;
    assert.equal(result.completed, true, result.error);
    assert.equal(result.signal, 'SIGKILL');
    assert.equal(result.rootExists, false);
    assert.equal(result.signalListeners, 0);
    assert.equal(processAlive(childPid), false);
  } finally {
    if (processAlive(childPid)) process.kill(-childPid, 'SIGKILL');
    if (launcher.exitCode === null && launcher.signalCode === null) { launcher.kill('SIGKILL'); await once(launcher, 'close'); }
  }
});

test('unknown shutdown ownership retains session and lease instead of claiming cleanup', async t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); markSessionLaunching(a);
  const child = new EventEmitter(), shutdown = new AbortController();
  const completion = superviseSharedChild(child, a, {shutdownTimeoutMs: 10, shutdownSignal: shutdown.signal});
  shutdown.abort();
  await assert.rejects(completion, /ownership is unknown/);
  assert.equal(fs.existsSync(a.root), true);
  assert.equal(fs.existsSync(path.join(a.base, '.autograde-shared-lock.json')), true);
});
