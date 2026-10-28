import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { execFileSync, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { createRequire, syncBuiltinESMExports } from 'node:module';
import test from 'node:test';
import { checkSource, extensionBundle, launchSpec, main, prepare, prepareRuntime, productOverlay, readJson, repoRoot, validateRuntimePath, verify, walk } from './manage.mjs';

const here = path.dirname(fileURLToPath(import.meta.url));
function write(file, content) {
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(file, typeof content === 'string' ? content : `${JSON.stringify(content, null, 2)}\n`);
}
function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-ide-test-')));
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  const source = path.join(root, 'Code OSS checkout');
  const extensionRoot = path.join(root, 'extension');
  const profileRoot = path.join(root, 'profile');
  const baseProduct = {nameShort: 'Code - OSS', nameLong: 'Code - OSS', applicationName: 'code-oss',
    extensionsGallery: {serviceUrl: 'https://invalid.example/gallery'}, updateUrl: 'https://invalid.example/update',
    defaultChatAgent: {extensionId: 'GitHub.copilot-chat'}, configurationSync: {url: 'https://invalid.example/sync'}};
  write(path.join(source, 'product.json'), baseProduct);
  write(path.join(source, 'package.json'), {name: 'code-oss', version: '1.92.0'});
  write(path.join(source, '.nvmrc'), '22.0.0\n');
  write(path.join(source, 'LICENSE.txt'), 'MIT fixture license\n');
  write(path.join(source, 'extensions', 'existing', 'package.json'), {name: 'existing', version: '1.0.0'});
  write(path.join(source, 'src', 'main.ts'), '// source fixture\n');
  const git = (...args) => execFileSync('git', ['-C', source, ...args], {encoding: 'utf8'}).trim();
  git('init', '--quiet');
  git('add', '.');
  git('-c', 'user.name=Autograde Fixture', '-c', 'user.email=fixture@example.invalid', 'commit', '--quiet', '-m', 'fixture');
  const upstream = {tag: '1.92.0', node: '22.0.0', commit: git('rev-parse', 'HEAD')};
  write(path.join(extensionRoot, 'package.json'), {publisher: 'autograde', name: 'autograde-vscode', version: '0.5.7',
    main: './out/extension.js', scripts: {test: 'test'}, dependencies: {parent: '1.0.0'}, devDependencies: {devonly: '1.0.0'}});
  write(path.join(extensionRoot, 'out', 'extension.js'), 'exports.activate = () => {};\n');
  write(path.join(extensionRoot, 'out', 'extension.js.map'), '{"sources":["private.ts"]}');
  write(path.join(extensionRoot, 'out', 'test', 'fixture.test.js'), 'throw new Error("test should not ship");');
  write(path.join(extensionRoot, 'src', 'extension.ts'), '// source not shipped\n');
  write(path.join(extensionRoot, 'tsconfig.json'), {compilerOptions: {outDir: 'out', rootDir: 'src'}, include: ['src/**/*.ts']});
  write(path.join(extensionRoot, 'media', 'autograde.svg'), '<svg/>');
  write(path.join(extensionRoot, 'README.md'), '# Autograde fixture\n');
  write(path.join(extensionRoot, 'old.vsix'), 'do not ship');
  write(path.join(extensionRoot, '.env'), 'do not ship');
  write(path.join(extensionRoot, 'node_modules', 'parent', 'package.json'), {name: 'parent', version: '1.0.0', dependencies: {child: '2.0.0', hoisted: '1.0.0'}});
  write(path.join(extensionRoot, 'node_modules', 'parent', 'index.js'), 'module.exports = require("child");');
  write(path.join(extensionRoot, 'node_modules', 'parent', 'node_modules', 'child', 'package.json'), {name: 'child', version: '2.0.0'});
  write(path.join(extensionRoot, 'node_modules', 'parent', 'node_modules', 'child', 'index.js'), 'module.exports = 2;');
  write(path.join(extensionRoot, 'node_modules', 'hoisted', 'package.json'), {name: 'hoisted', version: '1.0.0'});
  write(path.join(extensionRoot, 'node_modules', 'hoisted', 'index.js'), 'module.exports = 1;');
  write(path.join(extensionRoot, 'node_modules', 'devonly', 'package.json'), {name: 'devonly', version: '1.0.0'});
  write(path.join(extensionRoot, 'node_modules', 'devonly', 'index.js'), 'throw new Error("dev should not ship");');
  write(path.join(extensionRoot, 'package-lock.json'), {lockfileVersion: 3, packages: {
    '': {dependencies: {parent: '1.0.0'}, devDependencies: {devonly: '1.0.0'}},
    'node_modules/parent': {version: '1.0.0'}, 'node_modules/parent/node_modules/child': {version: '2.0.0'},
    'node_modules/hoisted': {version: '1.0.0'}, 'node_modules/devonly': {version: '1.0.0', dev: true},
  }});
  write(path.join(profileRoot, 'package.json'), {publisher: 'autograde', name: 'autograde-ide-profile', version: '0.1.0',
    contributes: {configurationDefaults: {'workbench.startupEditor': 'none'}}});
  return {root, source, extensionRoot, profileRoot, baseProduct, git, options: {upstream, extensionRoot, profileRoot, compile: () => {}, policySources: read => new Map([['src/main.ts', read('src/main.ts') + '// managed fixture policy\n']])}};
}

