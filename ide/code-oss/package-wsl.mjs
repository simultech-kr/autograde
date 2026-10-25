#!/usr/bin/env node
// Build on Linux/WSL2. The installed payload needs neither npm nor a Node install.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { verify, readJson, repoRoot } from './manage.mjs';
import { runtimePlan, isInside } from './package-macos.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const runtimeFiles = ['wsl-launcher.mjs', 'shared-session.mjs', 'learning-profile.mjs', 'runtime-settings.json'];
const suspicious = /^(?:\.git|\.env(?:\..*)?|\.data(?:-.*)?|\.cache|student_roster\.csv|.*\.(?:pem|p12|pfx|key)|id_(?:rsa|ed25519))$/i;
const versionPattern = /^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/;
function present(file) { try { return fs.lstatSync(file); } catch (error) { if (error.code === 'ENOENT') return undefined; throw error; } }
function directory(file) { if (!fs.lstatSync(file).isDirectory()) throw new Error(`A real directory is required: ${file}`); }
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

/** Inspect the executable instead of trusting a Linux-looking filename. */
export function elfArchitecture(file) {
  regular(file);
  const header = Buffer.alloc(64), descriptor = fs.openSync(file, 'r');
  let read;
  try { read = fs.readSync(descriptor, header, 0, header.length, 0); } finally { fs.closeSync(descriptor); }
  if (read !== 64 || !header.subarray(0, 4).equals(Buffer.from([0x7f, 0x45, 0x4c, 0x46])) ||
      header[4] !== 2 || header[5] !== 1 || header[6] !== 1 || ![2, 3].includes(header.readUInt16LE(16))) {
    throw new Error(`A 64-bit little-endian Linux ELF executable is required: ${file}`);
  }
  const arch = { 62: 'x64', 183: 'arm64' }[header.readUInt16LE(18)];
  if (!arch) throw new Error(`Unsupported Linux executable architecture: ${file}`);
  return arch;
}

/** Keep the complete pinned Electron runtime, but never follow an external link. */
export function electronRuntimePlan(root, binaryName, arch) {
  root = path.resolve(root); directory(root);
  if (!/^[a-z0-9][a-z0-9._-]*$/.test(binaryName)) throw new Error('Invalid Electron executable name');
  if (elfArchitecture(path.join(root, binaryName)) !== arch) throw new Error('Electron architecture differs from bundled Node');
  fs.accessSync(path.join(root, binaryName), fs.constants.X_OK);
  for (const name of ['LICENSE', 'LICENSES.chromium.html']) regular(path.join(root, name));
  const files = [];
  function visit(folder) {
    directory(folder);
    for (const name of fs.readdirSync(folder).sort()) {
      if (name === '.DS_Store') continue;
      if (suspicious.test(name)) throw new Error(`Unexpected private data in the Electron runtime: ${name}`);
      const file = path.join(folder, name), info = fs.lstatSync(file);
      if (info.isSymbolicLink()) {
        const target = fs.realpathSync(file);
        if (!isInside(root, target) || !fs.statSync(target).isFile()) throw new Error(`Electron contains an external or directory link: ${file}`);
        if (suspicious.test(path.basename(target))) throw new Error('Electron link refers to private data');
        files.push(path.relative(root, file));
      } else if (info.isDirectory()) visit(file);
      else { regular(file); files.push(path.relative(root, file)); }
    }
  }
  visit(root);
  return files.sort();
}

export function wslEntryScript() {
  return `#!/bin/sh
set -eu
# Resolve this package, not the current Windows/WSL working directory.
bundle_root=$(CDPATH= cd -- "$(/usr/bin/dirname -- "$0")/.." && pwd -P)
# Strip interpreter injection before Node starts; the JS launcher also sanitizes
# inherited IDE state. Preserve WSLg display/audio and normal user environment.
exec /usr/bin/env -u NODE_OPTIONS -u NODE_PATH -u NODE_EXTRA_CA_CERTS -u NODE_ICU_DATA \\
  -u NODE_COMPILE_CACHE -u ELECTRON_RUN_AS_NODE -u LD_PRELOAD -u LD_LIBRARY_PATH \\
  "$bundle_root/runtime/bin/node" "$bundle_root/autograde/wsl-launcher.mjs" "$@"
`;
}

