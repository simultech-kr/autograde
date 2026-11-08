import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { peArchitecture, windowsRelative, windowsElectronPlan, validateWindowsNativeModules, windowsManifest,
  windowsWslSupport, windowsWslInstallerFiles, bundleWindowsWslLauncher, assembleWindowsPackage, packageWindows } from './package-windows.mjs';

function fixture(t) { const root = fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-win-package-')); t.after(() => fs.rmSync(root, { recursive: true, force: true })); return root; }
function write(root, name, bytes) { const file = path.join(root, name); fs.mkdirSync(path.dirname(file), { recursive: true }); fs.writeFileSync(file, bytes); return file; }
function pe(machine = 0x8664) { const bytes = Buffer.alloc(128); bytes.write('MZ'); bytes.writeUInt32LE(64, 60); bytes.writeUInt32LE(0x4550, 64); bytes.writeUInt16LE(machine, 68); bytes.writeUInt16LE(0x20b, 88); return bytes; }
const metadata = { version: '1.143.0', extensionVersion: '0.5.14', nodeVersion: '26.13.0', upstreamCommit: 'a'.repeat(40), binaryName: 'Autograde IDE.exe', approvedBuiltins: ['autograde', 'autograde-ide-profile'] };

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
  const manifest = JSON.parse(fs.readFileSync(path.join(destination, 'windows-package.json'), 'utf8'));
  assert.equal(manifest.requires.wsl, false);
  assert.deepEqual(manifest.wsl, {launcher: 'wsl/Autograde IDE.exe', installer: 'wsl/Install-Shortcut.ps1',
    runtime: 'separate-linux-payload', requires: ['WSL2', 'WSLg']});
  assert.throws(() => assembleWindowsPackage({ source, node, nodeLicense, editorPlan: plan, electronRoot: electron, electronFiles: [], destination, manifest: {} }), /new Windows package/);
});

function wslFixture(t) {
  const root = fixture(t), destination = path.join(root, 'payload'), stage = path.join(root, 'stage'), launcherRoot = path.join(root, 'inputs');
  fs.mkdirSync(destination); fs.mkdirSync(stage);
  write(destination, 'Autograde IDE.exe', 'native launcher');
  for (const name of windowsWslInstallerFiles) write(launcherRoot, `windows/${name}`, `installer input: ${name}`);
  const calls = [];
  const options = {destination, stage, launcherRoot, extensionVersion: metadata.extensionVersion, powershell: 'powershell.exe',
    run(command, args, spawnOptions) {
      calls.push({command, args, spawnOptions});
      const output = args[args.indexOf('-OutputDirectory') + 1];
      write(output, 'Autograde IDE.exe', 'managed WSL launcher');
      write(output, 'Autograde Launcher Checks.exe', 'managed WSL checks');
      write(output, 'launcher-version.json', JSON.stringify({schema: 1, extensionVersion: metadata.extensionVersion,
        assemblyVersion: `${metadata.extensionVersion}.0`}));
      write(output, 'AssemblyInfo.cs', 'build-only');
      write(output, 'launcher.json', 'private operator settings');
    }};
  return {root, options, calls};
}

test('Windows packages always build and check a matching WSL launcher with only explicit installer inputs', t => {
  const {options, calls} = wslFixture(t);
  write(options.launcherRoot, 'windows/device.json', 'private token');
  assert.deepEqual(bundleWindowsWslLauncher(options), windowsWslSupport());
  assert.equal(calls.length, 1);
  assert.equal(calls[0].command, 'powershell.exe');
  assert.deepEqual(calls[0].args, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
    path.join(options.launcherRoot, 'windows/Build-Launcher.ps1'), '-OutputDirectory', path.join(options.stage, 'wsl-launcher'),
    '-ExtensionVersion', metadata.extensionVersion, '-RunChecks']);
  assert.deepEqual(calls[0].spawnOptions, {stdio: 'inherit'});
  assert.deepEqual(fs.readdirSync(path.join(options.destination, 'wsl')).sort(),
    [...windowsWslInstallerFiles, 'Autograde IDE.exe', 'Autograde Launcher Checks.exe', 'launcher-version.json'].sort());
  assert.equal(fs.readFileSync(path.join(options.destination, 'Autograde IDE.exe'), 'utf8'), 'native launcher');
  assert.equal(fs.readFileSync(path.join(options.destination, 'wsl/Autograde IDE.exe'), 'utf8'), 'managed WSL launcher');
  const version = JSON.parse(fs.readFileSync(path.join(options.destination, 'wsl/launcher-version.json'), 'utf8'));
  assert.equal(version.assemblyVersion, `${metadata.extensionVersion}.0`);
  assert.equal(fs.existsSync(path.join(options.destination, 'wsl/launcher.json')), false);
});

test('WSL compiler or lifecycle-check failure preserves staged output without publishing a launcher', t => {
  const {options} = wslFixture(t);
  options.run = (_command, args) => {
    write(args[args.indexOf('-OutputDirectory') + 1], 'compile-error.log', 'failed');
    throw new Error('WSL launcher checks failed');
  };
  assert.throws(() => bundleWindowsWslLauncher(options), /checks failed/);
  assert.equal(fs.existsSync(path.join(options.destination, 'wsl')), false);
  assert.equal(fs.readFileSync(path.join(options.stage, 'wsl-launcher/compile-error.log'), 'utf8'), 'failed');
  assert.equal(fs.readFileSync(path.join(options.destination, 'Autograde IDE.exe'), 'utf8'), 'native launcher');
});

test('missing artifacts and mismatched WSL versions prevent packaging', async t => {
  for (const failure of ['missing-checks', 'wrong-version']) await t.test(failure, t => {
    const {options} = wslFixture(t), original = options.run;
    options.run = (...args) => {
      original(...args);
      const compiled = path.join(options.stage, 'wsl-launcher');
      if (failure === 'missing-checks') fs.unlinkSync(path.join(compiled, 'Autograde Launcher Checks.exe'));
      else write(compiled, 'launcher-version.json', JSON.stringify({schema: 1, extensionVersion: '0.0.1', assemblyVersion: '0.0.1.0'}));
    };
    assert.throws(() => bundleWindowsWslLauncher(options), failure === 'missing-checks' ? /ENOENT/ : /version/);
    assert.equal(fs.existsSync(path.join(options.destination, 'wsl')), false);
  });
});

test('WSL packaging never overwrites previous outputs or builds without complete installer sources', async t => {
  for (const failure of ['destination-exists', 'build-exists', 'installer-missing']) await t.test(failure, t => {
    const {options, calls} = wslFixture(t);
    let previous;
    if (failure === 'destination-exists') previous = write(options.destination, 'wsl/keep.txt', 'preserved');
    else if (failure === 'build-exists') previous = write(options.stage, 'wsl-launcher/keep.txt', 'preserved');
    else fs.unlinkSync(path.join(options.launcherRoot, 'windows/Install-Shortcut.ps1'));
    assert.throws(() => bundleWindowsWslLauncher(options));
    assert.equal(calls.length, 0);
    if (previous) assert.equal(fs.readFileSync(previous, 'utf8'), 'preserved');
  });
});

test('public Windows packaging refuses non-Windows build hosts', { skip: process.platform === 'win32' }, () => {
  assert.throws(() => packageWindows({}), /Windows x64/);
});
