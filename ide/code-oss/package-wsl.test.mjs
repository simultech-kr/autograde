import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { elfArchitecture, electronRuntimePlan, wslEntryScript, wslManifest, assembleWslPackage,
  archiveWslPackage, fileSha256, parseWslOptions, packageWsl } from './package-wsl.mjs';

function elf(arch = 'x64') {
  const bytes = Buffer.alloc(64);
  Buffer.from([0x7f, 0x45, 0x4c, 0x46, 2, 1, 1]).copy(bytes);
  bytes.writeUInt16LE(3, 16); bytes.writeUInt16LE(arch === 'x64' ? 62 : 183, 18);
  return bytes;
}
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ag-wsl-package-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  function write(name, value = 'fixture', mode = 0o644) {
    const file = path.join(root, name); fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, Buffer.isBuffer(value) || typeof value === 'string' ? value : JSON.stringify(value), { mode }); return file;
  }
  const source = path.join(root, 'source'), electronRoot = path.join(root, 'electron'), launcherRoot = path.join(root, 'launcher');
  const node = write('node/bin/node', elf(), 0o755), nodeLicense = write('node/LICENSE', 'Node license');
  write('electron/autograde-ide', elf(), 0o755);
  write('electron/chrome-sandbox', elf(), 0o755);
  write('electron/LICENSE', 'Electron license'); write('electron/LICENSES.chromium.html', 'Chromium notices');
  write('electron/locales/ko.pak'); write('electron/resources/default_app.asar'); write('electron/libEGL.so', 'native library');
  write('source/out/main.js', '// compiled editor');
  write('source/LICENSE.txt', 'Code OSS license');
  write('source/node_modules/runtime/index.js', '// production module');
  write('source/.env', 'MUST NOT SHIP'); write('source/.git/config', 'MUST NOT SHIP'); write('source/student_roster.csv', 'MUST NOT SHIP');
  for (const file of ['wsl-launcher.mjs', 'shared-session.mjs', 'learning-profile.mjs', 'seat-reporter.mjs']) write(`launcher/${file}`, '// bundled launcher');
  write('launcher/runtime-settings.json', {});
  const editorPlan = { files: ['out/main.js', 'LICENSE.txt', 'node_modules/runtime/index.js'], links: {}, approvedBuiltins: ['autograde', 'autograde-ide-profile'] };
  const manifest = wslManifest({ version: '1.143.0', arch: 'x64', nodeVersion: '24.18.0', extensionVersion: '0.5.12',
    binaryName: 'autograde-ide', approvedBuiltins: editorPlan.approvedBuiltins, upstreamCommit: 'a'.repeat(40) });
  const options = { source, node, nodeLicense, electronRoot, electronFiles: electronRuntimePlan(electronRoot, 'autograde-ide', 'x64'),
    editorPlan, launcherRoot, destination: path.join(root, '한글 runtime'), manifest };
  return { root, write, options };
}

test('Node and Electron architecture is read from ELF headers rather than names', t => {
  const f = fixture(t);
  assert.equal(elfArchitecture(f.options.node), 'x64');
  assert.equal(elfArchitecture(f.write('aarch64', elf('arm64'))), 'arm64');
  assert.throws(() => electronRuntimePlan(f.options.electronRoot, 'autograde-ide', 'arm64'), /architecture differs/);
});

test('Mach-O, PE, truncated, 32-bit, big-endian and unsupported ELF binaries are rejected', async t => {
  for (const [name, value] of [
    ['Mach-O', Buffer.from([0xcf, 0xfa, 0xed, 0xfe])],
    ['PE', Buffer.from('MZnotlinux')], ['truncated', elf().subarray(0, 40)],
    ['32-bit', (() => { const b = elf(); b[4] = 1; return b; })()],
    ['big-endian', (() => { const b = elf(); b[5] = 2; return b; })()],
    ['object-only', (() => { const b = elf(); b.writeUInt16LE(1, 16); return b; })()],
    ['unsupported', (() => { const b = elf(); b.writeUInt16LE(40, 18); return b; })()],
  ]) await t.test(name, t => { const f = fixture(t); assert.throws(() => elfArchitecture(f.write(name, value)), /ELF|architecture/); });
});

