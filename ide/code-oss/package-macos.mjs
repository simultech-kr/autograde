#!/usr/bin/env node
// Build-time tool only. The resulting app runs without a checkout, npm or installed Node.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import { verify, readJson, repoRoot } from './manage.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
const excludedExtensions = new Set(['copilot', 'vscode-api-tests', 'vscode-colorize-tests', 'vscode-test-resolver']);
const excludedDirectories = new Set(['.git', '.github', '.vscode', '.cache', 'test', 'tests', '__tests__', 'test-workspace', 'fixtures', 'coverage']);
const extensionBuildDirectories = new Set(['src', 'build', 'scripts', 'node_modules']);
const knownSecrets = /^(?:\.env(?:\..*)?|student_roster\.csv|.*\.(?:pem|p12|pfx|key)|id_(?:rsa|ed25519))$/i;
const dependencyName = /^(?:@[a-z0-9_.-]+\/)?[a-z0-9_.-]+$/i;

export function isInside(root, target) {
  const relative = path.relative(root, target);
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}
function regular(file) {
  const stat = fs.lstatSync(file);
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error(`A regular file is required: ${file}`);
  return stat;
}
function directory(file) {
  if (!fs.lstatSync(file).isDirectory()) throw new Error(`A real directory is required: ${file}`);
}
function addFile(files, root, file) {
  regular(file);
  if (!isInside(root, file)) throw new Error(`Runtime file escaped source: ${file}`);
  files.add(path.relative(root, file).split(path.sep).join('/'));
}
function collectTree(files, source, tree, { extension = false, dependency = false, level = 0, linkRoot = tree } = {}) {
  directory(tree);
  for (const name of fs.readdirSync(tree).sort()) {
    const file = path.join(tree, name), stat = fs.lstatSync(file);
    if (excludedDirectories.has(name) || name === '.bin' || knownSecrets.test(name)) continue;
    if (stat.isSymbolicLink()) {
      const resolved = fs.realpathSync(file);
      if (!dependency || !isInside(linkRoot, resolved) || knownSecrets.test(path.basename(resolved)) || !fs.statSync(resolved).isFile()) {
        throw new Error(`Runtime input contains an external or non-file symlink: ${file}`);
      }
      files.add(path.relative(source, file).split(path.sep).join('/'));
      continue;
    }
    if (stat.isDirectory()) {
      if ((extension && level === 0 && extensionBuildDirectories.has(name)) || (dependency && name === 'node_modules')) continue;
      collectTree(files, source, file, { extension, dependency, level: level + 1, linkRoot });
    } else {
      if (/\.(?:map|tsbuildinfo)$/.test(name) || /^(?:package-lock\.json|yarn\.lock|pnpm-lock\.yaml|tsconfig.*\.json|\.eslintrc.*|\.vscodeignore)$/.test(name)) continue;
      addFile(files, source, file);
    }
  }
}

/** Production closure, preserving the installed Node resolution layout. No npm subprocess. */
export function collectProductionDependencies(source, packageRoot, files = new Set(), visited = new Set(), links = new Map()) {
  source = path.resolve(source); packageRoot = path.resolve(packageRoot);
  if (!isInside(source, packageRoot)) throw new Error('Dependency owner is outside the source');
  const manifest = readJson(path.join(packageRoot, 'package.json'));
  const requests = { ...manifest.dependencies, ...manifest.optionalDependencies, ...manifest.peerDependencies };
  for (const name of Object.keys(requests).sort()) {
    if (!dependencyName.test(name)) throw new Error(`Invalid dependency name: ${name}`);
    let current = packageRoot, resolved;
    while (isInside(source, current)) {
      const candidate = path.join(current, 'node_modules', name);
      if (fs.existsSync(candidate)) { resolved = candidate; break; }
      if (current === source) break;
      current = path.dirname(current);
    }
    if (!resolved) {
      // npm can omit platform-specific optional packages and optional peers.
      if (name in (manifest.optionalDependencies ?? {}) || name in (manifest.peerDependencies ?? {})) continue;
      throw new Error(`Missing runtime dependency ${name} required by ${packageRoot}`);
    }
    if (fs.lstatSync(resolved).isSymbolicLink()) {
      const target = fs.realpathSync(resolved);
      if (!isInside(source, target)) throw new Error(`Linked runtime dependency is outside the source: ${resolved}`);
      directory(target);
      links.set(path.relative(source, resolved), path.relative(path.dirname(resolved), target));
      resolved = target;
    }
    let ancestor = resolved;
    while (ancestor !== source) { directory(ancestor); ancestor = path.dirname(ancestor); }
    if (visited.has(resolved)) continue;
    visited.add(resolved);
    collectTree(files, source, resolved, { dependency: true });
    collectProductionDependencies(source, resolved, files, visited, links);
  }
  return files;
}

