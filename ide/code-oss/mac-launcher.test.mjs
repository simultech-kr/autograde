import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { spawn } from 'node:child_process';
import { pathToFileURL } from 'node:url';
import test from 'node:test';
import { cleanLaunchEnvironment, loadMacBundle, macRuntimeBase, macLaunchSpec, runMacSession, macFailureMessage, isMacLauncherEntry } from './mac-launcher.mjs';
import { sharedSessionLayout, prepareSharedSession, cleanupSharedSession, SESSION_MARKER } from './shared-session.mjs';

function write(file, value, mode = 0o600) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, typeof value === 'string' ? value : JSON.stringify(value), { mode });
}
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ag-mac-')));
  // Shared macOS socket paths need a short test directory as well.
  const shortTemp = process.platform === 'darwin' ? '/private/tmp' : fs.realpathSync(os.tmpdir());
  const base = fs.realpathSync(fs.mkdtempSync(path.join(shortTemp, 'ag-m-')));
  t.after(() => { fs.rmSync(root, { recursive: true, force: true }); fs.rmSync(base, { recursive: true, force: true }); });
  const resources = path.join(root, '학교 Autograde.app/Contents/Resources');
  const manifest = { schema: 1, arch: process.arch, version: '1.143.0', policy: 'standard',
    editorRoot: 'editor/app', executable: 'editor/Autograde IDE.app/Contents/MacOS/Autograde IDE',
    node: 'runtime/bin/node', approvedBuiltins: ['autograde', 'autograde-ide-profile'] };
  write(path.join(resources, 'autograde/bundle.json'), manifest);
  write(path.join(resources, 'autograde/runtime-settings.json'), { 'files.hotExit': 'onExit' });
  write(path.join(resources, 'editor/app/out/main.js'), '// fixture compiled editor');
  write(path.join(resources, manifest.executable), '#!/bin/sh\nexit 0\n', 0o700);
  write(path.join(resources, manifest.node), 'fixture runtime', 0o700);
  return { root, base, resources, manifest, load: () => loadMacBundle(resources, { platform: 'darwin' }) };
}
const sessionRoots = base => fs.readdirSync(base).filter(name => /^e-[0-9a-f]{24}$/.test(name)).map(name => path.join(base, name));
const fixtureStart = (_executable, _args, options) => spawn(process.execPath, ['-e', 'setTimeout(() => {}, 120)'], options);

test('relocated Unicode app loads only bundled files without Git, npm or system Node', t => {
  const f = fixture(t), bundle = f.load();
  assert.equal(bundle.source, path.join(f.resources, 'editor/app'));
  assert.equal(bundle.executable, path.join(f.resources, f.manifest.executable));
  const relocated = path.join(f.root, '다른 위치.app');
  fs.renameSync(path.resolve(f.resources, '../..'), relocated);
  assert.equal(loadMacBundle(path.join(relocated, 'Contents/Resources'), { platform: 'darwin' }).manifest.version, '1.143.0');
});

test('Finder entry resolves alias parents and decomposed Unicode without silently exiting', t => {
  const f = fixture(t), entry = path.join(f.resources, 'autograde/mac-launcher.mjs');
  write(entry, '// entry fixture');
  const alias = path.join(f.root, 'alias');
  fs.symlinkSync(f.resources, alias);
  const url = pathToFileURL(entry);
  assert.equal(isMacLauncherEntry(path.join(alias, 'autograde/mac-launcher.mjs'), url), true);
  if (process.platform === 'darwin') assert.equal(isMacLauncherEntry(entry.normalize('NFD'), url), true);
  assert.equal(isMacLauncherEntry(path.join(f.resources, 'autograde/bundle.json'), url), false);
  assert.equal(isMacLauncherEntry(path.join(f.root, 'missing'), url), false);
  assert.equal(isMacLauncherEntry(undefined, url), false);
});

