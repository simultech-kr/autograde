import test from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtemp, mkdir, writeFile, readFile, lstat, realpath, rm, symlink } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
import * as path from 'node:path';
import { tmpdir } from 'node:os';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { policyHelperSources, policySources } from './extension-install-policy.mjs';

const root = fileURLToPath(new URL('../../', import.meta.url));
const checkout = path.join(root, '.cache/code-oss');
const available = existsSync(path.join(checkout, '.git'));
const readUpstream = file => execFileSync('git', ['show', `HEAD:${file}`], {cwd: checkout, encoding: 'utf8'});
let generated;
function sources() { return generated ??= policySources(readUpstream); }
const commonKey = 'src/vs/platform/extensionManagement/common/autogradeExtensionPolicy.ts';
const nodeKey = 'src/vs/platform/extensionManagement/node/autogradeExtensionPackage.ts';

async function helpers(t) {
  const temp = await mkdtemp(path.join(await realpath(tmpdir()), 'autograde-policy-test-'));
  t.after(() => rm(temp, {recursive: true, force: true}));
  const standalone = policyHelperSources();
  const common = stripTypeScriptTypes(standalone.get(commonKey)
    .replace("import { localize } from '../../../nls.js';", 'const localize = (_key, value) => value;'));
  await writeFile(path.join(temp, 'policy.mjs'), common);
  const node = stripTypeScriptTypes(standalone.get(nodeKey))
    .replace("'../common/autogradeExtensionPolicy.js'", "'./policy.mjs'");
  await writeFile(path.join(temp, 'package.mjs'), node);
  return {temp, common: await import(pathToFileURL(path.join(temp, 'policy.mjs')).href), node: await import(pathToFileURL(path.join(temp, 'package.mjs')).href)};
}

test('overlay requires every pristine anchor exactly once and remains deterministic', {skip: !available}, () => {
  assert.deepEqual([...sources()], [...policySources(readUpstream)]);
  assert.throws(() => policySources(() => ''), /anchor must occur exactly once/);
  assert.throws(() => policySources(file => readUpstream(file) + readUpstream(file)), /anchor must occur exactly once/);
  assert.equal(sources().size, 14);
});

test('only the shipped installer identity can use the private route', async t => {
  const {common} = await helpers(t);
  const shipped = {identifier: {value: 'autograde.autograde-vscode'}, isBuiltin: true, isUserBuiltin: false,
    isUnderDevelopment: false, extensionLocation: {scheme: 'file'}};
  common.assertAutogradeInstaller(shipped);
  for (const invalid of [undefined, {...shipped, identifier: {value: 'student.extension'}}, {...shipped, isBuiltin: false},
    {...shipped, isUserBuiltin: true}, {...shipped, isUnderDevelopment: true}, {...shipped, extensionLocation: {scheme: 'vscode-remote'}}]) {
    assert.throws(() => common.assertAutogradeInstaller(invalid), /교수자/);
  }
  common.assertAutogradeDirectInstallAllowed({});
  assert.throws(() => common.assertAutogradeDirectInstallAllowed({autogradeManagedExtensions: true}), /교수자/);
  common.assertAutogradeManifest({publisher: 'class', name: 'extension'});
  for (const manifest of [{publisher: 'class', name: 'extension', extensionDependencies: ['other.extension']},
    {publisher: 'class', name: 'extension', extensionPack: ['other.extension']}, {publisher: 'autograde', name: 'autograde-vscode'},
    {publisher: 'autograde', name: 'autograde-ide-profile'}]) {
    assert.throws(() => common.assertAutogradeManifest(manifest), /교수자/);
  }
});

