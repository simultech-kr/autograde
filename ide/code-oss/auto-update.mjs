// Public release metadata lives outside the disposable student session. Never
// derive update locations or signing keys from mDNS or student settings.
import * as fs from 'node:fs';
import * as path from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import {authorize, canonical, digest, stageRelease, activateRelease, withReleaseStore,
  assertSessionIdle, inspectStagedRelease, recoverDeadLock, syncDirectory} from './release-channel.mjs';
import {downloadRelease} from './update-transport.mjs';

const ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$/;
const stateName = 'update-state.json';
function read(file, fallback) {
  try {
    const stat = fs.lstatSync(file);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size > 1024 * 1024
      || process.platform !== 'win32' && (stat.uid !== process.getuid() || stat.mode & 0o077)) throw new Error('Unsafe update metadata');
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  } catch (error) { if (error.code === 'ENOENT') return fallback; throw error; }
}
function atomic(file, value) {
  const temporary = `${file}.${randomUUID()}.tmp`;
  try {
    const fd = fs.openSync(temporary, 'wx', 0o600);
    try { fs.writeFileSync(fd, JSON.stringify(value)); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
    fs.renameSync(temporary, file);
    syncDirectory(path.dirname(file));
  } finally { fs.rmSync(temporary, {force: true}); }
}
function state(root) {
  const value = read(path.join(root, stateName), {schema: 1, highestSequence: 0, failedReleaseIds: []});
  if (value.schema !== 1 || !Number.isSafeInteger(value.highestSequence) || value.highestSequence < 0
    || !Array.isArray(value.failedReleaseIds) || value.failedReleaseIds.length > 32 || value.failedReleaseIds.some(id => !ID.test(id))
    || value.managedReleaseIds !== undefined && (!Array.isArray(value.managedReleaseIds) || value.managedReleaseIds.length > 128 || value.managedReleaseIds.some(id => !ID.test(id)))
    || value.pending && (!ID.test(value.pending.releaseId) || !Number.isSafeInteger(value.pending.sequence))
    || value.trial && (!ID.test(value.trial.releaseId) || !ID.test(value.trial.previousReleaseId))) throw new Error('Invalid update metadata');
  return value;
}
function active(root) {
  const value = read(path.join(root, 'active.json'));
  if (!ID.test(value?.releaseId) || !Number.isSafeInteger(value.sequence) || value.sequence < 1) throw new Error('A verified seed release is required');
  return value;
}
export function getUpdateState(store) { return withReleaseStore(store, root => ({...state(root), active: active(root)})); }
function change(store, modify) {
  return withReleaseStore(store, root => {
    const value = state(root); modify(value); atomic(path.join(root, stateName), value); return value;
  });
}
function status(store, report, phase, extra = {}) {
  try { change(store, value => { value.status = {phase, ...extra, at: new Date().toISOString()}; }); } catch { /* A diagnostic must not stop the verified IDE. */ }
  report({event: 'update-status', phase, ...extra});
}
export function updateSettings(context) {
  if (context.updates === undefined) return null;
  const value = context.updates, url = new URL(value.feedUrl);
  if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash
    || !url.pathname.endsWith('/feed.json')) throw new Error('Updates require an operator-configured HTTPS feed');
  const interval = value.checkIntervalSeconds ?? 3600;
  if (!Number.isSafeInteger(interval) || interval < 60 || interval > 86400) throw new Error('Invalid update check interval');
  return {feedUrl: url.href, checkIntervalSeconds: interval};
}

