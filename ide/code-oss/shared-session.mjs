// Disposable Code-OSS sessions for a shared lab seat. This is lifecycle hygiene,
// not an OS security boundary or secure erasure against the same OS account.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { randomBytes, createHash } from 'node:crypto';
import { restrictedSettings, validateLearningOptions } from './learning-profile.mjs';

export const SESSION_MARKER = '.autograde-session.json';
const LOCK = '.autograde-shared-lock.json';
const sessionPattern = /^e-[0-9a-f]{24}$/;
export const CURRENT_SESSION_DIRECTORY = 'current';
const cleanupPattern = /^\.autograde-cleanup-([0-9a-f]{24})\.json$/;
const ownedName = name => name === CURRENT_SESSION_DIRECTORY || sessionPattern.test(name);
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
  const root = path.join(base, CURRENT_SESSION_DIRECTORY), workspace = path.join(root, 'workspace');
  return { base, root, sessionId: id, lifetime: 'shared', policy,
    learnerHash: createHash('sha256').update(`autograde-local-learner-v1\0${learner}`).digest('hex'),
    workspaceRoot: workspace, workspace, materials: path.join(root, 'materials'), runtime: path.join(root, 'r') };
}

function readSession(base, root) {
  chain(base);
  if (path.dirname(root) !== base || !ownedName(path.basename(root))) throw new Error('Invalid shared session root');
  const info = directory(root, true), marker = privateJson(path.join(root, SESSION_MARKER));
  if (marker.schema !== 1 || !/^[0-9a-f]{24}$/.test(marker.sessionId ?? '') ||
      (path.basename(root) !== CURRENT_SESSION_DIRECTORY && marker.sessionId !== path.basename(root).slice(2)) || marker.root !== root ||
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
      (lock.rootName !== undefined && lock.rootName !== CURRENT_SESSION_DIRECTORY) ||
      (lock.phase !== undefined && !['recovering', 'active'].includes(lock.phase)) ||
      lock.baseDev !== info.dev || lock.baseIno !== info.ino) throw new Error('Shared session launch lock is invalid; refusing to start');
  return lock;
}
const lockRoot = (base, lock) => path.join(base, lock.rootName ?? `e-${lock.sessionId}`);
function cleanupTicket(base, marker) { return path.join(base, `.autograde-cleanup-${marker.sessionId}.json`); }

// Claims are never replaced or unlinked: a late contender must not remove a
// newer owner's lease. If a claimant dies, contenders exclusively create its
// successor instead; only the winner can reach the old-lease recheck.
function claimRecovery(base, sessionId, isAlive) {
  let file = path.join(base, `.autograde-recovered-${sessionId}.json`);
  for (let depth = 0; depth < 64; depth++) {
    try {
      writeJson(file, {schema: 2, sessionId, recoveringPid: process.pid, claimId: randomBytes(12).toString('hex')});
      return;
    } catch (error) { if (error.code !== 'EEXIST') throw error; }
    const claim = privateJson(file);
    if (![1, 2].includes(claim.schema) || claim.sessionId !== sessionId || !validPid(claim.recoveringPid) ||
        (claim.schema === 2 && !/^[0-9a-f]{24}$/.test(claim.claimId ?? ''))) {
      throw new Error('Invalid session recovery claim; operator recovery is required');
    }
    if (isAlive(claim.recoveringPid)) throw new Error('Another launcher is recovering this session; wait for it to finish');
    file = path.join(base, `.autograde-recovery-${sessionId}-${claim.claimId ?? sessionId}.json`);
  }
  throw new Error('Too many interrupted recovery attempts; operator recovery is required');
}