test('missing, untrusted, mismatched and external app paths fail before sessions are created', async t => {
  for (const [name, mutate] of [
    ['wrong arch', f => { f.manifest.arch = 'invalid'; }],
    ['absolute', f => { f.manifest.editorRoot = f.root; }],
    ['traversal', f => { f.manifest.executable = '../outside'; }],
    ['unknown policy', f => { f.manifest.policy = 'persistent'; }],
    ['builtin traversal', f => { f.manifest.approvedBuiltins = ['../elsewhere']; }],
    ['missing node', f => { fs.unlinkSync(path.join(f.resources, f.manifest.node)); }],
    ['linked executable', f => { const exe = path.join(f.resources, f.manifest.executable); fs.renameSync(exe, `${exe}.old`); fs.symlinkSync(`${exe}.old`, exe); }],
  ]) await t.test(name, t => {
    const f = fixture(t); mutate(f); write(path.join(f.resources, 'autograde/bundle.json'), f.manifest);
    assert.throws(f.load);
    assert.deepEqual(sessionRoots(f.base), []);
  });
});

test('launcher clears inherited runtime switches and uses a private shared session', t => {
  const f = fixture(t), layout = sharedSessionLayout(f.base, 'seat'), bundle = f.load();
  const inherited = { PATH: '/usr/bin:/bin', HOME: f.root, NODE_OPTIONS: '--require evil', NODE_PATH: '/evil',
    ELECTRON_RUN_AS_NODE: '1', VSCODE_PORTABLE: '/old', AUTOGRADE_SESSION_ROOT: '/old', DYLD_INSERT_LIBRARIES: '/evil' };
  assert.deepEqual(cleanLaunchEnvironment(inherited), { PATH: '/usr/bin:/bin', HOME: f.root });
  const spec = macLaunchSpec(bundle, layout, inherited);
  assert.equal(spec.options.env.PATH, '/usr/bin:/bin');
  assert.equal(spec.options.env.NODE_OPTIONS, undefined);
  assert.equal(spec.options.env.ELECTRON_RUN_AS_NODE, undefined);
  assert.equal(spec.options.env.AUTOGRADE_LEARNING_LIFETIME, 'shared');
  assert.equal(spec.options.env.AUTOGRADE_SESSION_ROOT, layout.root);
  assert.equal(spec.options.env.TMPDIR, path.join(layout.root, 'tmp'));
  assert.equal(spec.options.detached, true);
  assert.equal(spec.options.shell, false);
  assert.equal(spec.options.stdio, 'ignore');
  assert.equal(spec.args[0], bundle.source);
  assert.equal(spec.args.at(-1), layout.workspace);
  assert.equal(macRuntimeBase(501), '/private/tmp/ag-lab-501');
  assert.throws(() => macRuntimeBase(-1));
  assert.throws(() => macLaunchSpec(bundle, sharedSessionLayout(`${f.base}/${'x'.repeat(100)}`, 'seat')));
});

test('icon runtime removes code, materials, profiles and temp files after real child exit; exported files survive', async t => {
  const f = fixture(t), bundle = f.load(), events = [];
  const exported = path.join(f.root, 'export.zip'); write(exported, 'explicit export');
  let oldRoot;
  const first = await runMacSession(bundle, { runtimeBase: f.base, start: fixtureStart, report: event => {
    events.push(event.event);
    if (event.event !== 'ide-started') return;
    [oldRoot] = sessionRoots(f.base);
    for (const file of ['workspace/main.cpp', 'materials/slides.pdf', 'r/user-data/User/History/code', 'tmp/temporary-code']) {
      write(path.join(oldRoot, file), 'student private data');
    }
    assert.equal(fs.statSync(path.join(oldRoot, 'tmp')).mode & 0o777, 0o700);
  } });
  assert.equal(first.code, 0);
  assert.deepEqual(events, ['ide-started', 'session-cleaned']);
  assert.equal(fs.existsSync(oldRoot), false);
  assert.equal(fs.readFileSync(exported, 'utf8'), 'explicit export');
  await runMacSession(bundle, { runtimeBase: f.base, start: fixtureStart, report: event => {
    if (event.event !== 'ide-started') return;
    const [newRoot] = sessionRoots(f.base);
    assert.notEqual(newRoot, oldRoot);
    assert.deepEqual(fs.readdirSync(path.join(newRoot, 'workspace')), []);
  } });
  assert.deepEqual(sessionRoots(f.base), []);
});