test('Electron payload includes GUI resources, libraries and vendor licenses', t => {
  const f = fixture(t), files = f.options.electronFiles;
  for (const file of ['autograde-ide', 'chrome-sandbox', 'locales/ko.pak', 'resources/default_app.asar', 'libEGL.so', 'LICENSE', 'LICENSES.chromium.html']) assert.ok(files.includes(file), file);
  assert.throws(() => electronRuntimePlan(f.options.electronRoot, '../outside', 'x64'), /Invalid/);
  fs.unlinkSync(path.join(f.options.electronRoot, 'LICENSES.chromium.html'));
  assert.throws(() => electronRuntimePlan(f.options.electronRoot, 'autograde-ide', 'x64'));
});

test('a valid ELF binary without execute permission is rejected before copying', t => {
  const f = fixture(t); fs.chmodSync(path.join(f.options.electronRoot, 'autograde-ide'), 0o644);
  assert.throws(() => electronRuntimePlan(f.options.electronRoot, 'autograde-ide', 'x64'), /EACCES/);
});

test('Electron file links stay inside its runtime; external and directory links fail', t => {
  const f = fixture(t), link = path.join(f.options.electronRoot, 'libAlias.so');
  fs.symlinkSync('libEGL.so', link);
  assert.ok(electronRuntimePlan(f.options.electronRoot, 'autograde-ide', 'x64').includes('libAlias.so'));
  fs.unlinkSync(link); fs.symlinkSync(f.options.node, link);
  assert.throws(() => electronRuntimePlan(f.options.electronRoot, 'autograde-ide', 'x64'), /external or directory/);
  fs.unlinkSync(link); fs.symlinkSync('locales', link);
  assert.throws(() => electronRuntimePlan(f.options.electronRoot, 'autograde-ide', 'x64'), /external or directory/);
});

test('suspicious credentials or repository data inside Electron cause packaging to stop', t => {
  const f = fixture(t); f.write('electron/.env', 'DO NOT COPY');
  assert.throws(() => electronRuntimePlan(f.options.electronRoot, 'autograde-ide', 'x64'), /private data/);
});

test('manifest fixes shared mode and uses only relative installed paths', t => {
  const f = fixture(t), manifest = f.options.manifest;
  assert.equal(manifest.node, 'runtime/bin/node'); assert.equal(manifest.executable, 'editor/electron/autograde-ide');
  assert.equal(manifest.platform, 'linux'); assert.equal(manifest.lifecycle, 'shared');
  assert.equal(manifest.requires.wsl, 2); assert.equal(manifest.requires.gui, 'WSLg');
  assert.equal(manifest.requires.linuxFilesystem, true);
  assert.match(manifest.requires.sandbox, /user-namespace/);
  assert.throws(() => wslManifest({ ...manifest, binaryName: '../evil' }), /metadata/);
});

test('assembled payload is self-contained and excludes nonallowlisted source data', t => {
  const f = fixture(t), target = assembleWslPackage(f.options);
  for (const name of ['bin/autograde-ide', 'runtime/bin/node', 'runtime/LICENSE', 'autograde/wsl-launcher.mjs',
    'autograde/shared-session.mjs', 'autograde/learning-profile.mjs', 'autograde/runtime-settings.json',
    'editor/app/out/main.js', 'editor/app/LICENSE.txt', 'editor/electron/locales/ko.pak', 'wsl-package.json']) assert.ok(fs.existsSync(path.join(target, name)), name);
  for (const name of ['editor/app/.git', 'editor/app/.env', 'editor/app/student_roster.csv']) assert.equal(fs.existsSync(path.join(target, name)), false);
  assert.equal(fs.statSync(path.join(target, 'bin/autograde-ide')).mode & 0o777, 0o755);
  assert.equal(fs.readFileSync(path.join(target, 'runtime/LICENSE'), 'utf8'), 'Node license');
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(target, 'wsl-package.json'))), f.options.manifest);
});

