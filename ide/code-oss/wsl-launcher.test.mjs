import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { pathToFileURL } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import test from 'node:test';
import { assertLinuxStorage, loadWslBundle, cleanWslEnvironment, inspectWslEnvironment, checkWslSandbox,
  preflightWsl, wslRuntimeBase, wslLaunchSpec, runWslSession, wslFailure, WslLaunchError, isWslLauncherEntry } from './wsl-launcher.mjs';
import { sharedSessionLayout, prepareSharedSession, cleanupSharedSession, SESSION_MARKER } from './shared-session.mjs';

// WSL prerequisites are simulated here. Session tests use real POSIX child
// processes and files; this does not substitute for a Windows/WSLg acceptance run.
const linuxDisk = () => ({ type: 0xef53 });
const environment = { gui: 'x11', uid: 1000, distribution: 'Ubuntu-24.04' };
const sandbox = { sandbox: 'user-namespace' };
function write(file, value, mode = 0o600) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, typeof value === 'string' ? value : JSON.stringify(value), { mode });
}
function fixture(t) {
  const parent = fs.realpathSync(os.tmpdir());
  const short = process.platform === 'darwin' ? '/private/tmp' : parent;
  const root = fs.realpathSync(fs.mkdtempSync(path.join(parent, 'ag-wsl-test-')));
  const base = fs.realpathSync(fs.mkdtempSync(path.join(short, 'ag-w-')));
  t.after(() => { fs.rmSync(root, { recursive: true, force: true }); fs.rmSync(base, { recursive: true, force: true }); });
  const install = path.join(root, '앱 설치 위치');
  const manifest = { schema: 1, platform: 'linux', arch: process.arch, version: '1.143.0',
    nodeVersion: process.versions.node, extensionVersion: '0.5.12', lifecycle: 'shared', policy: 'standard',
    editorRoot: 'editor/app', executable: 'editor/electron/autograde-ide', node: 'runtime/bin/node',
    approvedBuiltins: ['autograde', 'autograde-ide-profile'] };
  write(path.join(install, 'wsl-package.json'), manifest);
  write(path.join(install, 'autograde/runtime-settings.json'), { 'files.hotExit': 'onExit' });
  write(path.join(install, 'editor/app/out/main.js'), '// fake compiled editor');
  write(path.join(install, manifest.executable), '#!/bin/sh\nexit 0\n', 0o700);
  write(path.join(install, manifest.node), 'runtime placeholder', 0o700);
  const load = () => loadWslBundle(install, { platform: 'linux', statfs: linuxDisk });
  const options = { runtimeBase: base, environment, sandbox, statfs: linuxDisk,
    start: (_exe, _args, opts) => spawn(process.execPath, ['-e', 'setTimeout(() => {}, 90)'], opts) };
  return { root, base, install, manifest, load, options };
}
const roots = base => fs.readdirSync(base).filter(name => /^e-[0-9a-f]{24}$/.test(name)).map(name => path.join(base, name));
const wslHost = { platform: 'linux', release: '6.6.87.2-microsoft-standard-WSL2', uid: 1000,
  env: { DISPLAY: ':0', WSL_DISTRO_NAME: 'Ubuntu-24.04' }, socketExists: file => file === '/tmp/.X11-unix/X0' };

test('preflight accepts local WSLg X11 and Wayland endpoints, and reports shared cleanup contract', t => {
  const f = fixture(t);
  assert.deepEqual(inspectWslEnvironment(wslHost), environment);
  const wayland = inspectWslEnvironment({ ...wslHost, env: { WAYLAND_DISPLAY: 'wayland-0', XDG_RUNTIME_DIR: '/run/user/1000' },
    socketExists: file => file === '/run/user/1000/wayland-0' });
  assert.equal(wayland.gui, 'wayland');
  const ready = preflightWsl(f.load(), { environment, sandbox });
  assert.equal(ready.event, 'ready'); assert.equal(ready.schema, 1);
  assert.equal(ready.platform, 'wsl2'); assert.equal(ready.lifetime, 'shared');
  assert.equal(ready.noSandbox, false); assert.equal(ready.uid, 1000);
  assert.equal(ready.distribution, 'Ubuntu-24.04');
  assert.deepEqual(roots(f.base), []);
});

