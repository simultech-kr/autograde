#!/usr/bin/env node
// Phase-one source overlay. No downloads, publishing, database or OS policy changes.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createHash } from 'node:crypto';
import { execFileSync, spawn } from 'node:child_process';
import { policySources } from './extension-install-policy.mjs';
import { compilePolicyRuntime } from './policy-runtime.mjs';
import { learnerEnvironment, learnerLayout, prepareLearner, validateLearningOptions, verifyRestrictedLaunch } from './learning-profile.mjs';
import { sharedSessionLayout, prepareSharedSession, markSessionLaunching, recordSessionProcess,
  cleanupSharedSession, superviseSharedChild } from './shared-session.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
export const repoRoot = path.resolve(here, '../..');
const markerName = '.autograde-ide-overlay.json';
const extensionNames = ['autograde', 'autograde-ide-profile'];
const removedProductKeys = [
  'extensionsGallery', 'updateUrl', 'downloadUrl',
  'configurationSync', 'trustedExtensionAuthAccess', 'extensionTips', 'keymapExtensionTips', 'voiceWsUrl',
];
export const readJson = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const digest = content => createHash('sha256').update(content).digest('hex');
const jsonBytes = value => Buffer.from(`${JSON.stringify(value, null, 2)}\n`);
const git = (source, ...args) => execFileSync('git', ['-C', source, ...args], { encoding: 'utf8' }).trim();

function requireRegular(file) {
  if (!fs.lstatSync(file).isFile()) throw new Error(`Regular file required (no symlink): ${file}`);
}

function requireDirectory(directory) {
  if (!fs.lstatSync(directory).isDirectory()) throw new Error(`Directory required (no symlink): ${directory}`);
}

export function walk(directory, prefix = '') {
  requireDirectory(directory);
  const result = new Map();
  for (const entry of fs.readdirSync(directory).sort()) {
    const file = path.join(directory, entry);
    const name = prefix ? `${prefix}/${entry}` : entry;
    const stat = fs.lstatSync(file);
    if (stat.isDirectory()) {
      for (const item of walk(file, name)) result.set(...item);
    } else {
      requireRegular(file);
      result.set(name, fs.readFileSync(file));
    }
  }
  return result;
}

export function productOverlay(base, overlay = readJson(path.join(here, 'product.json'))) {
  // 1.143's workbench asserts defaultChatAgent metadata at module load. Retain its
  // schema; hiding AI uses the profile and launch flags, not an incomplete product.
  const result = { ...base, ...overlay };
  for (const key of removedProductKeys) delete result[key];
  return result;
}

