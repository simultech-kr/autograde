#!/usr/bin/env node
// Fresh, native Code-OSS build. This script intentionally never restores node_modules.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
export function sha256(file) {
  const hash = createHash('sha256'), buffer = Buffer.alloc(1024 * 1024), fd = fs.openSync(file, 'r');
  try { let length; while ((length = fs.readSync(fd, buffer, 0, buffer.length, null))) hash.update(buffer.subarray(0, length)); }
  finally { fs.closeSync(fd); }
  return hash.digest('hex');
}

export function parseOptions(args) {
  const result = { includeWsl: false };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--include-wsl' && !result.includeWsl) { result.includeWsl = true; continue; }
    const key = { '--target': 'target', '--source': 'source', '--output': 'output' }[args[i]];
    if (!key || result[key] || !args[i + 1] || args[i + 1].startsWith('--')) throw new Error('Usage: build-runtime.mjs --target macos-arm64|linux-x64|windows-x64 --source NEW_PATH --output NEW_PATH [--include-wsl]');
    result[key] = args[++i];
  }
  if (!['macos-arm64', 'linux-x64', 'windows-x64'].includes(result.target) || !result.source || !result.output) throw new Error('Specify a native target, a new source directory and a new output directory');
  return result;
}

export function assertBuildHost(target, pin, host = process) {
  const expected = { 'macos-arm64': ['darwin', 'arm64'], 'linux-x64': ['linux', 'x64'], 'windows-x64': ['win32', 'x64'] }[target];
  if (!expected || expected[0] !== host.platform || expected[1] !== host.arch) throw new Error(`Build ${target} on its native operating system and architecture`);
  if (host.versions.node !== pin.node) throw new Error(`Use pinned Node ${pin.node}; found ${host.versions.node}`);
}

export function compileSteps(source, heap) {
  const gulp = task => ['--experimental-strip-types', `--max-old-space-size=${heap}`, './node_modules/gulp/bin/gulp.js', task];
  return [
    ['generate-api-proposal-names', gulp('compile-api-proposal-names')],
    ['generate-extension-point-names', gulp('compile-extension-point-names')],
    ['code-oss-source-typecheck', ['node_modules/@typescript/native/bin/tsc', '--project', path.join(source, 'src/tsconfig.json'), '--pretty', 'false', '--incremental', '--noEmit', '--tsBuildInfoFile', path.join(source, 'src/tsconfig.typecheck.tsbuildinfo')]],
    // compile-client reruns its authoritative check using that same incremental
    // cache, while emitting the client. Never substitute transpile-only output.
    ['code-oss-client-compile', gulp('compile-client')],
    ['code-oss-extension-compile', gulp('compile-extensions')],
    ['code-oss-extension-media', gulp('compile-extension-media')],
    ['code-oss-monaco-typecheck', ['node_modules/typescript/bin/tsc6', '-p', 'src/tsconfig.monaco.json', '--noEmit']],
  ];
}