/** Called only before starting the IDE. The original shared launcher owns cleanup. */
export function preparePendingUpdate({store, context, report = () => {}, activate = activateRelease, allowActivation = true}) {
  const before = getUpdateState(store);
  // A previous launch did not confirm a clean shutdown. Re-verify and restore the
  // previous release, but never switch while student files/locks remain.
  if (before.trial) {
    const result = failUpdateTrial({store, context, report, activate});
    if (!result.restored) throw new Error('Unconfirmed update or cleanup requires operator attention');
    return null;
  }
  if (!before.pending || !allowActivation) return null;
  try {
    assertSessionIdle(context);
    if (fs.existsSync(path.join(store, '.launch-lock.json'))) throw new Error('IDE active');
  } catch {
    status(store, report, 'deferred', {releaseId: before.pending.releaseId});
    return null; // The current original supervisor may recover its own stale seat.
  }
  const id = before.pending.releaseId;
  if (before.failedReleaseIds.includes(id) || before.pending.sequence <= before.active.sequence) {
    change(store, value => { delete value.pending; }); return null;
  }
  try {
    const trial = {releaseId: id, previousReleaseId: before.active.releaseId};
    activate({store, releaseId: id, context, beforeSwitch: () => {
      // activateRelease already holds the shared store lock.
      const value = state(store); value.trial = trial; delete value.pending;
      atomic(path.join(store, stateName), value);
    }});
    status(store, report, 'applied', {releaseId: id});
    return trial;
  } catch {
    // A failing rollback write (for example disk failure) must leave the durable
    // trial intent intact. Never mark an uncertain selection as a stable build.
    const selection = withReleaseStore(store, root => active(root));
    if (selection.releaseId !== before.active.releaseId) throw new Error('Cannot confirm previous selection after activation failure');
    change(store, value => {
      value.failedReleaseIds = [...new Set([...value.failedReleaseIds, id])].slice(-32);
      delete value.pending; delete value.trial;
    });
    status(store, report, 'kept-current', {reason: 'activation-failed', releaseId: before.active.releaseId});
    return null;
  }
}

export function failUpdateTrial({store, context, report = () => {}, activate = activateRelease}) {
  const before = getUpdateState(store), trial = before.trial;
  if (!trial) return {restored: false};
  change(store, value => { value.failedReleaseIds = [...new Set([...value.failedReleaseIds, trial.releaseId])].slice(-32); });
  try {
    // activateRelease checks every student root, launch lock, old signatures,
    // approval expiry and all old file hashes again. No data deletion here.
    if (before.active.releaseId !== trial.previousReleaseId) {
      if (before.active.releaseId !== trial.releaseId) throw new Error('Trial selection changed');
      activate({store, releaseId: trial.previousReleaseId, context, rollback: true});
    } else assertSessionIdle(context);
    change(store, value => { delete value.trial; });
    status(store, report, 'rolled-back', {releaseId: trial.previousReleaseId});
    return {restored: true};
  } catch {
    status(store, report, 'recovery-required', {reason: 'cleanup-or-previous-release-unverified'});
    return {restored: false};
  }
}
export function confirmUpdateTrial({store, context, cleaned, started}) {
  if (!cleaned || !started) throw new Error('Original launcher start and cleanup receipts are required');
  assertSessionIdle(context);
  change(store, value => { delete value.trial; });
  try { pruneOldUpdates({store, context}); } catch { /* Retention failure does not invalidate a working IDE. */ }
}

/** Retain current + rollback + pending builds, and never delete manually staged releases. */
export function pruneOldUpdates({store, context}) {
  assertSessionIdle(context);
  return withReleaseStore(store, root => {
    if (fs.existsSync(path.join(root, '.launch-lock.json'))) return;
    const value = state(root), selection = active(root);
    const keep = new Set([selection.releaseId, selection.previousReleaseId, value.pending?.releaseId, value.trial?.releaseId, value.trial?.previousReleaseId]);
    value.managedReleaseIds = (value.managedReleaseIds ?? []).filter(id => {
      if (keep.has(id)) return true;
      const target = path.join(root, 'releases', id);
      try {
        const stat = fs.lstatSync(target);
        if (!stat.isDirectory() || stat.isSymbolicLink() || process.platform !== 'win32' && stat.uid !== process.getuid()) return true;
        fs.rmSync(target, {recursive: true}); return false;
      } catch (error) { return error.code !== 'ENOENT'; }
    });
    atomic(path.join(root, stateName), value);
  });
}