test('prepare, verify and reprepare preserve existing extensions and recoverable previous generated bytes', t => {
  const f = fixture(t);
  assert.throws(() => verify(f.source, f.options), /prepare first/);
  const first = prepare(f.source, f.options);
  assert.equal(first.extensionVersion, '0.5.7');
  const verified = verify(f.source, f.options);
  assert.equal(verified.database, 'unchanged-sqlite');
  assert.equal(verified.enforcement, false);
  assert.equal(readJson(path.join(f.source, 'extensions/existing/package.json')).name, 'existing');
  const second = prepare(f.source, f.options);
  assert.notEqual(first.backup, second.backup);
  assert.equal(readJson(path.join(second.backup, 'previous-autograde/package.json')).version, '0.5.7');
  assert.equal(readJson(path.join(second.backup, 'previous-product.json')).nameLong, 'Autograde IDE');
  assert.deepEqual(verify(f.source, f.options), verified);
});

test('production closure contains nested and hoisted dependencies but not extension tests, maps, dev tools or secrets', t => {
  const f = fixture(t);
  const files = extensionBundle(f.extensionRoot);
  for (const name of ['out/extension.js', 'media/autograde.svg', 'README.md',
    'node_modules/parent/index.js', 'node_modules/parent/node_modules/child/index.js', 'node_modules/hoisted/index.js']) assert.ok(files.has(name), name);
  for (const name of ['src/extension.ts', 'out/extension.js.map', 'out/test/fixture.test.js', 'node_modules/devonly/index.js', '.env', 'old.vsix']) assert.equal(files.has(name), false, name);
  const manifest = JSON.parse(files.get('package.json'));
  assert.equal(manifest.scripts, undefined);
  assert.equal(manifest.devDependencies, undefined);
  assert.deepEqual(manifest.dependencies, {parent: '1.0.0'});
});

test('upstream commit, version, Node pin and modified tracked sources fail closed', async t => {
  for (const [label, modify, expected] of [
    ['commit', f => {f.options.upstream = {...f.options.upstream, commit: '0'.repeat(40)};}, /commit does not match/],
    ['version', f => {write(path.join(f.source, 'package.json'), {version: '0.0.0'});}, /version or Node/],
    ['node', f => {write(path.join(f.source, '.nvmrc'), '0.0.0');}, /version or Node/],
    ['source', f => {write(path.join(f.source, 'src/main.ts'), '// user edit');}, /modified upstream source/],
    ['product', f => {write(path.join(f.source, 'product.json'), {nameLong: 'User product'});}, /unowned modified product/],
  ]) await t.test(label, t => {
    const f = fixture(t); modify(f);
    assert.throws(() => prepare(f.source, f.options), expected);
    assert.equal(fs.existsSync(path.join(f.source, 'extensions/autograde')), false);
  });
});

test('unowned existing extension directory is never overwritten', t => {
  const f = fixture(t);
  const file = path.join(f.source, 'extensions/autograde/notes.txt');
  write(file, 'owned by user');
  assert.throws(() => prepare(f.source, f.options), /existing extension directory/);
  assert.equal(fs.readFileSync(file, 'utf8'), 'owned by user');
  assert.deepEqual(readJson(path.join(f.source, 'product.json')), f.baseProduct);
});

test('post-prepare user changes in owned bundles or product are refused, not overwritten', async t => {
  for (const [relative, expected] of [['extensions/autograde/out/extension.js', /Bundled autograde was modified/],
    ['extensions/autograde-ide-profile/package.json', /Bundled autograde-ide-profile was modified/],
    ['product.json', /Product configuration changed/]]) await t.test(relative, t => {
    const f = fixture(t); prepare(f.source, f.options);
    const content = 'user changed this file';
    write(path.join(f.source, relative), content);
    assert.throws(() => verify(f.source, f.options), expected);
    assert.throws(() => prepare(f.source, f.options), expected);
    assert.equal(fs.readFileSync(path.join(f.source, relative), 'utf8'), content);
  });
});