/** Collect only compiled editor assets, shipped extensions and their runtime dependencies. */
export function runtimePlan(source, { approvedExtensions } = {}) {
  source = path.resolve(source); directory(source);
  const files = new Set(), visited = new Set(), links = new Map();
  for (const name of ['package.json', 'product.json', 'LICENSE.txt', 'ThirdPartyNotices.txt']) addFile(files, source, path.join(source, name));
  for (const name of ['out', 'resources']) collectTree(files, source, path.join(source, name));
  for (const name of ['licenses']) if (fs.existsSync(path.join(source, name))) collectTree(files, source, path.join(source, name));
  collectProductionDependencies(source, source, files, visited, links);
  const extensionRoot = path.join(source, 'extensions'); directory(extensionRoot);
  const approvedBuiltins = [];
  for (const name of fs.readdirSync(extensionRoot).sort()) {
    if (excludedExtensions.has(name) || excludedDirectories.has(name) || name === 'node_modules') continue;
    const folder = path.join(extensionRoot, name);
    if (!fs.lstatSync(folder).isDirectory() || !fs.existsSync(path.join(folder, 'package.json'))) continue;
    const manifest = readJson(path.join(folder, 'package.json'));
    // Tests and arbitrary unbuilt source do not become shipped extensions.
    if (!manifest.engines?.vscode) continue;
    if (approvedExtensions && !approvedExtensions.has(name)) throw new Error(`Unapproved built-in extension: ${name}`);
    if (manifest.main) {
      const main = path.resolve(folder, manifest.main);
      if (!isInside(folder, main) || ![main, `${main}.js`, path.join(main, 'index.js')].some(f => fs.existsSync(f) && fs.lstatSync(f).isFile())) {
        throw new Error(`Built-in extension is not compiled: ${name}`);
      }
    }
    collectTree(files, source, folder, { extension: true });
    collectProductionDependencies(source, folder, files, visited, links);
    approvedBuiltins.push(name);
  }
  if (!approvedBuiltins.includes('autograde') || !approvedBuiltins.includes('autograde-ide-profile')) throw new Error('Prepared Autograde extensions are required');
  if (fs.existsSync(path.join(extensionRoot, 'package.json'))) {
    // Some built-ins share production dependencies above their own directory.
    collectProductionDependencies(source, extensionRoot, files, visited, links);
  }
  if (!files.has('out/main.js')) throw new Error('Compile the editor before packaging');
  return { files: [...files].sort(), links: Object.fromEntries(links), approvedBuiltins };
}

export function assertArchitecture(architectures, arch) {
  const expected = arch === 'x64' ? 'x86_64' : arch;
  if (!['arm64', 'x64'].includes(arch) || !architectures.trim().split(/\s+/).includes(expected)) {
    throw new Error(`Electron architecture must include ${expected}: ${architectures.trim()}`);
  }
}

const xml = value => String(value).replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;');
export function infoPlist(version, arch) {
  if (!['arm64', 'x64'].includes(arch) || !/^\d+\.\d+\.\d+(?:[.-][A-Za-z0-9.-]+)?$/.test(version)) throw new Error('Invalid package version or architecture');
  return `<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n<plist version="1.0"><dict>\n<key>CFBundleName</key><string>Autograde IDE</string>\n<key>CFBundleDisplayName</key><string>Autograde IDE</string>\n<key>CFBundleIdentifier</key><string>info.cbchoi.autograde.launcher</string>\n<key>CFBundleExecutable</key><string>AutogradeLauncher</string>\n<key>CFBundlePackageType</key><string>APPL</string>\n<key>CFBundleShortVersionString</key><string>${xml(version)}</string>\n<key>CFBundleVersion</key><string>${xml(version.split('-')[0])}</string>\n<key>CFBundleIconFile</key><string>Autograde.icns</string>\n<key>LSMinimumSystemVersion</key><string>13.5</string>\n<key>LSArchitecturePriority</key><array><string>${arch === 'x64' ? 'x86_64' : arch}</string></array>\n<key>LSUIElement</key><true/>\n<key>NSHighResolutionCapable</key><true/>\n</dict></plist>\n`;
}

