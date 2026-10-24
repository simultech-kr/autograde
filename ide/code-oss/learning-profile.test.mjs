import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import test from 'node:test';
import { learnerEnvironment, learnerLayout, prepareLearner, restrictedSettings, validateLearningOptions, verifyRestrictedLaunch } from './learning-profile.mjs';

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-learner-test-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const base = path.join(root, 'profiles');
  const source = path.join(root, 'source');
  fs.mkdirSync(path.join(source, 'extensions/autograde'), { recursive: true });
  fs.mkdirSync(path.join(source, 'extensions/cpp'));
  return { root, base, source, approved: new Set(['autograde', 'cpp']), defaults: { 'editor.fontSize': 16 } };
}
function json(file, value, mode = 0o600) {
  fs.writeFileSync(file, JSON.stringify(value), { mode });
}
function prepare(f, alias = 'test-a', policy = 'standard') {
  return prepareLearner(learnerLayout(f.base, alias, policy), f.defaults);
}

test('learner aliases are hashed and raw aliases are not persisted in files or names', t => {
  const f = fixture(t);
  const layout = prepare(f, 'non-sensitive-example');
  assert.match(path.basename(layout.root), /^l-[0-9a-f]{16}$/);
  assert.equal(layout.root.includes('non-sensitive-example'), false);
  const marker = fs.readFileSync(path.join(layout.root, '.autograde-learner.json'), 'utf8');
  assert.equal(marker.includes('non-sensitive-example'), false);
  assert.equal(JSON.parse(marker).identity, 'local-alias-only');
  assert.equal(JSON.parse(marker).isolation, 'directory-and-profile-only');
});

test('launch context reports local-only policy and does not inherit another learner portable data', t => {
  const f = fixture(t), a = prepare(f);
  const env = learnerEnvironment(a, { VSCODE_PORTABLE: '/unrelated', AUTOGRADE_LEARNING_POLICY: 'restricted', PATH: '/tools' });
  assert.equal(env.VSCODE_PORTABLE, a.runtime);
  assert.equal(env.AUTOGRADE_LEARNING_POLICY, 'standard');
  assert.equal(env.AUTOGRADE_LEARNER_WORKSPACE, a.workspaceRoot);
  assert.equal(env.AUTOGRADE_LEARNER_MATERIALS, a.materials);
  assert.equal(env.PATH, '/tools');
  const legacy = learnerEnvironment(undefined, env);
  assert.equal(legacy.AUTOGRADE_LEARNING_POLICY, 'unmanaged');
  assert.equal(legacy.AUTOGRADE_LEARNER_WORKSPACE, '');
  assert.equal(legacy.AUTOGRADE_LEARNER_MATERIALS, '');
});

test('each learner gets independent persistent workspace, materials and profile', t => {
  const f = fixture(t);
  const a = prepare(f), b = prepare(f, 'test-b');
  assert.notEqual(a.root, b.root);
  fs.writeFileSync(path.join(a.workspace, 'main.c'), 'int main(void) { return 0; }');
  fs.writeFileSync(path.join(a.materials, 'notes.txt'), 'private fixture notes');
  const settings = path.join(a.runtime, 'user-data/User/settings.json');
  fs.writeFileSync(settings, '// keep JSONC settings\n{"editor.fontSize": 18,}\n');
  const priorSettings = fs.readFileSync(settings);
  assert.deepEqual(prepare(f), a);
  assert.deepEqual(fs.readFileSync(settings), priorSettings);
  assert.equal(fs.readFileSync(path.join(a.workspace, 'main.c'), 'utf8'), 'int main(void) { return 0; }');
  assert.equal(fs.existsSync(path.join(b.workspace, 'main.c')), false);
  assert.equal(fs.existsSync(path.join(b.materials, 'notes.txt')), false);
  assert.equal(path.dirname(a.materials), a.root);
  assert.equal(fs.existsSync(path.join(a.workspace, 'materials')), false);
});