test('managed core policy edits are preserved and rejected rather than silently replaced', t => {
  const f = fixture(t); prepare(f.source, f.options);
  write(path.join(f.source, 'src/main.ts'), '// local policy edit\n');
  assert.throws(() => verify(f.source, f.options), /policy was modified/);
  assert.throws(() => prepare(f.source, f.options), /policy was modified/);
  assert.equal(fs.readFileSync(path.join(f.source, 'src/main.ts'), 'utf8'), '// local policy edit\n');
});

test('a newer managed policy requires preparation and retains the preceding policy backup', t => {
  const f = fixture(t); prepare(f.source, f.options);
  const previous = fs.readFileSync(path.join(f.source, 'src/main.ts'), 'utf8');
  f.options.policySources = read => new Map([['src/main.ts', read('src/main.ts') + '// policy v2\n']]);
  assert.throws(() => verify(f.source, f.options), /policy is stale/);
  const result = prepare(f.source, f.options);
  assert.equal(fs.readFileSync(path.join(result.backup, 'previous-policy-source/src/main.ts'), 'utf8'), previous);
  assert.equal(verify(f.source, f.options).managedExtensionInstall, true);
});

test('policy additions cannot adopt an unowned file or escape the source tree', async t => {
  for (const name of ['src/unowned.ts', 'src/../outside.ts']) await t.test(name, t => {
    const f = fixture(t);
    if (name === 'src/unowned.ts') write(path.join(f.source, name), '// keep user data\n');
    f.options.policySources = () => new Map([[name, '// managed replacement\n']]);
    assert.throws(() => prepare(f.source, f.options), /unowned IDE policy|Invalid managed policy/);
    if (name === 'src/unowned.ts') assert.equal(fs.readFileSync(path.join(f.source, name), 'utf8'), '// keep user data\n');
  });
});

test('stale compiled extension and profile require prepare again before verification succeeds', async t => {
  for (const relative of ['extension/out/extension.js', 'profile/package.json']) await t.test(relative, t => {
    const f = fixture(t); prepare(f.source, f.options);
    write(path.join(f.root, relative), relative.endsWith('.json') ? {name: 'updated-profile', version: '0.2.0'} : '// updated compiled code');
    assert.throws(() => verify(f.source, f.options), /stale/);
    const result = prepare(f.source, f.options);
    assert.equal(verify(f.source, f.options).commit, f.options.upstream.commit);
    assert.ok(fs.existsSync(path.join(result.backup, 'previous-autograde')));
  });
});

test('uncompiled source edits invalidate verification, and prepare invokes the supplied compiler', t => {
  const f = fixture(t);
  let compiled = 0;
  f.options.compile = root => {assert.equal(root, f.extensionRoot); compiled++;};
  prepare(f.source, f.options);
  write(path.join(f.extensionRoot, 'src/extension.ts'), '// source updated without recompilation\n');
  assert.throws(() => verify(f.source, f.options), /stale|sources? changed/i);
  prepare(f.source, f.options);
  assert.equal(compiled, 2);
  assert.equal(verify(f.source, f.options).enforcement, false);
});

test('compiler failure or source changes during compilation leave the original checkout unchanged', async t => {
  for (const label of ['failure', 'changed input']) await t.test(label, t => {
    const f = fixture(t);
    f.options.compile = root => {
      if (label === 'failure') throw new Error('compiler failed');
      write(path.join(root, 'src/extension.ts'), '// edited while compiling');
    };
    assert.throws(() => prepare(f.source, f.options), /compiler failed|changed while compiling/);
    assert.deepEqual(readJson(path.join(f.source, 'product.json')), f.baseProduct);
    assert.equal(fs.existsSync(path.join(f.source, 'extensions/autograde')), false);
  });
});

function fakeCompiler(f, {fail = false} = {}) {
  write(path.join(f.extensionRoot, 'node_modules/typescript/bin/tsc'), `
const fs = require('node:fs');
const path = require('node:path');
const output = process.argv[process.argv.indexOf('--outDir') + 1];
if (!output || output === process.argv[0]) throw new Error('fresh --outDir required');
fs.writeFileSync(path.join(process.cwd(), 'fixture-compiler-output.txt'), output);
fs.mkdirSync(output, {recursive: true});
fs.writeFileSync(path.join(output, 'extension.js'), '// fresh compiled runtime\\n');
${fail ? 'process.exit(2);' : ''}
`);
  f.options.compile = undefined;
}

