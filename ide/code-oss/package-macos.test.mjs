import test from 'node:test';
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import {execFileSync} from 'node:child_process';
import { runtimePlan, collectProductionDependencies, infoPlist, parseOptions, isInside, assertArchitecture,
  configureLocalNetworkPrivacy, LOCAL_NETWORK_PURPOSE, BONJOUR_SERVICE_TYPE } from './package-macos.mjs';

function fixture(t) {
  const root = fs.realpathSync(fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-mac-package-')));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const write = (name, value = 'test') => {
    const file = path.join(root, name); fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, typeof value === 'object' ? JSON.stringify(value) : value); return file;
  };
  write('package.json', { version: '1.143.0', dependencies: {} });
  for (const name of ['product.json', 'LICENSE.txt', 'ThirdPartyNotices.txt', 'out/main.js', 'resources/darwin/code.icns']) write(name);
  for (const name of ['autograde', 'autograde-ide-profile']) {
    write(`extensions/${name}/package.json`, { name, engines: { vscode: '*' }, main: './out/extension.js' });
    write(`extensions/${name}/out/extension.js`);
  }
  return { root, write };
}

test('runtime plan excludes repository state, student data and development-only modules', t => {
  const { root, write } = fixture(t);
  for (const name of ['.git/config', '.data/students.sqlite', 'student_roster.csv', '.env', 'src/private.ts', 'node_modules/unused-dev/index.js', 'out/test/check.js', 'out/main.js.map', 'extensions/autograde/src/main.ts', 'extensions/autograde/.env', 'extensions/autograde/id_rsa', 'extensions/autograde/tests/check.js']) write(name, 'DO NOT SHIP');
  const plan = runtimePlan(root);
  assert.ok(plan.files.includes('out/main.js'));
  assert.ok(plan.files.includes('LICENSE.txt'));
  assert.ok(plan.files.includes('extensions/autograde/out/extension.js'));
  assert.equal(plan.files.some(name => fs.readFileSync(path.join(root, name), 'utf8') === 'DO NOT SHIP'), false);
  assert.deepEqual(plan.approvedBuiltins, ['autograde', 'autograde-ide-profile']);
});

test('production closure handles transitive and nested resolution without copying unrelated dev dependencies', t => {
  const { root, write } = fixture(t);
  write('package.json', { dependencies: { first: '1' } });
  write('node_modules/first/package.json', { dependencies: { nested: '1', hoisted: '1' } });
  write('node_modules/first/index.js');
  write('node_modules/first/node_modules/nested/package.json', { dependencies: { first: '1' } });
  write('node_modules/first/node_modules/nested/index.js');
  write('node_modules/first/node_modules/dev-only/package.json', {});
  write('node_modules/hoisted/package.json', {});
  const plan = runtimePlan(root);
  assert.ok(plan.files.includes('node_modules/first/node_modules/nested/index.js'));
  assert.ok(plan.files.includes('node_modules/hoisted/package.json'));
  assert.equal(plan.files.some(name => name.includes('dev-only')), false);
});

test('required missing dependencies fail while absent optional platform packages are allowed', t => {
  const { root, write } = fixture(t);
  write('package.json', { optionalDependencies: { absent: '1' }, peerDependencies: { optionalPeer: '1' } });
  assert.doesNotThrow(() => runtimePlan(root));
  write('package.json', { dependencies: { absent: '1' } });
  assert.throws(() => runtimePlan(root), /Missing runtime dependency/);
});

test('invalid dependency names cannot traverse source paths', t => {
  const { root, write } = fixture(t);
  write('package.json', { dependencies: { '../private': '1' } });
  assert.throws(() => runtimePlan(root), /Invalid dependency name/);
  assert.throws(() => collectProductionDependencies(root, path.dirname(root)), /outside the source/);
});

test('runtime dependencies retain native binaries and license files', t => {
  const { root, write } = fixture(t);
  write('package.json', { dependencies: { native: '1' } });
  write('node_modules/native/package.json', {});
  write('node_modules/native/build/Release/native.node');
  write('node_modules/native/LICENSE');
  const plan = runtimePlan(root);
  assert.ok(plan.files.includes('node_modules/native/build/Release/native.node'));
  assert.ok(plan.files.includes('node_modules/native/LICENSE'));
});

test('contained upstream file links are included but links to outside the package are rejected', t => {
  const { root, write } = fixture(t);
  write('package.json', { dependencies: { native: '1' } });
  write('node_modules/native/package.json', {});
  write('node_modules/native/library.1.dylib');
  fs.symlinkSync('library.1.dylib', path.join(root, 'node_modules/native/library.dylib'));
  assert.ok(runtimePlan(root).files.includes('node_modules/native/library.dylib'));
  fs.unlinkSync(path.join(root, 'node_modules/native/library.dylib'));
  fs.symlinkSync('../../../../etc/passwd', path.join(root, 'node_modules/native/library.dylib'));
  assert.throws(() => runtimePlan(root));
});

test('contained linked packages preserve relative links and package runtime content', t => {
  const { root, write } = fixture(t);
  write('package.json', { dependencies: { mock: '1' } });
  write('packageMocks/mock/package.json', {});
  write('packageMocks/mock/index.js');
  fs.mkdirSync(path.join(root, 'node_modules'));
  fs.symlinkSync('../packageMocks/mock', path.join(root, 'node_modules/mock'));
  const plan = runtimePlan(root);
  assert.equal(plan.links['node_modules/mock'], '../packageMocks/mock');
  assert.ok(plan.files.includes('packageMocks/mock/index.js'));
});