// Copy only compiled runtime files and the installed production dependency closure.
// This avoids shipping credentials, tests, old VSIXs, development dependencies or symlinks.
export function extensionBundle(root) {
  requireDirectory(root);
  requireRegular(path.join(root, 'package.json'));
  const manifest = readJson(path.join(root, 'package.json'));
  if (manifest.publisher !== 'autograde' || manifest.name !== 'autograde-vscode') {
    throw new Error('Expected the Autograde VS Code extension');
  }
  const files = new Map();
  const runtimeManifest = { ...manifest };
  delete runtimeManifest.scripts;
  delete runtimeManifest.devDependencies;
  files.set('package.json', jsonBytes(runtimeManifest));
  for (const name of ['README.md', 'LICENSE', 'LICENSE.txt', 'ThirdPartyNotices.txt']) {
    const file = path.join(root, name);
    if (fs.existsSync(file)) { requireRegular(file); files.set(name, fs.readFileSync(file)); }
  }
  for (const name of ['out', 'media']) {
    for (const [relative, bytes] of walk(path.join(root, name))) {
      if (name === 'out' && (relative.startsWith('test/') || !relative.endsWith('.js'))) continue;
      files.set(`${name}/${relative}`, bytes);
    }
  }
  const main = manifest.main.replace(/^\.\//, '');
  if (!files.has(main)) throw new Error('Compile the extension before preparing the IDE');
  const visited = new Set();
  const packageLock = readJson(path.join(root, 'package-lock.json'));
  function addDependencies(parent, dependencies) {
    for (const name of Object.keys(dependencies ?? {}).sort()) {
      if (!/^(@[a-z0-9_.-]+\/)?[a-z0-9_.-]+$/i.test(name)) throw new Error(`Invalid dependency: ${name}`);
      let search = parent;
      let dependency;
      while (search === root || search.startsWith(`${root}${path.sep}`)) {
        const candidate = path.join(search, 'node_modules', name);
        if (fs.existsSync(candidate)) { dependency = candidate; break; }
        search = path.dirname(search);
      }
      if (!dependency) throw new Error(`Missing dependency ${name}. Run npm ci in extensions/vscode.`);
      // Check each ancestor too: a scoped package or node_modules may itself be a symlink.
      let ancestor = dependency;
      while (ancestor !== root) { requireDirectory(ancestor); ancestor = path.dirname(ancestor); }
      if (visited.has(dependency)) continue;
      visited.add(dependency);
      const relativeRoot = path.relative(root, dependency).split(path.sep).join('/');
      requireRegular(path.join(dependency, 'package.json'));
      const pkg = readJson(path.join(dependency, 'package.json'));
      if (packageLock.packages?.[relativeRoot]?.version !== pkg.version) {
        throw new Error(`Installed dependency differs from package-lock.json: ${relativeRoot}`);
      }
      // Nested dependencies are copied independently, not as an unchecked directory tree.
      for (const child of fs.readdirSync(dependency).sort()) {
        if (child === 'node_modules') continue;
        const file = path.join(dependency, child);
        if (fs.lstatSync(file).isDirectory()) {
          for (const [relative, bytes] of walk(file)) files.set(`${relativeRoot}/${child}/${relative}`, bytes);
        } else { requireRegular(file); files.set(`${relativeRoot}/${child}`, fs.readFileSync(file)); }
      }
      addDependencies(dependency, { ...pkg.dependencies, ...pkg.optionalDependencies });
    }
  }
  addDependencies(root, { ...manifest.dependencies, ...manifest.optionalDependencies });
  return files;
}

function hashes(files) { return Object.fromEntries([...files].sort(([left], [right]) => left.localeCompare(right, 'en')).map(([name, bytes]) => [name, digest(bytes)])); }
function equal(actual, expected) { return JSON.stringify(actual) === JSON.stringify(expected); }

function sourceFingerprint(root) {
  const inputs = new Map();
  for (const file of ['package.json', 'package-lock.json', 'tsconfig.json']) {
    requireRegular(path.join(root, file));
    inputs.set(file, fs.readFileSync(path.join(root, file)));
  }
  for (const [file, bytes] of walk(path.join(root, 'src'))) inputs.set(`src/${file}`, bytes);
  return digest(jsonBytes(hashes(inputs)));
}

function compileExtension(root) {
  const compiler = path.join(root, 'node_modules/typescript/bin/tsc');
  if (!fs.existsSync(compiler)) throw new Error('Run npm ci in extensions/vscode before prepare');
  requireRegular(compiler);
  const builds = path.join(repoRoot, 'build');
  fs.mkdirSync(builds, { recursive: true });
  requireDirectory(builds);
  const staging = fs.mkdtempSync(path.join(builds, 'autograde-extension-'));
  const compiled = path.join(staging, 'out');
  execFileSync(process.execPath, [compiler, '-p', root, '--outDir', compiled], { cwd: root, stdio: 'inherit' });
  requireDirectory(compiled);
  // A fresh compilation cannot retain JS belonging to deleted/renamed TypeScript modules.
  // Preserve previous generated output instead of deleting or overwriting it in place.
  const output = path.join(root, 'out');
  const previous = path.join(staging, 'previous-out');
  const existed = fs.existsSync(output);
  if (existed) { requireDirectory(output); fs.renameSync(output, previous); }
  try { fs.renameSync(compiled, output); }
  catch (error) { if (existed) fs.renameSync(previous, output); throw error; }
}

export function checkSource(source, upstream) {
  requireDirectory(source);
  source = fs.realpathSync(source);
  if (fs.realpathSync(git(source, 'rev-parse', '--show-toplevel')) !== source) {
    throw new Error('Source must be the Code-OSS checkout root');
  }
  if (git(source, 'rev-parse', 'HEAD') !== upstream.commit) throw new Error('Code-OSS commit does not match upstream.json');
  requireRegular(path.join(source, 'product.json'));
  requireRegular(path.join(source, 'package.json'));
  requireRegular(path.join(source, '.nvmrc'));
  requireRegular(path.join(source, 'LICENSE.txt'));
  requireDirectory(path.join(source, 'extensions'));
  if (readJson(path.join(source, 'package.json')).version !== upstream.tag
      || fs.readFileSync(path.join(source, '.nvmrc'), 'utf8').trim() !== upstream.node) {
    throw new Error('Upstream version or Node requirement differs from the pinned version');
  }
  const dirty = git(source, 'diff', '--name-only', 'HEAD').split('\n').filter(Boolean);
  const markerPath = path.join(source, markerName);
  const previousPolicy = fs.existsSync(markerPath) ? readJson(markerPath).policySources ?? {} : {};
  for (const file of Object.keys(previousPolicy)) {
    requirePolicyPath(file);
    requireRegular(path.join(source, file));
    if (digest(fs.readFileSync(path.join(source, file))) !== previousPolicy[file]) throw new Error(`Managed IDE policy was modified: ${file}; preserve edits and use a clean checkout`);
  }
  if (dirty.some(file => file !== 'product.json' && !(file in previousPolicy))) throw new Error(`Refusing modified upstream source: ${dirty.join(', ')}`);
  if (!fs.existsSync(markerPath)) {
    if (dirty.length) throw new Error('Refusing an unowned modified product.json');
    for (const name of extensionNames) {
      if (fs.existsSync(path.join(source, 'extensions', name))) throw new Error(`Refusing existing extension directory: ${name}`);
    }
  } else {
    requireRegular(markerPath);
    const marker = readJson(markerPath);
    if (marker.schema !== 1 || marker.commit !== upstream.commit) throw new Error('Invalid overlay ownership marker');
    for (const name of extensionNames) {
      if (!equal(hashes(walk(path.join(source, 'extensions', name))), marker.extensions[name])) {
        throw new Error(`Bundled ${name} was modified; preserve the edits and use a clean checkout`);
      }
    }
    if (digest(fs.readFileSync(path.join(source, 'product.json'))) !== marker.productHash) {
      throw new Error('Product configuration changed after prepare; use a clean checkout');
    }
  }
  return source;
}

function writeTree(directory, files) {
  fs.mkdirSync(directory, { recursive: true });
  for (const [name, bytes] of files) {
    const file = path.join(directory, name);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, bytes, { flag: 'wx' });
  }
}