function cleanSyntheticCompilerStage(t, f) {
  const output = fs.readFileSync(path.join(f.extensionRoot, 'fixture-compiler-output.txt'), 'utf8');
  const staging = path.dirname(output);
  assert.equal(path.dirname(staging), path.join(repoRoot, 'build'));
  assert.match(path.basename(staging), /^autograde-extension-/);
  // Delete only the temporary stage created by this test's fake compiler, never other build output.
  t.after(() => fs.rmSync(staging, {recursive: true, force: true}));
  return staging;
}

test('default compiler builds fresh runtime and preserves old output without shipping orphan JavaScript', t => {
  const f = fixture(t);
  write(path.join(f.extensionRoot, 'out/orphan.js'), '// deleted source should not ship');
  fakeCompiler(f);
  prepare(f.source, f.options);
  const staging = cleanSyntheticCompilerStage(t, f);
  assert.equal(fs.existsSync(path.join(f.source, 'extensions/autograde/out/orphan.js')), false);
  assert.equal(fs.existsSync(path.join(f.extensionRoot, 'out/orphan.js')), false);
  assert.equal(fs.readFileSync(path.join(staging, 'previous-out/orphan.js'), 'utf8'), '// deleted source should not ship');
  assert.equal(fs.readFileSync(path.join(f.source, 'extensions/autograde/out/extension.js'), 'utf8'), '// fresh compiled runtime\n');
  assert.equal(verify(f.source, f.options).enforcement, false);
});

test('default compiler failure leaves previous compiled output and upstream checkout unchanged', t => {
  const f = fixture(t);
  const oldRuntime = fs.readFileSync(path.join(f.extensionRoot, 'out/extension.js'), 'utf8');
  write(path.join(f.extensionRoot, 'out/orphan.js'), '// old generated module');
  fakeCompiler(f, {fail: true});
  assert.throws(() => prepare(f.source, f.options), /Command failed/);
  cleanSyntheticCompilerStage(t, f);
  assert.equal(fs.readFileSync(path.join(f.extensionRoot, 'out/extension.js'), 'utf8'), oldRuntime);
  assert.equal(fs.readFileSync(path.join(f.extensionRoot, 'out/orphan.js'), 'utf8'), '// old generated module');
  assert.deepEqual(readJson(path.join(f.source, 'product.json')), f.baseProduct);
  assert.equal(fs.existsSync(path.join(f.source, 'extensions/autograde')), false);
});

test('a failed final marker installation restores previous generated product, bundles and marker', t => {
  const f = fixture(t); prepare(f.source, f.options);
  const priorMarker = fs.readFileSync(path.join(f.source, '.autograde-ide-overlay.json'), 'utf8');
  const priorExtension = fs.readFileSync(path.join(f.source, 'extensions/autograde/out/extension.js'), 'utf8');
  const priorProduct = fs.readFileSync(path.join(f.source, 'product.json'), 'utf8');
  write(path.join(f.extensionRoot, 'out/extension.js'), '// next generated version');
  const mutableFs = createRequire(import.meta.url)('node:fs');
  const originalRename = mutableFs.renameSync;
  let injected = false;
  mutableFs.renameSync = (from, to) => {
    if (!injected && to === path.join(fs.realpathSync(f.source), '.autograde-ide-overlay.json')) {
      injected = true;
      throw new Error('simulated final rename failure');
    }
    return originalRename(from, to);
  };
  syncBuiltinESMExports();
  try { assert.throws(() => prepare(f.source, f.options), /simulated final rename failure/); }
  finally { mutableFs.renameSync = originalRename; syncBuiltinESMExports(); }
  assert.equal(injected, true);
  assert.equal(fs.readFileSync(path.join(f.source, '.autograde-ide-overlay.json'), 'utf8'), priorMarker);
  assert.equal(fs.readFileSync(path.join(f.source, 'extensions/autograde/out/extension.js'), 'utf8'), priorExtension);
  assert.equal(fs.readFileSync(path.join(f.source, 'product.json'), 'utf8'), priorProduct);
  assert.equal(checkSource(f.source, f.options.upstream), fs.realpathSync(f.source));
});

