// Verify a completed, same-host Windows build before reusing its editor runtime.
// The caller must hold the ordinary source and build-output locks throughout use.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { verifyCoreOverlay } from '../../ide/code-oss/manage.mjs';
import { buildStateFilename } from './build-resume.mjs';
import { assertReusableSource, dependencyPaths, inputFingerprint, runtimeCoreConfigurationPaths, runtimeCoreFingerprint, snapshotMatches, snapshotPaths } from './build-inputs.mjs';
import { builtExtensionPaths, validateArtifact } from './build-runtime.mjs';

export const quickRuntimeFilename = '.autograde-quick-runtime.json';
export const coreConfigurationPaths = runtimeCoreConfigurationPaths;
export const requiredBaselineStages = Object.freeze([
  'extension-dependencies', 'extension-compile', 'extension-platform-tests', 'launcher-and-policy-tests',
  'upstream-commit-and-node', 'managed-node-build-compatibility', 'windows-electron-header-preinstall',
  'native-code-oss-dependencies', 'teacher-only-policy-overlay', 'generate-api-proposal-names',
  'generate-extension-point-names', 'code-oss-source-typecheck', 'code-oss-client-compile',
  'code-oss-extension-compile', 'code-oss-extension-media', 'code-oss-monaco-typecheck',
  'native-electron-download', 'overlay-verification', 'package-windows',
]);
const snapshotStages = ['native-code-oss-dependencies', 'code-oss-client-compile',
  'code-oss-extension-compile', 'code-oss-extension-media', 'native-electron-download'];
const stageDependencies = {
  'extension-dependencies': [], 'extension-compile': ['extension-dependencies'],
  'extension-platform-tests': ['extension-compile'], 'launcher-and-policy-tests': [],
  'upstream-commit-and-node': [], 'managed-node-build-compatibility': ['upstream-commit-and-node'],
  'windows-electron-header-preinstall': ['managed-node-build-compatibility'],
  'native-code-oss-dependencies': ['managed-node-build-compatibility', 'windows-electron-header-preinstall'],
  'teacher-only-policy-overlay': ['extension-platform-tests', 'native-code-oss-dependencies'],
  'generate-api-proposal-names': ['teacher-only-policy-overlay'],
  'generate-extension-point-names': ['generate-api-proposal-names'],
  'code-oss-source-typecheck': ['generate-extension-point-names'],
  'code-oss-client-compile': ['code-oss-source-typecheck'],
  'code-oss-extension-compile': ['code-oss-client-compile'],
  'code-oss-extension-media': ['code-oss-extension-compile'],
  'code-oss-monaco-typecheck': ['code-oss-extension-media'],
  'native-electron-download': ['native-code-oss-dependencies'],
  'overlay-verification': ['code-oss-monaco-typecheck', 'native-electron-download'],
  'package-windows': ['overlay-verification', 'launcher-and-policy-tests'],
};
const ownedExtensions = ['extensions/autograde', 'extensions/autograde-ide-profile'];
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const canonical = value => Array.isArray(value) ? value.map(canonical) : object(value)
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
const equal = (left, right) => JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
const sorted = values => [...new Set(values)].sort();
const owned = relative => ownedExtensions.some(root => relative === root || relative.startsWith(`${root}/`));
const fail = message => { throw new Error(`${message}; run the full Windows build with --resume to establish a verified baseline`); };
const exists = file => { try { return fs.lstatSync(file); } catch (error) { if (error.code === 'ENOENT') return undefined; throw error; } };

