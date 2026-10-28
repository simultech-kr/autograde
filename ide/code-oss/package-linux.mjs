#!/usr/bin/env node
// Build on native Linux. The installed payload needs neither npm nor a Node install.
import * as fs from 'node:fs';
import { compilePolicyRuntime } from './policy-runtime.mjs';
import { prepareDistribution } from './distribution-inventory.mjs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { elfArchitecture, electronRuntimePlan, archiveWslPackage } from './package-wsl.mjs';
import { verify, readJson, repoRoot } from './manage.mjs';
import { runtimePlan, isInside } from './package-macos.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const runtimeFiles = ['linux-launcher.mjs', 'shared-session.mjs', 'learning-profile.mjs', 'seat-reporter.mjs', 'runtime-settings.json'];
const versionPattern = /^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/;
function present(file) { try { return fs.lstatSync(file); } catch (error) { if (error.code === 'ENOENT') return undefined; throw error; } }
function regular(file) {
  const value = fs.lstatSync(file);
  if (!value.isFile() || value.isSymbolicLink()) throw new Error(`A regular file is required: ${file}`);
  return value;
}
function relativePath(value) {
  if (typeof value !== 'string' || !value || path.isAbsolute(value) || /[\\\x00-\x1f\x7f]/.test(value) ||
      value.split('/').some(part => !part || part === '.' || part === '..')) throw new Error(`Invalid relative runtime path: ${value}`);
  return value;
}

export function linuxEntryScript(script = 'linux-launcher.mjs') {
  if (!['linux-launcher.mjs', 'linux-install-desktop.mjs'].includes(script)) throw new Error('Unknown Linux entry point');
  const environment = ['PATH', 'HOME', 'USER', 'LOGNAME', 'SHELL', 'LANG', 'LANGUAGE', 'LC_ALL', 'LC_CTYPE', 'LC_MESSAGES',
    'TERM', 'COLORTERM', 'TZ', 'DISPLAY', 'WAYLAND_DISPLAY', 'XAUTHORITY', 'XDG_RUNTIME_DIR', 'XDG_DATA_HOME',
    'XDG_SESSION_TYPE', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_DESKTOP', 'DESKTOP_SESSION', 'XDG_CONFIG_DIRS',
    'XDG_DATA_DIRS', 'DBUS_SESSION_BUS_ADDRESS', 'GTK_IM_MODULE', 'QT_IM_MODULE', 'XMODIFIERS', 'PULSE_SERVER', 'PIPEWIRE_REMOTE'];
  const inherited = environment.map(name => `${name}="\${${name}-}"`).join(' ');
  return `#!/bin/sh
set -eu
# Resolve this package independently of the current working directory.
bundle_root=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")/.." && pwd -P)
# Construct a clean environment before Node starts: no interpreter injection,
# inherited credentials, external coverage/history destinations or linker hooks.
exec /usr/bin/env -i ${inherited} \\
  "$bundle_root/runtime/bin/node" "$bundle_root/autograde/${script}" "$@"
`;

}

export function linuxManifest({ version, arch, nodeVersion, extensionVersion, binaryName, approvedBuiltins, upstreamCommit }) {
  if (![version, nodeVersion, extensionVersion].every(value => typeof value === 'string' && versionPattern.test(value)) ||
      !['x64', 'arm64'].includes(arch) || !/^[a-z0-9][a-z0-9._-]*$/.test(binaryName) ||
      !Array.isArray(approvedBuiltins) || !approvedBuiltins.includes('autograde') ||
      !approvedBuiltins.includes('autograde-ide-profile') ||
      approvedBuiltins.some(name => typeof name !== 'string' || !/^[\w-][\w.-]*$/.test(name)) ||
      !/^[a-f0-9]{40}$/.test(upstreamCommit)) throw new Error('Invalid native Linux package metadata');
  return { schema: 1, platform: 'linux', arch, version, nodeVersion, extensionVersion,
    editorRoot: 'editor/app', executable: `editor/electron/${binaryName}`, node: 'runtime/bin/node',
    approvedBuiltins: [...approvedBuiltins], policy: 'standard', lifecycle: 'shared', upstreamCommit,
    distribution: 'linux-desktop-shared-lab', requires: { gui: 'local X11 or Wayland', localFilesystem: true,
      sandbox: 'unprivileged-user-namespace-or-administrator-provisioned-helper' } };
}

function copyFile(file, target, root) {
  if (fs.lstatSync(file).isSymbolicLink()) {
    const resolved = fs.realpathSync(file);
    if (!root || !isInside(root, resolved)) throw new Error(`Runtime link escaped its package: ${file}`);
    file = resolved;
  }
  const info = regular(file);
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o755 });
  fs.copyFileSync(file, target, fs.constants.COPYFILE_EXCL);
  // Runtime packaging never grants setuid/root permissions or group write access.
  fs.chmodSync(target, info.mode & 0o755);
}

