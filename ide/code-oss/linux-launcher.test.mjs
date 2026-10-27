import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { pathToFileURL } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import test from 'node:test';
import { assertNativeLinuxStorage, loadLinuxBundle, cleanLinuxEnvironment, inspectLinuxEnvironment, checkLinuxSandbox,
  preflightLinux, linuxRuntimeBase, linuxLaunchSpec, runLinuxSession, linuxFailure, LinuxLaunchError, isLinuxLauncherEntry, checkLinuxLibraries, showLinuxFailure } from './linux-launcher.mjs';
import { sharedSessionLayout, prepareSharedSession, cleanupSharedSession, SESSION_MARKER } from './shared-session.mjs';

// Native Linux prerequisites are simulated here. Session tests use real POSIX child
// processes and files; this does not substitute for a native Linux desktop acceptance run.
const linuxDisk = () => ({ type: 0xef53 });
const environment = { gui: 'x11', uid: 1000 };
const libraries = { libraries: 'available' };
const sandbox = { sandbox: 'user-namespace' };
function write(file, value, mode = 0o600) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, typeof value === 'string' ? value : JSON.stringify(value), { mode });
}
function fixture(t) {
  const parent = fs.realpathSync(os.tmpdir());
  const short = process.platform === 'darwin' ? '/private/tmp' : parent;
  const root = fs.realpathSync(fs.mkdtempSync(path.join(parent, 'ag-linux-test-')));
  const base = fs.realpathSync(fs.mkdtempSync(path.join(short, 'ag-w-')));
  t.after(() => { fs.rmSync(root, { recursive: true, force: true }); fs.rmSync(base, { recursive: true, force: true }); });
  const install = path.join(root, '앱 설치 위치');
  const manifest = { schema: 1, platform: 'linux', arch: process.arch, version: '1.143.0',
    nodeVersion: process.versions.node, extensionVersion: '0.5.12', lifecycle: 'shared', policy: 'standard', distribution: 'linux-desktop-shared-lab',
    editorRoot: 'editor/app', executable: 'editor/electron/autograde-ide', node: 'runtime/bin/node',
    approvedBuiltins: ['autograde', 'autograde-ide-profile'] };
  write(path.join(install, 'linux-package.json'), manifest);
  write(path.join(install, 'autograde/runtime-settings.json'), { 'files.hotExit': 'onExit' });
  write(path.join(install, 'editor/app/out/main.js'), '// fake compiled editor');
  write(path.join(install, manifest.executable), '#!/bin/sh\nexit 0\n', 0o700);
  write(path.join(install, manifest.node), 'runtime placeholder', 0o700);
  const load = () => loadLinuxBundle(install, { platform: 'linux', statfs: linuxDisk });
  const options = { runtimeBase: base, environment, sandbox, statfs: linuxDisk,
    start: (_exe, _args, opts) => spawn(process.execPath, ['-e', 'setTimeout(() => {}, 90)'], opts) };
  return { root, base, install, manifest, load, options };
}
const roots = base => fs.readdirSync(base).filter(name => name === 'current' || /^e-[0-9a-f]{24}$/.test(name)).map(name => path.join(base, name));
const linuxHost = { platform: 'linux', release: '6.8.0-generic', uid: 1000,
  env: { DISPLAY: ':0' }, socketExists: file => file === '/tmp/.X11-unix/X0' };

test('preflight accepts local native X11 and Wayland endpoints, and reports shared cleanup contract', t => {
  const f = fixture(t);
  assert.deepEqual(inspectLinuxEnvironment(linuxHost), environment);
  const wayland = inspectLinuxEnvironment({ ...linuxHost, env: { WAYLAND_DISPLAY: 'wayland-0', XDG_RUNTIME_DIR: '/run/user/1000' },
    runtimeDirectory: () => true, socketExists: file => file === '/run/user/1000/wayland-0' });
  assert.equal(wayland.gui, 'wayland');
  const ready = preflightLinux(f.load(), { environment, sandbox, libraries });
  assert.equal(ready.event, 'ready'); assert.equal(ready.schema, 1);
  assert.equal(ready.platform, 'linux-desktop'); assert.equal(ready.lifetime, 'shared');
  assert.equal(ready.noSandbox, false); assert.equal(ready.uid, 1000);
  assert.equal(ready.libraries, 'available');
  assert.deepEqual(roots(f.base), []);
});