function requirePolicyPath(file) {
  if (!/^src\/[A-Za-z0-9_./-]+\.ts$/.test(file) || file.split('/').some(part => part === '..' || part === '.' || !part)) throw new Error('Invalid managed policy source path');
}

function expectedPolicy(source, options) {
  const files = (options.policySources ?? policySources)(file => git(source, 'show', `HEAD:${file}`) + '\n');
  for (const [file, bytes] of files) {
    requirePolicyPath(file);
    files.set(file, Buffer.from(bytes));
  }
  return files;
}

export function prepare(source, options = {}) {
  const upstream = options.upstream ?? readJson(path.join(here, 'upstream.json'));
  source = checkSource(path.resolve(source), upstream);
  const policy = expectedPolicy(source, options);
  const previousPolicy = fs.existsSync(path.join(source, markerName)) ? readJson(path.join(source, markerName)).policySources ?? {} : {};
  for (const file of policy.keys()) {
    let parent = path.dirname(path.join(source, file));
    while (parent !== source) { if (fs.existsSync(parent)) requireDirectory(parent); parent = path.dirname(parent); }
    if (fs.existsSync(path.join(source, file)) && !(file in previousPolicy)) {
      requireRegular(path.join(source, file));
      const tracked = git(source, 'ls-files', '--', file);
      if (!tracked) throw new Error(`Refusing unowned IDE policy source: ${file}`);
    }
  }
  const extensionRoot = path.resolve(options.extensionRoot ?? path.join(repoRoot, 'extensions/vscode'));
  const inputHash = sourceFingerprint(extensionRoot);
  (options.compile ?? compileExtension)(extensionRoot);
  if (inputHash !== sourceFingerprint(extensionRoot)) throw new Error('Extension source changed while compiling; retry prepare');
  const extension = extensionBundle(extensionRoot);
  const profile = walk(options.profileRoot ?? path.join(here, 'profile'));
  const base = JSON.parse(git(source, 'show', 'HEAD:product.json'));
  const product = jsonBytes(productOverlay(base));
  const bundles = { autograde: extension, 'autograde-ide-profile': profile };
  const marker = {
    schema: 1, commit: upstream.commit, productHash: digest(product), extensionInputsHash: inputHash,
    policySources: hashes(policy),
    extensionVersion: JSON.parse(extension.get('package.json')).version,
    extensions: Object.fromEntries(Object.entries(bundles).map(([name, files]) => [name, hashes(files)])),
  };
  // Stage all bytes before changing the checkout. Keep previous generated bundles recoverable.
  const staging = fs.mkdtempSync(path.join(source, '.autograde-stage-'));
  for (const [name, files] of Object.entries(bundles)) writeTree(path.join(staging, name), files);
  writeTree(path.join(staging, 'policy-source'), policy);
  fs.writeFileSync(path.join(staging, 'product.json'), product);
  fs.writeFileSync(path.join(staging, markerName), jsonBytes(marker));
  const moved = [];
  const installed = [];
  try {
    for (const name of [...extensionNames.map(name => `extensions/${name}`), 'product.json', ...policy.keys(), markerName]) {
      const target = path.join(source, name);
      const staged = policy.has(name) ? path.join(staging, 'policy-source', name) : path.join(staging, path.basename(name));
      fs.mkdirSync(path.dirname(target), { recursive: true });
      if (fs.existsSync(target)) {
        const backup = policy.has(name) ? path.join(staging, 'previous-policy-source', name) : path.join(staging, `previous-${path.basename(name)}`);
        fs.mkdirSync(path.dirname(backup), { recursive: true });
        fs.renameSync(target, backup);
        moved.push([target, backup]);
      }
      fs.renameSync(staged, target);
      installed.push(target);
    }
  } catch (error) {
    for (const target of installed.reverse()) {
      const failed = path.join(staging, 'failed', path.relative(source, target));
      fs.mkdirSync(path.dirname(failed), { recursive: true });
      fs.renameSync(target, failed);
    }
    for (const [target, backup] of moved.reverse()) fs.renameSync(backup, target);
    throw error;
  }
  return { source, commit: upstream.commit, extensionVersion: marker.extensionVersion, backup: staging };
}