function copyRegular(source, destination, linkRoot) {
  if (linkRoot && fs.lstatSync(source).isSymbolicLink()) {
    const resolved = fs.realpathSync(source);
    if (!isInside(linkRoot, resolved)) throw new Error('Runtime link changed outside the source');
    source = resolved;
  }
  const stat = regular(source);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.copyFileSync(source, destination, fs.constants.COPYFILE_EXCL);
  fs.chmodSync(destination, stat.mode & 0o777);
}
function sanitizedEnvironment() {
  const result = { ...process.env };
  for (const name of Object.keys(result)) if (/^(?:NODE_|ELECTRON_|DYLD_|VSCODE_|AUTOGRADE_)/.test(name)) delete result[name];
  return result;
}

export function packageMacos({ source, node, output, zip = true }) {
  if (process.platform !== 'darwin') throw new Error('Build the Mac app on macOS with the matching architecture');
  source = fs.realpathSync(path.resolve(source));
  node = path.resolve(node); regular(node);
  output = path.resolve(output);
  if (!output.endsWith('.app') || fs.existsSync(output) || fs.existsSync(`${output}.zip`) || fs.existsSync(`${output}.zip.sha256`)) throw new Error('Choose a new .app output path; existing artifacts are never overwritten');
  if (isInside(source, output)) throw new Error('Output must be outside the Code-OSS source');
  const checked = verify(source);
  const upstream = readJson(path.join(here, 'upstream.json'));
  const nodeInfo = JSON.parse(execFileSync(node, ['-p', 'JSON.stringify({version:process.versions.node,arch:process.arch,platform:process.platform})'], { encoding: 'utf8', env: sanitizedEnvironment() }));
  if (nodeInfo.version !== upstream.node || nodeInfo.arch !== process.arch || nodeInfo.platform !== 'darwin') throw new Error(`Bundle Node ${upstream.node} for this Mac architecture (${process.arch})`);
  const nodeLicense = path.resolve(path.dirname(node), '../LICENSE'); regular(nodeLicense);
  const product = readJson(path.join(source, 'product.json'));
  const version = readJson(path.join(source, 'package.json')).version;
  const electronApp = path.join(source, '.build/electron', `${product.nameLong}.app`);
  directory(electronApp);
  const electronBinary = path.join(electronApp, 'Contents/MacOS', product.nameShort);
  regular(electronBinary);
  assertArchitecture(execFileSync('/usr/bin/lipo', ['-archs', electronBinary], { encoding: 'utf8' }), process.arch);
  const nativeSource = path.join(here, 'mac-launcher.m'); regular(nativeSource);
  const approvedExtensions = new Set([...execFileSync('git', ['-C', source, 'ls-tree', '--name-only', 'HEAD:extensions'], { encoding: 'utf8' }).trim().split('\n'), 'autograde', 'autograde-ide-profile']);
  const plan = runtimePlan(source, { approvedExtensions });
  fs.mkdirSync(path.dirname(output), { recursive: true });
  const staging = fs.mkdtempSync(path.join(path.dirname(output), '.autograde-mac-build-'));
  const app = path.join(staging, path.basename(output));
  const contents = path.join(app, 'Contents'), resources = path.join(contents, 'Resources');
  fs.mkdirSync(path.join(contents, 'MacOS'), { recursive: true });
  fs.mkdirSync(resources, { recursive: true });
  try {
    for (const relative of plan.files) copyRegular(path.join(source, relative), path.join(resources, 'editor/app', relative), source);
    for (const [relative, target] of Object.entries(plan.links)) {
      const destination = path.join(resources, 'editor/app', relative);
      fs.mkdirSync(path.dirname(destination), { recursive: true });
      fs.symlinkSync(target, destination);
    }
    // Framework symlinks are part of the upstream Electron application; ditto
    // preserves that known bundle, rather than dereferencing its Frameworks tree.
    execFileSync('/usr/bin/ditto', [electronApp, path.join(resources, 'editor/Autograde IDE.app')]);
    copyRegular(node, path.join(resources, 'runtime/bin/node'));
    copyRegular(nodeLicense, path.join(resources, 'runtime/LICENSE'));
    for (const name of ['mac-launcher.mjs', 'shared-session.mjs', 'learning-profile.mjs', 'runtime-settings.json']) copyRegular(path.join(here, name), path.join(resources, 'autograde', name));
    for (const name of ['LICENSE', 'version']) {
      const file = path.join(source, '.build/electron', name);
      if (fs.existsSync(file)) copyRegular(file, path.join(resources, 'licenses', `Electron-${name}`));
    }
    copyRegular(path.join(source, 'resources/darwin/code.icns'), path.join(resources, 'Autograde.icns'));
    const manifest = { schema: 1, version, arch: process.arch, editorRoot: 'editor/app',
      executable: `editor/Autograde IDE.app/Contents/MacOS/${product.nameShort}`, node: 'runtime/bin/node',
      approvedBuiltins: plan.approvedBuiltins, extensionVersion: checked.extensionVersion, policy: 'standard',
      minimumMacOS: '13.5', upstreamCommit: upstream.commit, nodeVersion: nodeInfo.version, distribution: 'macos-shared-lab-pilot' };
    fs.writeFileSync(path.join(resources, 'autograde/bundle.json'), `${JSON.stringify(manifest, null, 2)}\n`);
    fs.writeFileSync(path.join(contents, 'Info.plist'), infoPlist(version, process.arch));
    execFileSync('/usr/bin/xcrun', ['clang', '-arch', process.arch === 'x64' ? 'x86_64' : 'arm64', '-fobjc-arc', '-framework', 'Cocoa', '-mmacosx-version-min=13.5', nativeSource, '-o', path.join(contents, 'MacOS/AutogradeLauncher')], { stdio: 'inherit' });
    execFileSync('/usr/bin/plutil', ['-lint', path.join(contents, 'Info.plist')], { stdio: 'inherit' });
    // Local pilot signing makes the generated native executable runnable. This
    // is not Developer ID signing/notarization and does not bypass Gatekeeper.
    execFileSync('/usr/bin/codesign', ['--force', '--sign', '-', app], { stdio: 'inherit' });
    fs.renameSync(app, output);
    if (zip) {
      const temporaryZip = path.join(staging, 'Autograde.zip');
      execFileSync('/usr/bin/ditto', ['-c', '-k', '--sequesterRsrc', '--keepParent', output, temporaryZip]);
      fs.renameSync(temporaryZip, `${output}.zip`);
      const hash = execFileSync('/usr/bin/shasum', ['-a', '256', `${output}.zip`], { encoding: 'utf8' }).split(/\s+/)[0];
      fs.writeFileSync(`${output}.zip.sha256`, `${hash}  ${path.basename(output)}.zip\n`, { flag: 'wx' });
    }
    fs.rmdirSync(staging);
    return { app: output, zip: zip ? `${output}.zip` : null, architecture: process.arch,
      extensionVersion: checked.extensionVersion, files: plan.files.length, signing: 'ad-hoc-pilot', node: 'bundled' };
  } catch (error) {
    // Keep failed build artifacts for diagnosis; never delete an unknown target.
    throw new Error(`Mac package failed. Build files are preserved at ${staging}: ${error.message}`, { cause: error });
  }
}

export function parseOptions(args) {
  const result = { source: path.join(repoRoot, '.cache/code-oss'), node: process.execPath, output: path.join(repoRoot, 'build/macos/Autograde IDE.app'), zip: true };
  for (let index = 0; index < args.length; index++) {
    if (args[index] === '--no-zip') { result.zip = false; continue; }
    const key = { '--source': 'source', '--node': 'node', '--output': 'output' }[args[index]];
    if (!key || !args[index + 1] || args[index + 1].startsWith('--')) throw new Error('Usage: package-macos.mjs [--source PATH] [--node PATH] [--output NEW.app] [--no-zip]');
    result[key] = args[++index];
  }
  return result;
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(packageMacos(parseOptions(process.argv.slice(2))), null, 2)); }
  catch (error) { console.error(error.message); process.exitCode = 1; }
}
