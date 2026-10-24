import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { spawn } from 'node:child_process';
import test from 'node:test';
import { learnerEnvironment, learnerLayout, prepareLearner } from './learning-profile.mjs';
import { SESSION_MARKER, sharedSessionLayout, prepareSharedSession, markSessionLaunching,
  recordSessionProcess, cleanupSharedSession, superviseSharedChild, sharedSettings } from './shared-session.mjs';

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

test('shared sessions use new owned roots and do not adopt persistent student profiles', t => {
  const f = fixture(t);
  const old = prepareLearner(learnerLayout(f.base, 'fixture-seat'), defaults);
  fs.writeFileSync(path.join(old.workspace, 'answer.c'), 'old private answer');
  const a = prepareSharedSession(f.layout(), defaults);
  assert.match(path.basename(a.root), /^e-[0-9a-f]{24}$/);
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
  assert.notEqual(b.root, a.root);
  cleanupSharedSession(b);
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
  assert.equal(fs.existsSync(b.root), false);
  cleanupSharedSession(a);
});

test('stale sessions are erased before the next workspace is created', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  fs.writeFileSync(path.join(a.workspace, 'answer.c'), 'previous student'); stale(a, 'running', 99999998);
  const b = prepareSharedSession(f.layout(), defaults, dead);
  assert.equal(fs.existsSync(a.root), false);
  assert.deepEqual(b.recovered, [a.sessionId]);
  assert.deepEqual(fs.readdirSync(b.workspace), []);
  cleanupSharedSession(b);
});

for (const liveKind of ['main', 'group']) test(`orphaned live IDE ${liveKind} blocks recovery without deleting files`, t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults);
  stale(a, 'running', 12345);
  const b = f.layout();
  const states = { isAlive: pid => liveKind === 'main' && pid === 12345, isGroupAlive: pid => liveKind === 'group' && pid === 12345 };
  assert.throws(() => prepareSharedSession(b, defaults, states), /previous IDE is still active/);
  assert.equal(fs.existsSync(a.root), true);
  assert.equal(fs.existsSync(b.root), false);
});

test('unknown interrupted spawn fails closed instead of assuming no IDE is alive', t => {
  const f = fixture(t), a = prepareSharedSession(f.layout(), defaults); stale(a, 'launching');
  const b = f.layout();
  assert.throws(() => prepareSharedSession(b, defaults, dead), /interrupted before its process was recorded/);
  assert.equal(fs.existsSync(a.root), true);
  assert.equal(fs.existsSync(b.root), false);
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
    assert.equal(fs.existsSync(b.root), false);
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