/** Assemble validated inputs; exported separately to test the actual copied payload. */
export function assembleLinuxPackage({ source, node, nodeLicense, electronRoot, electronFiles,
  editorPlan, launcherRoot = here, destination, manifest }) {
  source = fs.realpathSync(source); electronRoot = fs.realpathSync(electronRoot);
  if (present(destination)) throw new Error('Linux desktop package destination already exists');
  if (isInside(source, path.resolve(destination)) || isInside(electronRoot, path.resolve(destination))) throw new Error('Package output must be outside its input trees');
  fs.mkdirSync(destination, { mode: 0o755 });
  for (const relative of editorPlan.files) {
    relativePath(relative);
    copyFile(path.join(source, relative), path.join(destination, 'editor/app', relative), source);
  }
  for (const [relative, target] of Object.entries(editorPlan.links ?? {})) {
    relativePath(relative);
    const base = path.join(destination, 'editor/app'), link = path.join(base, relative);
    if (path.isAbsolute(target) || !isInside(base, path.resolve(path.dirname(link), target))) throw new Error('Linked dependency escapes the installed editor');
    fs.mkdirSync(path.dirname(link), { recursive: true }); fs.symlinkSync(target, link);
  }
  for (const relative of electronFiles) {
    relativePath(relative);
    copyFile(path.join(electronRoot, relative), path.join(destination, 'editor/electron', relative), electronRoot);
  }
  copyFile(node, path.join(destination, 'runtime/bin/node'));
  copyFile(nodeLicense, path.join(destination, 'runtime/LICENSE'));
  for (const name of runtimeFiles) copyFile(path.join(launcherRoot, name), path.join(destination, 'autograde', name));
  copyFile(path.join(launcherRoot, 'linux-install-desktop.mjs'), path.join(destination, 'autograde/linux-install-desktop.mjs'));
  copyFile(path.join(launcherRoot, 'linux/autograde-ide.svg'), path.join(destination, 'share/icons/autograde-ide.svg'));
  fs.mkdirSync(path.join(destination, 'bin'), { mode: 0o755 });
  fs.writeFileSync(path.join(destination, 'bin/autograde-ide'), linuxEntryScript(), { flag: 'wx', mode: 0o755 });
  fs.writeFileSync(path.join(destination, 'bin/install-autograde-desktop'), linuxEntryScript('linux-install-desktop.mjs'), { flag: 'wx', mode: 0o755 });
  fs.writeFileSync(path.join(destination, 'linux-package.json'), `${JSON.stringify(manifest, null, 2)}\n`, { flag: 'wx', mode: 0o644 });
  copyFile(path.join(launcherRoot, 'linux/INSTALL-LINUX.md'), path.join(destination, 'INSTALL-LINUX.md'));
  return destination;
}

export const archiveLinuxPackage = archiveWslPackage;

function cleanBuildEnvironment() {
  return Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_|LD_)/.test(key)));
}

// node-gyp-build packages may ship several OS/CPU prebuilds in one npm tarball.
// Keep the target's prebuilds and the actual local build; never mistake dormant
// foreign binaries for the active Linux addon, or ship those unused binaries.
export function selectPlatformPrebuilds(plan, platform, arch) {
  if (!['linux', 'darwin', 'win32'].includes(platform) || !['x64', 'arm64'].includes(arch)) throw new Error('Unsupported native target architecture');
  return { ...plan, files: plan.files.filter(relative => {
    const match = /(?:^|\/)prebuilds\/(darwin|win32|linux|android|freebsd|openbsd|sunos)-((?:x64|arm64|ia32|arm|armv7|riscv64|ppc64)(?:\+(?:x64|arm64|ia32|arm|armv7|riscv64|ppc64))*)\//.exec(relative);
    if (match) return match[1] === platform && match[2].split('+').includes(arch);
    // MSAL 5.x ships DPAPI for every Windows CPU and one runtime per OS. Its
    // loader uses DPAPI only on win32 and selects dist/<OS>/<CPU> at install.
    const dpapi = /(?:^|\/)node_modules\/@azure\/msal-node-extensions\/bin\/(x64|arm64|ia32)\//.exec(relative);
    if (dpapi) return platform === 'win32' && dpapi[1] === arch;
    const msal = /(?:^|\/)node_modules\/@azure\/msal-node-runtime\/dist\/(linux|macos|windows)\/(x64|arm64|x86)\//.exec(relative);
    if (msal) return msal[1] === ({ linux: 'linux', darwin: 'macos', win32: 'windows' }[platform]) && msal[2] === arch;
    return true;
  }) };
}

export const selectLinuxRuntimeFiles = (plan, arch) => selectPlatformPrebuilds(plan, 'linux', arch);

export function validateLinuxNativeModules(source, editorPlan, arch) {
  for (const relative of editorPlan.files.filter(file => file.endsWith('.node'))) {
    relativePath(relative);
    const file = fs.realpathSync(path.join(source, relative));
    if (!isInside(source, file) || elfArchitecture(file) !== arch) throw new Error(`Native editor dependency is not Linux ${arch}: ${relative}`);
  }
}