export function verify(source, options = {}) {
  const upstream = options.upstream ?? readJson(path.join(here, 'upstream.json'));
  source = checkSource(path.resolve(source), upstream);
  if (!fs.existsSync(path.join(source, markerName))) throw new Error('Run prepare first');
  const expectedProduct = productOverlay(JSON.parse(git(source, 'show', 'HEAD:product.json')));
  if (!equal(readJson(path.join(source, 'product.json')), expectedProduct)) throw new Error('Product overlay is out of date; run prepare');
  const extensionRoot = path.resolve(options.extensionRoot ?? path.join(repoRoot, 'extensions/vscode'));
  const expected = extensionBundle(extensionRoot);
  const profile = walk(options.profileRoot ?? path.join(here, 'profile'));
  const marker = readJson(path.join(source, markerName));
  if (!equal(marker.policySources, hashes(expectedPolicy(source, options)))) throw new Error('IDE installation policy is stale; run prepare');
  if (marker.extensionInputsHash !== sourceFingerprint(extensionRoot)) throw new Error('Extension sources changed; run prepare to compile and bundle again');
  if (!equal(marker.extensions.autograde, hashes(expected))
      || !equal(marker.extensions['autograde-ide-profile'], hashes(profile))) {
    throw new Error('Bundled extension/profile is stale; compile the extension and run prepare');
  }
  return { source, commit: upstream.commit, extensionVersion: marker.extensionVersion, files: expected.size, database: 'unchanged-sqlite', enforcement: false, managedExtensionInstall: true };
}

export function launchSpec(source, runtime, workspace, platform = process.platform) {
  const product = readJson(path.join(source, 'product.json'));
  const executable = platform === 'darwin'
    ? path.join(source, '.build/electron', `${product.nameLong}.app`, 'Contents/MacOS', product.nameShort)
    : path.join(source, '.build/electron', platform === 'win32' ? `${product.nameShort}.exe` : product.applicationName);
  return { executable, args: [source, '--user-data-dir', path.join(runtime, 'user-data'),
    '--extensions-dir', path.join(runtime, 'extensions'), '--new-window',
    '--shared-data-dir', path.join(runtime, 'shared-data'),
    '--disable-extension=GitHub.copilot', '--disable-extension=GitHub.copilot-chat',
    '--disable-extension=vscode.vscode-api-tests', ...(workspace ? [workspace] : [])] };
}