test('external linked dependency packages are rejected', t => {
  const { root, write } = fixture(t), external = fixture(t);
  write('package.json', { dependencies: { outsider: '1' } });
  fs.mkdirSync(path.join(root, 'node_modules'));
  fs.symlinkSync(external.root, path.join(root, 'node_modules/outsider'));
  assert.throws(() => runtimePlan(root), /outside the source/);
});

test('uncompiled built-ins fail and Copilot/test extensions are not distributed', t => {
  const { root, write } = fixture(t);
  for (const name of ['copilot', 'vscode-api-tests']) write(`extensions/${name}/package.json`, { engines: { vscode: '*' }, main: './missing.js' });
  assert.doesNotThrow(() => runtimePlan(root));
  write('extensions/not-built/package.json', { engines: { vscode: '*' }, main: './missing.js' });
  assert.throws(() => runtimePlan(root), /not compiled/);
});

test('packaging requires both Autograde built-ins and compiled editor', t => {
  const { root } = fixture(t);
  fs.unlinkSync(path.join(root, 'out/main.js'));
  assert.throws(() => runtimePlan(root), /Compile the editor/);
  fs.rmSync(path.join(root, 'extensions/autograde'), { recursive: true });
  assert.throws(() => runtimePlan(root), /Prepared Autograde/);
});

test('app plist selects native launcher and distinct bundle identity', () => {
  const xml = infoPlist('1.143.0', 'arm64');
  assert.match(xml, /AutogradeLauncher/);
  assert.match(xml, /<key>LSMinimumSystemVersion<\/key><string>13\.5<\/string>/);
  assert.match(xml, /info\.cbchoi\.autograde\.launcher/);
  assert.match(xml, /<key>LSUIElement<\/key><true\/>/);
  assert.match(xml, /<key>NSLocalNetworkUsageDescription<\/key><string>같은 실습실/);
  assert.match(xml, /<key>NSBonjourServices<\/key><array><string>_autograde\._tcp<\/string><\/array>/);
  assert.match(infoPlist('1.143.0', 'x64'), /x86_64/);
  assert.throws(() => infoPlist('1.2.3<script>', 'arm64'), /Invalid/);
  assert.throws(() => infoPlist('1.2.3', 'win32'), /Invalid/);
});

test('copied Electron privacy metadata preserves identity, unrelated values and existing services', {skip: process.platform !== 'darwin'}, t => {
  const {root, write} = fixture(t);
  const original = {CFBundleIdentifier: 'info.cbchoi.autograde.electron', CFBundleExecutable: 'Autograde IDE',
    CFBundleVersion: '1.143.0', NSBonjourServices: ['_existing._tcp'], unrelated: {enabled: true, count: 3}};
  const source = write('source.plist', original), copied = path.join(root, 'copied.plist');
  execFileSync('/usr/bin/plutil', ['-convert', 'xml1', source]);
  const originalBytes = fs.readFileSync(source);
  fs.copyFileSync(source, copied);
  configureLocalNetworkPrivacy(copied);
  configureLocalNetworkPrivacy(copied); // Repeating during a staged build does not duplicate entries.
  const result = JSON.parse(execFileSync('/usr/bin/plutil', ['-convert', 'json', '-o', '-', copied], {encoding: 'utf8'}));
  assert.deepEqual(result, {...original, NSLocalNetworkUsageDescription: LOCAL_NETWORK_PURPOSE,
    NSBonjourServices: ['_existing._tcp', BONJOUR_SERVICE_TYPE]});
  assert.deepEqual(fs.readFileSync(source), originalBytes);
  const absent = write('missing-keys.plist', {CFBundleIdentifier: 'unchanged'});
  execFileSync('/usr/bin/plutil', ['-convert', 'binary1', absent]);
  configureLocalNetworkPrivacy(absent);
  assert.deepEqual(JSON.parse(execFileSync('/usr/bin/plutil', ['-convert', 'json', '-o', '-', absent], {encoding: 'utf8'})),
    {CFBundleIdentifier: 'unchanged', NSLocalNetworkUsageDescription: LOCAL_NETWORK_PURPOSE, NSBonjourServices: [BONJOUR_SERVICE_TYPE]});
});

test('CLI accepts explicit runtime paths and refuses unknown switches', () => {
  const options = parseOptions(['--source', '/a/source', '--node', '/a/node', '--output', '/a/Autograde IDE.app', '--no-zip']);
  assert.deepEqual(options, { source: '/a/source', node: '/a/node', output: '/a/Autograde IDE.app', zip: false });
  assert.throws(() => parseOptions(['--output']), /Usage/);
  assert.throws(() => parseOptions(['--server-password', 'secret']), /Usage/);
});

test('path containment compares complete path segments', () => {
  assert.equal(isInside('/a/root', '/a/root/out/main.js'), true);
  assert.equal(isInside('/a/root', '/a/root-other/file'), false);
  assert.equal(isInside('/a/root', '/etc/passwd'), false);
});


test('Electron architecture must match the bundled Node and launcher architecture', () => {
  assert.doesNotThrow(() => assertArchitecture('arm64', 'arm64'));
  assert.doesNotThrow(() => assertArchitecture('x86_64 arm64', 'x64'));
  assert.throws(() => assertArchitecture('x86_64', 'arm64'), /architecture/);
  assert.throws(() => assertArchitecture('arm64', 'x64'), /architecture/);
});


test('package allowlist rejects an untracked extension in the build checkout', t => {
  const { root, write } = fixture(t);
  const approvedExtensions = new Set(['autograde', 'autograde-ide-profile']);
  assert.doesNotThrow(() => runtimePlan(root, { approvedExtensions }));
  write('extensions/extra/package.json', { name: 'extra', engines: { vscode: '*' } });
  assert.throws(() => runtimePlan(root, { approvedExtensions }), /Unapproved built-in/);
});