test('dependency lock version mismatch, missing runtime and dependencies are detected before replacing bundles', async t => {
  for (const [label, modify, expected] of [
    ['version mismatch', f => write(path.join(f.extensionRoot, 'node_modules/parent/package.json'), {name: 'parent', version: '99.0.0'}), /differs from package-lock/],
    ['missing dependency', f => fs.renameSync(path.join(f.extensionRoot, 'node_modules/parent'), path.join(f.root, 'preserved-parent')), /Missing dependency/],
    ['missing main', f => fs.renameSync(path.join(f.extensionRoot, 'out/extension.js'), path.join(f.root, 'preserved-main.js')), /Compile the extension/],
  ]) await t.test(label, t => {
    const f = fixture(t); modify(f);
    assert.throws(() => prepare(f.source, f.options), expected);
    assert.deepEqual(readJson(path.join(f.source, 'product.json')), f.baseProduct);
    assert.equal(fs.existsSync(path.join(f.source, 'extensions/autograde')), false);
  });
});

test('symlinked runtime files, dependencies and extension destinations are never followed', async t => {
  if (process.platform === 'win32') return t.skip('symlink creation requires host-specific privileges on Windows');
  for (const [label, modify] of [
    ['runtime', f => {fs.renameSync(path.join(f.extensionRoot, 'out/extension.js'), path.join(f.root, 'real.js')); fs.symlinkSync(path.join(f.root, 'real.js'), path.join(f.extensionRoot, 'out/extension.js'));}],
    ['dependency ancestor', f => {fs.renameSync(path.join(f.extensionRoot, 'node_modules'), path.join(f.root, 'real-modules')); fs.symlinkSync(path.join(f.root, 'real-modules'), path.join(f.extensionRoot, 'node_modules'));}],
    ['extensions target', f => {fs.renameSync(path.join(f.source, 'extensions'), path.join(f.root, 'real-extensions')); fs.symlinkSync(path.join(f.root, 'real-extensions'), path.join(f.source, 'extensions'));}],
  ]) await t.test(label, t => {
    const f = fixture(t); modify(f);
    assert.throws(() => prepare(f.source, f.options), /no symlink/);
    assert.deepEqual(readJson(path.join(f.source, 'product.json')), f.baseProduct);
  });
});

test('product overlay removes marketplace, update, sync, voice and inherited extension recommendations', () => {
  const keys = ['extensionsGallery', 'updateUrl', 'downloadUrl', 'voiceWsUrl', 'configurationSync',
    'trustedExtensionAuthAccess', 'extensionTips', 'keymapExtensionTips'];
  const product = productOverlay(Object.fromEntries(keys.map(key => [key, 'must not remain'])));
  for (const key of keys) assert.equal(key in product, false, key);
  assert.equal(product.enableTelemetry, false);
  assert.equal(product.nameLong, 'Autograde IDE');
  assert.deepEqual(product.builtInExtensions, []);
  assert.deepEqual(product.builtInExtensionsEnabledWithAutoUpdates, []);
  const profile = readJson(path.join(here, 'profile/package.json'));
  assert.match(profile.description, /not an exam security policy/);
  assert.equal(profile.contributes.configurationDefaults['workbench.startupEditor'], 'none');
  assert.equal(profile.contributes.configurationDefaults['chat.disableAIFeatures'], true);
  assert.deepEqual(Object.keys(profile.contributes.configurationDefaults).sort(), [
    'chat.disableAIFeatures', 'workbench.startupEditor', 'workbench.tips.enabled',
  ]);
});

test('required default chat metadata remains compatible with upstream startup without installing an AI extension', () => {
  const defaultChatAgent = {extensionId: 'GitHub.copilot', chatExtensionId: 'GitHub.copilot-chat',
    provider: {default: {id: 'github', name: 'GitHub'}}, documentationUrl: 'https://invalid.example/documentation'};
  const product = productOverlay({defaultChatAgent, builtInExtensionsEnabledWithAutoUpdates: ['GitHub.copilot-chat']});
  assert.deepEqual(product.defaultChatAgent, defaultChatAgent);
  assert.deepEqual(product.builtInExtensions, []);
  assert.deepEqual(product.webBuiltInExtensions, []);
  assert.deepEqual(product.builtInExtensionsEnabledWithAutoUpdates, []);
});

