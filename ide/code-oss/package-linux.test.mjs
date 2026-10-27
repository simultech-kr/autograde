import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { linuxEntryScript, linuxManifest, assembleLinuxPackage, archiveLinuxPackage,
  parseLinuxOptions, packageLinux, validateLinuxNativeModules, selectLinuxRuntimeFiles, selectPlatformPrebuilds } from './package-linux.mjs';
import { electronRuntimePlan } from './package-wsl.mjs';
import { loadLinuxBundle } from './linux-launcher.mjs';
import { desktopExec, desktopEntry, installDesktop, parseDesktopOptions } from './linux-install-desktop.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
function elf(arch = process.arch) {
  const bytes = Buffer.alloc(64); Buffer.from([0x7f, 0x45, 0x4c, 0x46, 2, 1, 1]).copy(bytes);
  bytes.writeUInt16LE(3, 16); bytes.writeUInt16LE(arch === 'x64' ? 62 : 183, 18); return bytes;
}
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'ag-linux-package-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  function write(relative, content = 'fixture', mode = 0o644) {
    const file = path.join(root, relative); fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, typeof content === 'string' || Buffer.isBuffer(content) ? content : JSON.stringify(content), { mode }); return file;
  }
  const source = path.join(root, 'source'), electronRoot = path.join(root, 'electron');
  const node = write('node/bin/node', elf(), 0o755), nodeLicense = write('node/LICENSE', 'Node license');
  write('electron/autograde-ide', elf(), 0o755); write('electron/LICENSE', 'Electron license');
  write('electron/LICENSES.chromium.html', 'Chromium license'); write('electron/locales/ko.pak');
  write('source/out/main.js', '// compiled editor'); write('source/LICENSE.txt', 'Code OSS license');
  write('source/.env', 'private data'); write('source/student_roster.csv', 'private data');
  const editorPlan = { files: ['out/main.js', 'LICENSE.txt'], links: {}, approvedBuiltins: ['autograde', 'autograde-ide-profile'] };
  const manifest = linuxManifest({ version: '1.143.0', arch: process.arch, nodeVersion: process.versions.node,
    extensionVersion: '0.5.12', binaryName: 'autograde-ide', approvedBuiltins: editorPlan.approvedBuiltins, upstreamCommit: 'a'.repeat(40) });
  const options = { source, node, nodeLicense, electronRoot, editorPlan, launcherRoot: here,
    electronFiles: electronRuntimePlan(electronRoot, 'autograde-ide', process.arch),
    destination: path.join(root, '한글 runtime'), manifest };
  const load = root => loadLinuxBundle(root, { platform: 'linux', statfs: () => ({ type: 0xef53 }) });
  const installOptions = { platform: 'linux', uid: process.getuid(), load };
  return { root, write, options, load, installOptions };
}

test('native package contains runnable entry points, bundled Node, cleanup modules, licenses and desktop installer', async t => {
  const f = fixture(t), destination = assembleLinuxPackage(f.options);
  assert.equal(f.load(destination).manifest.distribution, 'linux-desktop-shared-lab');
  const installed = await import(pathToFileURL(path.join(destination, 'autograde/linux-install-desktop.mjs')));
  assert.equal(installed.desktopEntry(destination), desktopEntry(destination));
  for (const name of ['bin/autograde-ide', 'bin/install-autograde-desktop', 'runtime/bin/node']) fs.accessSync(path.join(destination, name), fs.constants.X_OK);
  for (const name of ['runtime/LICENSE', 'editor/electron/LICENSES.chromium.html', 'editor/app/LICENSE.txt',
    'autograde/linux-launcher.mjs', 'autograde/shared-session.mjs', 'autograde/learning-profile.mjs',
    'autograde/linux-install-desktop.mjs', 'share/icons/autograde-ide.svg', 'INSTALL-LINUX.md']) assert.ok(fs.statSync(path.join(destination, name)).isFile(), name);
  assert.equal(fs.existsSync(path.join(destination, 'editor/app/.env')), false);
  assert.equal(fs.existsSync(path.join(destination, 'editor/app/student_roster.csv')), false);
  assert.equal(fs.existsSync(path.join(destination, 'wsl-package.json')), false);
  assert.throws(() => assembleLinuxPackage(f.options), /already exists/);
});

test('native metadata cannot masquerade as WSL or persistent student profiles', t => {
  const f = fixture(t); assembleLinuxPackage(f.options);
  const file = path.join(f.options.destination, 'linux-package.json');
  fs.writeFileSync(file, JSON.stringify({ ...f.options.manifest, distribution: 'wsl2-shared-lab-pilot' }));
  assert.throws(() => f.load(f.options.destination), error => error.code === 'bundle_invalid');
  assert.throws(() => linuxManifest({ ...f.options.manifest, binaryName: '../escape' }), /metadata/);
});

