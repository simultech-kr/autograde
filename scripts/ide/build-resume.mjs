// Durable checkpoints for a single native build. Source trees are never removed here.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { createHash, randomUUID } from 'node:crypto';

export const buildStateFilename = '.autograde-build-state.json';

function json(value, seen = new Set()) {
  if (value === null || typeof value === 'string' || typeof value === 'boolean') return value;
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (!value || typeof value !== 'object' || seen.has(value)
      || (!Array.isArray(value) && Object.getPrototypeOf(value) !== Object.prototype && Object.getPrototypeOf(value) !== null)) {
    throw new Error('Build checkpoint values must be finite, serializable JSON');
  }
  seen.add(value);
  const result = Array.isArray(value) ? value.map(item => json(item, seen))
    : Object.fromEntries(Object.keys(value).sort().map(key => [key, json(value[key], seen)]));
  seen.delete(value);
  return result;
}

const fingerprint = value => createHash('sha256').update(JSON.stringify(json(value))).digest('hex');
const plainObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const validName = name => typeof name === 'string' && /^[a-zA-Z0-9][a-zA-Z0-9._-]*$/.test(name);
const exists = file => {
  try { return fs.lstatSync(file); }
  catch (error) { if (error.code === 'ENOENT') return undefined; throw error; }
};

function regularFile(file) {
  const stat = exists(file);
  if (stat && (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1)) {
    throw new Error(`Build state and lock files must be regular files without links: ${file}`);
  }
  return stat;
}

function buildDirectory(directory) {
  if (typeof directory !== 'string' || !path.isAbsolute(directory)) throw new Error('Build source and output paths must be absolute');
  directory = path.resolve(directory);
  const stat = exists(directory);
  if (stat && (!stat.isDirectory() || stat.isSymbolicLink())) throw new Error(`Build path must be a regular directory: ${directory}`);
  if (directory === path.parse(directory).root) throw new Error('A filesystem root cannot be a build directory');
  // Resolve parent aliases (including macOS /var -> /private/var) so aliases
  // cannot acquire different locks for the same source tree.
  fs.mkdirSync(path.dirname(directory), { recursive: true });
  return stat ? fs.realpathSync.native(directory) : path.join(fs.realpathSync.native(path.dirname(directory)), path.basename(directory));
}

function contains(parent, child) {
  const relative = path.relative(parent, child);
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
}

function sameFile(a, b) { return a && b && a.dev === b.dev && a.ino === b.ino; }

function acquireLock(directory) {
  const file = path.join(path.dirname(directory), `.${path.basename(directory)}.autograde-build.lock`);
  const owner = { schema: 1, hostname: os.hostname(), pid: process.pid, token: randomUUID(), createdAt: new Date().toISOString() };
  for (let attempt = 0; attempt < 3; attempt++) {
    let fd;
    try { fd = fs.openSync(file, 'wx', 0o600); }
    catch (error) {
      if (error.code !== 'EEXIST') throw error;
      const stat = regularFile(file);
      if (!stat) continue;
      let prior;
      try { prior = JSON.parse(fs.readFileSync(file, 'utf8')); }
      catch { throw new Error(`Build directory is locked; lock owner cannot be verified: ${file}`); }
      if (prior.schema !== 1 || prior.hostname !== owner.hostname || !Number.isSafeInteger(prior.pid) || prior.pid < 1) {
        throw new Error(`Build directory is locked; lock owner cannot be verified: ${file}`);
      }
      let dead = false;
      try { process.kill(prior.pid, 0); }
      catch (error) { dead = error.code === 'ESRCH'; }
      if (!dead) throw new Error(`Build directory is locked by process ${prior.pid}: ${directory}`);
      if (!sameFile(stat, regularFile(file))) continue;
      fs.unlinkSync(file);
      continue;
    }
    try { fs.writeFileSync(fd, `${JSON.stringify(owner)}\n`); fs.fsyncSync(fd); }
    catch (error) { fs.closeSync(fd); fs.unlinkSync(file); throw error; }
    fs.closeSync(fd);
    const ownedStat = regularFile(file);
    return () => {
      const stat = regularFile(file);
      if (!sameFile(ownedStat, stat)) return;
      const current = JSON.parse(fs.readFileSync(file, 'utf8'));
      if (current.token === owner.token) fs.unlinkSync(file);
    };
  }
  throw new Error(`Could not acquire build lock: ${file}`);
}

function readState(file) {
  regularFile(file);
  let state;
  try { state = JSON.parse(fs.readFileSync(file, 'utf8')); }
  catch (error) { throw new Error(`Cannot read build checkpoint ${file}: ${error.message}`); }
  if (!plainObject(state) || state.schema !== 1 || typeof state.source !== 'string' || typeof state.output !== 'string'
      || !plainObject(state.identity) || !plainObject(state.steps)) throw new Error(`Invalid build checkpoint: ${file}`);
  for (const [name, step] of Object.entries(state.steps)) {
    if (!validName(name) || !plainObject(step) || !['running', 'passed', 'failed'].includes(step.status)
        || !/^[a-f0-9]{64}$/.test(step.signature) || !plainObject(step.dependencies)
        || !Object.entries(step.dependencies).every(([dependency, token]) => validName(dependency) && typeof token === 'string' && token.length > 0)
        || (step.status === 'passed' && (typeof step.token !== 'string' || !step.token || !Object.hasOwn(step, 'value')))) {
      throw new Error(`Invalid build checkpoint stage ${name}: ${file}`);
    }
  }
  return state;
}

