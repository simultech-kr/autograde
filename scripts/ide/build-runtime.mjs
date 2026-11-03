#!/usr/bin/env node
// Native Code-OSS build with verified, same-host checkpoints for --resume.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { createHash, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { prepareNodeCompatibility, verifyNodeCompatibility } from '../../ide/code-oss/node-compatibility.mjs';
import { verify as verifyOverlay } from '../../ide/code-oss/manage.mjs';
import { openBuildJournal } from './build-resume.mjs';
import { inputFingerprint, snapshotPaths, snapshotMatches, dependencyPaths, assertReusableSource } from './build-inputs.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
export function sha256(file) {
  const hash = createHash('sha256'), buffer = Buffer.alloc(1024 * 1024), fd = fs.openSync(file, 'r');
  try { let length; while ((length = fs.readSync(fd, buffer, 0, buffer.length, null))) hash.update(buffer.subarray(0, length)); }
  finally { fs.closeSync(fd); }
  return hash.digest('hex');
}

export function parseOptions(args) {
  const result = { includeWsl: false, local: false, resume: false };
  for (let i = 0; i < args.length; i++) {
    if (args[i] === '--include-wsl' && !result.includeWsl) { result.includeWsl = true; continue; }
    if (args[i] === '--local' && !result.local) { result.local = true; continue; }
    if (args[i] === '--resume' && !result.resume) { result.resume = true; continue; }
    const key = { '--target': 'target', '--source': 'source', '--output': 'output' }[args[i]];
    if (!key || result[key] || !args[i + 1] || args[i + 1].startsWith('--')) throw new Error('Usage: build-runtime.mjs --target macos-arm64|linux-x64|windows-x64 --source PATH --output PATH [--include-wsl] [--local] [--resume]');
    result[key] = args[++i];
  }
  if (!['macos-arm64', 'linux-x64', 'windows-x64'].includes(result.target) || !result.source || !result.output) throw new Error('Specify a native target, a source directory and an output directory');
  return result;
}

