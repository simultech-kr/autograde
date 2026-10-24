// Disposable Code-OSS sessions for a shared lab seat. This is lifecycle hygiene,
// not an OS security boundary or secure erasure against the same OS account.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomBytes, createHash } from 'node:crypto';
import { restrictedSettings, validateLearningOptions } from './learning-profile.mjs';

export const SESSION_MARKER = '.autograde-session.json';
const LOCK = '.autograde-shared-lock.json';
const sessionPattern = /^e-[0-9a-f]{24}$/;
export const sharedSettings = Object.freeze({
  'files.hotExit': 'off',
  'workbench.localHistory.enabled': false,
  'window.restoreWindows': 'none',
  'window.restoreFullscreen': false,
});

function present(target) {
  try { return fs.lstatSync(target); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
}
function directory(target, privateMode = false) {
  const value = fs.lstatSync(target);
  if (!value.isDirectory() || value.isSymbolicLink()) throw new Error(`Shared session requires a real directory: ${target}`);
  if (privateMode && process.platform !== 'win32' && (value.uid !== process.getuid() || (value.mode & 0o077))) {
    throw new Error(`Shared session directory must be owned by this OS user and private (0700): ${target}`);
  }
  return value;
}
function chain(target, create = false) {
  let current = path.parse(target).root;
  directory(current);
  for (const part of path.relative(current, target).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    if (!present(current) && create) fs.mkdirSync(current, { mode: 0o700 });
    directory(current);
  }
}
function privateJson(file) {
  const info = fs.lstatSync(file);
  if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 8192 ||
      (process.platform !== 'win32' && (info.uid !== process.getuid() || (info.mode & 0o077)))) {
    throw new Error(`Shared session ownership file is invalid: ${file}`);
  }
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}
function writeJson(file, value) {
  fs.writeFileSync(file, `${JSON.stringify(value)}\n`, { flag: 'wx', mode: 0o600 });
}
export function processAlive(pid) {
  if (!Number.isSafeInteger(pid) || pid < 1) return false;
  try { process.kill(pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; return true; }
}
export function processGroupAlive(pid) {
  if (process.platform === 'win32' || !Number.isSafeInteger(pid) || pid < 1) return false;
  try { process.kill(-pid, 0); return true; }
  catch (error) { if (error.code === 'ESRCH') return false; return true; }
}
function validPid(value) { return Number.isSafeInteger(value) && value > 0; }

export function sharedSessionLayout(base, learner, policy = 'standard', id = randomBytes(12).toString('hex')) {
  validateLearningOptions(learner, policy);
  if (!learner || !/^[0-9a-f]{24}$/.test(id)) throw new Error('Shared session requires a learner alias and a fresh session identifier');
  base = path.resolve(base);
  const root = path.join(base, `e-${id}`), workspace = path.join(root, 'workspace');
  return { base, root, sessionId: id, lifetime: 'shared', policy,
    learnerHash: createHash('sha256').update(`autograde-local-learner-v1\0${learner}`).digest('hex'),
    workspaceRoot: workspace, workspace, materials: path.join(root, 'materials'), runtime: path.join(root, 'r') };
}

function readSession(base, root) {
  chain(base);
  if (path.dirname(root) !== base || !sessionPattern.test(path.basename(root))) throw new Error('Invalid shared session root');
  const info = directory(root, true), marker = privateJson(path.join(root, SESSION_MARKER));
  if (marker.schema !== 1 || marker.sessionId !== path.basename(root).slice(2) || marker.root !== root ||
      marker.rootDev !== info.dev || marker.rootIno !== info.ino || !validPid(marker.launcherPid) ||
      !['prepared', 'launching', 'running'].includes(marker.phase) ||
      !['standard', 'restricted'].includes(marker.policy) ||
      !(marker.idePid === null || validPid(marker.idePid)) ||
      (marker.phase === 'running' && marker.idePid === null)) {
    throw new Error(`Shared session ownership mismatch; no files were removed: ${root}`);
  }
  return marker;
}
function readLock(base) {
  const lock = privateJson(path.join(base, LOCK));
  const info = directory(base);
  if (lock.schema !== 1 || !/^[0-9a-f]{24}$/.test(lock.sessionId ?? '') || !validPid(lock.launcherPid) ||
      lock.baseDev !== info.dev || lock.baseIno !== info.ino) throw new Error('Shared session launch lock is invalid; refusing to start');
  return lock;
}
function removeSession(base, root, marker) {
  const current = readSession(base, root);
  if (current.sessionId !== marker.sessionId || current.rootDev !== marker.rootDev || current.rootIno !== marker.rootIno ||
      current.launcherPid !== marker.launcherPid || current.idePid !== marker.idePid || current.phase !== marker.phase) {
    throw new Error('Shared session ownership changed during cleanup');
  }
  // Keep the ownership marker until every payload directory has gone, so an
  // interrupted large cleanup can still be retried on the next launch. Node's
  // removal unlinks child symlinks rather than following them outside this root.
  for (const name of fs.readdirSync(root).filter(name => name !== SESSION_MARKER)) {
    const checked = readSession(base, root);
    if (checked.rootDev !== marker.rootDev || checked.rootIno !== marker.rootIno || checked.sessionId !== marker.sessionId) {
      throw new Error('Shared session root changed during cleanup');
    }
    fs.rmSync(path.join(root, name), { recursive: true, force: false, maxRetries: 2, retryDelay: 100 });
  }
  readSession(base, root);
  fs.unlinkSync(path.join(root, SESSION_MARKER));
  fs.rmdirSync(root);
  if (present(root)) throw new Error('Shared session cleanup did not finish');
}

/** Recover only verifiably stopped sessions, before creating any new workbench. */
export function prepareSharedSession(layout, defaults, { isAlive = processAlive, isGroupAlive = processGroupAlive } = {}) {
  chain(layout.base, true);
  const baseInfo = directory(layout.base);
  if (process.platform !== 'win32' && (baseInfo.uid !== process.getuid() || (baseInfo.mode & 0o022))) {
    throw new Error('Shared runtime base must be owned by this OS user and not writable by other users');
  }
  const lockPath = path.join(layout.base, LOCK);
  if (present(lockPath)) {
    const old = readLock(layout.base);
    if (isAlive(old.launcherPid)) throw new Error('Another shared IDE session is active. End that session before the next student starts.');
    const oldRoot = path.join(layout.base, `e-${old.sessionId}`);
    if (present(oldRoot)) {
      const oldSession = readSession(layout.base, oldRoot);
      if (oldSession.launcherPid !== old.launcherPid || isAlive(oldSession.idePid) || isGroupAlive(oldSession.idePid)) {
        throw new Error('The previous IDE is still active or its ownership changed. Close it before starting a new session.');
      }
      if (oldSession.phase === 'launching' && oldSession.idePid === null) {
        throw new Error('The previous IDE launch was interrupted before its process was recorded. Operator cleanup is required; no new student session was started.');
      }
    }
    // A one-time recovery ticket serializes competing launchers recovering the
    // same dead lease. Keep the tiny ticket (no student data) as a tombstone;
    // removing it would reopen the stale-lock unlink race. An interrupted ticket
    // fails closed if the old lease is still present.
    writeJson(path.join(layout.base, `.autograde-recovered-${old.sessionId}.json`), {
      schema: 1, sessionId: old.sessionId, recoveringPid: process.pid,
    });
    // Recheck the exact lease immediately before replacing it.
    const again = readLock(layout.base);
    if (again.sessionId !== old.sessionId || again.launcherPid !== old.launcherPid) throw new Error('Shared session launch lock changed');
    fs.unlinkSync(lockPath);
  }
  writeJson(lockPath, { schema: 1, sessionId: layout.sessionId, launcherPid: process.pid, baseDev: baseInfo.dev, baseIno: baseInfo.ino });
  // Any unrecognized owned-root-shaped path is an error, never adopted/deleted.
  const recovered = [];
  for (const name of fs.readdirSync(layout.base).filter(name => sessionPattern.test(name)).sort()) {
    const root = path.join(layout.base, name), marker = readSession(layout.base, root);
    if (isAlive(marker.launcherPid) || isAlive(marker.idePid) || isGroupAlive(marker.idePid)) throw new Error('A shared IDE process is still active. Refusing concurrent student access.');
    if (marker.phase === 'launching' && marker.idePid === null) throw new Error('Interrupted IDE launch has an unknown process; operator cleanup is required');
    removeSession(layout.base, root, marker); recovered.push(marker.sessionId);
  }
  if (present(layout.root)) throw new Error('Shared session directory already exists; refusing to reuse it');
  fs.mkdirSync(layout.root, { mode: 0o700 });
  const info = directory(layout.root, true);
  writeJson(path.join(layout.root, SESSION_MARKER), { schema: 1, sessionId: layout.sessionId, root: layout.root,
    rootDev: info.dev, rootIno: info.ino, launcherPid: process.pid, idePid: null, phase: 'prepared',
    policy: layout.policy, createdAt: new Date().toISOString() });
  for (const relative of ['workspace', 'materials', 'r', 'r/user-data', 'r/extensions', 'r/shared-data', 'r/user-data/User']) {
    fs.mkdirSync(path.join(layout.root, relative), { mode: 0o700 });
  }
  writeJson(path.join(layout.runtime, 'user-data/User/settings.json'), {
    ...defaults, ...sharedSettings, ...(layout.policy === 'restricted' ? restrictedSettings : {}),
  });
  return { ...layout, recovered,
    preservedLegacyProfiles: fs.readdirSync(layout.base).filter(name => /^l-[0-9a-f]{16}$/.test(name)).length };
}

function updateSession(layout, update) {
  const current = readSession(layout.base, layout.root);
  if (current.sessionId !== layout.sessionId || current.launcherPid !== process.pid) throw new Error('Cannot change another launcher’s session');
  const temporary = path.join(layout.root, `.session-${randomBytes(8).toString('hex')}.tmp`);
  writeJson(temporary, { ...current, ...update });
  fs.renameSync(temporary, path.join(layout.root, SESSION_MARKER));
}
export function markSessionLaunching(layout) { updateSession(layout, { phase: 'launching' }); }
export function recordSessionProcess(layout, pid) {
  if (!validPid(pid)) throw new Error('The IDE process did not start');
  updateSession(layout, { phase: 'running', idePid: pid });
}

export function cleanupSharedSession(layout, { isAlive = processAlive, isGroupAlive = processGroupAlive, spawnFailed = false } = {}) {
  const lock = readLock(layout.base);
  if (lock.sessionId !== layout.sessionId || lock.launcherPid !== process.pid) throw new Error('Cannot clean another launcher’s session');
  const marker = readSession(layout.base, layout.root);
  if (marker.launcherPid !== process.pid || isAlive(marker.idePid) || isGroupAlive(marker.idePid)) throw new Error('IDE still running or session owner changed; no cleanup performed');
  if (marker.phase === 'launching' && !spawnFailed) throw new Error('IDE launch outcome unknown; retaining files until process ownership is checked');
  removeSession(layout.base, layout.root, marker);
  fs.unlinkSync(path.join(layout.base, LOCK));
}

/** Observe the real detached Electron main process, not the short-lived CLI. */
export function superviseSharedChild(child, layout, { shutdownTimeoutMs = 3000 } = {}) {
  let spawnError;
  const signalGroup = signal => {
    try { if (child.pid) process.kill(-child.pid, signal); }
    catch (error) { if (error.code !== 'ESRCH') throw error; }
  };
  const forward = () => {
    try { signalGroup('SIGTERM'); }
    catch (error) { console.error(`Unable to stop shared IDE: ${error.message}`); }
  };
  for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.on(signal, forward);
  return new Promise((resolve, reject) => {
    child.once('error', error => { spawnError = error; });
    child.once('close', async (code, signal) => {
      try {
        // Terminals normally exit with Electron. A surviving process in its
        // private process group must stop before files can safely be removed.
        for (const stopSignal of ['SIGTERM', 'SIGKILL']) {
          if (!processGroupAlive(child.pid)) break;
          signalGroup(stopSignal);
          const until = Date.now() + shutdownTimeoutMs;
          while (processGroupAlive(child.pid) && Date.now() < until) await new Promise(done => setTimeout(done, 25));
        }
        cleanupSharedSession(layout, { spawnFailed: Boolean(spawnError && !child.pid) });
        if (spawnError) throw spawnError;
        resolve({ code: code ?? (signal ? 1 : 0), signal });
      } catch (error) { reject(error); }
      finally { for (const name of ['SIGINT', 'SIGTERM', 'SIGHUP']) process.removeListener(name, forward); }
    });
  });
}