export function validateRuntimePath(runtime, version, platform = process.platform) {
  const limit = platform === 'darwin' ? 103 : platform === 'linux' ? 107 : undefined;
  // The upstream Unix socket lives under user-data; measure bytes, not JS characters.
  // Use the longest static socket suffix, even on Linux hosts with XDG_RUNTIME_DIR.
  const socket = path.join(runtime, 'user-data', `${version.slice(0, 4)}-shared.sock`);
  if (limit && Buffer.byteLength(socket, 'utf8') >= limit) {
    throw new Error(`IDE profile path is too long for a Unix socket. Use run --runtime /absolute/short/path (limit ${limit} bytes): ${runtime}`);
  }
}

export function prepareRuntime(root = repoRoot, runtimeOverride) {
  const target = path.resolve(runtimeOverride ?? path.join(root, '.cache/ide'));
  let directory = runtimeOverride ? path.parse(target).root : root;
  requireDirectory(directory);
  const components = runtimeOverride ? path.relative(directory, target).split(path.sep).filter(Boolean) : ['.cache', 'ide'];
  for (const component of components) {
    directory = path.join(directory, component);
    if (!fs.existsSync(directory)) fs.mkdirSync(directory);
    requireDirectory(directory);
  }
  for (const component of ['user-data', 'extensions', 'shared-data']) {
    const child = path.join(directory, component);
    if (!fs.existsSync(child)) fs.mkdirSync(child);
    requireDirectory(child);
  }
  // Application-scoped defaults cannot be contributed by extensions, and startup
  // defaults must exist before the first workbench. Never overwrite user settings.
  const user = path.join(directory, 'user-data/User');
  if (!fs.existsSync(user)) fs.mkdirSync(user);
  requireDirectory(user);
  const settings = path.join(user, 'settings.json');
  if (fs.existsSync(settings)) requireRegular(settings);
  else fs.writeFileSync(settings, fs.readFileSync(path.join(here, 'runtime-settings.json')), { flag: 'wx', mode: 0o600 });
  return directory;
}

function optionsFrom(args) {
  const options = {};
  for (let index = 0; index < args.length; index += 2) {
    const key = args[index];
    if (!['--source', '--workspace', '--runtime', '--learner', '--learning-policy', '--learning-lifetime'].includes(key) || !args[index + 1] || args[index + 1].startsWith('--') || key in options) {
      throw new Error('Usage: node ide/code-oss/manage.mjs prepare|verify|run [--source PATH] [--workspace PATH (run only)] [--runtime PATH (run only)] [--learner LOCAL_ALIAS (run only)] [--learning-policy standard|restricted (run only)] [--learning-lifetime shared|persistent (run only)]');
    }
    options[key] = args[index + 1];
  }
  return options;
}