export function readBuildSource(repository, { local = false } = {}) {
  if (typeof local !== 'boolean') throw new Error('Local build mode must be a boolean');
  const git = args => execFileSync('git', args, { cwd: repository, encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
  const sourceDirty = git(['status', '--porcelain=v1', '-z', '--untracked-files=all']).length > 0;
  if (sourceDirty && !local) {
    throw new Error('Candidate builds require a clean committed checkout. For a local test build that preserves your changes, add --local; local outputs cannot be finalized as release candidates.');
  }
  const sourceRevision = git(['rev-parse', '--verify', 'HEAD']).trim();
  return { sourceRevision, sourceDirty, status: local ? 'local-build' : 'release-candidate', buildMode: local ? 'local' : 'candidate' };
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

export function nativeBuildEnvironment(node, heap, inherited = process.env, platform = process.platform) {
  const env = { ...inherited };
  const inheritedPath = Object.entries(env).find(([key]) => key.toUpperCase() === 'PATH')?.[1] ?? '';
  // Nested upstream installs call npm by name, so they must resolve the same
  // official distribution as the parent build even when invoked by absolute path.
  for (const key of Object.keys(env)) {
    if (key.toUpperCase() === 'PATH'
        || /^(VSCODE_SKIP_NODE_VERSION_CHECK|VSCODE_SKIP_POSTINSTALL)$/i.test(key)
        || /^npm_config_(arch|platform|target_arch|target_platform|target|runtime|disturl|dist_url|nodedir|node_gyp|ignore_scripts)$/i.test(key)) delete env[key];
  }
  return { ...env, PATH: `${path.dirname(node)}${platform === 'win32' ? ';' : ':'}${inheritedPath}`,
    NODE_OPTIONS: `--max-old-space-size=${heap}`, npm_config_jobs: '2', npm_config_ignore_scripts: 'false', MAKEFLAGS: '-j2', VSCODE_FORCE_INSTALL: '1',
    GOMEMLIMIT: '3GiB', GOGC: '50', GOMAXPROCS: '2',
    ...(platform === 'linux' ? { CC: 'clang-15', CXX: 'clang++-15' } : {}) };
}

const digest = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');
const exists = file => { try { return fs.lstatSync(file); } catch (error) { if (error.code === 'ENOENT') return undefined; throw error; } };
const regular = file => { const info = fs.lstatSync(file); if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1) throw new Error(`A regular build file is required: ${file}`); return info; };

export function validateArtifact(output, artifact) {
  try {
    if (!artifact || typeof artifact.file !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]+$/.test(artifact.file)) return false;
    const archive = path.join(output, artifact.file);
    return regular(archive).size === artifact.bytes && sha256(archive) === artifact.sha256
      && regular(`${archive}.sha256`) && fs.readFileSync(`${archive}.sha256`, 'utf8').trim() === `${artifact.sha256}  ${artifact.file}`;
  } catch { return false; }
}

/** Unique publication names preserve older packages and unowned output files. */
export function publishArchive(archive, output, target) {
  regular(archive); regular(`${archive}.sha256`);
  const hash = sha256(archive), expected = fs.readFileSync(`${archive}.sha256`, 'utf8').trim();
  if (expected !== `${hash}  ${path.basename(archive)}`) throw new Error(`Package checksum mismatch: ${archive}`);
  const suffix = archive.endsWith('.tar.gz') ? '.tar.gz' : '.zip';
  const basename = path.basename(archive, suffix), filename = `${basename}-build-${randomUUID()}${suffix}`;
  const destination = path.join(output, filename);
  fs.copyFileSync(archive, destination, fs.constants.COPYFILE_EXCL);
  fs.writeFileSync(`${destination}.sha256`, `${hash}  ${filename}\n`, {flag: 'wx'});
  const artifact = {target, file: filename, bytes: regular(destination).size, sha256: hash};
  if (!validateArtifact(output, artifact)) throw new Error('Published package did not pass checksum verification');
  return artifact;
}

export function builtExtensionPaths(source) {
  const paths = new Set();
  const visit = relative => {
    for (const entry of fs.readdirSync(path.join(source, relative), {withFileTypes: true})) {
      if (!entry.isDirectory() || ['node_modules', '.git'].includes(entry.name)) continue;
      const child = `${relative}/${entry.name}`;
      // Include complete client/server trees, not just manifest entry points:
      // imports in sibling directories are equally important compiled outputs.
      if (['out', 'dist'].includes(entry.name)) paths.add(child);
      else visit(child);
    }
  };
  visit('extensions');
  return [...paths].sort();
}

export function nativeEnvironmentFingerprint(env) {
  return digest(Object.fromEntries(Object.entries(env).filter(([key]) =>
    /^(PATH|CC|CXX|CFLAGS|CXXFLAGS|CPPFLAGS|LDFLAGS|INCLUDE|LIB|LIBPATH|SDKROOT|MACOSX_DEPLOYMENT_TARGET|CPATH|C_INCLUDE_PATH|CPLUS_INCLUDE_PATH|PKG_CONFIG_PATH|CMAKE_.*|VC.*|VS.*|WindowsSdk.*|GYP_.*|npm_config_.*)$/i.test(key)).sort()));
}

function mediaPaths(source) {
  const paths = [];
  for (const entry of fs.readdirSync(path.join(source, 'extensions'), {withFileTypes: true})) {
    if (!entry.isDirectory()) continue;
    for (const child of fs.readdirSync(path.join(source, 'extensions', entry.name), {withFileTypes: true})) {
      if (child.isDirectory() && (child.name === 'media' || child.name.endsWith('-out'))) paths.push(`extensions/${entry.name}/${child.name}`);
    }
  }
  return paths;
}

function sourceMapPaths(directory, relative = 'out') {
  const result = [];
  for (const entry of fs.readdirSync(path.join(directory, relative), {withFileTypes: true})) {
    const child = `${relative}/${entry.name}`;
    if (entry.isDirectory()) result.push(...sourceMapPaths(directory, child));
    else if (entry.name.endsWith('.map')) result.push(child);
  }
  return result;
}

export function buildRuntime({ target, source, output, includeWsl = false, local = false, resume = false }) {
  const pin = read(path.join(root, 'ide/code-oss/upstream.json'));
  assertBuildHost(target, pin);
  const provenance = readBuildSource(root, { local });
  if (pin.repository !== 'https://github.com/microsoft/vscode.git' || !/^[a-f0-9]{40}$/.test(pin.commit) || !/^\d+\.\d+\.\d+$/.test(pin.tag)) throw new Error('Invalid upstream pin');
  source = path.resolve(source); output = path.resolve(output);
  for (const directory of [source, output]) {
    if (!resume && exists(directory)) throw new Error(`Refusing an existing build directory: ${directory}; add --resume to reuse this build`);
  }
  if (exists(source)) assertReusableSource(source, pin);
  const node = fs.realpathSync(process.execPath);
  if (!fs.existsSync(path.resolve(path.dirname(node), process.platform === 'win32' ? 'LICENSE' : '../LICENSE'))) throw new Error('Use an official Node distribution with its adjacent LICENSE');
  if (os.totalmem() < 6 * 1024 ** 3) throw new Error('Use a build machine with at least 6 GiB RAM');
  const heap = Math.min(5120, Math.floor(os.totalmem() / 1024 ** 2) - 2048);
  const env = nativeBuildEnvironment(node, heap);
  const environmentKey = nativeEnvironmentFingerprint(env);
  const inputs = {
    extension: inputFingerprint(root, ['extensions/vscode']),
    extensionDependencies: inputFingerprint(root, ['extensions/vscode/package.json', 'extensions/vscode/package-lock.json']),
    ide: inputFingerprint(root, ['ide/code-oss']),
    pipeline: inputFingerprint(root, ['scripts/ide']),
    compatibility: inputFingerprint(root, ['ide/code-oss/node-compatibility.mjs']),
  };
  const identity = {target, hostname: os.hostname(), platform: process.platform, architecture: process.arch, node: pin.node, upstream: pin, local, includeWsl: target === 'linux-x64' && includeWsl};
  const journal = openBuildJournal({source, output, identity, resume});
  source = journal.source; output = journal.output;
  try {
    if (exists(source)) assertReusableSource(source, pin);
    // The canonical report certifies a completed invocation only. Preserve an
    // earlier report outside the finalizer's filename pattern while resuming.
    const reportPath = path.join(output, `build-${target}.json`);
    if (exists(reportPath)) { regular(reportPath); fs.renameSync(reportPath, path.join(output, `.previous-report-${randomUUID()}.json`)); }
    const free = fs.statfsSync(path.dirname(source));
    if (free.bavail * free.bsize < 12 * 1024 ** 3) throw new Error('At least 12 GiB free disk space is needed for dependencies and package staging');
    const run = (command, args, cwd = root, extra = {}) => execFileSync(command, args, {cwd, env, stdio: 'inherit', ...extra});
    const npmCli = [path.join(path.dirname(node), 'node_modules/npm/bin/npm-cli.js'), path.resolve(path.dirname(node), '../lib/node_modules/npm/bin/npm-cli.js')].find(file => fs.existsSync(file));
    if (!npmCli) throw new Error('Cannot find npm in the official Node distribution');
    const npm = (args, cwd) => run(node, [npmCli, '--ignore-scripts=false', ...args], cwd);
    const report = {schema: 1, ...provenance, buildTarget: target, includeWsl: identity.includeWsl, upstream: pin,
      extensionVersion: read(path.join(root, 'extensions/vscode/package.json')).version,
      host: {platform: process.platform, architecture: process.arch, node: process.versions.node, heapMiB: heap},
      buildInputs: inputs, checks: [], artifacts: [], acceptance: {institution: 'not-tested', windowsWsl2: 'not-tested', productionReady: false}};
    const complete = name => report.checks.push({name, result: 'passed'});
    const stage = (name, {key, deps = [], directory = source, paths, exclude = [], validate = () => true}, action) => {
      const value = journal.step(name, {key, deps, validate: previous => previous && Object.hasOwn(previous, 'snapshot')
        && (paths ? Boolean(previous.snapshot) && snapshotMatches(directory, previous.snapshot) : previous.snapshot === null) && validate(previous)}, () => {
        action();
        const selected = typeof paths === 'function' ? paths() : paths;
        if (selected && !selected.length) throw new Error(`No build outputs were produced by ${name}`);
        if (selected) for (const relative of selected) if (!exists(path.join(directory, relative))) throw new Error(`Missing output of ${name}: ${relative}`);
        return {snapshot: selected ? snapshotPaths(directory, selected, {exclude: typeof exclude === 'function' ? exclude() : exclude}) : null};
      });
      complete(name); return value;
    };
    // Increment when the install recipe changes independently of lockfiles/env.
    const installKey = {recipe: 1, node: pin.node, environmentKey};
    stage('extension-dependencies', {key: {...installKey, files: inputs.extensionDependencies}, directory: path.join(root, 'extensions/vscode'), paths: ['node_modules']},
      () => npm(['ci'], path.join(root, 'extensions/vscode')));
    let extensionTests;
    if (process.platform === 'win32') {
      stage('extension-compile', {key: inputs.extension, deps: ['extension-dependencies'], directory: path.join(root, 'extensions/vscode'), paths: ['out'],
        exclude: () => sourceMapPaths(path.join(root, 'extensions/vscode'))}, () => {
        npm(['run', 'clean'], path.join(root, 'extensions/vscode')); npm(['run', 'compile'], path.join(root, 'extensions/vscode'));
      });
      extensionTests = 'extension-platform-tests';
      stage(extensionTests, {key: {inputs: inputs.extension, pipeline: inputs.pipeline}, deps: ['extension-compile']}, () => run(node, ['--test',
        'out/test/helpers.test.js', 'out/test/filesystemFixtures.test.js', 'out/test/learningEnvironment.test.js',
        'out/test/learningExport.test.js', 'out/test/learningExportView.test.js', 'out/test/sharedLearningSession.test.js',
        'out/test/learningCheckpoint.test.js', 'out/test/managedExtensions.test.js', 'out/test/managedExtensionsApi.test.js', 'out/test/managedExtensionsView.test.js'], path.join(root, 'extensions/vscode')));
    } else {
      extensionTests = 'extension-tests';
      stage(extensionTests, {key: inputs.extension, deps: ['extension-dependencies']}, () => npm(['test'], path.join(root, 'extensions/vscode')));
    }
    stage('launcher-and-policy-tests', {key: {ide: inputs.ide, pipeline: inputs.pipeline}}, () => {
      const tests = fs.readdirSync(path.join(root, 'ide/code-oss')).filter(name => name.endsWith('.test.mjs') && (process.platform !== 'win32' || ['package-windows.test.mjs', 'windows-native-bootstrap.test.mjs'].includes(name))).map(name => path.join(root, 'ide/code-oss', name));
      run(node, ['--test', ...tests]);
    });
    stage('upstream-commit-and-node', {key: pin, validate: () => { assertReusableSource(source, pin); return true; }}, () => {
      if (!exists(source)) {
        const attempt = fs.mkdtempSync(path.join(path.dirname(source), '.autograde-clone-'));
        const checkout = path.join(attempt, 'source');
        run('git', ['clone', '--depth', '1', '--branch', pin.tag, '--single-branch', pin.repository, checkout]);
        assertReusableSource(checkout, pin);
        if (exists(source)) throw new Error('Source appeared while cloning; the new checkout was preserved');
        fs.renameSync(checkout, source); fs.rmdirSync(attempt);
      }
      assertReusableSource(source, pin);
    });
    stage('managed-node-build-compatibility', {key: {pin, code: inputs.compatibility}, deps: ['upstream-commit-and-node'], validate: () => { verifyNodeCompatibility(source, pin, {required: true}); return true; }},
      () => { prepareNodeCompatibility(source, pin); });
    const installDependencies = ['managed-node-build-compatibility'];
    if (process.platform === 'win32') {
      stage('windows-electron-header-preinstall', {key: {...installKey, pin}, deps: installDependencies, validate: () => {
        const targets = ['.npmrc', 'remote/.npmrc'].map(file => /^\s*target\s*=\s*"([\w.-]+)"/m.exec(fs.readFileSync(path.join(source, file), 'utf8'))?.[1]).filter(Boolean);
        return targets.length > 0 && targets.every(version => exists(path.join(env.LOCALAPPDATA ?? '', 'node-gyp', 'Cache', version, 'include', 'node', 'node.h'))?.isFile());
      }}, () => run(node, ['build/npm/preinstall.ts'], source));
      installDependencies.push('windows-electron-header-preinstall');
    }
    stage('native-code-oss-dependencies', {key: {...installKey, pin}, deps: installDependencies,
      paths: () => [...dependencyPaths(source), '.build/typings/electron.d.ts'], validate: () => exists(path.join(source, 'node_modules/gulp/bin/gulp.js'))?.isFile() === true},
      () => npm(['ci'], source));
    stage('teacher-only-policy-overlay', {key: {extension: inputs.extension, ide: inputs.ide}, deps: [extensionTests, 'native-code-oss-dependencies'], validate: () => { verifyOverlay(source); return true; }},
      () => run(node, ['ide/code-oss/manage.mjs', 'prepare', '--source', source]));
    const policyOutputs = () => Object.keys(read(path.join(source, '.autograde-ide-overlay.json')).policySources).map(file => file.replace(/^src\//, 'out/').replace(/\.ts$/, '.js'));
    let prior = 'teacher-only-policy-overlay';
    for (const [name, args] of compileSteps(source, heap)) {
      const options = {key: {args, environmentKey}, deps: [prior]};
      if (name === 'code-oss-client-compile') Object.assign(options, {paths: ['out'], exclude: policyOutputs});
      else if (name === 'code-oss-extension-compile') options.paths = () => builtExtensionPaths(source);
      else if (name === 'code-oss-extension-media') options.paths = () => mediaPaths(source);
      stage(name, options, () => run(node, args, source)); prior = name;
    }
    const product = read(path.join(source, 'product.json'));
    stage('native-electron-download', {key: {pin, product: {nameShort: product.nameShort, nameLong: product.nameLong, applicationName: product.applicationName}}, deps: ['native-code-oss-dependencies'], paths: ['.build/electron']},
      () => npm(['run', 'electron'], source));
    stage('overlay-verification', {key: {extension: inputs.extension, ide: inputs.ide}, deps: [prior, 'native-electron-download'], validate: () => { verifyOverlay(source); return true; }},
      () => run(node, ['ide/code-oss/manage.mjs', 'verify', '--source', source]));
    const packageTarget = (kind, basename, suffix) => {
      const name = `package-${kind}`;
      const value = journal.step(name, {key: {basename, suffix, pipeline: inputs.pipeline, ide: inputs.ide}, deps: ['overlay-verification', 'launcher-and-policy-tests'],
        validate: previous => validateArtifact(output, previous.artifact)}, () => {
        // Keep failed package trees outside the artifact directory: candidate
        // finalization must not traverse partial apps or their runtime links.
        const attempt = fs.mkdtempSync(path.join(path.dirname(output), `.${path.basename(output)}.package-attempt-`)), directory = path.join(attempt, basename);
        run(node, [`ide/code-oss/package-${kind}.mjs`, '--source', source, '--node', node, '--output', directory]);
        const checks = [name];
        if (kind === 'macos') { run(node, ['ide/code-oss/check-extension-policy.mjs', directory]); checks.push('macos-policy-smoke'); }
        else {
          const electron = path.join(directory, 'editor/electron', kind === 'windows' ? `${product.nameShort}.exe` : product.applicationName);
          const app = path.join(directory, 'editor/app');
          run(electron, [path.join(app, 'out/cli.js'), app, '--version'], app, {env: {...env, ELECTRON_RUN_AS_NODE: '1'}});
          checks.push(`${kind}-packaged-cli-version`);
        }
        if (kind === 'windows') checks.push('windows-native-job-acl-msvc-checks');
        const artifact = publishArchive(`${directory}${suffix}`, output,
          kind === 'macos' ? 'macos-native' : kind === 'wsl' ? 'windows-wsl2-payload' : kind === 'windows' ? 'windows-native' : 'linux-native');
        // This uniquely created attempt belongs to this invocation. Failed attempts
        // are preserved; only a verified and published package is cleaned up.
        fs.rmSync(attempt, {recursive: true});
        return {checks, artifact};
      });
      for (const checked of value.checks) complete(checked);
      report.artifacts.push(value.artifact);
    };
    const version = `${report.extensionVersion}${local ? '-local' : ''}`;
    if (target === 'macos-arm64') packageTarget('macos', `Autograde-IDE-${version}-macos-arm64.app`, '.zip');
    else if (target === 'windows-x64') packageTarget('windows', `Autograde-IDE-${version}-windows-native-x64`, '.zip');
    else {
      packageTarget('linux', `Autograde-IDE-${version}-linux-x64`, '.tar.gz');
      if (includeWsl) packageTarget('wsl', `Autograde-IDE-${version}-windows-wsl2-linux-x64`, '.tar.gz');
    }
    // Do not attach current provenance to outputs built while the source changed.
    for (const [key, pathspecs] of Object.entries({extension: ['extensions/vscode'], ide: ['ide/code-oss'], pipeline: ['scripts/ide']})) {
      if (inputFingerprint(root, pathspecs) !== inputs[key]) throw new Error('Autograde build inputs changed during the build; rerun with --resume');
    }
    if (JSON.stringify(readBuildSource(root, {local})) !== JSON.stringify(provenance)) throw new Error('Autograde revision changed during the build; rerun with --resume');
    assertReusableSource(source, pin); verifyOverlay(source);
    const destination = path.join(output, `build-${target}.json`);
    // Preserve any prior report; only the newly completed build receives the
    // canonical final report name. Never leave partial JSON at that name.
    const temporary = path.join(output, `.report-${randomUUID()}.json`);
    fs.writeFileSync(temporary, `${JSON.stringify(report, null, 2)}\n`, {flag: 'wx'});
    if (exists(destination)) { regular(destination); fs.renameSync(destination, path.join(output, `.previous-report-${randomUUID()}.json`)); }
    fs.renameSync(temporary, destination);
    return report;
  } finally { journal.close(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { console.log(JSON.stringify(buildRuntime(parseOptions(process.argv.slice(2))), null, 2)); }
  catch (error) { console.error(error.stack || error); process.exitCode = 1; }
}