test('native dependencies reject foreign architecture and Mach-O before output assembly', t => {
  const f = fixture(t), file = f.write('source/node_modules/example/native.node', elf());
  const plan = { files: ['node_modules/example/native.node'] };
  assert.doesNotThrow(() => validateLinuxNativeModules(f.options.source, plan, process.arch));
  fs.writeFileSync(file, elf(process.arch === 'x64' ? 'arm64' : 'x64'));
  assert.throws(() => validateLinuxNativeModules(f.options.source, plan, process.arch), /Native editor dependency/);
  fs.writeFileSync(file, Buffer.from([0xcf, 0xfa, 0xed, 0xfe]));
  assert.throws(() => validateLinuxNativeModules(f.options.source, plan, process.arch), /Linux ELF/);
});

test('source traversal, linked payload escape and unexpected outputs are rejected', t => {
  const f = fixture(t);
  assert.throws(() => assembleLinuxPackage({ ...f.options, editorPlan: { ...f.options.editorPlan, files: ['../private'] } }), /Invalid relative/);
  assert.throws(() => assembleLinuxPackage({ ...f.options, destination: path.join(f.options.source, 'output') }), /outside/);
  assert.throws(() => assembleLinuxPackage({ ...f.options, destination: path.join(f.root, 'bad-link'), editorPlan: { ...f.options.editorPlan, links: { 'node_modules/alias': '../../../../outside' } } }), /escapes/);
});

test('archive and checksum match actual payload and existing artifacts are preserved', t => {
  const f = fixture(t), directory = assembleLinuxPackage(f.options), archive = path.join(f.root, 'Linux distribution.tar.gz');
  const result = archiveLinuxPackage(directory, archive);
  assert.equal(result.sha256, createHash('sha256').update(fs.readFileSync(archive)).digest('hex'));
  assert.match(fs.readFileSync(`${archive}.sha256`, 'utf8'), new RegExp(`^${result.sha256}  Linux distribution.tar.gz`));
  const files = execFileSync('tar', ['-tzf', archive], { encoding: 'utf8' });
  assert.match(files, /bin\/install-autograde-desktop/); assert.match(files, /linux-package.json/);
  assert.throws(() => archiveLinuxPackage(directory, archive), /never overwritten/);
  assert.equal(createHash('sha256').update(fs.readFileSync(archive)).digest('hex'), result.sha256);
});

test('entry executes bundled Node with literal paths and arguments after dropping interpreter injection', t => {
  const f = fixture(t), root = path.join(f.root, 'App space $(touch SHOULD_NOT_EXIST); `echo bad`');
  fs.mkdirSync(path.join(root, 'bin'), { recursive: true }); fs.mkdirSync(path.join(root, 'runtime/bin'), { recursive: true }); fs.mkdirSync(path.join(root, 'autograde'));
  // Test the entry's invocation, using the host runtime without relocating its shared libraries.
  fs.symlinkSync(process.execPath, path.join(root, 'runtime/bin/node'));
  fs.writeFileSync(path.join(root, 'bin/autograde-ide'), linuxEntryScript(), { mode: 0o755 });
  fs.writeFileSync(path.join(root, 'autograde/linux-launcher.mjs'), 'console.log(JSON.stringify({args:process.argv.slice(2),options:process.env.NODE_OPTIONS,path:process.env.NODE_PATH,coverage:process.env.NODE_V8_COVERAGE,token:process.env.GITHUB_TOKEN}));');
  const argument = '$(touch SHOULD_NOT_EXIST); "literal"';
  const result = JSON.parse(execFileSync(path.join(root, 'bin/autograde-ide'), [argument], { cwd: f.root, encoding: 'utf8', env: { ...process.env, NODE_OPTIONS: '--require /missing', NODE_PATH: '/untrusted', NODE_V8_COVERAGE: path.join(f.root, 'external-coverage'), GITHUB_TOKEN: 'secret' } }));
  assert.deepEqual(result, { args: [argument] }); assert.equal(fs.existsSync(path.join(f.root, 'external-coverage')), false); assert.equal(fs.existsSync(path.join(f.root, 'SHOULD_NOT_EXIST')), false);
  assert.throws(() => linuxEntryScript('../bad'), /Unknown/);
});

test('desktop entries quote spaces and shell metacharacters and reject field code/control injection', () => {
  assert.equal(desktopExec('/opt/App space/bin/autograde-ide'), '"/opt/App space/bin/autograde-ide"');
  assert.equal(desktopExec('/opt/$a`b"c\\d'), '"/opt/\\\\$a\\\\`b\\\\"c\\\\\\\\d"');
  const entry = desktopEntry('/opt/App $(touch bad); name');
  assert.ok(entry.includes('Exec="/opt/App \\\\$(touch bad); name/bin/autograde-ide" --desktop'));
  assert.equal(entry.split('\n').filter(line => line.startsWith('Exec=')).length, 1);
  for (const invalid of ['/opt/%f', '/opt/a=b', '/opt/a\nExec=evil', 'relative/path']) assert.throws(() => desktopExec(invalid));
});