export function main(args) {
  const [command, ...rest] = args;
  if (!['prepare', 'verify', 'run'].includes(command)) throw new Error('Expected prepare, verify or run');
  const options = optionsFrom(rest);
  for (const key of ['--workspace', '--runtime', '--learner', '--learning-policy', '--learning-lifetime']) {
    if (options[key] && command !== 'run') throw new Error(`${key} is only supported by run`);
  }
  validateLearningOptions(options['--learner'], options['--learning-policy']);
  const lifetime = options['--learning-lifetime'] ?? (options['--learner'] ? 'shared' : undefined);
  if (lifetime && !['shared', 'persistent'].includes(lifetime)) throw new Error('Learning lifetime must be shared or persistent');
  if (lifetime && !options['--learner']) throw new Error('Learning lifetime requires --learner');
  if (lifetime === 'shared' && options['--workspace']) throw new Error('Shared sessions always create a fresh workspace; --workspace is only available with --learning-lifetime persistent');
  if (lifetime === 'shared' && process.platform === 'win32') throw new Error('Shared disposable sessions currently require macOS or Linux/WSL2. Native Windows cleanup is not supported.');
  const source = path.resolve(options['--source'] ?? path.join(repoRoot, '.cache/code-oss'));
  if (command === 'prepare') return console.log(JSON.stringify(prepare(source), null, 2));
  const checked = verify(source);
  if (command === 'verify') return console.log(JSON.stringify(checked, null, 2));
  const runtimeBase = path.resolve(options['--runtime'] ?? path.join(repoRoot, '.cache/ide'));
  let learner = options['--learner'] ? (lifetime === 'shared'
    ? sharedSessionLayout(runtimeBase, options['--learner'], options['--learning-policy'])
    : learnerLayout(runtimeBase, options['--learner'], options['--learning-policy'], options['--workspace'])) : undefined;
  const workspace = learner?.workspace ?? (options['--workspace'] ? fs.realpathSync(path.resolve(options['--workspace'])) : undefined);
  if (workspace && !learner) requireDirectory(workspace);
  const runtime = learner?.runtime ?? runtimeBase;
  validateRuntimePath(runtime, readJson(path.join(source, 'package.json')).version);
  const spec = launchSpec(source, runtime, workspace);
  for (const required of [path.join(source, 'out/main.js'), spec.executable]) {
    if (!fs.existsSync(required)) throw new Error(`Code-OSS is not built: ${required}. Follow docs/operations/autograde-ide-poc.md`);
  }
  compilePolicyRuntime(source);
  // No prelaunch downloads and no shared VS Code profile. No OS/network controls.
  if (learner) {
    learner = lifetime === 'shared'
      ? prepareSharedSession(learner, readJson(path.join(here, 'runtime-settings.json')))
      : prepareLearner(learner, readJson(path.join(here, 'runtime-settings.json')));
    try {
      const approvedBuiltins = new Set([...git(source, 'ls-tree', '--name-only', 'HEAD:extensions').split('\n'), ...extensionNames]);
      verifyRestrictedLaunch(learner, source, approvedBuiltins);
    } catch (error) {
      if (lifetime === 'shared') cleanupSharedSession(learner);
      throw error;
    }
    console.log(JSON.stringify({ learner: { runtime, workspace, materials: learner.materials, policy: learner.policy,
      lifetime,
      identity: 'local-alias-only-not-server-authentication', isolation: 'directory-and-profile-only',
      osIsolation: false, networkRestriction: false,
      notice: lifetime === 'shared'
        ? 'Shared lab session: code, materials and IDE profile are deleted after the IDE process exits. Export or submit work before quitting. Export outside this session is retained. This is not OS isolation.'
        : 'Persistent learner mode: local files remain after exit. Do not use for a shared lab seat.',
      ...(learner.policy === 'restricted' ? { restrictionNotice: 'Launch-time IDE AI/extension restrictions only. External AI, browser and terminal traffic are not blocked.' } : {}),
      ...(learner.preservedLegacyProfiles ? { preservedLegacyProfiles: learner.preservedLegacyProfiles,
        legacyNotice: 'Old persistent learner profiles were preserved. The operator must relocate or remove them before this OS account is shared with students.' } : {}),
      ...(process.platform === 'win32' ? { permissionsNotice: 'Windows account/ACL isolation must be provisioned separately.' } : {}),
    } }, null, 2));
  } else {
    prepareRuntime(repoRoot, options['--runtime'] ? runtime : undefined);
    console.log('Developer mode: files remain after exit. Shared lab seats require --learner with shared lifetime.');
  }
  if (lifetime === 'shared') markSessionLaunching(learner);
  const child = spawn(spec.executable, spec.args, {
    cwd: source, stdio: 'inherit', shell: false, detached: lifetime === 'shared',
    env: { ...learnerEnvironment(learner), VSCODE_DEV: '1', VSCODE_CLI: '1', NODE_ENV: 'development' },
  });
  if (lifetime === 'shared') {
    const completion = superviseSharedChild(child, learner);
    try { if (child.pid) recordSessionProcess(learner, child.pid); }
    catch (error) { child.kill('SIGTERM'); console.error(error.message); process.exitCode = 1; }
    void completion.then(({ code }) => { process.exitCode = process.exitCode || code; }, error => {
      console.error(`Shared IDE cleanup failed; the next student cannot start until cleanup succeeds: ${error.message}`);
      process.exitCode = 1;
    });
  } else {
    child.on('error', error => { console.error(error.message); process.exitCode = 1; });
    child.on('exit', (code, signal) => { process.exitCode = code ?? (signal ? 1 : 0); });
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try { main(process.argv.slice(2)); }
  catch (error) { console.error(`Autograde IDE: ${error.message}`); process.exitCode = 1; }
}
