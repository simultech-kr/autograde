#!/usr/bin/env node
// Operator build tool; the generated Mac icon needs no student Node installation.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { infoPlist, assertArchitecture } from './package-macos.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
function plain(file) { const stat = fs.lstatSync(file); if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('Regular channel input required'); }
const inside = (root, candidate) => { const part = path.relative(root, candidate); return part === '' || part !== '..' && !part.startsWith(`..${path.sep}`) && !path.isAbsolute(part); };
export function channelBootstrapConfig({store, context, output}) {
  store = path.resolve(store); context = path.resolve(context); output = path.resolve(output);
  if (inside(store, output) || inside(output, store) || inside(output, context)) throw new Error('Stable icon, release store and operator context must be separate');
  plain(context);
  const settings = JSON.parse(fs.readFileSync(context, 'utf8'));
  if (settings.platform !== 'darwin' || settings.architecture !== process.arch) throw new Error('Mac channel must match the build architecture');
  if (!Array.isArray(settings.sessionRoots) || !settings.sessionRoots.length || settings.sessionRoots.some(root => !path.isAbsolute(root)
    || inside(root, output) || inside(root, store) || inside(root, context))) throw new Error('Channel configuration and binaries must be outside student session roots');
  return {schema: 1, store, context};
}
export function installMacChannel({store, context, output, node = process.execPath}) {
  if (process.platform !== 'darwin') throw new Error('Build the Mac channel icon on macOS');
  output = path.resolve(output); node = path.resolve(node);
  if (!output.endsWith('.app') || fs.existsSync(output)) throw new Error('Choose a new .app path; installed icons are never replaced while running');
  const config = channelBootstrapConfig({store, context, output}); plain(node);
  assertArchitecture(execFileSync('/usr/bin/lipo', ['-archs', node], {encoding: 'utf8'}), process.arch);
  const expectedNode = JSON.parse(fs.readFileSync(path.join(here, 'upstream.json'), 'utf8')).node;
  const cleanEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_|DYLD_)/.test(key)));
  if (execFileSync(node, ['-p', 'process.versions.node'], {encoding: 'utf8', env: cleanEnv}).trim() !== expectedNode) throw new Error('Use the pinned bundled Node version');
  const license = path.resolve(path.dirname(node), '../LICENSE'); plain(license);
  fs.mkdirSync(path.dirname(output), {recursive: true});
  const staging = fs.mkdtempSync(path.join(path.dirname(output), '.channel-build-')), app = path.join(staging, path.basename(output));
  const contents = path.join(app, 'Contents'), resources = path.join(contents, 'Resources'), runtime = path.join(resources, 'autograde');
  fs.mkdirSync(runtime, {recursive: true}); fs.mkdirSync(path.join(resources, 'runtime/bin'), {recursive: true}); fs.mkdirSync(path.join(contents, 'MacOS'));
  fs.copyFileSync(node, path.join(resources, 'runtime/bin/node')); fs.copyFileSync(license, path.join(resources, 'runtime/LICENSE'));
  for (const name of ['release-channel.mjs', 'channel-launcher.mjs']) fs.copyFileSync(path.join(here, name), path.join(runtime, name));
  fs.writeFileSync(path.join(runtime, 'channel.json'), `${JSON.stringify(config, null, 2)}\n`, {flag: 'wx'});
  fs.writeFileSync(path.join(contents, 'Info.plist'), infoPlist('1.0.0', process.arch)
    .replaceAll('Autograde IDE', 'Autograde Channel').replaceAll('info.cbchoi.autograde.launcher', 'info.cbchoi.autograde.channel')
    .replace('<key>CFBundleIconFile</key><string>Autograde.icns</string>\n', ''));
  const native = path.join(staging, 'channel.m');
  fs.writeFileSync(native, fs.readFileSync(path.join(here, 'mac-launcher.m'), 'utf8').replace('@"autograde/mac-launcher.mjs"', '@"autograde/channel-launcher.mjs"'));
  execFileSync('/usr/bin/xcrun', ['clang', '-arch', process.arch === 'arm64' ? 'arm64' : 'x86_64', '-fobjc-arc', '-framework', 'Cocoa', '-mmacosx-version-min=13.5', native, '-o', path.join(contents, 'MacOS/AutogradeLauncher')], {stdio: 'pipe'});
  execFileSync('/usr/bin/plutil', ['-lint', path.join(contents, 'Info.plist')], {stdio: 'pipe'});
  execFileSync('/usr/bin/codesign', ['--force', '--sign', '-', app], {stdio: 'pipe'});
  fs.renameSync(app, output); fs.rmSync(staging, {recursive: true});
  return {app: output, signing: 'ad-hoc-candidate', node: 'bundled', deploymentReady: false, restartRequired: true};
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const args = {}; for (let i = 2; i < process.argv.length; i += 2) { const key = process.argv[i].slice(2); if (!['store', 'context', 'output', 'node'].includes(key) || !process.argv[i + 1]) throw new Error('Use --store PATH --context FILE --output NEW.app [--node PATH]'); args[key] = process.argv[i + 1]; }
    process.stdout.write(`${JSON.stringify(installMacChannel(args), null, 2)}\n`);
  } catch (error) { process.stderr.write(`${error.message}\n`); process.exitCode = 1; }
}