test('launch executable naming follows upstream on macOS, Linux and Windows without shell quoting', t => {
  const f = fixture(t); prepare(f.source, f.options);
  const runtime = path.join(f.root, 'runtime with spaces');
  const workspace = path.join(f.root, 'workspace with spaces;not-a-command');
  for (const [platform, executable] of [['darwin', path.join('Autograde IDE.app', 'Contents', 'MacOS', 'Autograde IDE')],
    ['linux', 'autograde-ide'], ['win32', 'Autograde IDE.exe']]) {
    const spec = launchSpec(f.source, runtime, workspace, platform);
    assert.equal(spec.executable, path.join(f.source, '.build/electron', executable));
    assert.deepEqual(spec.args.slice(0, 6), [f.source, '--user-data-dir', path.join(runtime, 'user-data'), '--extensions-dir', path.join(runtime, 'extensions'), '--new-window']);
    const sharedDataIndex = spec.args.indexOf('--shared-data-dir');
    assert.ok(sharedDataIndex >= 0);
    assert.equal(spec.args[sharedDataIndex + 1], path.join(runtime, 'shared-data'));
    assert.ok(spec.args.includes('--disable-extension=GitHub.copilot'));
    assert.ok(spec.args.includes('--disable-extension=GitHub.copilot-chat'));
    assert.ok(spec.args.includes('--disable-extension=vscode.vscode-api-tests'));
    assert.equal(spec.args.at(-1), workspace);
    assert.equal(spec.args.includes('--no-sandbox'), false);
    assert.equal(launchSpec(f.source, runtime, undefined, platform).args.includes(undefined), false);
  }
});

test('runtime preparation uses separate owned profile directories and preserves existing contents', t => {
  const f = fixture(t);
  const runtime = prepareRuntime(f.root);
  assert.equal(runtime, path.join(f.root, '.cache/ide'));
  for (const name of ['user-data', 'extensions', 'shared-data']) assert.equal(fs.lstatSync(path.join(runtime, name)).isDirectory(), true);
  write(path.join(runtime, 'user-data/User/settings.json'), {setting: 'retained'});
  write(path.join(runtime, 'extensions/user-owned.txt'), 'retained');
  write(path.join(runtime, 'shared-data/shared-storage.json'), {retained: true});
  assert.equal(prepareRuntime(f.root), runtime);
  assert.deepEqual(readJson(path.join(runtime, 'user-data/User/settings.json')), {setting: 'retained'});
  assert.equal(fs.readFileSync(path.join(runtime, 'extensions/user-owned.txt'), 'utf8'), 'retained');
  assert.deepEqual(readJson(path.join(runtime, 'shared-data/shared-storage.json')), {retained: true});
});

test('fresh runtime seeds first-boot user settings privately before the workbench starts', t => {
  const f = fixture(t);
  const runtime = prepareRuntime(f.root);
  const settings = path.join(runtime, 'user-data/User/settings.json');
  const expected = {
    'autograde.serviceBaseUrl': 'https://ai.cbchoi.info:20000',
    'autograde.studentPortalBaseUrl': 'https://ai.cbchoi.info:20010',
    'workbench.startupEditor': 'none',
    'workbench.tips.enabled': false,
    'chat.disableAIFeatures': true,
    'telemetry.telemetryLevel': 'off',
    'workbench.enableExperiments': false,
    'extensions.autoCheckUpdates': false,
    'extensions.autoUpdate': false,
    'extensions.autoRestart': false,
    'update.mode': 'none',
  };
  assert.deepEqual(readJson(settings), expected);
  assert.deepEqual(readJson(path.join(here, 'runtime-settings.json')), expected);
  if (process.platform !== 'win32') assert.equal(fs.statSync(settings).mode & 0o777, 0o600);
  const original = fs.readFileSync(settings);
  prepareRuntime(f.root);
  assert.deepEqual(fs.readFileSync(settings), original);
});

test('preexisting JSONC settings remain byte-for-byte intact and are not merged with learning defaults', t => {
  const f = fixture(t);
  const runtime = path.join(f.root, '.cache/ide');
  const settings = path.join(runtime, 'user-data/User/settings.json');
  const userBytes = Buffer.from('\ufeff// Student-selected presentation settings\r\n{\r\n  "workbench.startupEditor": "welcomePage",\r\n  "editor.fontSize": 17, // keep comments and trailing commas\r\n}\r\n');
  fs.mkdirSync(path.dirname(settings), {recursive: true});
  fs.writeFileSync(settings, userBytes);
  prepareRuntime(f.root);
  assert.deepEqual(fs.readFileSync(settings), userBytes);
  prepareRuntime(f.root);
  assert.deepEqual(fs.readFileSync(settings), userBytes);
});