test('WSL1, native Linux, root and unavailable or external graphics fail with actionable errors', async t => {
  for (const [changes, code] of [
    [{ platform: 'darwin' }, 'wsl_missing'], [{ release: '6.8.0-generic' }, 'wsl_missing'],
    [{ release: '4.4.0-19041-Microsoft' }, 'wsl1_unsupported'], [{ uid: 0 }, 'root_user'],
    [{ socketExists: () => false }, 'gui_unavailable'],
    [{ env: { DISPLAY: '192.0.2.1:0' }, socketExists: () => true }, 'gui_unavailable'],
    [{ env: { XDG_RUNTIME_DIR: '/mnt/wslg/runtime-dir', WAYLAND_DISPLAY: '../foreign' }, socketExists: () => true }, 'gui_unavailable'],
  ]) await t.test(code + JSON.stringify(changes), () => assert.throws(() => inspectWslEnvironment({ ...wslHost, ...changes }), error => error.code === code));
});

test('bundle rejects Windows or unknown filesystems, mismatched runtime and escaped paths', async t => {
  const f = fixture(t);
  assert.equal(assertLinuxStorage(f.install, linuxDisk), fs.realpathSync(f.install));
  for (const type of [0x01021997, 0x53464846, 0xff534d42, 0x65735546, 0]) {
    assert.throws(() => assertLinuxStorage(f.install, () => ({ type })), error => error.code === 'unsafe_install');
  }
  assert.throws(() => loadWslBundle(f.install, { platform: 'darwin' }), error => error.code === 'wsl_missing');
  for (const change of [{ nodeVersion: '0.0.0' }, { arch: 'wrong' }, { lifecycle: 'persistent' },
    { editorRoot: '../outside' }, { node: '/usr/bin/node' }, { approvedBuiltins: ['../outside'] }]) await t.test(JSON.stringify(change), t => {
    const f = fixture(t); write(path.join(f.install, 'wsl-package.json'), { ...f.manifest, ...change });
    assert.throws(f.load);
    assert.deepEqual(roots(f.base), []);
  });
});

test('bundle does not follow executable or runtime links outside its installation', t => {
  const f = fixture(t), node = path.join(f.install, f.manifest.node);
  fs.renameSync(node, `${node}.old`); fs.symlinkSync(`${node}.old`, node);
  assert.throws(f.load, error => error.code === 'bundle_invalid');
});

test('sandbox prerequisites never fall back to disabling Chromium protection', t => {
  const f = fixture(t), bundle = f.load();
  const probe = (exe, args, options) => {
    assert.equal(exe, '/usr/bin/unshare'); assert.deepEqual(args, ['--user', '--map-root-user', '/usr/bin/true']);
    assert.equal(options.timeout, 5000); assert.equal(options.env.LD_PRELOAD, undefined);
    return { status: 0 };
  };
  assert.deepEqual(checkWslSandbox(bundle, { env: { LD_PRELOAD: '/bad' }, probe, helperTrusted: () => false }), sandbox);
  assert.equal(checkWslSandbox(bundle, { helperTrusted: () => true, probe: () => { throw new Error('must not run'); } }).sandbox, 'setuid-helper');
  for (const result of [{ status: 1 }, { status: null, error: new Error('timeout') }]) {
    assert.throws(() => checkWslSandbox(bundle, { helperTrusted: () => false, probe: () => result }), error => error.code === 'sandbox_unavailable');
  }
});

