import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { peArchitecture, windowsRelative, windowsElectronPlan, validateWindowsNativeModules, windowsManifest, assembleWindowsPackage, packageWindows } from './package-windows.mjs';

function fixture(t) { const root = fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-win-package-')); t.after(() => fs.rmSync(root, { recursive: true, force: true })); return root; }
function write(root, name, bytes) { const file = path.join(root, name); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, bytes); return file; }
function pe(machine = 0x8664) { const bytes = Buffer.alloc(128); bytes.write('MZ'); bytes.writeUInt32LE(64, 60); bytes.writeUInt32LE(0x4550, 64); bytes.writeUInt16LE(machine, 68); bytes.writeUInt16LE(0x20b, 88); return bytes; }
const metadata = { version: '1.143.0', extensionVersion: '0.5.14', nodeVersion: '24.18.0', upstreamCommit: 'a'.repeat(40), binaryName: 'Autograde IDE.exe', approvedBuiltins: ['autograde', 'autograde-ide-profile'] };

test('Windows payload binaries are identified by PE machine and 64-bit image headers', t => {
  const root = fixture(t);
  assert.equal(peArchitecture(write(root, 'node.exe', pe())), 'x64');
  assert.equal(peArchitecture(write(root, 'arm.node', pe(0xaa64))), 'arm64');
  for (const [name, data] of [['elf', Buffer.from('\x7fELF')], ['truncated', Buffer.from('MZ')], ['x86', pe(0x14c)]]) assert.throws(() => peArchitecture(write(root, name, data)));
  const wrongMagic = pe(); wrongMagic.writeUInt16LE(0x10b, 88); assert.throws(() => peArchitecture(write(root, 'pe32', wrongMagic)), /64-bit/);
});
test('Windows runtime paths reject traversal, device names and alternate streams', () => {
  assert.equal(windowsRelative('node_modules\\module\\addon.node'), 'node_modules/module/addon.node');
  for (const value of ['../outside', '/absolute', 'C:\\file', 'name:stream', 'dir/NUL.txt', 'dir/end.', 'dir/end ', 'dir//file']) assert.throws(() => windowsRelative(value));
});
test('native dependency validation rejects Linux/Mac or wrong-architecture active addons', t => {
  const root = fixture(t), file = 'node_modules/addon/build/Release/addon.node';
  write(root, file, pe()); assert.doesNotThrow(() => validateWindowsNativeModules(root, { files: [file] }));
  write(root, file, pe(0xaa64)); assert.throws(() => validateWindowsNativeModules(root, { files: [file] }), /Foreign/);
  write(root, file, Buffer.from('\x7fELF')); assert.throws(() => validateWindowsNativeModules(root, { files: [file] }), /Windows PE/);
});
test('Electron plan requires all resources and rejects foreign DLLs', t => {
  const root = fixture(t);
  for (const name of ['LICENSE', 'LICENSES.chromium.html', 'icudtl.dat', 'resources.pak', 'v8_context_snapshot.bin', 'locales/en-US.pak']) write(root, name, 'resource');
  write(root, 'Autograde IDE.exe', pe()); assert.equal(windowsElectronPlan(root, 'Autograde IDE.exe').length, 7);
  write(root, 'foreign.dll', pe(0xaa64)); assert.throws(() => windowsElectronPlan(root, 'Autograde IDE.exe'), /Foreign/);
});
test('Windows assembly materializes aliases and bundles only explicit runtime inputs', t => {
  const root = fixture(t), source = path.join(root, 'source'), electron = path.join(root, 'electron'), launchers = path.join(root, 'launchers'), destination = path.join(root, 'payload');
  write(source, 'out/main.js', 'main'); write(source, 'node_modules/real/index.js', 'runtime'); write(source, '.env', 'not shipped');
  const node = write(root, 'runtime/node.exe', pe()), nodeLicense = write(root, 'runtime/LICENSE', 'node license');
  write(electron, 'Autograde IDE.exe', pe());
  for (const name of ['windows-native-bootstrap.mjs', 'seat-reporter.mjs', 'runtime-settings.json', 'windows/INSTALL-NATIVE-WINDOWS.md']) write(launchers, name, '{}');
  const plan = { files: ['out/main.js', 'node_modules/real/index.js'], links: { 'node_modules\\alias': 'real' } };
  assembleWindowsPackage({ source, node, nodeLicense, editorPlan: plan, electronRoot: electron, electronFiles: ['Autograde IDE.exe'], destination, manifest: windowsManifest(metadata), launcherRoot: launchers });
  const alias = path.join(destination, 'editor/app/node_modules/alias');
  assert.equal(fs.lstatSync(alias).isSymbolicLink(), false); assert.equal(fs.readFileSync(path.join(alias, 'index.js'), 'utf8'), 'runtime');
  assert.equal(fs.existsSync(path.join(destination, 'editor/app/.env')), false);
  assert.equal(JSON.parse(fs.readFileSync(path.join(destination, 'windows-package.json'), 'utf8')).requires.wsl, false);
  assert.throws(() => assembleWindowsPackage({ source, node, nodeLicense, editorPlan: plan, electronRoot: electron, electronFiles: [], destination, manifest: {} }), /new Windows package/);
});
test('public Windows packaging refuses non-Windows build hosts', { skip: process.platform === 'win32' }, () => {
  assert.throws(() => packageWindows({}), /Windows x64/);
});
