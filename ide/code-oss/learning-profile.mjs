// Local organization and launch-time checks only: not OS isolation or server identity.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { createHash } from 'node:crypto';

const markerName = '.autograde-learner.json';
export const restrictedSettings = Object.freeze({
  'chat.disableAIFeatures': true,
  'chat.agent.enabled': false,
  'chat.copilotCliCommand.enabled': false,
  'extensions.allowed': { '*': false },
});

function statIfPresent(target) {
  try { return fs.lstatSync(target); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
}

function checkDirectory(target, { privateDirectory = false } = {}) {
  const stat = fs.lstatSync(target);
  if (!stat.isDirectory()) throw new Error(`Directory required (no symlink): ${target}`);
  if (privateDirectory && process.platform !== 'win32') {
    if (stat.uid !== process.getuid() || (stat.mode & 0o077) !== 0) {
      throw new Error(`Learner directory must belong to the current OS user with mode 0700: ${target}`);
    }
  }
}

function checkFile(target, privateFile = true) {
  const stat = fs.lstatSync(target);
  if (!stat.isFile() || stat.nlink !== 1) throw new Error(`Private regular file required (no symlink or hard link): ${target}`);
  if (process.platform !== 'win32' && (stat.uid !== process.getuid() || (privateFile && (stat.mode & 0o077) !== 0))) {
    throw new Error(`Learner profile file must belong to the current OS user with mode 0600: ${target}`);
  }
}

function directoryChain(target, { create = false } = {}) {
  let current = path.parse(target).root;
  checkDirectory(current);
  for (const part of path.relative(current, target).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    if (!statIfPresent(current)) {
      if (!create) return;
      fs.mkdirSync(current, { mode: 0o700 });
    }
    checkDirectory(current);
  }
}

function privateDirectory(target) {
  if (!statIfPresent(target)) fs.mkdirSync(target, { mode: 0o700 });
  checkDirectory(target, { privateDirectory: true });
}

function writePrivateJson(target, value) {
  fs.writeFileSync(target, `${JSON.stringify(value, null, 2)}\n`, { flag: 'wx', mode: 0o600 });
}

function readPrivateJson(target, privateFile = true) {
  checkFile(target, privateFile);
  try { return JSON.parse(fs.readFileSync(target, 'utf8')); }
  catch { throw new Error(`Managed learner configuration must be valid JSON: ${target}`); }
}

export function validateLearningOptions(learner, policy = 'standard') {
  if (!['standard', 'restricted'].includes(policy)) throw new Error('Learning policy must be standard or restricted');
  if (learner !== undefined && (typeof learner !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(learner))) {
    throw new Error('Learner must be a non-sensitive local alias of 1–64 ASCII letters, digits, dots, underscores or hyphens');
  }
  if (policy === 'restricted' && !learner) throw new Error('Restricted learning policy requires --learner; existing personal profiles are never changed');
}

export function learnerLayout(base, learner, policy = 'standard', workspace) {
  validateLearningOptions(learner, policy);
  if (!learner) throw new Error('A learner alias is required');
  const learnerHash = createHash('sha256').update(`autograde-local-learner-v1\0${learner}`).digest('hex');
  base = path.resolve(base);
  const root = path.join(base, `l-${learnerHash.slice(0, 16)}`);
  const workspaceRoot = path.join(root, 'workspace');
  const selectedWorkspace = path.resolve(workspace ?? workspaceRoot);
  const relative = path.relative(workspaceRoot, selectedWorkspace);
  if (relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative)) {
    throw new Error('Learner workspace must be inside this learner’s workspace directory; no other learner or external folder may be selected');
  }
  return { base, root, learnerHash, policy, runtime: path.join(root, policy === 'restricted' ? 'r' : 's'), materials: path.join(root, 'materials'),
    workspaceRoot, workspace: selectedWorkspace };
}

export function learnerEnvironment(layout, inherited = process.env) {
  return { ...inherited,
    // Pinned Code-OSS also reads argv.json outside --user-data-dir unless portable
    // data is explicit. Keep that application configuration within this profile.
    VSCODE_PORTABLE: layout?.runtime ?? '',
    AUTOGRADE_LEARNING_POLICY: layout?.policy ?? 'unmanaged',
    AUTOGRADE_LEARNER_WORKSPACE: layout?.workspaceRoot ?? '',
    AUTOGRADE_LEARNER_MATERIALS: layout?.materials ?? '',
    AUTOGRADE_LEARNING_LIFETIME: layout?.lifetime ?? (layout ? 'persistent' : 'unmanaged'),
    AUTOGRADE_SESSION_ROOT: layout?.lifetime === 'shared' ? layout.root : '',
    AUTOGRADE_SESSION_ID: layout?.lifetime === 'shared' ? layout.sessionId : '',
  };
}