export function buildRuntime({ target, source, output, includeWsl = false }) {
  const pin = read(path.join(root, 'ide/code-oss/upstream.json'));
  assertBuildHost(target, pin);
  if (execFileSync('git', ['status', '--porcelain', '--untracked-files=all'], { cwd: root, encoding: 'utf8' }).trim()) throw new Error('Candidate builds require a clean committed checkout; preserve local changes and use a separate clean checkout');
  if (pin.repository !== 'https://github.com/microsoft/vscode.git' || !/^[a-f0-9]{40}$/.test(pin.commit) || !/^\d+\.\d+\.\d+$/.test(pin.tag)) throw new Error('Invalid upstream pin');
  source = path.resolve(source); output = path.resolve(output);
  for (const directory of [source, output]) {
    if (fs.existsSync(directory)) throw new Error(`Refusing an existing build directory: ${directory}`);
    fs.mkdirSync(path.dirname(directory), { recursive: true });
  }
  if (source.startsWith(`${output}${path.sep}`) || output.startsWith(`${source}${path.sep}`) || source === output) throw new Error('Source and output must be separate directories');
  // setup-node keeps the original distribution LICENSE alongside bin/node.
  const node = fs.realpathSync(process.execPath);
  if (!fs.existsSync(path.resolve(path.dirname(node), process.platform === 'win32' ? 'LICENSE' : '../LICENSE'))) throw new Error('Use an official Node distribution with its adjacent LICENSE');
  const free = fs.statfsSync(path.dirname(source));
  if (free.bavail * free.bsize < 12 * 1024 ** 3) throw new Error('At least 12 GiB free disk space is needed for the source, dependencies and package staging');
  if (os.totalmem() < 6 * 1024 ** 3) throw new Error('Use a build machine with at least 6 GiB RAM');
  const heap = Math.min(5120, Math.floor(os.totalmem() / 1024 ** 2) - 2048);
  const env = { ...process.env, NODE_OPTIONS: `--max-old-space-size=${heap}`, npm_config_jobs: '2', MAKEFLAGS: '-j2', VSCODE_FORCE_INSTALL: '1',
    GOMEMLIMIT: '3GiB', GOGC: '50', GOMAXPROCS: '2',
    ...(process.platform === 'linux' ? { CC: 'clang-15', CXX: 'clang++-15' } : {}) };
  // A caller's cross-compilation overrides must not contaminate native modules.
  for (const key of Object.keys(env)) if (/^npm_config_(arch|platform|target_arch|target_platform)$/i.test(key)) delete env[key];
  const run = (command, args, cwd = root, extra = {}) => execFileSync(command, args, { cwd, env, stdio: 'inherit', ...extra });
  const capture = (command, args, cwd = root) => run(command, args, cwd, { encoding: 'utf8', stdio: ['ignore', 'pipe', 'inherit'] }).trim();
  const sourceRevision = capture('git', ['rev-parse', 'HEAD']);
  fs.mkdirSync(output);
  const report = { schema: 1, status: 'release-candidate', sourceDirty: false, buildTarget: target, includeWsl: target === 'linux-x64' && includeWsl, sourceRevision, upstream: pin,
    extensionVersion: read(path.join(root, 'extensions/vscode/package.json')).version,
    host: { platform: process.platform, architecture: process.arch, node: process.versions.node, heapMiB: heap },
    checks: [], artifacts: [], acceptance: { institution: 'not-tested', windowsWsl2: 'not-tested', productionReady: false } };
  const check = (name, command, args, cwd) => { run(command, args, cwd); report.checks.push({ name, result: 'passed' }); };
  const npmCli = [path.join(path.dirname(node), 'node_modules/npm/bin/npm-cli.js'), path.resolve(path.dirname(node), '../lib/node_modules/npm/bin/npm-cli.js')].find(file => fs.existsSync(file));
  if (!npmCli) throw new Error('Cannot find npm in the official Node distribution');
  const npmCheck = (name, args, cwd) => check(name, node, [npmCli, ...args], cwd);
  npmCheck('extension-dependencies', ['ci'], path.join(root, 'extensions/vscode'));
  if (process.platform === 'win32') {
    npmCheck('extension-compile', ['run', 'compile'], path.join(root, 'extensions/vscode'));
    check('extension-platform-tests', node, ['--test', 'out/test/helpers.test.js', 'out/test/learningEnvironment.test.js',
      'out/test/learningExport.test.js', 'out/test/learningExportView.test.js', 'out/test/sharedLearningSession.test.js',
      'out/test/learningCheckpoint.test.js',
      'out/test/managedExtensions.test.js', 'out/test/managedExtensionsApi.test.js', 'out/test/managedExtensionsView.test.js'], path.join(root, 'extensions/vscode'));
  } else npmCheck('extension-tests', ['test'], path.join(root, 'extensions/vscode'));
  const tests = fs.readdirSync(path.join(root, 'ide/code-oss')).filter(name => name.endsWith('.test.mjs') && (process.platform !== 'win32' || ['package-windows.test.mjs', 'windows-native-bootstrap.test.mjs'].includes(name))).map(name => path.join(root, 'ide/code-oss', name));
  check('launcher-and-policy-tests', node, ['--test', ...tests]);
  run('git', ['clone', '--depth', '1', '--branch', pin.tag, '--single-branch', pin.repository, source]);
  if (capture('git', ['rev-parse', 'HEAD'], source) !== pin.commit) throw new Error('Upstream tag no longer matches the pinned commit');
  if (fs.readFileSync(path.join(source, '.nvmrc'), 'utf8').trim() !== pin.node) throw new Error('Upstream Node pin differs from the build definition');
  report.checks.push({ name: 'upstream-commit-and-node', result: 'passed' });
  // Install BEFORE adding the built-in extension; upstream postinstall sees only
  // upstream's own locked dependency directories and builds for this host.
  if (process.platform === 'win32') check('windows-electron-header-preinstall', node, ['build/npm/preinstall.ts'], source);
  npmCheck('native-code-oss-dependencies', ['ci'], source);
  check('teacher-only-policy-overlay', node, ['ide/code-oss/manage.mjs', 'prepare', '--source', source]);
  // Upstream's full compile starts several memory-heavy compilers together and
  // its npm wrapper hardcodes 8 GiB. Keep every check, but execute sequentially.
  for (const [name, args] of compileSteps(source, heap)) check(name, node, args, source);
  npmCheck('native-electron-download', ['run', 'electron'], source);
  check('overlay-verification', node, ['ide/code-oss/manage.mjs', 'verify', '--source', source]);
  const packageTarget = (kind, basename, suffix) => {
    const directory = path.join(output, basename);
    check(`package-${kind}`, node, [`ide/code-oss/package-${kind}.mjs`, '--source', source, '--node', node, '--output', directory]);
    if (kind === 'macos') {
      check('macos-policy-smoke', node, ['ide/code-oss/check-extension-policy.mjs', directory]);
    } else {
      const product = read(path.join(source, 'product.json'));
      const electron = path.join(directory, 'editor/electron', kind === 'windows' ? `${product.nameShort}.exe` : product.applicationName);
      const app = path.join(directory, 'editor/app');
      // Exercises the packaged ELF binary without bypassing Chromium's sandbox
      // or pretending that a headless --version check validates a desktop.
      run(electron, [path.join(app, 'out/cli.js'), app, '--version'], app, { env: { ...env, ELECTRON_RUN_AS_NODE: '1' } });
      report.checks.push({ name: `${kind}-packaged-cli-version`, result: 'passed' });
    }
    const archive = `${directory}${suffix}`, digest = sha256(archive);
    const recorded = fs.readFileSync(`${archive}.sha256`, 'utf8').trim().split(/\s+/)[0];
    if (digest !== recorded) throw new Error(`Package checksum mismatch: ${archive}`);
    if (kind === 'windows') report.checks.push({ name: 'windows-native-job-acl-msvc-checks', result: 'passed' });
    report.artifacts.push({ target: kind === 'macos' ? 'macos-native' : kind === 'wsl' ? 'windows-wsl2-payload' : kind === 'windows' ? 'windows-native' : 'linux-native',
      file: path.basename(archive), bytes: fs.statSync(archive).size, sha256: digest });
    // This directory was created by the successful package step in our new
    // output folder; archives are retained. Avoid doubling CI disk use.
    fs.rmSync(directory, { recursive: true });
  };
  const version = report.extensionVersion;
  if (target === 'macos-arm64') packageTarget('macos', `Autograde-IDE-${version}-macos-arm64.app`, '.zip');
  else if (target === 'windows-x64') packageTarget('windows', `Autograde-IDE-${version}-windows-native-x64`, '.zip');
  else {
    packageTarget('linux', `Autograde-IDE-${version}-linux-x64`, '.tar.gz');
    if (includeWsl) packageTarget('wsl', `Autograde-IDE-${version}-windows-wsl2-linux-x64`, '.tar.gz');
  }
  fs.writeFileSync(path.join(output, `build-${target}.json`), `${JSON.stringify(report, null, 2)}\n`, { flag: 'wx' });
  return report;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(buildRuntime(parseOptions(process.argv.slice(2))), null, 2)); }
  catch (error) { console.error(error.stack || error); process.exitCode = 1; }
}
