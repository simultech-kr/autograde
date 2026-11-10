// Read-only discovery is a shortlist, never authorization to reuse a runtime.
// The caller must lock each candidate and run validateBaseline before using it.
import fs from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';
import { buildStateFilename } from './build-resume.mjs';
import { requiredBaselineStages } from './windows-quick-baseline.mjs';

const reportFilename = 'build-windows-x64.json';
const maximumRecordBytes = 32 * 1024 * 1024;
const maximumDepth = 3, maximumDirectories = 4096;
const excludedDirectories = new Set([
  'node_modules', 'editor', 'runtime', 'electron', 'extensions', 'resources', 'licenses',
  'src', 'out', 'out-vscode', 'test', 'tests', 'code-oss', 'vscode',
]);
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const canonical = value => Array.isArray(value) ? value.map(canonical) : object(value)
  ? Object.fromEntries(Object.keys(value).sort().map(key => [key, canonical(value[key])])) : value;
const equal = (left, right) => JSON.stringify(canonical(left)) === JSON.stringify(canonical(right));
const comparePaths = (left, right) => left < right ? -1 : left > right ? 1 : 0;
const exists = file => {
  try { return fs.lstatSync(file); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
};

function directory(value, label) {
  if (typeof value !== 'string' || !value) throw new Error(`${label} must name an existing directory`);
  const absolute = path.resolve(value), info = exists(absolute);
  if (!info?.isDirectory() || info.isSymbolicLink()) throw new Error(`${label} must be a real directory without a symbolic link or junction: ${absolute}`);
  return fs.realpathSync.native(absolute);
}

function record(file) {
  const info = exists(file);
  const valid = stat => stat?.isFile() && !stat.isSymbolicLink() && stat.nlink === 1 && stat.size <= maximumRecordBytes;
  if (!valid(info)) throw new Error(`Missing or invalid build record (regular file, no links, at most 32 MiB required): ${file}`);
  const descriptor = fs.openSync(file, fs.constants.O_RDONLY | (fs.constants.O_NOFOLLOW ?? 0));
  try {
    const opened = fs.fstatSync(descriptor);
    if (!valid(opened) || opened.dev !== info.dev || opened.ino !== info.ino || opened.size !== info.size) {
      throw new Error(`Build record changed while being read: ${file}`);
    }
    // One extra byte detects growth without allowing an unbounded read.
    const bytes = Buffer.allocUnsafe(opened.size + 1);
    let length = 0, read;
    while (length < bytes.length && (read = fs.readSync(descriptor, bytes, length, bytes.length - length, null))) length += read;
    const after = fs.fstatSync(descriptor);
    if (length !== opened.size || !valid(after) || after.size !== opened.size || after.mtimeMs !== opened.mtimeMs) {
      throw new Error(`Build record changed while being read: ${file}`);
    }
    let value;
    try { value = JSON.parse(bytes.subarray(0, length).toString('utf8')); }
    catch { throw new Error(`Unreadable build record JSON: ${file}`); }
    if (!object(value)) throw new Error(`Build record must be a JSON object: ${file}`);
    return {value, completedAt: opened.mtimeMs};
  } finally { fs.closeSync(descriptor); }
}

function inspect(buildOutput, {host, pin}) {
  buildOutput = directory(buildOutput, 'Full-build output');
  const {value: state} = record(path.join(buildOutput, buildStateFilename));
  const {value: report, completedAt} = record(path.join(buildOutput, reportFilename));
  if (state.schema !== 1 || !object(state.identity) || !object(state.steps)) throw new Error('Invalid full-build checkpoint schema or identity');
  if (report.schema !== 1 || report.buildTarget !== 'windows-x64' || report.includeWsl !== false
      || !['local-build', 'release-candidate'].includes(report.status)
      || report.buildMode !== (report.status === 'local-build' ? 'local' : 'candidate')
      || typeof report.sourceDirty !== 'boolean' || !/^[a-f0-9]{40}$/.test(report.sourceRevision ?? '')
      || (report.status === 'release-candidate' && report.sourceDirty)
      || !/^\d+\.\d+\.\d+$/.test(report.extensionVersion ?? '')
      || report.acceptance?.productionReady !== false || report.acceptance?.institution !== 'not-tested'
      || report.acceptance?.windowsWsl2 !== 'not-tested') {
    throw new Error('A completed native Windows x64 full-build report is required');
  }
  const identity = state.identity;
  if (host?.platform !== 'win32' || host.architecture !== 'x64' || host.node !== pin?.node
      || typeof host.hostname !== 'string' || !host.hostname
      || identity.target !== 'windows-x64' || identity.platform !== host.platform
      || identity.architecture !== host.architecture || identity.node !== host.node
      || identity.hostname !== host.hostname
      || report.host?.platform !== host.platform || report.host?.architecture !== host.architecture
      || report.host?.node !== host.node
      // Full reports historically omit the hostname; the local checkpoint is authoritative.
      || (Object.hasOwn(report.host, 'hostname') && report.host.hostname !== host.hostname)) {
    throw new Error('Full build belongs to a different PC, Windows architecture, or Node version');
  }
  if (!equal(report.upstream, pin) || !equal(identity.upstream, pin)
      || identity.local !== (report.buildMode === 'local') || identity.includeWsl !== false) {
    throw new Error('Full-build upstream pin or build mode differs from this invocation');
  }
  if (typeof state.source !== 'string' || !path.isAbsolute(state.source)
      || typeof state.output !== 'string' || !path.isAbsolute(state.output)) {
    throw new Error('Full-build checkpoint requires absolute source and output paths');
  }
  const source = directory(state.source, 'Full-build source');
  if (state.source !== source || state.output !== buildOutput) throw new Error('Full-build checkpoint source or output does not match its canonical directory');
  const checks = report.checks;
  if (!Array.isArray(checks) || !checks.every(check => object(check) && typeof check.name === 'string' && check.result === 'passed')
      || new Set(checks.map(check => check.name)).size !== checks.length) throw new Error('Invalid or failed full-build report checks');
  for (const name of [...requiredBaselineStages, 'windows-packaged-cli-version', 'windows-native-job-acl-msvc-checks']) {
    if (!checks.some(check => check.name === name)) throw new Error(`Missing successful full-build check: ${name}`);
  }
  for (const name of requiredBaselineStages) {
    const step = state.steps[name];
    if (!object(step) || step.status !== 'passed' || !/^[a-f0-9]{64}$/.test(step.signature ?? '')
        || typeof step.token !== 'string' || !step.token || !object(step.dependencies) || !object(step.value)) {
      throw new Error(`Invalid or incomplete full-build checkpoint stage: ${name}`);
    }
  }
  return {source, buildOutput, completedAt};
}

/** Return metadata-qualified candidates, newest completed report first. No files are changed. */
export function discoverWindowsBuilds({repository, host, pin, source, buildOutput}) {
  const candidates = [], rejected = [];
  // Explicit pairs preserve the existing CLI path semantics and locked verifier.
  if (source !== undefined && buildOutput !== undefined) {
    return {candidates: [{source: path.resolve(source), buildOutput: path.resolve(buildOutput), completedAt: null}], rejected};
  }
  let selectedSource;
  if (source !== undefined) {
    try { selectedSource = directory(source, 'Requested source'); }
    catch (error) { return {candidates, rejected: [{directory: path.resolve(source), reason: error.message}]}; }
  }
  const consider = output => {
    try {
      const candidate = inspect(output, {host, pin});
      if (selectedSource && candidate.source !== selectedSource) throw new Error('Full-build checkpoint belongs to a different source directory');
      candidates.push(candidate);
    } catch (error) { rejected.push({directory: output, reason: error.message}); }
  };
  if (buildOutput !== undefined) {
    consider(path.resolve(buildOutput));
    return {candidates, rejected};
  }
  const root = directory(repository, 'Repository'), visited = new Set();
  let examined = 0, exhausted = false;
  function walk(output, depth) {
    if (exhausted) return;
    try {
      const info = exists(output);
      if (!info) return;
      if (!info.isDirectory() || info.isSymbolicLink()) return;
      const actual = fs.realpathSync.native(output);
      if (visited.has(actual)) return;
      visited.add(actual);
      if (++examined > maximumDirectories) {
        exhausted = true;
        rejected.push({directory: output, reason: `Stopped after ${maximumDirectories} directories; use --build-output to select a full build explicitly`});
        return;
      }
      if (exists(path.join(actual, reportFilename)) || exists(path.join(actual, buildStateFilename))) {
        consider(actual);
        // Completed and interrupted builds are self-contained. Do not search
        // their package payloads or preserved intermediate output for records.
        return;
      }
      if (depth === maximumDepth) return;
      const entries = fs.readdirSync(actual, {withFileTypes: true}).sort((left, right) => comparePaths(left.name, right.name));
      for (const entry of entries) {
        if (!entry.isDirectory() || entry.isSymbolicLink() || entry.name.startsWith('.')
            || excludedDirectories.has(entry.name.toLowerCase())) continue;
        walk(path.join(actual, entry.name), depth + 1);
      }
    } catch (error) { rejected.push({directory: output, reason: `Cannot inspect build directory: ${error.message}`}); }
  }
  for (const name of ['dist', 'build', '.cache']) walk(path.join(root, name), 0);
  candidates.sort((left, right) => right.completedAt - left.completedAt || comparePaths(left.buildOutput, right.buildOutput));
  return {candidates, rejected};
}

/** Choose an unused destination without reserving it or creating its parent. */
export function newQuickOutput(repository, {now = () => new Date(), random = () => randomBytes(4).toString('hex')} = {}) {
  const date = now();
  if (!(date instanceof Date) || !Number.isFinite(date.getTime())) throw new Error('A valid date is required for the quick output name');
  const pad = value => String(value).padStart(2, '0');
  const stamp = `${date.getFullYear()}${pad(date.getMonth() + 1)}${pad(date.getDate())}-${pad(date.getHours())}${pad(date.getMinutes())}${pad(date.getSeconds())}`;
  for (let attempt = 0; attempt < 128; attempt++) {
    const suffix = random();
    if (typeof suffix !== 'string' || !/^[a-f0-9]{8}$/i.test(suffix)) throw new Error('Quick output randomness must contain eight hexadecimal characters');
    const output = path.resolve(repository, 'dist', `windows-quick-${stamp}-${suffix}`);
    const lock = path.join(path.dirname(output), `.${path.basename(output)}.autograde-build.lock`);
    if (![output, `${output}.zip`, `${output}.zip.sha256`, lock].some(exists)) return output;
  }
  throw new Error('Could not choose an unused quick output after 128 attempts; specify --output with a new directory');
}