test('operator installs menu and optional desktop shortcuts using the bundled fixed package', t => {
  const f = fixture(t), root = assembleLinuxPackage(f.options), applicationsDir = path.join(f.root, 'my applications'), desktopDir = path.join(f.root, 'my Desktop');
  const result = installDesktop({ root, applicationsDir, desktopDir }, f.installOptions);
  assert.equal(result.files.length, 2);
  for (const file of result.files) assert.equal(fs.readFileSync(file, 'utf8'), desktopEntry(root));
  assert.equal(fs.statSync(path.join(desktopDir, 'autograde-ide.desktop')).mode & 0o777, 0o700);
  assert.throws(() => installDesktop({ root, applicationsDir }, f.installOptions), /never replaced/);
});

test('existing destination, symlink directory and wrong ownership modes never authorize overwrite', t => {
  const f = fixture(t), root = assembleLinuxPackage(f.options), apps = path.join(f.root, 'apps'), desktop = path.join(f.root, 'Desktop');
  fs.mkdirSync(apps); fs.mkdirSync(desktop); const existing = path.join(desktop, 'autograde-ide.desktop'); fs.writeFileSync(existing, 'keep');
  assert.throws(() => installDesktop({ root, applicationsDir: apps, desktopDir: desktop }, f.installOptions), /never replaced/);
  assert.equal(fs.readFileSync(existing, 'utf8'), 'keep'); assert.deepEqual(fs.readdirSync(apps), []);
  const linked = path.join(f.root, 'linked-apps'); fs.symlinkSync(apps, linked);
  assert.throws(() => installDesktop({ root, applicationsDir: linked }, f.installOptions), /real directories/);
  fs.chmodSync(apps, 0o777);
  assert.throws(() => installDesktop({ root, applicationsDir: apps }, f.installOptions), /writable/);
  assert.throws(() => installDesktop({ root, applicationsDir: apps }, { ...f.installOptions, uid: 0 }), /ordinary Linux/);
});

test('package and registration parsers reject unsupported and repeated options', () => {
  assert.equal(parseLinuxOptions(['--output', '/tmp/new Linux', '--no-archive']).archive, false);
  assert.equal(parseDesktopOptions(['--desktop-dir', '/home/learner/바탕 화면'], { HOME: '/home/learner' }).desktopDir, '/home/learner/바탕 화면');
  for (const args of [['--no-sandbox'], ['--output'], ['--output', '/tmp/a', '--output', '/tmp/b']]) assert.throws(() => parseLinuxOptions(args));
  for (const args of [['--desktop-dir', 'relative'], ['--replace'], ['--desktop-dir', '/tmp/a', '--desktop-dir', '/tmp/b']]) assert.throws(() => parseDesktopOptions(args, { HOME: '/home/user' }));
  if (process.platform !== 'linux') assert.throws(() => packageLinux({}), /native Linux payload on Linux/);
});


test('known foreign npm prebuilds are omitted while the native build and target prebuild remain validated', t => {
  const f = fixture(t);
  const paths = ['node_modules/bufferutil/build/Release/bufferutil.node', 'node_modules/bufferutil/prebuilds/linux-x64/bufferutil.node',
    'node_modules/bufferutil/prebuilds/darwin-x64+arm64/bufferutil.node', 'node_modules/bufferutil/prebuilds/win32-x64/bufferutil.node',
    'node_modules/bufferutil/prebuilds/linux-arm64/bufferutil.node', 'node_modules/bufferutil/package.json'];
  for (const file of paths.slice(0, 2)) f.write(`source/${file}`, elf('x64'));
  for (const file of paths.slice(2, 5)) f.write(`source/${file}`, 'foreign unused binary');
  const plan = selectLinuxRuntimeFiles({ files: paths }, 'x64');
  assert.deepEqual(plan.files, [paths[0], paths[1], paths[5]]);
  assert.doesNotThrow(() => validateLinuxNativeModules(f.options.source, plan, 'x64'));
  fs.writeFileSync(path.join(f.options.source, paths[0]), elf('arm64'));
  assert.throws(() => validateLinuxNativeModules(f.options.source, plan, 'x64'), /Native editor dependency/);
});


test('MSAL vendor bundles retain only target DPAPI/runtime directories and always keep the selected active addon', () => {
  const base = 'extensions/microsoft-authentication/node_modules/@azure/';
  const paths = ['msal-node-extensions/bin/x64/dpapi.node', 'msal-node-extensions/bin/arm64/dpapi.node',
    'msal-node-runtime/dist/linux/x64/msal-node-runtime.node', 'msal-node-runtime/dist/windows/x64/msal-node-runtime.node',
    'msal-node-runtime/dist/macos/arm64/msal-node-runtime.node', 'msal-node-runtime/dist/msal-node-runtime.node'].map(file => base + file);
  assert.deepEqual(selectPlatformPrebuilds({ files: paths }, 'linux', 'x64').files, [paths[2], paths[5]]);
  assert.deepEqual(selectPlatformPrebuilds({ files: paths }, 'win32', 'x64').files, [paths[0], paths[3], paths[5]]);
  assert.deepEqual(selectPlatformPrebuilds({ files: paths }, 'darwin', 'arm64').files, [paths[4], paths[5]]);
});