export function prepareLearner(layout, defaults) {
  // Validate all existing ancestors and the selected path before creating anything.
  directoryChain(layout.workspace);
  directoryChain(layout.runtime);
  directoryChain(layout.materials);
  const existing = statIfPresent(layout.root);
  if (existing) {
    checkDirectory(layout.root, { privateDirectory: true });
    const marker = readPrivateJson(path.join(layout.root, markerName));
    if (marker.schema !== 1 || marker.learnerHash !== layout.learnerHash) throw new Error('Learner ownership marker mismatch; refusing profile reuse');
  }
  directoryChain(layout.base, { create: true });
  const baseStat = fs.lstatSync(layout.base);
  if (process.platform !== 'win32' && (baseStat.uid !== process.getuid() || (baseStat.mode & 0o022) !== 0)) {
    throw new Error(`Learner runtime base must be owned by the current OS user and not group/world writable: ${layout.base}`);
  }
  if (!existing) {
    // Never adopt a preexisting unmarked profile, even an empty one.
    fs.mkdirSync(layout.root, { mode: 0o700 });
    writePrivateJson(path.join(layout.root, markerName), { schema: 1, learnerHash: layout.learnerHash,
      identity: 'local-alias-only', isolation: 'directory-and-profile-only' });
  }
  privateDirectory(layout.workspaceRoot);
  privateDirectory(layout.materials);
  privateDirectory(layout.runtime);
  const argv = path.join(layout.runtime, 'argv.json');
  if (statIfPresent(argv)) checkFile(argv, false);
  for (const part of ['user-data', 'extensions', 'shared-data']) privateDirectory(path.join(layout.runtime, part));
  const user = path.join(layout.runtime, 'user-data/User');
  privateDirectory(user);
  const settings = path.join(user, 'settings.json');
  if (!statIfPresent(settings)) writePrivateJson(settings, { ...defaults, ...(layout.policy === 'restricted' ? restrictedSettings : {}) });
  // Code-OSS may atomically replace settings with a 0644 temporary file. The
  // owner-only parent directories provide privacy; do not reject normal saves.
  else checkFile(settings, false);
  // A custom child workspace must already exist. Only the owned default is created.
  checkDirectory(layout.workspace);
  return layout;
}

function assertRestrictedConfiguration(file, required, requireAll) {
  const actual = readPrivateJson(file, false);
  if (!actual || typeof actual !== 'object' || Array.isArray(actual)) throw new Error(`Managed configuration must be an object: ${file}`);
  for (const [key, value] of Object.entries(required)) {
    if ((requireAll || key in actual) && JSON.stringify(actual[key]) !== JSON.stringify(value)) {
      throw new Error(`Restricted learning configuration changed (${key}); preserve your settings and use a clean restricted profile: ${file}`);
    }
  }
}

export function verifyRestrictedLaunch(layout, source, approvedBuiltins) {
  if (layout.policy !== 'restricted') return;
  const user = path.join(layout.runtime, 'user-data/User');
  assertRestrictedConfiguration(path.join(user, 'settings.json'), restrictedSettings, true);
  // Named profiles could supply a different settings file on folder reopen.
  const profiles = path.join(user, 'profiles');
  if (statIfPresent(profiles)) {
    checkDirectory(profiles, { privateDirectory: true });
    if (fs.readdirSync(profiles).length) throw new Error('Restricted learning does not allow alternate IDE profiles');
  }
  const extensions = path.join(layout.runtime, 'extensions');
  checkDirectory(extensions, { privateDirectory: true });
  for (const name of fs.readdirSync(extensions)) {
    const target = path.join(extensions, name);
    // Code-OSS can create this index even with no user extensions installed.
    if (name === 'extensions.json') {
      const index = readPrivateJson(target, false);
      if (Array.isArray(index) && index.length === 0) continue;
    }
    if (name === '.obsolete') {
      const index = readPrivateJson(target, false);
      if (index && !Array.isArray(index) && Object.keys(index).length === 0) continue;
    }
    throw new Error(`Restricted learning refuses user-installed extension content: ${name}. Move it to a standard profile; nothing was deleted.`);
  }
  const builtinRoot = path.join(source, 'extensions');
  checkDirectory(builtinRoot);
  for (const name of fs.readdirSync(builtinRoot)) {
    const target = path.join(builtinRoot, name);
    const stat = fs.lstatSync(target);
    if (stat.isSymbolicLink()) throw new Error(`Restricted learning refuses symlinked built-ins: ${name}`);
    const buildSupport = ['node_modules', 'out'].includes(name) && !statIfPresent(path.join(target, 'package.json'));
    if (stat.isDirectory() && !approvedBuiltins.has(name) && !buildSupport) throw new Error(`Restricted learning refuses unapproved built-in extension: ${name}`);
  }
  // WINDOW-scoped chat settings may otherwise be overridden by the opened folder.
  const folderSettings = path.join(layout.workspace, '.vscode/settings.json');
  const vscode = path.dirname(folderSettings);
  if (statIfPresent(vscode)) checkDirectory(vscode);
  if (statIfPresent(folderSettings)) assertRestrictedConfiguration(folderSettings, restrictedSettings, false);
}