/** Open one exclusively owned build. Call close() in a finally block. */
export function openBuildJournal({ source, output, identity, resume = false }) {
  if (typeof resume !== 'boolean') throw new Error('Build resume mode must be a boolean');
  identity = json(identity);
  if (!plainObject(identity)) throw new Error('Build identity must be a JSON object');
  source = buildDirectory(source); output = buildDirectory(output);
  // Creating the second parent may have created the first path through a
  // differently cased alias on a case-insensitive filesystem.
  if (exists(source)) source = fs.realpathSync.native(source);
  if (exists(output)) output = fs.realpathSync.native(output);
  if (contains(source, output) || contains(output, source)) {
    throw new Error('Source and output must be separate directories');
  }
  const releases = [];
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    let failure;
    for (const release of releases.reverse()) { try { release(); } catch (error) { failure ??= error; } }
    if (failure) throw failure;
  };
  try {
    for (const directory of [source, output].sort()) releases.push(acquireLock(directory));
    if (!exists(output)) fs.mkdirSync(output);
    const statePath = path.join(output, buildStateFilename);
    const stateExists = regularFile(statePath);
    const entries = fs.readdirSync(output);
    if (!resume && entries.length) throw new Error(`Refusing an existing nonempty build output without --resume: ${output}`);
    if (resume && !stateExists && entries.length) throw new Error(`Cannot resume a nonempty output without a build checkpoint: ${output}`);
    const resumed = Boolean(stateExists);
    const state = stateExists ? readState(statePath) : { schema: 1, source, output, identity, steps: {} };
    if (state.source !== source || state.output !== output || fingerprint(state.identity) !== fingerprint(identity)) {
      throw new Error('Build checkpoint identity or source/output paths do not match this build');
    }
    const save = () => {
      regularFile(statePath);
      const temporary = `${statePath}.${process.pid}.${randomUUID()}.tmp`;
      let fd;
      try {
        fd = fs.openSync(temporary, 'wx', 0o600);
        fs.writeFileSync(fd, `${JSON.stringify(state, null, 2)}\n`);
        fs.fsyncSync(fd); fs.closeSync(fd); fd = undefined;
        regularFile(statePath);
        fs.renameSync(temporary, statePath);
      } finally {
        if (fd !== undefined) fs.closeSync(fd);
        if (exists(temporary)) fs.unlinkSync(temporary);
      }
    };
    if (!stateExists) save();
    const completed = new Set(), visited = new Set();
    const step = (name, { key, deps = [], validate = () => true }, action) => {
      if (closed) throw new Error('Build journal is closed');
      if (!validName(name) || !Array.isArray(deps) || !deps.every(validName) || new Set(deps).size !== deps.length
          || deps.includes(name) || typeof validate !== 'function' || typeof action !== 'function') throw new Error('Invalid build checkpoint stage');
      if (visited.has(name)) throw new Error(`Build stage was already visited: ${name}`);
      const dependencies = Object.fromEntries(deps.map(dependency => {
        if (!completed.has(dependency) || !Object.hasOwn(state.steps, dependency) || state.steps[dependency].status !== 'passed') throw new Error(`Build dependency has not passed in this run: ${dependency}`);
        return [dependency, state.steps[dependency].token];
      }));
      const signature = fingerprint({ key, dependencies });
      visited.add(name);
      const previous = Object.hasOwn(state.steps, name) ? state.steps[name] : undefined;
      let reusable = false;
      if (previous?.status === 'passed' && previous.signature === signature) {
        try { reusable = validate(json(previous.value)) === true; } catch { /* Missing or invalid output reruns this stage. */ }
      }
      if (reusable) {
        completed.add(name);
        console.log(`[resume] Skipping passed stage: ${name}`);
        return json(previous.value);
      }
      const startedAt = new Date().toISOString();
      state.steps[name] = { status: 'running', signature, dependencies, startedAt };
      save(); // A killed process must leave a running stage, never a success.
      try {
        console.log(`[build] Running stage: ${name}`);
        const result = action();
        const value = json(result === undefined ? null : result);
        state.steps[name] = { status: 'passed', signature, dependencies, startedAt, finishedAt: new Date().toISOString(), token: randomUUID(), value };
        save();
        completed.add(name);
        return json(value);
      } catch (error) {
        const message = String(error?.message ?? error).slice(0, 4096);
        state.steps[name] = { status: 'failed', signature, dependencies, startedAt, finishedAt: new Date().toISOString(), error: message };
        try { save(); } catch (checkpointError) { throw new AggregateError([error, checkpointError], `Build stage ${name} failed and its checkpoint could not be saved`); }
        throw new Error(`Build stage ${name} failed: ${message}`, { cause: error });
      }
    };
    return { source, output, statePath, resumed, step, close };
  } catch (error) {
    try { close(); } catch (closeError) { throw new AggregateError([error, closeError], 'Cannot open build journal or release its locks'); }
    throw error;
  }
}
