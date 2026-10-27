#!/usr/bin/env node
// Native Windows x64 packaging; no WSL or cross-platform node_modules reuse.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { verify, readJson, repoRoot } from './manage.mjs';
import { compilePolicyRuntime } from './policy-runtime.mjs';
import { runtimePlan, isInside } from './package-macos.mjs';
import { fileSha256 } from './package-wsl.mjs';
import { selectPlatformPrebuilds } from './package-linux.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
function regular(file) { const info = fs.lstatSync(file); if (!info.isFile() || info.isSymbolicLink()) throw new Error(`A regular file is required: ${file}`); return info; }
export function windowsRelative(value) {
  if (typeof value !== 'string') throw new Error('Invalid Windows runtime path');
  value = value.replaceAll('\\', '/');
  if (!value || path.posix.isAbsolute(value) || /[:\x00-\x1f<>"|?*]/.test(value) || value.split('/').some(p => !p || p === '.' || p === '..' || /[. ]$/.test(p) || /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(p))) throw new Error('Invalid Windows runtime path');
  return value;
}
export function peArchitecture(file) {
  regular(file); const descriptor = fs.openSync(file, 'r');
  try {
    const dos = Buffer.alloc(64); if (fs.readSync(descriptor, dos, 0, 64, 0) !== 64 || dos.toString('ascii', 0, 2) !== 'MZ') throw new Error('Expected a Windows PE binary');
    const offset = dos.readUInt32LE(60); if (offset < 64 || offset > 16 * 1024 * 1024) throw new Error('Invalid PE header location');
    const header = Buffer.alloc(26); if (fs.readSync(descriptor, header, 0, 26, offset) !== 26 || header.readUInt32LE(0) !== 0x4550 || header.readUInt16LE(24) !== 0x20b) throw new Error('Expected a 64-bit Windows PE image');
    const machine = header.readUInt16LE(4); if (machine !== 0x8664 && machine !== 0xaa64) throw new Error('Unsupported PE architecture');
    return machine === 0x8664 ? 'x64' : 'arm64';
  } finally { fs.closeSync(descriptor); }
}
export function windowsElectronPlan(root, binaryName, arch = 'x64') {
  windowsRelative(binaryName); const files = [];
  const walk = directory => { for (const name of fs.readdirSync(directory).sort()) {
    const file = path.join(directory, name), info = fs.lstatSync(file);
    if (info.isSymbolicLink()) throw new Error('Electron Windows runtime must not contain links');
    if (/^(\.git|\.env(?:\..*)?|student_roster\.csv|.*\.(pem|pfx|key))$/i.test(name)) throw new Error('Unexpected private material in Electron runtime');
    if (info.isDirectory()) walk(file); else { regular(file); files.push(windowsRelative(path.relative(root, file))); }
  } };
  walk(root);
  for (const file of [binaryName, 'LICENSE', 'LICENSES.chromium.html', 'icudtl.dat', 'resources.pak', 'v8_context_snapshot.bin', 'locales/en-US.pak']) if (!files.includes(file)) throw new Error(`Missing Windows Electron runtime component: ${file}`);
  for (const file of files.filter(name => /\.(exe|dll)$/i.test(name))) if (peArchitecture(path.join(root, file)) !== arch) throw new Error(`Foreign Windows Electron architecture: ${file}`);
  return files;
}
export function validateWindowsNativeModules(source, plan, arch = 'x64') {
  source = fs.realpathSync(source);
  for (const name of plan.files.filter(file => /\.node$/i.test(file))) {
    const file = fs.realpathSync(path.join(source, windowsRelative(name)));
    if (!isInside(source, file) || peArchitecture(file) !== arch) throw new Error(`Foreign native dependency: ${name}`);
  }
}
export function windowsManifest({ version, extensionVersion, nodeVersion, upstreamCommit, binaryName, approvedBuiltins }) {
  if (![version, extensionVersion, nodeVersion].every(value => /^\d+\.\d+\.\d+$/.test(value ?? '')) || !/^[a-f0-9]{40}$/.test(upstreamCommit ?? '') ||
      !Array.isArray(approvedBuiltins) || !['autograde', 'autograde-ide-profile'].every(name => approvedBuiltins.includes(name)) || approvedBuiltins.some(name => !/^[\w-][\w.-]*$/.test(name))) throw new Error('Invalid Windows package metadata');
  windowsRelative(binaryName);
  return { schema: 1, platform: 'win32', arch: 'x64', version, extensionVersion, nodeVersion, upstreamCommit,
    editorRoot: 'editor/app', executable: `editor/electron/${binaryName}`, node: 'runtime/node.exe', approvedBuiltins,
    policy: 'standard', lifecycle: 'shared', distribution: 'windows-native-shared-lab-candidate',
    requires: { toolchain: 'Administrator-provisioned MSVC x64 and Windows SDK', filesystem: 'local NTFS', framework: '.NET Framework 4.8', wsl: false },
    acceptance: 'not-tested-on-institution-windows', productionReady: false };
}
function copy(file, destination, sourceRoot) {
  if (fs.lstatSync(file).isSymbolicLink()) { file = fs.realpathSync(file); if (!sourceRoot || !isInside(sourceRoot, file)) throw new Error('Runtime input link escaped source'); }
  regular(file); fs.mkdirSync(path.dirname(destination), { recursive: true }); fs.copyFileSync(file, destination, fs.constants.COPYFILE_EXCL);
}
export function assembleWindowsPackage({ source, node, nodeLicense, editorPlan, electronRoot, electronFiles, destination, manifest, launcherRoot = here }) {
  if (fs.existsSync(destination) || isInside(source, destination) || isInside(electronRoot, destination)) throw new Error('Choose a new Windows package outside the source');
  fs.mkdirSync(destination);
  const app = path.join(destination, 'editor/app');
  for (const name of editorPlan.files) { const relative = windowsRelative(name); copy(path.join(source, relative), path.join(app, relative), source); }
  // Materialize dependency aliases: ZIP extraction must not require symlink privileges.
  const pending = Object.entries(editorPlan.links ?? {});
  for (let rounds = 0; pending.length && rounds <= Object.keys(editorPlan.links ?? {}).length; rounds++) {
    for (let index = pending.length - 1; index >= 0; index--) {
      const [name, value] = pending[index], relative = windowsRelative(name), link = path.join(app, relative);
      if (typeof value !== 'string' || path.win32.isAbsolute(value) || path.posix.isAbsolute(value)) throw new Error('Invalid linked dependency');
      const target = path.resolve(path.dirname(link), ...value.split(/[\\/]/));
      if (!isInside(app, target)) throw new Error('Linked dependency escapes Windows package');
      if (!fs.existsSync(target)) continue;
      fs.cpSync(target, link, { recursive: true, dereference: false, errorOnExist: true, force: false }); pending.splice(index, 1);
    }
  }
  if (pending.length) throw new Error('Unresolved Windows dependency links');
  for (const name of electronFiles) { const relative = windowsRelative(name); copy(path.join(electronRoot, relative), path.join(destination, 'editor/electron', relative)); }
  copy(node, path.join(destination, 'runtime/node.exe')); copy(nodeLicense, path.join(destination, 'runtime/LICENSE'));
  for (const name of ['windows-native-bootstrap.mjs', 'runtime-settings.json']) copy(path.join(launcherRoot, name), path.join(destination, 'autograde', name));
  fs.writeFileSync(path.join(destination, 'windows-package.json'), `${JSON.stringify(manifest, null, 2)}\n`, { flag: 'wx' });
  copy(path.join(launcherRoot, 'windows/INSTALL-NATIVE-WINDOWS.md'), path.join(destination, 'INSTALL-NATIVE-WINDOWS.md'));
}
export function packageWindows({ source, node, output, archive = true }) {
  if (process.platform !== 'win32' || process.arch !== 'x64') throw new Error('Build native Windows x64 on Windows x64 with fresh Windows dependencies');
  source = fs.realpathSync(source); node = fs.realpathSync(node); output = path.resolve(output);
  for (const file of [output, `${output}.zip`, `${output}.zip.sha256`]) if (fs.existsSync(file)) throw new Error('Windows package output already exists');
  const checked = verify(source), upstream = readJson(path.join(here, 'upstream.json')); compilePolicyRuntime(source);
  const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_)/i.test(key)));
  const info = JSON.parse(execFileSync(node, ['-p', 'JSON.stringify({platform:process.platform,arch:process.arch,version:process.versions.node})'], { encoding: 'utf8', env }));
  if (info.platform !== 'win32' || info.arch !== 'x64' || info.version !== upstream.node || peArchitecture(node) !== 'x64') throw new Error('Bundle the pinned Windows x64 Node executable');
  const nodeLicense = path.join(path.dirname(node), 'LICENSE'); regular(nodeLicense);
  const product = readJson(path.join(source, 'product.json')), binaryName = `${product.nameShort}.exe`;
  const electronRoot = path.join(source, '.build/electron'), electronFiles = windowsElectronPlan(electronRoot, binaryName);
  const approvedExtensions = new Set([...execFileSync('git', ['-C', source, 'ls-tree', '--name-only', 'HEAD:extensions'], { encoding: 'utf8' }).trim().split(/\r?\n/), 'autograde', 'autograde-ide-profile']);
  const editorPlan = selectPlatformPrebuilds(runtimePlan(source, { approvedExtensions }), 'win32', 'x64'); validateWindowsNativeModules(source, editorPlan);
  const manifest = windowsManifest({ version: readJson(path.join(source, 'package.json')).version, extensionVersion: checked.extensionVersion,
    nodeVersion: info.version, upstreamCommit: upstream.commit, binaryName, approvedBuiltins: editorPlan.approvedBuiltins });
  fs.mkdirSync(path.dirname(output), { recursive: true }); const stage = fs.mkdtempSync(path.join(path.dirname(output), '.autograde-windows-build-'));
  const payload = path.join(stage, path.basename(output)), compiled = path.join(stage, 'launcher');
  assembleWindowsPackage({ source, node, nodeLicense, editorPlan, electronRoot, electronFiles, destination: payload, manifest });
  const powershell = path.join(process.env.SystemRoot, 'System32/WindowsPowerShell/v1.0/powershell.exe');
  execFileSync(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', path.join(here, 'windows/Build-NativeLauncher.ps1'), '-OutputDirectory', compiled, '-ExtensionVersion', checked.extensionVersion, '-RunChecks'], { stdio: 'inherit' });
  for (const name of ['Autograde IDE.exe', 'Autograde Native Checks.exe']) { if (peArchitecture(path.join(compiled, name)) !== 'x64') throw new Error('Native launcher architecture mismatch'); copy(path.join(compiled, name), path.join(payload, name)); }
  fs.renameSync(payload, output);
  if (archive) {
    // Windows bsdtar creates ZIP without PowerShell's 2 GiB-per-file restriction.
    execFileSync('tar.exe', ['-a', '-cf', `${output}.zip`, '-C', path.dirname(output), path.basename(output)], { stdio: 'inherit' });
    fs.writeFileSync(`${output}.zip.sha256`, `${fileSha256(`${output}.zip`)}  ${path.basename(output)}.zip\n`, { flag: 'wx' });
  }
  return { directory: output, archive: archive ? `${output}.zip` : null, extensionVersion: checked.extensionVersion, platform: 'win32', architecture: 'x64', node: 'bundled', lifecycle: 'shared', productionReady: false };
}
export function parseWindowsOptions(args) {
  const result = { source: path.join(repoRoot, '.cache/code-oss'), node: process.execPath, output: path.join(repoRoot, 'build/windows/Autograde-IDE-windows-x64'), archive: true }, seen = new Set();
  for (let i = 0; i < args.length; i++) {
    const option = args[i]; if (seen.has(option)) throw new Error('Repeated Windows package option'); seen.add(option);
    if (option === '--no-archive') { result.archive = false; continue; }
    const key = { '--source': 'source', '--node': 'node', '--output': 'output' }[option];
    if (!key || !args[i + 1] || args[i + 1].startsWith('--')) throw new Error('Usage: package-windows.mjs --source PATH --node WINDOWS_NODE --output NEW_DIRECTORY [--no-archive]'); result[key] = args[++i];
  }
  return result;
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(packageWindows(parseWindowsOptions(process.argv.slice(2))), null, 2)); }
  catch (error) { console.error(error.stack || error); process.exitCode = 1; }
}