test('runtime rejects symlinks at every profile boundary without touching the destination profile', async t => {
  if (process.platform === 'win32') return t.skip('symlink creation requires host-specific privileges on Windows');
  for (const component of ['.cache', '.cache/ide', '.cache/ide/user-data', '.cache/ide/extensions', '.cache/ide/shared-data', '.cache/ide/user-data/User']) {
    await t.test(component, t => {
      const f = fixture(t);
      const existing = path.join(f.root, 'existing-vscode-profile');
      write(path.join(existing, 'do-not-touch.txt'), 'existing profile');
      const link = path.join(f.root, component);
      fs.mkdirSync(path.dirname(link), {recursive: true});
      fs.symlinkSync(existing, link);
      assert.throws(() => prepareRuntime(f.root), /no symlink/);
      assert.deepEqual(fs.readdirSync(existing), ['do-not-touch.txt']);
      assert.equal(fs.readFileSync(path.join(existing, 'do-not-touch.txt'), 'utf8'), 'existing profile');
      assert.equal(fs.lstatSync(link).isSymbolicLink(), true);
    });
  }
});

test('settings symlinks are refused without replacing the link or writing its destination', async t => {
  if (process.platform === 'win32') return t.skip('symlink creation requires host-specific privileges on Windows');
  for (const dangling of [false, true]) await t.test(dangling ? 'dangling link' : 'existing target', t => {
    const f = fixture(t);
    const existing = path.join(f.root, 'unrelated-settings.json');
    const original = '// unrelated user settings\n{"editor.fontSize": 20}\n';
    if (!dangling) write(existing, original);
    const link = path.join(f.root, '.cache/ide/user-data/User/settings.json');
    fs.mkdirSync(path.dirname(link), {recursive: true});
    fs.symlinkSync(existing, link);
    assert.throws(() => prepareRuntime(f.root), /no symlink|EEXIST/);
    assert.equal(fs.lstatSync(link).isSymbolicLink(), true);
    assert.equal(fs.existsSync(existing), !dangling);
    if (!dangling) assert.equal(fs.readFileSync(existing, 'utf8'), original);
  });
});

test('runtime override selects an independent explicit directory without creating the default profile', t => {
  const f = fixture(t);
  const override = path.join(f.root, 'short-profile');
  assert.equal(prepareRuntime(f.root, override), override);
  assert.equal(fs.existsSync(path.join(f.root, '.cache/ide')), false);
  for (const name of ['user-data', 'extensions', 'shared-data']) assert.equal(fs.lstatSync(path.join(override, name)).isDirectory(), true);
  write(path.join(override, 'user-data/User/settings.json'), {keep: true});
  assert.equal(prepareRuntime(f.root, override), override);
  assert.deepEqual(readJson(path.join(override, 'user-data/User/settings.json')), {keep: true});
});

test('runtime override rejects symlink ancestors and root links without changing their destinations', async t => {
  if (process.platform === 'win32') return t.skip('symlink creation requires host-specific privileges on Windows');
  for (const variant of ['root', 'ancestor', 'runtime']) await t.test(variant, t => {
    const f = fixture(t);
    const destination = path.join(f.root, 'existing-profile');
    write(path.join(destination, 'keep.txt'), 'existing data');
    const link = path.join(f.root, 'profile-link');
    fs.symlinkSync(destination, link);
    const root = variant === 'root' ? link : f.root;
    const override = variant === 'root' ? undefined : variant === 'ancestor' ? path.join(link, 'nested/profile') : link;
    assert.throws(() => prepareRuntime(root, override), /no symlink/);
    assert.deepEqual(fs.readdirSync(destination), ['keep.txt']);
    assert.equal(fs.readFileSync(path.join(destination, 'keep.txt'), 'utf8'), 'existing data');
  });
});

test('Unix socket length guard checks exact byte boundaries and suggests an explicit shorter runtime', () => {
  const version = '1.143.0';
  const tail = path.join('user-data', `${version.slice(0, 4)}-shared.sock`);
  const fixedBytes = Buffer.byteLength(path.join('/x', tail)) - 1;
  for (const [platform, limit] of [['darwin', 103], ['linux', 107]]) {
    const runtimeForBytes = bytes => `/${'a'.repeat(bytes - fixedBytes)}`;
    const allowed = runtimeForBytes(limit - 1);
    const rejected = runtimeForBytes(limit);
    assert.equal(Buffer.byteLength(path.join(allowed, tail)), limit - 1);
    assert.equal(Buffer.byteLength(path.join(rejected, tail)), limit);
    assert.doesNotThrow(() => validateRuntimePath(allowed, version, platform));
    assert.throws(() => validateRuntimePath(rejected, version, platform), /--runtime/);
    assert.throws(() => validateRuntimePath(runtimeForBytes(limit + 20), version, platform), /--runtime/);
  }
  assert.doesNotThrow(() => validateRuntimePath(`/${'a'.repeat(300)}`, version, 'win32'));
});