// A parent receipt survives interruption between unlink(marker) and rmdir(root).
// It authorizes removing only that same EMPTY inode, never adopting old files.
function recoverFinishedRemovals(base, { isAlive, isGroupAlive }, onlyId) {
  const recovered = [];
  for (const name of fs.readdirSync(base).filter(name => cleanupPattern.test(name))) {
    const id = cleanupPattern.exec(name)[1];
    if (onlyId && id !== onlyId) continue;
    const file = path.join(base, name), ticket = privateJson(file), marker = ticket.marker;
    if (ticket.schema !== 1 || !marker || marker.sessionId !== id || !ownedName(ticket.rootName) ||
        marker.root !== path.join(base, ticket.rootName) ||
        (ticket.rootName !== CURRENT_SESSION_DIRECTORY && ticket.rootName !== `e-${id}`) ||
        !validPid(marker.launcherPid) || !(marker.idePid === null || validPid(marker.idePid)) ||
        !Number.isSafeInteger(marker.rootDev) || !Number.isSafeInteger(marker.rootIno) ||
        !['prepared', 'launching', 'running'].includes(marker.phase)) throw new Error('Invalid cleanup receipt; operator recovery is required');
    if (isAlive(marker.launcherPid) || isAlive(marker.idePid) || isGroupAlive(marker.idePid)) throw new Error('A previous shared process is still active; cleanup is blocked');
    const root = marker.root;
    if (present(root)) {
      const info = directory(root, true);
      if (info.dev !== marker.rootDev || info.ino !== marker.rootIno) throw new Error('Cleanup root identity changed; no files were removed');
      if (present(path.join(root, SESSION_MARKER))) continue;
      if (fs.readdirSync(root).length) throw new Error('Unmarked cleanup root is not empty; no files were removed');
      fs.rmdirSync(root);
    }
    fs.unlinkSync(file); recovered.push(id);
  }
  return recovered;
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
  const ticket = cleanupTicket(base, marker), receipt = { schema: 1, rootName: path.basename(root), marker };
  if (present(ticket)) {
    if (JSON.stringify(privateJson(ticket)) !== JSON.stringify(receipt)) throw new Error('Cleanup receipt ownership changed');
  } else writeJson(ticket, receipt);
  fs.unlinkSync(path.join(root, SESSION_MARKER));
  fs.rmdirSync(root);
  if (present(root)) throw new Error('Shared session cleanup did not finish');
  fs.unlinkSync(ticket);
}