export function cleanupInterruptedDownloads(store) {
  return withReleaseStore(store, root => {
    for (const name of fs.readdirSync(root)) {
      if (!/^\.download-[a-f0-9-]{36}$/.test(name)) continue;
      const directory = path.join(root, name), stat = fs.lstatSync(directory);
      if (!stat.isDirectory() || stat.isSymbolicLink() || process.platform !== 'win32' && (stat.uid !== process.getuid() || stat.mode & 0o077)) continue;
      try {
        const lease = read(path.join(directory, '.download-owner.json'));
        if (lease?.schema !== 1 || !Number.isSafeInteger(lease.pid) || lease.pid <= 0) continue;
        try { process.kill(lease.pid, 0); }
        catch (error) { if (error.code === 'ESRCH') fs.rmSync(directory, {recursive: true}); }
      } catch { /* Unknown directories are not ours to remove. */ }
    }
  });
}

/** Safe to run during class: only creates a verified, immutable candidate. */
export async function checkForUpdate({store, context, signal, report = () => {}, download = downloadRelease}) {
  const settings = updateSettings(context);
  if (!settings) return;
  const destination = path.join(store, `.download-${randomUUID()}`);
  try {
    const before = getUpdateState(store);
    // Keep at most one queued candidate. This also avoids retaining many large
    // downloads while a long teaching session is still using its current build.
    if (before.pending || before.trial) return;
    if ((before.managedReleaseIds?.length ?? 0) >= 128) throw new Error('Release retention needs operator attention');
    status(store, report, 'checking');
    const result = await download({...settings, context, destination, signal,
      minSequence: Math.max(before.active.sequence, before.highestSequence), excludeReleaseIds: before.failedReleaseIds,
      report: event => {
        const phase = {'update-downloading': 'downloading', 'update-verifying': 'verifying'}[event.event];
        if (phase) report({event: 'update-status', phase});
      }});
    signal?.throwIfAborted();
    if (!result) { status(store, report, 'current', {releaseId: before.active.releaseId}); return; }
    const release = authorize(result.releaseEnvelope, result.approvalEnvelope, context);
    if (release.sequence <= Math.max(before.active.sequence, before.highestSequence) || before.failedReleaseIds.includes(release.releaseId)) throw new Error('Older update refused');
    const candidate = path.join(store, 'releases', release.releaseId);
    const alreadyStaged = fs.existsSync(candidate);
    if (alreadyStaged) {
      const staged = inspectStagedRelease(store, release.releaseId, context);
      if (digest(canonical(staged.release)) !== digest(canonical(release))) throw new Error('Immutable release identity changed');
    } else stageRelease({store, payload: result.payload, releaseEnvelope: result.releaseEnvelope, approvalEnvelope: result.approvalEnvelope, context});
    signal?.throwIfAborted();
    change(store, value => {
      value.highestSequence = Math.max(value.highestSequence, release.sequence);
      value.pending = {releaseId: release.releaseId, sequence: release.sequence};
      if (!alreadyStaged) value.managedReleaseIds = [...new Set([...(value.managedReleaseIds ?? []), release.releaseId])];
    });
    status(store, report, 'ready-next-launch', {releaseId: release.releaseId});
  } catch {
    status(store, report, signal?.aborted ? 'download-paused' : 'kept-current', {reason: signal?.aborted ? 'ide-closed' : 'download-or-verification-failed'});
  } finally {
    // This random directory is created exclusively by downloadRelease. Existing
    // student roots, installed releases and operator configuration are untouched.
    fs.rmSync(destination, {recursive: true, force: true});
  }
}
export async function watchForUpdates(options) {
  const settings = updateSettings(options.context);
  if (!settings) return;
  while (!options.signal.aborted) {
    await checkForUpdate(options);
    try { await delay(settings.checkIntervalSeconds * 1000, undefined, {signal: options.signal}); }
    catch { return; }
  }
}

/** Serializes complete icon lifetimes, including the gap before launchActive. */
export function acquireChannelLease(store) {
  return withReleaseStore(store, root => {
    const file = path.join(root, '.channel-lock.json');
    recoverDeadLock(file);
    fs.writeFileSync(file, JSON.stringify({pid: process.pid}), {flag: 'wx', mode: 0o600});
    try { recoverDeadLock(path.join(root, '.launch-lock.json')); }
    catch (error) { fs.unlinkSync(file); throw error; }
    return () => { fs.unlinkSync(file); };
  });
}