export function wslManifest({ version, arch, nodeVersion, extensionVersion, binaryName, approvedBuiltins, upstreamCommit }) {
  if (![version, nodeVersion, extensionVersion].every(value => typeof value === 'string' && versionPattern.test(value)) ||
      !['x64', 'arm64'].includes(arch) || !/^[a-z0-9][a-z0-9._-]*$/.test(binaryName) ||
      !Array.isArray(approvedBuiltins) || !approvedBuiltins.includes('autograde') ||
      !approvedBuiltins.includes('autograde-ide-profile') ||
      approvedBuiltins.some(name => typeof name !== 'string' || !/^[\w-][\w.-]*$/.test(name)) ||
      !/^[a-f0-9]{40}$/.test(upstreamCommit)) throw new Error('Invalid WSL package metadata');
  return { schema: 1, platform: 'linux', arch, version, nodeVersion, extensionVersion,
    editorRoot: 'editor/app', executable: `editor/electron/${binaryName}`, node: 'runtime/bin/node',
    approvedBuiltins: [...approvedBuiltins], policy: 'standard', lifecycle: 'shared', upstreamCommit,
    distribution: 'wsl2-shared-lab-pilot', requires: { wsl: 2, gui: 'WSLg', linuxFilesystem: true,
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
export function assembleWslPackage({ source, node, nodeLicense, electronRoot, electronFiles,
  editorPlan, launcherRoot = here, destination, manifest }) {
  source = fs.realpathSync(source); electronRoot = fs.realpathSync(electronRoot);
  if (present(destination)) throw new Error('WSL package destination already exists');
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
  fs.mkdirSync(path.join(destination, 'bin'), { mode: 0o755 });
  fs.writeFileSync(path.join(destination, 'bin/autograde-ide'), wslEntryScript(), { flag: 'wx', mode: 0o755 });
  fs.writeFileSync(path.join(destination, 'wsl-package.json'), `${JSON.stringify(manifest, null, 2)}\n`, { flag: 'wx', mode: 0o644 });
  fs.writeFileSync(path.join(destination, 'INSTALL-WSL.txt'),
    'Install the complete package in the configured non-root WSL2 user’s Linux filesystem.\n' +
    'The Windows shortcut runs bin/autograde-ide and waits until session cleanup finishes.\n' +
    'Never add --no-sandbox. A working Chromium user namespace or administrator-provisioned sandbox helper is required.\n' +
    'Packaging does not install WSL, compilers, Linux libraries, or modify the distribution.\n', { flag: 'wx', mode: 0o644 });
  return destination;
}

export function fileSha256(file) {
  const hash = createHash('sha256'), descriptor = fs.openSync(file, 'r'), buffer = Buffer.alloc(1024 * 1024);
  try { let count; while ((count = fs.readSync(descriptor, buffer, 0, buffer.length, null))) hash.update(buffer.subarray(0, count)); }
  finally { fs.closeSync(descriptor); }
  return hash.digest('hex');
}

export function archiveWslPackage(directory, archive) {
  directory = fs.realpathSync(directory);
  if (present(archive) || present(`${archive}.sha256`)) throw new Error('Choose a new archive path; existing archives are never overwritten');
  if (isInside(directory, path.resolve(archive))) throw new Error('Archive must be outside its package');
  const staging = fs.mkdtempSync(path.join(path.dirname(path.resolve(archive)), '.autograde-wsl-archive-'));
  const temporary = path.join(staging, 'payload.tar.gz');
  try {
    execFileSync('tar', ['-czf', temporary, '-C', path.dirname(directory), '--', path.basename(directory)], { stdio: 'inherit' });
    fs.copyFileSync(temporary, archive, fs.constants.COPYFILE_EXCL);
    const hash = fileSha256(archive);
    fs.writeFileSync(`${archive}.sha256`, `${hash}  ${path.basename(archive)}\n`, { flag: 'wx', mode: 0o644 });
    fs.unlinkSync(temporary); fs.rmdirSync(staging);
    return { archive, sha256: hash };
  } catch (error) { throw new Error(`Archive failed; diagnostic files are preserved at ${staging}: ${error.message}`, { cause: error }); }
}

function cleanBuildEnvironment() {
  return Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_|LD_)/.test(key)));
}