/** Recover only verifiably stopped sessions, before creating any new workbench. */
export function prepareSharedSession(layout, defaults, { isAlive = processAlive, isGroupAlive = processGroupAlive } = {}) {
  if (layout.root !== path.join(layout.base, CURRENT_SESSION_DIRECTORY)) throw new Error('Shared workspace root must be the owned fixed current directory');
  chain(layout.base, true);
  const baseInfo = directory(layout.base);
  if (process.platform !== 'win32' && (baseInfo.uid !== process.getuid() || (baseInfo.mode & 0o022))) {
    throw new Error('Shared runtime base must be owned by this OS user and not writable by other users');
  }
  const lockPath = path.join(layout.base, LOCK);
  const recovered = [];
  if (present(lockPath)) {
    const old = readLock(layout.base);
    if (isAlive(old.launcherPid)) throw new Error('Another shared IDE session is active. End that session before the next student starts.');
    recovered.push(...recoverFinishedRemovals(layout.base, {isAlive, isGroupAlive}));
    const oldRoot = lockRoot(layout.base, old);
    if (present(oldRoot)) {
      const oldSession = readSession(layout.base, oldRoot);
      if ((old.phase !== 'recovering' && (oldSession.launcherPid !== old.launcherPid || oldSession.sessionId !== old.sessionId))
          || isAlive(oldSession.launcherPid) || isAlive(oldSession.idePid) || isGroupAlive(oldSession.idePid)) {
        throw new Error('The previous IDE is still active or its ownership changed. Close it before starting a new session.');
      }
      if (oldSession.phase === 'launching' && oldSession.idePid === null) {
        throw new Error('The previous IDE launch was interrupted before its process was recorded. Operator cleanup is required; no new student session was started.');
      }
    }
    claimRecovery(layout.base, old.sessionId, isAlive);
    // Recheck the exact lease immediately before replacing it.
    const again = readLock(layout.base);
    if (again.sessionId !== old.sessionId || again.launcherPid !== old.launcherPid) throw new Error('Shared session launch lock changed');
    fs.unlinkSync(lockPath);
  }
  writeJson(lockPath, { schema: 1, sessionId: layout.sessionId, rootName: CURRENT_SESSION_DIRECTORY, phase: 'recovering',
    launcherPid: process.pid, baseDev: baseInfo.dev, baseIno: baseInfo.ino });
  // Any unrecognized owned-root-shaped path is an error, never adopted/deleted.
  recovered.push(...recoverFinishedRemovals(layout.base, {isAlive, isGroupAlive}));
  for (const name of fs.readdirSync(layout.base).filter(ownedName).sort()) {
    const root = path.join(layout.base, name), marker = readSession(layout.base, root);
    if (isAlive(marker.launcherPid) || isAlive(marker.idePid) || isGroupAlive(marker.idePid)) throw new Error('A shared IDE process is still active. Refusing concurrent student access.');
    if (marker.phase === 'launching' && marker.idePid === null) throw new Error('Interrupted IDE launch has an unknown process; operator cleanup is required');
    removeSession(layout.base, root, marker); recovered.push(marker.sessionId);
  }
  if (present(layout.root)) throw new Error('Shared workspace cleanup did not finish');
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
  const readyLock = readLock(layout.base);
  if (readyLock.sessionId !== layout.sessionId || readyLock.launcherPid !== process.pid) throw new Error('Shared session lease changed');
  const readyPath = path.join(layout.base, `.autograde-ready-${layout.sessionId}.json`);
  writeJson(readyPath, {...readyLock, phase: 'active'}); fs.renameSync(readyPath, lockPath);
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

/** Observe the real detached Electron process and bound requested shutdown.
 * A close event is not a prerequisite for escalation: Electron can ignore TERM.
 * An AbortSignal requests the same shutdown used by launcher signal forwarding.
 */
export function superviseSharedChild(child, layout, { shutdownTimeoutMs = 3000, shutdownSignal } = {}) {
  if (!Number.isFinite(shutdownTimeoutMs) || shutdownTimeoutMs < 1 || shutdownTimeoutMs > 60_000) {
    throw new Error('Shared shutdown timeout must be between 1 and 60000 milliseconds');
  }
  let spawnError, closeResult, stopping;
  const signals = ['SIGINT', 'SIGTERM', 'SIGHUP'];
  const stopped = () => !processAlive(child.pid) && !processGroupAlive(child.pid);
  const stopGroup = async () => {
    if (!validPid(child.pid)) {
      if (spawnError) return; // Known failed spawn; never guess an unknown outcome.
      throw new Error('IDE process ownership is unknown; session cleanup is blocked');
    }
    for (const signal of ['SIGTERM', 'SIGKILL']) {
      if (stopped()) return;
      try { process.kill(-child.pid, signal); }
      catch (error) { if (error.code !== 'ESRCH') throw error; }
      const deadline = Date.now() + shutdownTimeoutMs;
      while (!stopped() && Date.now() < deadline) {
        await new Promise(done => setTimeout(done, Math.min(25, Math.max(1, deadline - Date.now()))));
      }
    }
    if (!stopped()) throw new Error('IDE processes did not stop after SIGKILL; session and lease were retained');
  };
  let resolveCompletion, rejectCompletion;
  const completion = new Promise((resolve, reject) => { resolveCompletion = resolve; rejectCompletion = reject; });
  const finish = () => {
    if (stopping) return;
    // One task owns escalation and cleanup. Repeated signals cannot restart the
    // deadline, and close racing with a forwarded signal cannot delete twice.
    stopping = (async () => {
      try {
        await stopGroup();
        cleanupSharedSession(layout, { spawnFailed: Boolean(spawnError && !child.pid) });
        if (spawnError) throw spawnError;
        const signal = closeResult?.signal ?? child.signalCode ?? null;
        resolveCompletion({ code: closeResult?.code ?? child.exitCode ?? (signal ? 1 : 0), signal });
      } catch (error) { rejectCompletion(error); }
      finally {
        for (const name of signals) process.removeListener(name, finish);
        shutdownSignal?.removeEventListener('abort', finish);
        child.removeListener('close', closed);
        child.removeListener('error', failed);
      }
    })();
  };
  const failed = error => { spawnError = error; };
  const closed = (code, signal) => { closeResult = { code, signal }; finish(); };
  child.once('error', failed);
  child.once('close', closed);
  for (const signal of signals) process.on(signal, finish);
  shutdownSignal?.addEventListener('abort', finish, { once: true });
  if (shutdownSignal?.aborted) finish();
  return completion;
}