test('spawn uses bundled executable, local GUI and disposable caches without inherited overrides', t => {
  const f = fixture(t), bundle = f.load(), layout = sharedSessionLayout(f.base, 'seat');
  const env = { PATH: '/usr/bin:/bin', DISPLAY: ':0', XDG_RUNTIME_DIR: '/mnt/wslg/runtime-dir',
    NODE_OPTIONS: '--require secret', ELECTRON_RUN_AS_NODE: '1', VSCODE_PORTABLE: '/old',
    LD_PRELOAD: '/bad', AUTOGRADE_SESSION_ROOT: '/old', CHROME_DEVEL_SANDBOX: '/untrusted', WSLENV: 'NODE_OPTIONS',
    BASH_ENV: '/shared/startup', SSH_AUTH_SOCK: '/shared/agent' };
  const clean = cleanWslEnvironment(env);
  assert.deepEqual(Object.keys(clean).sort(), ['DISPLAY', 'PATH', 'XDG_RUNTIME_DIR']);
  const spec = wslLaunchSpec(bundle, layout, environment, sandbox, env);
  assert.equal(spec.executable, path.join(f.install, f.manifest.executable));
  assert.equal(spec.options.env.AUTOGRADE_LEARNING_LIFETIME, 'shared');
  assert.equal(spec.options.env.XDG_CONFIG_HOME, path.join(layout.root, 'config'));
  assert.equal(spec.options.env.XDG_CACHE_HOME, path.join(layout.root, 'cache'));
  assert.equal(spec.options.env.HOME, path.join(layout.root, 'home'));
  assert.equal(spec.options.env.HISTFILE, path.join(layout.root, 'home/.shell_history'));
  assert.equal(spec.options.env.BASH_ENV, undefined); assert.equal(spec.options.env.SSH_AUTH_SOCK, undefined);
  assert.equal(spec.options.env.XDG_RUNTIME_DIR, '/mnt/wslg/runtime-dir');
  assert.equal(spec.options.env.NODE_OPTIONS, undefined);
  assert.equal(spec.options.env.CHROME_DEVEL_SANDBOX, undefined);
  assert.equal(spec.options.shell, false); assert.equal(spec.options.detached, true);
  assert.ok(spec.args.includes('--ozone-platform=x11')); assert.ok(!spec.args.includes('--no-sandbox'));
  assert.equal(wslRuntimeBase(1000), '/tmp/ag-wsl-lab-1000');
  assert.throws(() => wslRuntimeBase(0));
  assert.throws(() => wslLaunchSpec(bundle, sharedSessionLayout('/tmp/' + 'x'.repeat(120), 'seat'), environment, sandbox));
});

test('actual child exit erases code, materials, IDE state and XDG/temp data but preserves exported ZIP', async t => {
  const f = fixture(t), events = [], bundle = f.load();
  const exported = path.join(f.root, 'student-export.zip'); write(exported, 'independent export');
  let previous;
  const result = await runWslSession(bundle, { ...f.options, report: event => {
    events.push(event.event); if (event.event !== 'ide-started') return;
    [previous] = roots(f.base);
    for (const file of ['workspace/main.cpp', 'materials/lecture.pdf', 'r/user-data/Backups/code', 'home/.bash_history', 'home/.gitconfig', 'cache/code', 'config/code', 'data/code', 'state/code', 'tmp/code']) write(path.join(previous, file), 'student private code');
  } });
  assert.equal(result.code, 0); assert.deepEqual(events, ['ide-started', 'session-cleaned']);
  assert.equal(fs.existsSync(previous), false); assert.equal(fs.readFileSync(exported, 'utf8'), 'independent export');
  await runWslSession(bundle, { ...f.options, report: event => {
    if (event.event !== 'ide-started') return;
    const [current] = roots(f.base); assert.notEqual(current, previous);
    assert.deepEqual(fs.readdirSync(path.join(current, 'workspace')), []);
  } });
  assert.deepEqual(roots(f.base), []);
});