test('other OS, WSL, root and unavailable or external graphics fail with actionable errors', async t => {
  for (const [changes, code] of [
    [{ platform: 'darwin' }, 'linux_required'], [{ release: '6.6.87.2-microsoft-standard-WSL2' }, 'linux_required'],
    [{ release: '4.4.0-19041-Microsoft' }, 'linux_required'], [{ uid: 0 }, 'root_user'],
    [{ socketExists: () => false }, 'gui_unavailable'],
    [{ env: { DISPLAY: '192.0.2.1:0' }, socketExists: () => true }, 'gui_unavailable'],
    [{ env: { XDG_RUNTIME_DIR: '/run/user/1000', WAYLAND_DISPLAY: '../foreign' }, socketExists: () => true }, 'gui_unavailable'],
  ]) await t.test(code + JSON.stringify(changes), () => assert.throws(() => inspectLinuxEnvironment({ ...linuxHost, ...changes }), error => error.code === code));
});

test('bundle rejects Windows or unknown filesystems, mismatched runtime and escaped paths', async t => {
  const f = fixture(t);
  assert.equal(assertNativeLinuxStorage(f.install, linuxDisk), fs.realpathSync(f.install));
  for (const type of [0x01021997, 0x53464846, 0xff534d42, 0x65735546, 0]) {
    assert.throws(() => assertNativeLinuxStorage(f.install, () => ({ type })), error => error.code === 'unsafe_install');
  }
  assert.throws(() => loadLinuxBundle(f.install, { platform: 'darwin' }), error => error.code === 'linux_required');
  for (const change of [{ nodeVersion: '0.0.0' }, { arch: 'wrong' }, { lifecycle: 'persistent' },
    { editorRoot: '../outside' }, { node: '/usr/bin/node' }, { approvedBuiltins: ['../outside'] }]) await t.test(JSON.stringify(change), t => {
    const f = fixture(t); write(path.join(f.install, 'linux-package.json'), { ...f.manifest, ...change });
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
  assert.deepEqual(checkLinuxSandbox(bundle, { env: { LD_PRELOAD: '/bad' }, probe, helperTrusted: () => false }), sandbox);
  assert.equal(checkLinuxSandbox(bundle, { helperTrusted: () => true, probe: () => { throw new Error('must not run'); } }).sandbox, 'setuid-helper');
  for (const result of [{ status: 1 }, { status: null, error: new Error('timeout') }]) {
    assert.throws(() => checkLinuxSandbox(bundle, { helperTrusted: () => false, probe: () => result }), error => error.code === 'sandbox_unavailable');
  }
});

test('spawn uses bundled executable, local GUI and disposable caches without inherited overrides', t => {
  const f = fixture(t), bundle = f.load(), layout = sharedSessionLayout(f.base, 'seat');
  const env = { PATH: '/usr/bin:/bin', DISPLAY: ':0', XDG_RUNTIME_DIR: '/run/user/1000',
    NODE_OPTIONS: '--require secret', ELECTRON_RUN_AS_NODE: '1', VSCODE_PORTABLE: '/old',
    LD_PRELOAD: '/bad', AUTOGRADE_SESSION_ROOT: '/old', CHROME_DEVEL_SANDBOX: '/untrusted', WSLENV: 'NODE_OPTIONS',
    BASH_ENV: '/shared/startup', SSH_AUTH_SOCK: '/shared/agent' };
  const clean = cleanLinuxEnvironment(env);
  assert.deepEqual(Object.keys(clean).sort(), ['DISPLAY', 'PATH', 'XDG_RUNTIME_DIR']);
  const spec = linuxLaunchSpec(bundle, layout, environment, sandbox, env);
  assert.equal(spec.executable, path.join(f.install, f.manifest.executable));
  assert.equal(spec.options.env.AUTOGRADE_LEARNING_LIFETIME, 'shared');
  assert.equal(spec.options.env.XDG_CONFIG_HOME, path.join(layout.root, 'config'));
  assert.equal(spec.options.env.XDG_CACHE_HOME, path.join(layout.root, 'cache'));
  assert.equal(spec.options.env.HOME, path.join(layout.root, 'home'));
  assert.equal(spec.options.env.HISTFILE, path.join(layout.root, 'home/.shell_history'));
  assert.equal(spec.options.env.BASH_ENV, undefined); assert.equal(spec.options.env.SSH_AUTH_SOCK, undefined);
  assert.equal(spec.options.env.XDG_RUNTIME_DIR, '/run/user/1000');
  assert.equal(spec.options.env.NODE_OPTIONS, undefined);
  assert.equal(spec.options.env.CHROME_DEVEL_SANDBOX, undefined);
  assert.equal(spec.options.shell, false); assert.equal(spec.options.detached, true);
  assert.ok(spec.args.includes('--ozone-platform=x11')); assert.ok(!spec.args.includes('--no-sandbox'));
  assert.equal(linuxRuntimeBase(1000), '/tmp/ag-linux-lab-1000');
  assert.throws(() => linuxRuntimeBase(0));
  assert.throws(() => linuxLaunchSpec(bundle, sharedSessionLayout('/tmp/' + 'x'.repeat(120), 'seat'), environment, sandbox));
});

test('actual child exit erases code, materials, IDE state and XDG/temp data but preserves exported ZIP', async t => {
  const f = fixture(t), events = [], bundle = f.load();
  const exported = path.join(f.root, 'student-export.zip'); write(exported, 'independent export');
  let previous;
  const result = await runLinuxSession(bundle, { ...f.options, report: event => {
    events.push(event.event); if (event.event !== 'ide-started') return;
    [previous] = roots(f.base);
    for (const file of ['workspace/main.cpp', 'materials/lecture.pdf', 'r/user-data/Backups/code', 'home/.bash_history', 'home/.gitconfig', 'cache/code', 'config/code', 'data/code', 'state/code', 'tmp/code']) write(path.join(previous, file), 'student private code');
  } });
  assert.equal(result.code, 0); assert.deepEqual(events, ['ide-started', 'session-cleaned']);
  assert.equal(fs.existsSync(previous), false); assert.equal(fs.readFileSync(exported, 'utf8'), 'independent export');
  await runLinuxSession(bundle, { ...f.options, report: event => {
    if (event.event !== 'ide-started') return;
    const [current] = roots(f.base); assert.equal(current, previous);
    assert.deepEqual(fs.readdirSync(path.join(current, 'workspace')), []);
  } });
  assert.deepEqual(roots(f.base), []);
});

test('second Linux desktop launcher cannot bypass a live Linux session lease', async t => {
  const f = fixture(t), bundle = f.load();
  let started; const signal = new Promise(resolve => { started = resolve; });
  const first = runLinuxSession(bundle, { ...f.options,
    start: (_exe, _args, options) => spawn(process.execPath, ['-e', 'setTimeout(() => {}, 250)'], options),
    report: event => { if (event.event === 'ide-started') started(); } });
  await signal;
  await assert.rejects(runLinuxSession(bundle, f.options), /active/);
  assert.equal(roots(f.base).length, 1); await first;
  assert.deepEqual(roots(f.base), []);
});

test('spawn failure and IDE crash still complete owned-session cleanup', async t => {
  const f = fixture(t), bundle = f.load();
  await assert.rejects(runLinuxSession(bundle, { ...f.options, start: () => { throw new Error('fixture spawn failure'); } }), /fixture/);
  assert.deepEqual(roots(f.base), []);
  await assert.rejects(runLinuxSession({ ...bundle, executable: path.join(f.root, 'missing') }, { ...f.options, start: spawn }), /ENOENT/);
  assert.deepEqual(roots(f.base), []);
  const result = await runLinuxSession(bundle, { ...f.options,
    start: (_exe, _args, options) => spawn(process.execPath, ['-e', 'process.exit(7)'], options) });
  assert.equal(result.code, 7); assert.deepEqual(roots(f.base), []);
});

test('previous stopped session is cleaned before the next student, without touching exports', async t => {
  const f = fixture(t), old = prepareSharedSession(sharedSessionLayout(f.base, 'seat'), {});
  write(path.join(old.workspace, 'old.c'), 'old code');
  for (const file of [path.join(old.root, SESSION_MARKER), path.join(f.base, '.autograde-shared-lock.json')]) {
    write(file, { ...JSON.parse(fs.readFileSync(file)), launcherPid: 99999999 });
  }
  await runLinuxSession(f.load(), { ...f.options, report: event => {
    if (event.event === 'ide-started') {
      assert.equal(fs.existsSync(path.join(old.workspace, 'old.c')), false);
      assert.notEqual(JSON.parse(fs.readFileSync(path.join(old.root, SESSION_MARKER))).sessionId, old.sessionId);
    }
  } });
  assert.deepEqual(roots(f.base), []);
});

test('ownership damage and insecure storage never authorize deleting previous student files', async t => {
  const f = fixture(t), old = prepareSharedSession(sharedSessionLayout(f.base, 'seat'), {});
  const file = path.join(old.root, SESSION_MARKER), original = fs.readFileSync(file, 'utf8');
  write(file, { ...JSON.parse(original), rootIno: 0 });
  await assert.rejects(runLinuxSession(f.load(), f.options));
  assert.ok(fs.existsSync(old.root)); write(file, original); cleanupSharedSession(old);
  await assert.rejects(runLinuxSession(f.load(), { ...f.options, statfs: () => ({ type: 0x01021997 }) }), error => error.code === 'unsafe_install');
  assert.deepEqual(roots(f.base), []);
});

test('alias invocation uses file identity and launcher errors never forward child logs', t => {
  const f = fixture(t), file = path.join(f.install, 'autograde/linux-launcher.mjs'); write(file, '// fixture');
  const alias = path.join(f.root, 'alias'); fs.symlinkSync(f.install, alias);
  assert.ok(isLinuxLauncherEntry(path.join(alias, 'autograde/linux-launcher.mjs'), pathToFileURL(file)));
  assert.equal(isLinuxLauncherEntry(undefined), false);
  assert.equal(linuxFailure(new LinuxLaunchError('root_user', '일반 사용자로 실행하세요.')).code, 'root_user');
  assert.match(linuxFailure(new Error('Another shared IDE session is active')).message, /이전 실습/);
  assert.doesNotMatch(linuxFailure(new Error('password=secret-code')).message, /secret-code/);
});

test('entry rejects unsupported arguments with a structured error instead of silent success', () => {
  const script = new URL('./linux-launcher.mjs', import.meta.url);
  const result = spawnSync(process.execPath, [script.pathname, '--unsafe-disable-sandbox'], { encoding: 'utf8' });
  assert.equal(result.status, 1);
  const event = JSON.parse(result.stdout.trim());
  assert.equal(event.event, 'error'); assert.equal(event.code, 'bundle_invalid');
});


test('library preflight rejects missing dependencies without echoing paths or child output', t => {
  const bundle = fixture(t).load();
  const probe = (executable, args, options) => {
    assert.equal(executable, '/usr/bin/ldd'); assert.deepEqual(args, [bundle.executable]);
    assert.equal(options.shell, false); assert.equal(options.env.LD_PRELOAD, undefined);
    return { status: 0, stdout: 'libc.so.6 => /lib/libc.so.6' };
  };
  assert.deepEqual(checkLinuxLibraries(bundle, { env: { LD_PRELOAD: '/bad' }, probe }), libraries);
  for (const result of [{ status: 0, stdout: 'libgtk-3.so.0 => not found' }, { status: 0, stderr: "GLIBC_2.38 not found" }, { status: 1 }, { error: new Error('private details') }]) {
    assert.throws(() => checkLinuxLibraries(bundle, { probe: () => result }), error => error.code === 'libraries_unavailable' && !error.message.includes('private details'));
  }
});

test('Wayland requires private runtime ownership and X11 authority survives disposable HOME', t => {
  assert.throws(() => inspectLinuxEnvironment({ ...linuxHost, env: { WAYLAND_DISPLAY: 'wayland-0', XDG_RUNTIME_DIR: '/tmp/foreign' }, runtimeDirectory: () => false, socketExists: () => true }), error => error.code === 'gui_unavailable');
  const f = fixture(t), originalHome = path.join(f.root, 'original home');
  write(path.join(originalHome, '.Xauthority'), 'desktop auth');
  const layout = sharedSessionLayout(f.base, 'seat');
  const spec = linuxLaunchSpec(f.load(), layout, environment, sandbox, { HOME: originalHome, DISPLAY: ':0', GITHUB_TOKEN: 'private', PYTHONSTARTUP: '/bad', GIT_CONFIG_GLOBAL: '/bad' });
  assert.equal(spec.options.env.XAUTHORITY, path.join(originalHome, '.Xauthority'));
  assert.equal(spec.options.env.HOME, path.join(layout.root, 'home'));
  assert.equal(spec.options.env.GITHUB_TOKEN, undefined); assert.equal(spec.options.env.PYTHONSTARTUP, undefined);
  assert.equal(spec.options.env.GIT_CONFIG_GLOBAL, undefined);
});

test('desktop failure dialogs pass text as a literal argument and never use a shell', () => {
  const message = '$(touch injected); "quoted"'; let called = false;
  showLinuxFailure({ message }, { exists: file => file === '/usr/bin/zenity', env: { NODE_OPTIONS: 'bad', DISPLAY: ':0' }, run: (exe, args, options) => {
    called = true; assert.equal(exe, '/usr/bin/zenity'); assert.equal(args.at(-1), message);
    assert.equal(options.shell, false); assert.equal(options.env.NODE_OPTIONS, undefined);
  } });
  assert.equal(called, true);
});