test('policy changes use independent profiles without modifying student work or standard settings', t => {
  const f = fixture(t), a = prepare(f);
  const setting = path.join(a.runtime, 'user-data/User/settings.json');
  json(setting, { 'chat.disableAIFeatures': false, 'editor.fontSize': 20 });
  const before = fs.readFileSync(setting);
  const b = prepare(f, 'test-a', 'restricted');
  assert.equal(a.workspace, b.workspace);
  assert.equal(a.materials, b.materials);
  assert.notEqual(a.runtime, b.runtime);
  assert.deepEqual(fs.readFileSync(setting), before);
  assert.deepEqual(JSON.parse(fs.readFileSync(path.join(b.runtime, 'user-data/User/settings.json'))), { ...f.defaults, ...restrictedSettings });
  assert.doesNotThrow(() => verifyRestrictedLaunch(b, f.source, f.approved));
});

for (const policy of ['standard', 'restricted']) test(`${policy} learner settings survive a normal atomic IDE save with mode 0644`, t => {
  const f = fixture(t), a = prepare(f, 'test-a', policy);
  const settings = path.join(a.runtime, 'user-data/User/settings.json');
  const temporary = `${settings}.tmp`;
  const edited = policy === 'restricted'
    ? JSON.stringify({ ...restrictedSettings, 'editor.fontSize': 20 })
    : '// Keep student JSONC comments\n{"editor.fontSize": 20,}\n';
  fs.writeFileSync(temporary, edited, { mode: 0o644 });
  fs.renameSync(temporary, settings);
  assert.deepEqual(prepare(f, 'test-a', policy), a);
  assert.doesNotThrow(() => verifyRestrictedLaunch(a, f.source, f.approved));
  assert.equal(fs.readFileSync(settings, 'utf8'), edited);
  if (process.platform !== 'win32') {
    assert.equal(fs.statSync(settings).mode & 0o777, 0o644);
    assert.equal(fs.statSync(path.dirname(settings)).mode & 0o777, 0o700);
  }
});

test('POSIX learner directory boundaries are 0700 and initial profile/marker are 0600', t => {
  if (process.platform === 'win32') return t.skip('POSIX modes do not configure Windows ACLs');
  const f = fixture(t), a = prepare(f);
  for (const dir of [a.root, a.workspace, a.materials, a.runtime, path.join(a.runtime, 'user-data'),
    path.join(a.runtime, 'user-data/User'), path.join(a.runtime, 'extensions'), path.join(a.runtime, 'shared-data')]) {
    assert.equal(fs.statSync(dir).mode & 0o777, 0o700, dir);
  }
  for (const file of [path.join(a.root, '.autograde-learner.json'), path.join(a.runtime, 'user-data/User/settings.json')]) {
    assert.equal(fs.statSync(file).mode & 0o777, 0o600, file);
  }
});

test('invalid aliases and unknown policies fail before filesystem work', () => {
  for (const alias of ['', '../other', 'a/b', 'a\\b', 'a b', '가나다', 'a'.repeat(65), '-other', 'a\n']) {
    assert.throws(() => learnerLayout('/unused', alias), /Learner/);
  }
  for (const policy of ['exam', '', '../restricted']) assert.throws(() => validateLearningOptions('test', policy), /policy/);
  assert.throws(() => validateLearningOptions(undefined, 'restricted'), /requires --learner/);
  assert.doesNotThrow(() => validateLearningOptions(undefined));
});

test('external, sibling learner, materials and workspace-prefix lookalikes are rejected', t => {
  const f = fixture(t), a = prepare(f), b = prepare(f, 'test-b');
  for (const target of [b.workspace, a.root, a.materials, `${a.workspace}-other`, f.root]) {
    assert.throws(() => learnerLayout(f.base, 'test-a', 'standard', target), /inside this learner/);
  }
  const child = path.join(a.workspace, 'assignment-1');
  fs.mkdirSync(child);
  assert.equal(prepareLearner(learnerLayout(f.base, 'test-a', 'standard', child), f.defaults).workspace, child);
});

