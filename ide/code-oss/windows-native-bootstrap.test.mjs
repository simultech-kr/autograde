import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { windowsManifest } from './package-windows.mjs';
import { loadWindowsPackage, packagePath, prepareWindowsSession, windowsLaunchSpec } from './windows-native-bootstrap.mjs';

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-native-bootstrap-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const write = (name, data) => { const file = path.join(root, name); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, typeof data === 'object' ? JSON.stringify(data) : data); };
  write('windows-package.json', windowsManifest({ version: '1.143.0', extensionVersion: '0.5.14', nodeVersion: '24.18.0', upstreamCommit: 'a'.repeat(40), binaryName: 'Autograde IDE.exe', approvedBuiltins: ['autograde', 'autograde-ide-profile'] }));
  write('editor/app/out/main.js', ''); write('editor/electron/Autograde IDE.exe', ''); write('runtime/node.exe', ''); write('autograde/runtime-settings.json', { 'chat.disableAIFeatures': true });
  const bundle = loadWindowsPackage(root, { platform: 'win32', arch: 'x64', versions: { node: '24.18.0' } });
  const id = 'a'.repeat(24), session = path.join(root, 'sessions/current'), pid = process.pid;
  const owner = { schema: 1, sid: 'S-1-5-21-example', sessionId: id, root: session, launcherPid: pid,
    launcherStarted: '123', rootIdentity: '12345678:0000000000000001', jobName: `Local\\AutogradeNative-S-1-5-21-example-${id}` };
  write('sessions/current/.native-owner.json', owner);
  write('sessions/.current-owner.json', owner);
  return { root, write, bundle, session, pid, owner };
}
test('native bootstrap requires the matching bundled Windows runtime and contained package paths', t => {
  const { root } = fixture(t);
  assert.throws(() => loadWindowsPackage(root, { platform: 'linux', arch: 'x64', versions: { node: '24.18.0' } }), /mismatch/);
  assert.throws(() => packagePath(root, '../outside'), /Invalid/);
  assert.throws(() => packagePath(root, 'runtime/node.exe:stream'), /Invalid/);
});
test('session marker uses real inode/device, parent supervisor and policy-compatible runtime layout', t => {
  const { bundle, session, pid } = fixture(t);
  assert.throws(() => prepareWindowsSession(bundle, session, pid + 1, { parentPid: pid }), /supervisor/);
  const layout = prepareWindowsSession(bundle, session, pid, { parentPid: pid });
  const marker = JSON.parse(fs.readFileSync(path.join(session, '.autograde-session.json'), 'utf8')), stat = fs.statSync(session);
  assert.equal(marker.rootDev, stat.dev); assert.equal(marker.rootIno, stat.ino); assert.equal(marker.policy, 'standard');
  assert.equal(marker.sessionId, 'a'.repeat(24)); assert.equal(path.basename(layout.root), 'current');
  assert.equal(layout.workspace, path.join(session, 'workspace'));
  assert.equal(layout.runtime, path.join(session, 'r'));
  const tasks = JSON.parse(fs.readFileSync(path.join(layout.runtime, 'user-data/User/tasks.json'), 'utf8')).tasks;
  assert.ok(tasks[0].args.includes('/std:c++17')); assert.ok(tasks[1].args.includes('/std:c17'));
  assert.equal(tasks[0].options.cwd, path.join(session, 'build'));
  assert.equal(fs.existsSync(path.join(layout.workspace, '.vscode')), false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(layout.runtime, 'user-data/User/settings.json'), 'utf8'))['workbench.localHistory.enabled'], false);
});

test('new native launches use the fixed current path and must match parent ownership proof', t => {
  const { bundle, session, pid, write, owner } = fixture(t);
  const prepare = () => prepareWindowsSession(bundle, session, pid, { parentPid: pid });
  for (const field of ['sessionId', 'rootIdentity', 'launcherStarted', 'jobName', 'sid']) {
    write('sessions/.current-owner.json', { ...owner, [field]: 'changed' });
    assert.throws(prepare, /ownership mismatch/);
  }
  write('sessions/.current-owner.json', owner);
  assert.throws(() => prepareWindowsSession(bundle, path.join(path.dirname(session), `e-${owner.sessionId}`), pid, { parentPid: pid }), /supervisor/);
  write('sessions/current/.native-owner.json', {...owner, sessionId: 'not-a-session'});
  assert.throws(prepare, /ownership mismatch/);
  assert.equal(fs.existsSync(path.join(session, 'workspace')), false);
});

test('a fixed path can host a fresh identity only after the old session tree was cleared', t => {
  const { bundle, session, pid, write, owner } = fixture(t);
  const old = prepareWindowsSession(bundle, session, pid, {parentPid: pid});
  fs.writeFileSync(path.join(old.workspace, 'old-answer.cpp'), 'previous student');
  assert.throws(() => prepareWindowsSession(bundle, session, pid, {parentPid: pid}), /EEXIST/);
  fs.rmSync(session, {recursive: true});
  const fresh = {...owner, sessionId: 'b'.repeat(24), jobName: owner.jobName.replace(/a{24}$/, 'b'.repeat(24)), rootIdentity: '12345678:0000000000000002'};
  write('sessions/current/.native-owner.json', fresh); write('sessions/.current-owner.json', fresh);
  const next = prepareWindowsSession(bundle, session, pid, {parentPid: pid});
  assert.equal(next.workspace, old.workspace); assert.notEqual(next.id, old.id);
  assert.deepEqual(fs.readdirSync(next.workspace), []);
});
test('spawn environment removes interpreter injection, confines caches and carries shared-session hints', t => {
  const { bundle, session, pid } = fixture(t), layout = prepareWindowsSession(bundle, session, pid, { parentPid: pid });
  const spec = windowsLaunchSpec(bundle, layout, { PATH: 'MSVC', NODE_OPTIONS: '--require hostile', ELECTRON_RUN_AS_NODE: '1', AUTOGRADE_SESSION_ROOT: 'outside', INCLUDE: 'SDK' });
  assert.equal(spec.options.shell, false); assert.equal(spec.options.env.NODE_OPTIONS, undefined); assert.equal(spec.options.env.ELECTRON_RUN_AS_NODE, undefined);
  assert.equal(spec.options.env.AUTOGRADE_SESSION_ROOT, session); assert.equal(spec.options.env.AUTOGRADE_LEARNING_LIFETIME, 'shared');
  assert.equal(spec.options.env.INCLUDE, 'SDK'); assert.equal(spec.options.env.USERPROFILE, path.join(session, 'home'));
  assert.equal(spec.args.includes('--no-sandbox'), false); assert.ok(spec.args.includes(path.join(session, 'r/user-data')));
});
