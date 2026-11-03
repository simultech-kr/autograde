// Managed build-host compatibility only. Electron and remote native-module
// targets remain the exact upstream targets from their respective .npmrc files.
import fs from 'node:fs';
import path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const nodeCompatibilityPath = 'build/npm/preinstall.ts';
export const nodeCompatibilityMarker = '.autograde-node-compatibility.json';
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const git = (source, ...args) => execFileSync('git', ['-C', source, ...args], { encoding: 'utf8' });
const regular = file => { if (!fs.lstatSync(file).isFile()) throw new Error(`Regular file required (no symlink): ${file}`); };
const exists = file => { try { fs.lstatSync(file); return true; } catch (error) { if (error.code === 'ENOENT') return false; throw error; } };
const enabled = pin => pin.sourceNode !== undefined && pin.sourceNode !== pin.node;

export function nodeCompatibilitySource(original, pin) {
  if (!enabled(pin)) return original;
  if (pin.node !== '26.13.0' || pin.sourceNode !== '24.18.0') throw new Error('Unsupported managed Node compatibility pair');
  const before = '\tif (majorNodeVersion !== requiredMajor ||\n\t\tminorNodeVersion < requiredMinor ||\n\t\t(minorNodeVersion === requiredMinor && patchNodeVersion < requiredPatch)) {';
  const first = original.indexOf(before);
  if (first < 0 || original.indexOf(before, first + before.length) !== -1) throw new Error('Pinned upstream Node version guard changed');
  const after = `\t// Autograde: also permit the exact independently verified build-host Node.\n\tif (process.versions.node !== '${pin.node}' && (majorNodeVersion !== requiredMajor ||\n\t\tminorNodeVersion < requiredMinor ||\n\t\t(minorNodeVersion === requiredMinor && patchNodeVersion < requiredPatch))) {`;
  return original.slice(0, first) + after + original.slice(first + before.length);
}

function state(source, pin) {
  source = path.resolve(source);
  if (!fs.lstatSync(source).isDirectory() || fs.realpathSync(git(source, 'rev-parse', '--show-toplevel').trim()) !== fs.realpathSync(source)) throw new Error('Source must be the Code-OSS checkout root');
  if (git(source, 'rev-parse', 'HEAD').trim() !== pin.commit) throw new Error('Code-OSS commit does not match upstream.json');
  for (const folder of ['build', 'build/npm']) if (!fs.lstatSync(path.join(source, folder)).isDirectory()) throw new Error('Node compatibility source directories must not be symlinks');
  for (const file of ['.nvmrc', nodeCompatibilityPath]) regular(path.join(source, file));
  const sourceNode = pin.sourceNode ?? pin.node;
  const originalNvmrc = git(source, 'show', 'HEAD:.nvmrc');
  // --cached works with Git 2.34 on Ubuntu 22.04. Check the tracked root
  // attributes against HEAD first; .nvmrc cannot inherit nested attributes.
  if (git(source, 'diff', '--cached', '--name-only', 'HEAD', '--', '.gitattributes').trim()) throw new Error('Upstream source attributes differ from the pinned version');
  const attributes = git(source, 'check-attr', '--cached', '-z', 'text', 'eol', '--', '.nvmrc').split('\0');
  const textAttribute = attributes[2], eolAttribute = attributes[5];
  const normalizeNvmrc = textAttribute !== 'unset' && (['set', 'auto'].includes(textAttribute) || ['lf', 'crlf'].includes(eolAttribute));
  const actualNvmrc = fs.readFileSync(path.join(source, '.nvmrc'), 'utf8');
  if (originalNvmrc.trim() !== sourceNode || (normalizeNvmrc ? actualNvmrc.replace(/\r\n/g, '\n') : actualNvmrc) !== originalNvmrc) throw new Error('Upstream source Node pin differs from the build definition');
  const original = git(source, 'show', `HEAD:${nodeCompatibilityPath}`);
  const content = nodeCompatibilitySource(original, pin);
  const marker = { schema: 1, commit: pin.commit, node: pin.node, sourceNode, originalHash: hash(original), patchedHash: hash(content) };
  return { file: path.join(source, nodeCompatibilityPath), markerFile: path.join(source, nodeCompatibilityMarker), original, content, marker };
}

export function verifyNodeCompatibility(source, pin, { required = false } = {}) {
  if (!enabled(pin)) {
    if (exists(path.join(source, nodeCompatibilityMarker))) throw new Error('Managed Node compatibility marker conflicts with the build definition');
    return false;
  }
  const expected = state(source, pin);
  if (!exists(expected.markerFile)) {
    if (fs.readFileSync(expected.file, 'utf8') !== expected.original) throw new Error('Refusing unowned modified Node preinstall source');
    if (required) throw new Error('Managed Node compatibility is missing; run prepare');
    return false;
  }
  regular(expected.markerFile);
  const marker = JSON.parse(fs.readFileSync(expected.markerFile, 'utf8'));
  if (JSON.stringify(marker) !== JSON.stringify(expected.marker)) throw new Error('Invalid managed Node compatibility marker');
  // Compare the full regenerated patch too, never merely trust a stored hash.
  if (fs.readFileSync(expected.file, 'utf8') !== expected.content) throw new Error('Managed Node preinstall was modified; preserve edits and use a clean checkout');
  return true;
}

export function prepareNodeCompatibility(source, pin) {
  if (verifyNodeCompatibility(source, pin)) return true;
  if (!enabled(pin)) return false;
  const expected = state(source, pin);
  const staging = fs.mkdtempSync(path.join(source, '.autograde-node-stage-'));
  const stagedSource = path.join(staging, 'preinstall.ts');
  const stagedMarker = path.join(staging, nodeCompatibilityMarker);
  const backup = path.join(staging, 'previous-preinstall.ts');
  fs.writeFileSync(stagedSource, expected.content, { flag: 'wx', mode: fs.statSync(expected.file).mode });
  fs.writeFileSync(stagedMarker, `${JSON.stringify(expected.marker)}\n`, { flag: 'wx' });
  fs.renameSync(expected.file, backup);
  try {
    fs.renameSync(stagedSource, expected.file);
    fs.renameSync(stagedMarker, expected.markerFile);
  } catch (error) {
    if (fs.existsSync(expected.file)) fs.renameSync(expected.file, path.join(staging, 'failed-preinstall.ts'));
    fs.renameSync(backup, expected.file);
    throw error;
  }
  return verifyNodeCompatibility(source, pin, { required: true });
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    const [command, flag, source, ...extra] = process.argv.slice(2);
    if (!['prepare', 'verify'].includes(command) || flag !== '--source' || !source || extra.length) throw new Error('Usage: node-compatibility.mjs prepare|verify --source PATH');
    const pin = JSON.parse(fs.readFileSync(new URL('./upstream.json', import.meta.url), 'utf8'));
    if (command === 'prepare') prepareNodeCompatibility(path.resolve(source), pin);
    else verifyNodeCompatibility(path.resolve(source), pin, { required: true });
    console.log(JSON.stringify({ node: pin.node, sourceNode: pin.sourceNode ?? pin.node, managedNodeCompatibility: true }));
  } catch (error) { console.error(error.message); process.exitCode = 1; }
}