test('a missing explicitly selected assignment folder is not silently created', t => {
  const f = fixture(t), a = prepare(f);
  const missing = path.join(a.workspace, 'misspelled-assignment');
  assert.throws(() => prepareLearner(learnerLayout(f.base, 'test-a', 'standard', missing), f.defaults), /ENOENT/);
  assert.equal(fs.existsSync(missing), false);
});

test('existing unmarked and mismatched learner directories are not adopted or overwritten', t => {
  const f = fixture(t), a = learnerLayout(f.base, 'test-a');
  fs.mkdirSync(a.root, { recursive: true, mode: 0o700 });
  fs.writeFileSync(path.join(a.root, 'keep.txt'), 'existing data');
  assert.throws(() => prepareLearner(a, f.defaults), /ENOENT/);
  assert.deepEqual(fs.readdirSync(a.root), ['keep.txt']);
  json(path.join(a.root, '.autograde-learner.json'), { schema: 1, learnerHash: 'wrong' });
  assert.throws(() => prepareLearner(a, f.defaults), /ownership marker mismatch/);
  assert.equal(fs.readFileSync(path.join(a.root, 'keep.txt'), 'utf8'), 'existing data');
});

test('existing learner directory permissions are checked without chmod on user files', t => {
  if (process.platform === 'win32') return t.skip('POSIX modes');
  const f = fixture(t), a = prepare(f);
  fs.chmodSync(a.root, 0o755);
  assert.throws(() => prepare(f), /0700/);
  assert.equal(fs.statSync(a.root).mode & 0o777, 0o755);
});

test('a group/world-writable runtime base is refused without changing it', t => {
  if (process.platform === 'win32') return t.skip('POSIX modes');
  const f = fixture(t);
  fs.mkdirSync(f.base, { mode: 0o700 });
  fs.chmodSync(f.base, 0o777);
  assert.throws(() => prepare(f), /not group\/world writable/);
  assert.deepEqual(fs.readdirSync(f.base), []);
  assert.equal(fs.statSync(f.base).mode & 0o777, 0o777);
});

test('symlinked learner paths, profiles, materials and workspace selection are refused', async t => {
  if (process.platform === 'win32') return t.skip('Windows symlink creation needs host privileges');
  for (const part of ['root', 'workspace', 'materials', 'runtime', 'settings', 'assignment']) await t.test(part, t => {
    const f = fixture(t), a = prepare(f);
    const outside = path.join(f.root, 'outside');
    fs.mkdirSync(outside);
    fs.writeFileSync(path.join(outside, 'keep.txt'), 'outside unchanged');
    let target = a[part];
    if (part === 'settings') target = path.join(a.runtime, 'user-data/User/settings.json');
    if (part === 'assignment') target = path.join(a.workspace, 'assignment');
    if (fs.existsSync(target)) fs.renameSync(target, `${target}-preserved`);
    fs.symlinkSync(outside, target);
    const selected = part === 'assignment' ? learnerLayout(f.base, 'test-a', 'standard', target) : a;
    assert.throws(() => prepareLearner(selected, f.defaults), /no symlink/);
    assert.deepEqual(fs.readdirSync(outside), ['keep.txt']);
  });
});

test('symlinked and hard-linked markers/settings never write to external files', async t => {
  if (process.platform === 'win32') return t.skip('Windows link creation needs host privileges');
  for (const kind of ['symlink', 'hardlink']) await t.test(kind, t => {
    const f = fixture(t), a = prepare(f);
    const target = path.join(a.runtime, 'user-data/User/settings.json');
    const outside = path.join(f.root, 'outside.json');
    json(outside, { unchanged: true });
    fs.renameSync(target, `${target}.preserved`);
    if (kind === 'symlink') fs.symlinkSync(outside, target);
    else fs.linkSync(outside, target);
    assert.throws(() => prepare(f), /no symlink or hard link/);
    assert.deepEqual(JSON.parse(fs.readFileSync(outside)), { unchanged: true });
  });
});