async function session(temp, legacy = false) {
  const id = '1234567890abcdef12345678';
  const root = path.join(temp, legacy ? `e-${id}` : 'current');
  await mkdir(root, {mode: 0o700});
  for (const child of ['workspace', 'materials', 'r', 'r/user-data', 'r/extensions', 'r/extension-install-Abc123']) {
    await mkdir(path.join(root, child), {mode: 0o700});
  }
  const stat = await lstat(root);
  const marker = {schema: 1, sessionId: id, root, rootDev: stat.dev, rootIno: stat.ino, launcherPid: process.pid, phase: 'running', policy: 'standard'};
  await writeFile(path.join(root, '.autograde-session.json'), JSON.stringify(marker), {mode: 0o600});
  const environment = {AUTOGRADE_LEARNING_LIFETIME: 'shared', AUTOGRADE_LEARNING_POLICY: 'standard', AUTOGRADE_SESSION_ROOT: root,
    AUTOGRADE_SESSION_ID: id, AUTOGRADE_LEARNER_WORKSPACE: path.join(root, 'workspace'), AUTOGRADE_LEARNER_MATERIALS: path.join(root, 'materials')};
  const file = path.join(root, 'r/extension-install-Abc123/course.vsix');
  const bytes = Buffer.from('exact instructor-approved package bytes');
  const digest = createHash('sha256').update(bytes).digest('hex');
  await writeFile(file, bytes, {mode: 0o600});
  return {root, file, bytes, digest, environment, data: path.join(root, 'r/user-data'), extensions: path.join(root, 'r/extensions')};
}

test('managed installation snapshots checked bytes and cleans the private copy', async t => {
  const {temp, node} = await helpers(t);
  const s = await session(temp);
  const result = await node.snapshotAutogradeExtension(s.file, s.digest, s.data, s.extensions, s.environment);
  await writeFile(s.file, 'student replacement', {mode: 0o600});
  assert.deepEqual(await readFile(result.path), s.bytes);
  assert.equal((await lstat(result.path)).mode & 0o777, 0o600);
  await result.cleanup();
  await assert.rejects(lstat(result.path), {code: 'ENOENT'});
});

test('managed installation rejects wrong policy, session, destination, digest, and linked files', async t => {
  const {temp, node} = await helpers(t);
  const s = await session(temp);
  const run = (override = {}) => node.snapshotAutogradeExtension(override.file ?? s.file, override.digest ?? s.digest,
    override.data ?? s.data, s.extensions, {...s.environment, ...override.environment});
  for (const override of [{environment: {AUTOGRADE_LEARNING_POLICY: 'restricted'}}, {environment: {AUTOGRADE_LEARNING_LIFETIME: 'persistent'}},
    {environment: {AUTOGRADE_SESSION_ID: 'another-session'}}, {environment: {AUTOGRADE_SESSION_ID: 'a'.repeat(24)}}, {data: path.join(temp, 'other-profile')}, {digest: '0'.repeat(64)},
    {file: path.join(s.root, 'workspace/course.vsix')}]) {
    await assert.rejects(run(override));
  }
  const linked = path.join(s.root, 'r/extension-install-Abc123/linked.vsix');
  await symlink(s.file, linked);
  await assert.rejects(run({file: linked}));
  const markerPath = path.join(s.root, '.autograde-session.json');
  const marker = JSON.parse(await readFile(markerPath, 'utf8'));
  await writeFile(markerPath, JSON.stringify({...marker, policy: 'restricted'}), {mode: 0o600});
  await assert.rejects(run());
  await writeFile(path.join(s.root, '.autograde-session.json'), '{}', {mode: 0o600});
  await assert.rejects(run());
});

test('private route is absent from command registry and ordinary install has no override flag', {skip: !available}, () => {
  const s = sources();
  const host = s.get('src/vs/workbench/api/common/extHost.api.impl.ts');
  assert.match(host, /assertAutogradeInstaller\(extension\)/);
  assert.match(host, /\$installAutogradeExtension\(vsix, expectedSha256, extension.identifier.value\)/);
  const service = s.get('src/vs/platform/extensionManagement/node/extensionManagementService.ts');
  assert.match(service, /async install\(vsix: URI, options: InstallOptions = \{\}\): Promise<ILocalExtension> \{\n\t\tassertAutogradeDirectInstallAllowed\(this.productService\);/);
  const main = s.get('src/vs/workbench/api/browser/mainThreadCommands.ts');
  assert.match(main, /extensionHostKind !== ExtensionHostKind.LocalProcess/);
  assert.match(main, /assertAutogradeInstaller\(await this._extensionService.getExtension\(callerId\)\)/);
  assert.doesNotMatch(main, /registerCommand\(AUTOGRADE_INSTALL_COMMAND/);
  assert.match(s.get('src/vs/workbench/api/common/extHostCommands.ts'), /if \(id === AUTOGRADE_INSTALL_COMMAND\) \{\n\t\t\tthrow autogradeInstallError\(\);/);
});