test('two launches cannot create simultaneous students or bypass the active session', async t => {
  const f = fixture(t), bundle = f.load();
  let resolveStarted;
  const started = new Promise(resolve => { resolveStarted = resolve; });
  const first = runMacSession(bundle, { runtimeBase: f.base,
    start: (_exe, _args, opts) => spawn(process.execPath, ['-e', 'setTimeout(() => {}, 400)'], opts),
    report: event => { if (event.event === 'ide-started') resolveStarted(); } });
  await started;
  await assert.rejects(runMacSession(bundle, { runtimeBase: f.base, start: fixtureStart }), /active/);
  assert.equal(sessionRoots(f.base).length, 1);
  await first;
  assert.deepEqual(sessionRoots(f.base), []);
});

test('failed executable launch cleans the prepared session and does not require a system runtime', async t => {
  const f = fixture(t), bundle = f.load(); bundle.executable = path.join(f.root, 'missing executable');
  await assert.rejects(runMacSession(bundle, { runtimeBase: f.base }), /ENOENT/);
  assert.deepEqual(sessionRoots(f.base), []);
  assert.equal(fs.existsSync(path.join(f.base, '.autograde-shared-lock.json')), false);
});

test('crash and synchronous spawn failures retain no student payload', async t => {
  const f = fixture(t), bundle = f.load();
  const result = await runMacSession(bundle, { runtimeBase: f.base,
    start: (_exe, _args, opts) => spawn(process.execPath, ['-e', 'process.exit(3)'], opts) });
  assert.equal(result.code, 3);
  assert.deepEqual(sessionRoots(f.base), []);
  await assert.rejects(runMacSession(bundle, { runtimeBase: f.base, start: () => { throw new Error('fixture spawn failed'); } }), /fixture spawn failed/);
  assert.deepEqual(sessionRoots(f.base), []);
});

test('unsafe base and leftover persistent profiles block startup without deleting preserved files', async t => {
  const f = fixture(t), bundle = f.load();
  fs.chmodSync(f.base, 0o777);
  await assert.rejects(runMacSession(bundle, { runtimeBase: f.base, start: fixtureStart }), /not writable/);
  fs.chmodSync(f.base, 0o700);
  const legacy = path.join(f.base, 'l-0123456789abcdef'); write(path.join(legacy, 'keep.c'), 'keep');
  await assert.rejects(runMacSession(bundle, { runtimeBase: f.base, start: fixtureStart }), /이전 개인/);
  assert.equal(fs.readFileSync(path.join(legacy, 'keep.c'), 'utf8'), 'keep');
  assert.deepEqual(sessionRoots(f.base), []);
});

test('tampered previous session is never adopted or erased by the icon launcher', async t => {
  const f = fixture(t), bundle = f.load();
  const layout = prepareSharedSession(sharedSessionLayout(f.base, 'seat'), {});
  const markerPath = path.join(layout.root, SESSION_MARKER);
  const original = fs.readFileSync(markerPath, 'utf8');
  write(markerPath, { ...JSON.parse(original), rootIno: 0 });
  await assert.rejects(runMacSession(bundle, { runtimeBase: f.base, start: fixtureStart }));
  assert.equal(fs.existsSync(layout.root), true);
  write(markerPath, original); cleanupSharedSession(layout);
});

test('error dialogs explain recovery without forwarding arbitrary logs or credentials', () => {
  assert.match(macFailureMessage(new Error('Another shared IDE session is active')), /이전 실습/);
  assert.match(macFailureMessage(new Error('Shared session ownership mismatch')), /학생 정보 보호/);
  assert.match(macFailureMessage(new Error('spawn /private/path ENOENT')), /앱 전체/);
  assert.doesNotMatch(macFailureMessage(new Error('token=not-a-real-token private source')), /token|private source/);
});