test('package copying does not grant setuid/root privileges to Chromium helpers', t => {
  const f = fixture(t), helper = path.join(f.options.electronRoot, 'chrome-sandbox');
  fs.chmodSync(helper, 0o4755);
  const target = assembleWslPackage(f.options);
  assert.equal(fs.statSync(path.join(target, 'editor/electron/chrome-sandbox')).mode & 0o7777, 0o755);
  assert.doesNotMatch(wslEntryScript(), /--no-sandbox|sudo|chmod/);
});

test('assembled dependencies preserve internal relative links without escaping the payload', t => {
  const f = fixture(t);
  f.options.editorPlan.links = { 'node_modules/alias': 'runtime' };
  const target = assembleWslPackage(f.options);
  assert.equal(fs.readFileSync(path.join(target, 'editor/app/node_modules/alias/index.js'), 'utf8'), '// production module');
});

test('unsafe relative inputs and escaping links cannot write outside a new package', async t => {
  for (const kind of ['traversal', 'absolute', 'link']) await t.test(kind, t => {
    const f = fixture(t);
    if (kind === 'traversal') f.options.editorPlan.files.push('../../outside');
    if (kind === 'absolute') f.options.electronFiles.push('/etc/passwd');
    if (kind === 'link') f.options.editorPlan.links = { 'node_modules/escape': '../../../../outside' };
    assert.throws(() => assembleWslPackage(f.options), /relative runtime path|escapes/);
    assert.equal(fs.existsSync(path.join(f.root, 'outside')), false);
  });
});

test('existing targets and build output inside the source are never overwritten', t => {
  const f = fixture(t); assembleWslPackage(f.options);
  assert.throws(() => assembleWslPackage(f.options), /already exists/);
  const before = fs.readFileSync(path.join(f.options.destination, 'wsl-package.json'), 'utf8');
  assert.equal(before, fs.readFileSync(path.join(f.options.destination, 'wsl-package.json'), 'utf8'));
  assert.throws(() => assembleWslPackage({ ...f.options, destination: path.join(f.options.source, 'package') }), /outside its input/);
});

test('entry script runs bundled runtime from a moved Unicode/spaced path and clears interpreter injection', t => {
  const f = fixture(t), target = assembleWslPackage(f.options);
  // A tiny fake runtime observes the real POSIX entry, not an implementation string.
  fs.writeFileSync(path.join(target, 'runtime/bin/node'), '#!/bin/sh\nprintf "%s\\n" "$NODE_OPTIONS_VALUE" "${NODE_OPTIONS-unset}" "${NODE_PATH-unset}" "${ELECTRON_RUN_AS_NODE-unset}" "$1" "$2"\n');
  const moved = path.join(f.root, '다른 폴더', 'IDE payload'); fs.mkdirSync(path.dirname(moved)); fs.renameSync(target, moved);
  const output = execFileSync('/bin/sh', [path.join(moved, 'bin/autograde-ide'), '--check'], { cwd: '/', encoding: 'utf8',
    env: { PATH: '/usr/bin:/bin', NODE_OPTIONS_VALUE: 'ordinary value', NODE_OPTIONS: '--require malicious.js', NODE_PATH: '/not-used', ELECTRON_RUN_AS_NODE: '1' } });
  const lines = output.trim().split('\n');
  assert.deepEqual(lines.slice(0, 4), ['ordinary value', 'unset', 'unset', 'unset']);
  // Filesystems can normalize Unicode; compare the resolved file identity.
  assert.equal(fs.statSync(lines[4]).ino, fs.statSync(path.join(moved, 'autograde/wsl-launcher.mjs')).ino);
  assert.equal(lines[5], '--check');
});