test('Unicode runtime names are counted in UTF-8 bytes rather than characters', () => {
  const version = '1.143.0';
  const runtime = `/${'가'.repeat(28)}`;
  const socket = path.join(runtime, 'user-data', `${version.slice(0, 4)}-shared.sock`);
  assert.ok(socket.length < 103);
  assert.ok(Buffer.byteLength(socket) >= 107);
  for (const platform of ['darwin', 'linux']) assert.throws(() => validateRuntimePath(runtime, version, platform), /--runtime/);
  assert.doesNotThrow(() => validateRuntimePath('/private/tmp/ag', version, 'darwin'));
});

test('CLI rejects missing, duplicated, unknown, positional and inappropriate workspace arguments before doing work', () => {
  for (const args of [[], ['unknown'], ['prepare', '--bad', 'value'], ['prepare', '--source'],
    ['prepare', '--source', '--workspace'], ['prepare', '--source', 'a', '--source', 'b'],
    ['verify', '--workspace', 'a'], ['prepare', 'unexpected'], ['run', '--workspace'],
    ['prepare', '--runtime', 'a'], ['verify', '--runtime', 'a'], ['run', '--runtime'],
    ['prepare', '--learner', 'test-a'], ['verify', '--learning-policy', 'standard'], ['run', '--learner'],
    ['run', '--learner', 'a', '--learner', 'b'], ['run', '--learning-policy'],
    ['run', '--runtime', 'a', '--runtime', 'b'], ['run', '--runtime', '--source']]) {
    assert.throws(() => main(args), /Expected prepare|Usage:|only supported/);
  }
  const result = spawnSync(process.execPath, [path.join(here, 'manage.mjs'), 'invalid'], {encoding: 'utf8'});
  assert.equal(result.status, 1);
  assert.match(result.stderr, /^Autograde IDE: Expected prepare/);
  assert.equal(result.stdout, '');
});

test('run validates learner and policy before checking source or creating runtime', t => {
  const f = fixture(t);
  for (const options of [['--learner', '../student'], ['--learning-policy', 'unknown'], ['--learning-policy', 'restricted']]) {
    const result = spawnSync(process.execPath, [path.join(here, 'manage.mjs'), 'run', '--source', path.join(f.root, 'missing'), ...options], { encoding: 'utf8' });
    assert.equal(result.status, 1);
    assert.match(result.stderr, /Learner|Learning policy|requires --learner/);
    assert.doesNotMatch(result.stderr, /ENOENT/);
  }
});

test('shared lifetime defaults with a learner and rejects unsafe or ambiguous launch options before source access', () => {
  const bad = [
    [['run', '--learning-lifetime', 'unknown', '--learner', 'test'], /Learning lifetime must/],
    [['run', '--learning-lifetime', 'shared'], /requires --learner/],
    [['prepare', '--learning-lifetime', 'shared'], /only supported by run/],
    [['run', '--learner', 'test', '--workspace', '/old-user-files'], /fresh workspace/],
  ];
  for (const [args, pattern] of bad) assert.throws(() => main(args), pattern);
});

test('run accepts an explicit runtime path option before checking the required source checkout', t => {
  const f = fixture(t);
  const missing = path.join(f.root, 'missing-checkout');
  const result = spawnSync(process.execPath, [path.join(here, 'manage.mjs'), 'run', '--runtime', path.join(f.root, 'short-profile'), '--source', missing], {encoding: 'utf8'});
  assert.equal(result.status, 1);
  assert.match(result.stderr, /missing-checkout|ENOENT/);
  assert.doesNotMatch(result.stderr, /Usage:|only supported/);
  assert.equal(fs.existsSync(path.join(f.root, 'short-profile')), false);
});

test('walk rejects symlink files instead of reading outside the owned tree', t => {
  if (process.platform === 'win32') return t.skip('symlink creation requires host-specific privileges on Windows');
  const f = fixture(t);
  fs.symlinkSync(path.join(f.source, 'product.json'), path.join(f.profileRoot, 'outside.json'));
  assert.throws(() => walk(f.profileRoot), /no symlink/);
});