export function packageWsl({ source, node, output, archive = true }) {
  if (process.platform !== 'linux') throw new Error('Build the WSL payload on Linux/WSL2 with matching Linux Node and Electron binaries');
  source = fs.realpathSync(path.resolve(source)); node = path.resolve(node); output = path.resolve(output);
  if (present(output) || present(`${output}.tar.gz`) || present(`${output}.tar.gz.sha256`)) throw new Error('Choose a new package output; existing artifacts are never overwritten');
  if (isInside(source, output)) throw new Error('Output must be outside the Code-OSS source');
  const checked = verify(source), upstream = readJson(path.join(here, 'upstream.json'));
  if (elfArchitecture(node) !== process.arch) throw new Error('Bundled Node architecture must match this Linux build host');
  const nodeInfo = JSON.parse(execFileSync(node, ['-p', 'JSON.stringify({version:process.versions.node,platform:process.platform,arch:process.arch})'],
    { encoding: 'utf8', env: cleanBuildEnvironment() }));
  if (nodeInfo.version !== upstream.node || nodeInfo.platform !== 'linux' || nodeInfo.arch !== process.arch) throw new Error(`Bundle Linux Node ${upstream.node} for ${process.arch}`);
  const nodeLicense = path.resolve(path.dirname(node), '../LICENSE'); regular(nodeLicense);
  const product = readJson(path.join(source, 'product.json'));
  const electronRoot = path.join(source, '.build/electron');
  const electronFiles = electronRuntimePlan(electronRoot, product.applicationName, process.arch);
  const approvedExtensions = new Set([...execFileSync('git', ['-C', source, 'ls-tree', '--name-only', 'HEAD:extensions'], { encoding: 'utf8' }).trim().split('\n'), 'autograde', 'autograde-ide-profile']);
  const editorPlan = runtimePlan(source, { approvedExtensions });
  const manifest = wslManifest({ version: readJson(path.join(source, 'package.json')).version, arch: process.arch,
    nodeVersion: nodeInfo.version, extensionVersion: checked.extensionVersion, binaryName: product.applicationName,
    approvedBuiltins: editorPlan.approvedBuiltins, upstreamCommit: upstream.commit });
  fs.mkdirSync(path.dirname(output), { recursive: true });
  const staging = fs.mkdtempSync(path.join(path.dirname(output), '.autograde-wsl-build-'));
  const payload = path.join(staging, path.basename(output));
  try {
    assembleWslPackage({ source, node, nodeLicense, electronRoot, electronFiles, editorPlan, destination: payload, manifest });
    // Reserve a new empty destination before moving the owned build into place.
    fs.mkdirSync(output, { mode: 0o755 }); fs.renameSync(payload, output); fs.rmdirSync(staging);
    const result = archive ? archiveWslPackage(output, `${output}.tar.gz`) : {};
    return { directory: output, ...result, architecture: process.arch, extensionVersion: checked.extensionVersion,
      node: 'bundled', lifecycle: 'shared', sandboxBypass: false, files: editorPlan.files.length + electronFiles.length };
  } catch (error) { throw new Error(`WSL package failed. Build files are preserved at ${staging}: ${error.message}`, { cause: error }); }
}

export function parseWslOptions(args) {
  const result = { source: path.join(repoRoot, '.cache/code-oss'), node: process.execPath,
    output: path.join(repoRoot, `build/wsl/autograde-ide-linux-${process.arch}`), archive: true };
  const seen = new Set();
  for (let index = 0; index < args.length; index++) {
    const option = args[index];
    if (seen.has(option)) throw new Error(`Repeated option: ${option}`);
    seen.add(option);
    if (option === '--no-archive') { result.archive = false; continue; }
    const key = { '--source': 'source', '--node': 'node', '--output': 'output' }[option];
    if (!key || !args[index + 1] || args[index + 1].startsWith('--')) throw new Error('Usage: package-wsl.mjs [--source PATH] [--node LINUX_NODE] [--output NEW_DIRECTORY] [--no-archive]');
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
  try { console.log(JSON.stringify(packageWsl(parseWslOptions(process.argv.slice(2))), null, 2)); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