test('restricted settings violations fail closed and are not overwritten', async t => {
  for (const key of Object.keys(restrictedSettings)) await t.test(key, t => {
    const f = fixture(t), a = prepare(f, 'test-a', 'restricted');
    const file = path.join(a.runtime, 'user-data/User/settings.json');
    json(file, { ...restrictedSettings, [key]: key === 'extensions.allowed' ? { '*': true } : !restrictedSettings[key] });
    const before = fs.readFileSync(file);
    assert.throws(() => verifyRestrictedLaunch(a, f.source, f.approved), /configuration changed/);
    assert.deepEqual(fs.readFileSync(file), before);
  });
});

test('restricted mode rejects user extension content but accepts empty Code-OSS metadata', t => {
  const f = fixture(t), a = prepare(f, 'test-a', 'restricted');
  const directory = path.join(a.runtime, 'extensions');
  json(path.join(directory, 'extensions.json'), [], 0o644);
  json(path.join(directory, '.obsolete'), {}, 0o644);
  assert.doesNotThrow(() => verifyRestrictedLaunch(a, f.source, f.approved));
  fs.mkdirSync(path.join(directory, 'unapproved.ai-1.0.0'));
  assert.throws(() => verifyRestrictedLaunch(a, f.source, f.approved), /user-installed extension content/);
  assert.equal(fs.existsSync(path.join(directory, 'unapproved.ai-1.0.0')), true);
});

test('restricted mode rejects indexed extensions and alternate profiles', async t => {
  for (const kind of ['index', 'profile']) await t.test(kind, t => {
    const f = fixture(t), a = prepare(f, 'test-a', 'restricted');
    if (kind === 'index') json(path.join(a.runtime, 'extensions/extensions.json'), [{ identifier: { id: 'unapproved.ai' } }]);
    else fs.mkdirSync(path.join(a.runtime, 'user-data/User/profiles/other'), { recursive: true, mode: 0o700 });
    assert.throws(() => verifyRestrictedLaunch(a, f.source, f.approved), /extension content|alternate IDE profiles/);
  });
});

test('restricted mode rejects unapproved built-ins while allowing compiler output/dependencies', t => {
  const f = fixture(t), a = prepare(f, 'test-a', 'restricted');
  for (const name of ['out', 'node_modules']) fs.mkdirSync(path.join(f.source, 'extensions', name));
  assert.doesNotThrow(() => verifyRestrictedLaunch(a, f.source, f.approved));
  json(path.join(f.source, 'extensions/out/package.json'), { name: 'sideloaded' });
  assert.throws(() => verifyRestrictedLaunch(a, f.source, f.approved), /unapproved built-in/);
});

test('restricted opened-workspace settings cannot reenable AI at launch', t => {
  const f = fixture(t), a = prepare(f, 'test-a', 'restricted');
  fs.mkdirSync(path.join(a.workspace, '.vscode'));
  const file = path.join(a.workspace, '.vscode/settings.json');
  json(file, { 'editor.fontSize': 18 }, 0o644);
  assert.doesNotThrow(() => verifyRestrictedLaunch(a, f.source, f.approved));
  json(file, { 'chat.disableAIFeatures': false });
  const before = fs.readFileSync(file);
  assert.throws(() => verifyRestrictedLaunch(a, f.source, f.approved), /configuration changed/);
  assert.deepEqual(fs.readFileSync(file), before);
});

test('standard learning remains an overridable profile and does not claim restrictions', t => {
  const f = fixture(t), a = prepare(f);
  json(path.join(a.runtime, 'user-data/User/settings.json'), { 'chat.disableAIFeatures': false });
  fs.mkdirSync(path.join(a.runtime, 'extensions/user-chosen'));
  assert.doesNotThrow(() => verifyRestrictedLaunch(a, f.source, f.approved));
});