function regular(file, maximum = 32 * 1024 * 1024) {
  let stat;
  try { stat = fs.lstatSync(file); } catch { fail(`Cannot read required baseline file: ${file}`); }
  if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > maximum) fail(`Invalid baseline file: ${file}`);
  return stat;
}
function jsonFile(file) {
  regular(file);
  const bytes = fs.readFileSync(file);
  let value;
  try { value = JSON.parse(bytes.toString('utf8')); } catch { fail(`Unreadable baseline JSON: ${file}`); }
  if (!object(value)) fail(`Invalid baseline JSON object: ${file}`);
  return { value, sha256: hash(bytes) };
}
function directory(value) {
  const absolute = path.resolve(value), stat = fs.lstatSync(absolute);
  if (!stat.isDirectory() || stat.isSymbolicLink()) fail(`A real build directory is required: ${absolute}`);
  return fs.realpathSync.native(absolute);
}
function inside(parent, child) {
  const relative = path.relative(parent, child);
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}
function git(repository, args) {
  return execFileSync('git', ['-C', repository, ...args], {encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'], maxBuffer: 32 * 1024 * 1024});
}

function verifyConfiguration(repository, report) {
  if (Object.hasOwn(report.buildInputs ?? {}, 'runtimeCore')) {
    if (!/^[a-f0-9]{64}$/.test(report.buildInputs.runtimeCore ?? '')
        || runtimeCoreFingerprint(repository) !== report.buildInputs.runtimeCore) fail('Editor core configuration changed since the full build');
    return;
  }
  if (report.sourceDirty) {
    // A dirty baseline has no committed per-file evidence. Do not guess which
    // uncommitted changes were present during the authoritative editor compile.
    if (inputFingerprint(repository, ['ide/code-oss']) !== report.buildInputs?.ide
        || inputFingerprint(repository, ['scripts/ide']) !== report.buildInputs?.pipeline) {
      fail('Dirty baseline IDE or pipeline inputs changed; their original core configuration cannot be proven');
    }
    return;
  }
  try { git(repository, ['cat-file', '-e', `${report.sourceRevision}^{commit}`]); }
  catch { fail('The baseline Autograde commit is unavailable'); }
  if (git(repository, ['diff', '--no-ext-diff', '--name-only', report.sourceRevision, '--', ...coreConfigurationPaths]).trim()
      || git(repository, ['ls-files', '--others', '--exclude-standard', '--', ...coreConfigurationPaths]).trim()) {
    fail('Editor core configuration changed since the full build');
  }
}

function validateRecords(report, state, source, buildOutput, pin, host) {
  if (report.schema !== 1 || report.buildTarget !== 'windows-x64' || report.includeWsl !== false
      || !['local-build', 'release-candidate'].includes(report.status)
      || report.buildMode !== (report.status === 'local-build' ? 'local' : 'candidate')
      || typeof report.sourceDirty !== 'boolean' || !/^[a-f0-9]{40}$/.test(report.sourceRevision ?? '')
      || (report.status === 'release-candidate' && report.sourceDirty)
      || !/^\d+\.\d+\.\d+$/.test(report.extensionVersion ?? '')
      || !equal(report.upstream, pin) || report.host?.platform !== 'win32'
      || report.host?.architecture !== 'x64' || report.host?.node !== pin.node
      || report.acceptance?.productionReady !== false || report.acceptance?.institution !== 'not-tested'
      || report.acceptance?.windowsWsl2 !== 'not-tested') fail('A completed native Windows full-build report is required');
  const identity = state.identity;
  if (state.schema !== 1 || state.source !== source || state.output !== buildOutput || !object(state.steps)
      || !object(identity) || identity.target !== 'windows-x64' || identity.hostname !== host.hostname
      || identity.platform !== host.platform || identity.architecture !== host.architecture || identity.node !== host.node
      || host.platform !== 'win32' || host.architecture !== 'x64' || host.node !== pin.node
      || identity.local !== (report.buildMode === 'local') || identity.includeWsl !== false || !equal(identity.upstream, pin)) {
    fail('Baseline checkpoint paths, host, target, Node or upstream identity differ');
  }
  const checks = report.checks;
  if (!Array.isArray(checks) || !checks.every(check => object(check) && typeof check.name === 'string' && check.result === 'passed')
      || new Set(checks.map(check => check.name)).size !== checks.length) fail('Invalid full-build check results');
  for (const name of [...requiredBaselineStages, 'windows-packaged-cli-version', 'windows-native-job-acl-msvc-checks']) {
    if (!checks.some(check => check.name === name)) fail(`Missing successful full-build check: ${name}`);
  }
  const verified = new Set(), visiting = new Set();
  function stage(name) {
    if (verified.has(name)) return;
    const step = state.steps[name];
    if (!object(step) || step.status !== 'passed' || !/^[a-f0-9]{64}$/.test(step.signature ?? '')
        || typeof step.token !== 'string' || !step.token || !object(step.dependencies) || !object(step.value)
        || visiting.has(name)) fail(`Invalid or incomplete baseline checkpoint stage: ${name}`);
    visiting.add(name);
    for (const [dependency, token] of Object.entries(step.dependencies)) {
      if (!/^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(dependency) || typeof token !== 'string'
          || token !== state.steps[dependency]?.token) fail(`Stale baseline dependency: ${name}/${dependency}`);
      stage(dependency);
    }
    visiting.delete(name); verified.add(name);
  }
  for (const name of requiredBaselineStages) {
    stage(name);
    if (!equal(sorted(Object.keys(state.steps[name].dependencies)), sorted(stageDependencies[name]))) fail(`Missing baseline dependency edges: ${name}`);
  }
  if (!Array.isArray(report.artifacts) || report.artifacts.length !== 1 || report.artifacts[0]?.target !== 'windows-native'
      || !report.artifacts[0].file?.endsWith('.zip')
      || !equal(state.steps['package-windows'].value.artifact, report.artifacts[0])
      || !validateArtifact(buildOutput, report.artifacts[0])) fail('The original full-build ZIP or checksum does not match its checkpoint');
}

function mediaPaths(source) {
  const paths = [];
  for (const entry of fs.readdirSync(path.join(source, 'extensions'), {withFileTypes: true})) {
    if (!entry.isDirectory()) continue;
    for (const child of fs.readdirSync(path.join(source, 'extensions', entry.name), {withFileTypes: true})) {
      if (child.isDirectory() && (child.name === 'media' || child.name.endsWith('-out'))) paths.push(`extensions/${entry.name}/${child.name}`);
    }
  }
  return sorted(paths);
}
function policyOutputs(marker) {
  if (!object(marker.policySources) || !Object.keys(marker.policySources).length) fail('Missing managed editor policy');
  return sorted(Object.keys(marker.policySources).map(file => file.replace(/^src\//, 'out/').replace(/\.ts$/, '.js')));
}
function snapshotShape(snapshot, paths, exclude = []) {
  return object(snapshot) && /^[a-f0-9]{64}$/.test(snapshot.digest ?? '')
    && equal(snapshot.paths, sorted(paths)) && equal(snapshot.exclude, sorted(exclude));
}
function initialSnapshots(source, state, marker) {
  const expected = {
    'native-code-oss-dependencies': [...dependencyPaths(source), '.build/typings/electron.d.ts'],
    'code-oss-client-compile': ['out'],
    'code-oss-extension-compile': builtExtensionPaths(source),
    'code-oss-extension-media': mediaPaths(source),
    'native-electron-download': ['.build/electron'],
  };
  for (const name of snapshotStages) {
    const snapshot = state.steps[name].value.snapshot;
    if (!snapshotShape(snapshot, expected[name], name === 'code-oss-client-compile' ? policyOutputs(marker) : [])
        || !snapshotMatches(source, snapshot)) fail(`Full-build runtime output changed or is missing: ${name}`);
  }
}
function coreShape(source, marker) {
  const candidates = sorted(['out', 'extensions', 'resources', 'licenses', 'package.json', 'product.json',
    '.build/electron', '.build/typings/electron.d.ts', ...dependencyPaths(source)]);
  // Recursive snapshots already include every descendant. In particular, do
  // not hash extension dependencies again after walking the extensions tree.
  const paths = candidates.filter(relative => !candidates.some(parent => relative.startsWith(`${parent}/`)));
  return {paths,
  exclude: sorted([...ownedExtensions, ...policyOutputs(marker)])};
}
function coreMarker(marker) { return {commit: marker.commit, productHash: marker.productHash, policySources: marker.policySources}; }

/** Validate before prepare, then call verifyCore again after preparing/packaging. */
export function validateBaseline({repository, source, buildOutput, pin,
  host = {hostname: os.hostname(), platform: process.platform, architecture: process.arch, node: process.versions.node}}) {
  repository = directory(repository); source = directory(source); buildOutput = directory(buildOutput);
  if (inside(source, buildOutput) || inside(buildOutput, source)) fail('Baseline source and output must be separate');
  const reportPath = path.join(buildOutput, 'build-windows-x64.json'), checkpointPath = path.join(buildOutput, buildStateFilename);
  const reportFile = jsonFile(reportPath), stateFile = jsonFile(checkpointPath);
  const report = reportFile.value, state = stateFile.value;
  validateRecords(report, state, source, buildOutput, pin, host);
  verifyConfiguration(repository, report);
  assertReusableSource(source, pin);
  const checked = verifyCoreOverlay(source, {upstream: pin});
  const baseline = {reportSha256: reportFile.sha256, checkpointSha256: stateFile.sha256,
    sourceRevision: report.sourceRevision, sourceDirty: report.sourceDirty, buildMode: report.buildMode,
    upstream: pin, source, buildOutput, artifact: report.artifacts[0]};
  const cachePath = path.join(buildOutput, quickRuntimeFilename), shape = coreShape(source, checked.marker);
  let proof;
  if (exists(cachePath)) {
    proof = jsonFile(cachePath).value;
    if (proof.schema !== 1 || !equal(proof.baseline, baseline) || !equal(proof.coreMarker, coreMarker(checked.marker))
        || !snapshotShape(proof.snapshot, shape.paths, shape.exclude)) fail('Quick runtime proof does not match the original baseline');
  } else {
    initialSnapshots(source, state, checked.marker);
    proof = {schema: 1, baseline, coreMarker: coreMarker(checked.marker),
      snapshot: snapshotPaths(source, shape.paths, {exclude: shape.exclude})};
    const temporary = `${cachePath}.${randomUUID()}.tmp`;
    try {
      fs.writeFileSync(temporary, `${JSON.stringify(proof, null, 2)}\n`, {flag: 'wx', mode: 0o600});
      if (exists(cachePath)) fail('Quick runtime proof appeared while validating the baseline');
      fs.renameSync(temporary, cachePath);
    } finally { if (exists(temporary)) fs.unlinkSync(temporary); }
  }
  const verifyCore = () => {
    if (jsonFile(reportPath).sha256 !== baseline.reportSha256 || jsonFile(checkpointPath).sha256 !== baseline.checkpointSha256) {
      fail('Full-build evidence changed during quick packaging');
    }
    verifyConfiguration(repository, report);
    assertReusableSource(source, pin);
    const current = verifyCoreOverlay(source, {upstream: pin});
    if (!equal(coreMarker(current.marker), proof.coreMarker)
        || !equal(coreShape(source, current.marker), shape) || !snapshotMatches(source, proof.snapshot)) {
      fail('Verified Code-OSS runtime changed during quick packaging');
    }
  };
  verifyCore();
  return {baseline, verifyCore};
}