test('second Windows launcher cannot bypass a live Linux session lease', async t => {
  const f = fixture(t), bundle = f.load();
  let started; const signal = new Promise(resolve => { started = resolve; });
  const first = runWslSession(bundle, { ...f.options,
    start: (_exe, _args, options) => spawn(process.execPath, ['-e', 'setTimeout(() => {}, 250)'], options),
    report: event => { if (event.event === 'ide-started') started(); } });
  await signal;
  await assert.rejects(runWslSession(bundle, f.options), /active/);
  assert.equal(roots(f.base).length, 1); await first;
  assert.deepEqual(roots(f.base), []);
});

test('spawn failure and IDE crash still complete owned-session cleanup', async t => {
  const f = fixture(t), bundle = f.load();
  await assert.rejects(runWslSession(bundle, { ...f.options, start: () => { throw new Error('fixture spawn failure'); } }), /fixture/);
  assert.deepEqual(roots(f.base), []);
  await assert.rejects(runWslSession({ ...bundle, executable: path.join(f.root, 'missing') }, { ...f.options, start: spawn }), /ENOENT/);
  assert.deepEqual(roots(f.base), []);
  const result = await runWslSession(bundle, { ...f.options,
    start: (_exe, _args, options) => spawn(process.execPath, ['-e', 'process.exit(7)'], options) });
  assert.equal(result.code, 7); assert.deepEqual(roots(f.base), []);
});

test('previous stopped session is cleaned before the next student, without touching exports', async t => {
  const f = fixture(t), old = prepareSharedSession(sharedSessionLayout(f.base, 'seat'), {});
  write(path.join(old.workspace, 'old.c'), 'old code');
  for (const file of [path.join(old.root, SESSION_MARKER), path.join(f.base, '.autograde-shared-lock.json')]) {
    write(file, { ...JSON.parse(fs.readFileSync(file)), launcherPid: 99999999 });
  }
  await runWslSession(f.load(), { ...f.options, report: event => {
    if (event.event === 'ide-started') assert.equal(fs.existsSync(old.root), false);
  } });
  assert.deepEqual(roots(f.base), []);
});

test('ownership damage and insecure storage never authorize deleting previous student files', async t => {
  const f = fixture(t), old = prepareSharedSession(sharedSessionLayout(f.base, 'seat'), {});
  const file = path.join(old.root, SESSION_MARKER), original = fs.readFileSync(file, 'utf8');
  write(file, { ...JSON.parse(original), rootIno: 0 });
  await assert.rejects(runWslSession(f.load(), f.options));
  assert.ok(fs.existsSync(old.root)); write(file, original); cleanupSharedSession(old);
  await assert.rejects(runWslSession(f.load(), { ...f.options, statfs: () => ({ type: 0x01021997 }) }), error => error.code === 'unsafe_install');
  assert.deepEqual(roots(f.base), []);
});

test('alias invocation uses file identity and native errors never forward child logs', t => {
  const f = fixture(t), file = path.join(f.install, 'autograde/wsl-launcher.mjs'); write(file, '// fixture');
  const alias = path.join(f.root, 'alias'); fs.symlinkSync(f.install, alias);
  assert.ok(isWslLauncherEntry(path.join(alias, 'autograde/wsl-launcher.mjs'), pathToFileURL(file)));
  assert.equal(isWslLauncherEntry(undefined), false);
  assert.equal(wslFailure(new WslLaunchError('root_user', '일반 사용자로 실행하세요.')).code, 'root_user');
  assert.match(wslFailure(new Error('Another shared IDE session is active')).message, /이전 실습/);
  assert.doesNotMatch(wslFailure(new Error('password=secret-code')).message, /secret-code/);
});

test('entry rejects unsupported arguments with a structured error instead of silent success', () => {
  const script = new URL('./wsl-launcher.mjs', import.meta.url);
  const result = spawnSync(process.execPath, [script.pathname, '--unsafe-disable-sandbox'], { encoding: 'utf8' });
  assert.equal(result.status, 1);
  const event = JSON.parse(result.stdout.trim());
  assert.equal(event.event, 'error'); assert.equal(event.code, 'bundle_invalid');
});