test('real tar archive retains executable modes, links, licenses and the manifest', t => {
  const f = fixture(t); f.options.editorPlan.links = { 'node_modules/alias': 'runtime' };
  const target = assembleWslPackage(f.options), archive = path.join(f.root, 'payload.tar.gz');
  const result = archiveWslPackage(target, archive);
  assert.equal(result.sha256, fileSha256(archive));
  assert.equal(fs.readFileSync(`${archive}.sha256`, 'utf8'), `${result.sha256}  payload.tar.gz\n`);
  const extracted = path.join(f.root, 'extracted'); fs.mkdirSync(extracted);
  execFileSync('tar', ['-xzf', archive, '-C', extracted]);
  const copied = path.join(extracted, path.basename(target));
  assert.equal(fs.statSync(path.join(copied, 'bin/autograde-ide')).mode & 0o777, 0o755);
  assert.equal(fs.readFileSync(path.join(copied, 'runtime/LICENSE'), 'utf8'), 'Node license');
  assert.equal(fs.lstatSync(path.join(copied, 'editor/app/node_modules/alias')).isSymbolicLink(), true);
  assert.equal(fs.readFileSync(path.join(copied, 'editor/app/node_modules/alias/index.js'), 'utf8'), '// production module');
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(copied, 'wsl-package.json'))), f.options.manifest);
  assert.throws(() => archiveWslPackage(target, archive), /never overwritten/);
  assert.throws(() => archiveWslPackage(target, path.join(target, 'recursive.tar.gz')), /outside its package/);
});

test('actual bundled launcher reads generated manifest after relocation without the source checkout', async t => {
  const f = fixture(t);
  f.options.launcherRoot = path.dirname(fileURLToPath(import.meta.url));
  const target = assembleWslPackage(f.options);
  const moved = path.join(f.root, 'moved real 런처'); fs.renameSync(target, moved);
  const imported = await import(pathToFileURL(path.join(moved, 'autograde/wsl-launcher.mjs')));
  const bundle = imported.loadWslBundle(moved, { platform: 'linux', arch: 'x64', nodeVersion: '24.18.0', statfs: () => ({ type: 0xef53 }) });
  assert.equal(bundle.manifest.lifecycle, 'shared');
  assert.equal(bundle.manifest.platform, 'linux');
  assert.equal(bundle.source, path.join(moved, 'editor/app'));
  assert.equal(bundle.executable, path.join(moved, 'editor/electron/autograde-ide'));
  assert.equal(bundle.defaults['files.hotExit'], undefined);
  assert.equal(bundle.defaults['telemetry.telemetryLevel'], 'off');
  assert.deepEqual(imported.preflightWsl(bundle, { environment: { gui: 'x11', uid: 1000, distribution: 'Ubuntu' }, sandbox: { sandbox: 'user-namespace' } }), {
    event: 'ready', schema: 1, platform: 'wsl2', architecture: 'x64', extensionVersion: '0.5.12',
    gui: 'x11', uid: 1000, distribution: 'Ubuntu', sandbox: 'user-namespace', lifetime: 'shared', noSandbox: false,
  });
});

test('streaming checksum matches standard SHA-256 and option parser rejects ambiguity', t => {
  const f = fixture(t), bytes = Buffer.alloc(3 * 1024 * 1024 + 19, 0x5a), file = f.write('large.bin', bytes);
  assert.equal(fileSha256(file), createHash('sha256').update(bytes).digest('hex'));
  assert.deepEqual(parseWslOptions(['--source', '/linux/src', '--node', '/linux/node', '--output', '/linux/release', '--no-archive']),
    { source: '/linux/src', node: '/linux/node', output: '/linux/release', archive: false });
  assert.throws(() => parseWslOptions(['--output']), /Usage/);
  assert.throws(() => parseWslOptions(['--output', '/a', '--output', '/b']), /Repeated/);
  assert.throws(() => parseWslOptions(['--no-sandbox']), /Usage/);
});

test('public build entry refuses non-Linux hosts instead of shipping Mac binaries as WSL', t => {
  if (process.platform === 'linux') return t.skip('Host restriction specifically verifies non-Linux builds');
  assert.throws(() => packageWsl({ source: '/unused', node: process.execPath, output: '/unused' }), /Build.*Linux/);
});