export function packageLinux({ source, node, output, institutionProfile, archive = true }) {
  if (process.platform !== 'linux') throw new Error('Build the native Linux payload on Linux with matching Linux Node and Electron binaries');
  source = fs.realpathSync(path.resolve(source)); node = path.resolve(node); output = path.resolve(output);
  if (present(output) || present(`${output}.tar.gz`) || present(`${output}.tar.gz.sha256`)) throw new Error('Choose a new package output; existing artifacts are never overwritten');
  if (isInside(source, output)) throw new Error('Output must be outside the Code-OSS source');
  const checked = verify(source), upstream = readJson(path.join(here, 'upstream.json'));
  compilePolicyRuntime(source);
  if (elfArchitecture(node) !== process.arch) throw new Error('Bundled Node architecture must match this Linux build host');
  const nodeInfo = JSON.parse(execFileSync(node, ['-p', 'JSON.stringify({version:process.versions.node,platform:process.platform,arch:process.arch})'],
    { encoding: 'utf8', env: cleanBuildEnvironment() }));
  if (nodeInfo.version !== upstream.node || nodeInfo.platform !== 'linux' || nodeInfo.arch !== process.arch) throw new Error(`Bundle Linux Node ${upstream.node} for ${process.arch}`);
  const nodeLicense = path.resolve(path.dirname(node), '../LICENSE'); regular(nodeLicense);
  const product = readJson(path.join(source, 'product.json'));
  const electronRoot = path.join(source, '.build/electron');
  const electronFiles = electronRuntimePlan(electronRoot, product.applicationName, process.arch);
  const approvedExtensions = new Set([...execFileSync('git', ['-C', source, 'ls-tree', '--name-only', 'HEAD:extensions'], { encoding: 'utf8' }).trim().split('\n'), 'autograde', 'autograde-ide-profile']);
  const editorPlan = selectLinuxRuntimeFiles(runtimePlan(source, { approvedExtensions }), process.arch);
  validateLinuxNativeModules(source, editorPlan, process.arch);
  const manifest = linuxManifest({ version: readJson(path.join(source, 'package.json')).version, arch: process.arch,
    nodeVersion: nodeInfo.version, extensionVersion: checked.extensionVersion, binaryName: product.applicationName,
    approvedBuiltins: editorPlan.approvedBuiltins, upstreamCommit: upstream.commit });
  fs.mkdirSync(path.dirname(output), { recursive: true });
  const staging = fs.mkdtempSync(path.join(path.dirname(output), '.autograde-linux-build-'));
  const payload = path.join(staging, path.basename(output));
  try {
    assembleLinuxPackage({ source, node, nodeLicense, electronRoot, electronFiles, editorPlan, destination: payload, manifest });
    prepareDistribution(payload, path.join(payload, 'autograde'), {ideVersion: manifest.version, extensionVersion: checked.extensionVersion, nodeVersion: nodeInfo.version, upstreamCommit: upstream.commit}, institutionProfile);
    // Reserve a new empty destination before moving the owned build into place.
    fs.mkdirSync(output, { mode: 0o755 }); fs.renameSync(payload, output); fs.rmdirSync(staging);
    const result = archive ? archiveLinuxPackage(output, `${output}.tar.gz`) : {};
    return { directory: output, ...result, architecture: process.arch, extensionVersion: checked.extensionVersion,
      node: 'bundled', lifecycle: 'shared', sandboxBypass: false, files: editorPlan.files.length + electronFiles.length };
  } catch (error) { throw new Error(`Linux desktop package failed. Build files are preserved at ${staging}: ${error.message}`, { cause: error }); }
}

export function parseLinuxOptions(args) {
  const result = { source: path.join(repoRoot, '.cache/code-oss'), node: process.execPath,
    output: path.join(repoRoot, `build/linux/autograde-ide-linux-${process.arch}`), archive: true };
  const seen = new Set();
  for (let index = 0; index < args.length; index++) {
    const option = args[index];
    if (seen.has(option)) throw new Error(`Repeated option: ${option}`);
    seen.add(option);
    if (option === '--no-archive') { result.archive = false; continue; }
    const key = { '--source': 'source', '--node': 'node', '--output': 'output', '--institution-profile': 'institutionProfile' }[option];
    if (!key || !args[index + 1] || args[index + 1].startsWith('--')) throw new Error('Usage: package-linux.mjs [--source PATH] [--node LINUX_NODE] [--output NEW_DIRECTORY] [--no-archive]');
    result[key] = args[++index];
  }
  return result;
}

function isMain(argument) {
  if (!argument) return false;
  try { const a = fs.statSync(argument), b = fs.statSync(fileURLToPath(import.meta.url)); return a.dev === b.dev && a.ino === b.ino; }
  catch { return false; }
}
if (isMain(process.argv[1])) {
  try { console.log(JSON.stringify(packageLinux(parseLinuxOptions(process.argv.slice(2))), null, 2)); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
