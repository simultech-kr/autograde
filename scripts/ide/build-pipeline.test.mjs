import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { parseOptions, assertBuildHost, compileSteps, sha256 } from './build-runtime.mjs';
import { verifyReports, buildTargets } from './finalize-artifacts.mjs';

const expected = { sourceRevision: 'a'.repeat(40), extensionVersion: '0.5.14',
  upstream: { repository: 'https://github.com/microsoft/vscode.git', tag: '1.143.0', commit: 'b'.repeat(40), node: '24.18.0' } };

function fixture(t, includeWsl = true) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'autograde-build-inventory-'));
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }));
  for (const [target, definition] of Object.entries(buildTargets)) {
    if (target === 'windows-launcher' && !includeWsl) continue;
    const folder = path.join(directory, target); fs.mkdirSync(folder);
    const report = { schema: 1, status: 'release-candidate', sourceDirty: false, buildTarget: target, ...expected,
      host: { platform: definition.platform, architecture: definition.architecture },
      includeWsl: target === 'linux-x64' && includeWsl,
      checks: [...definition.checks, ...(target === 'linux-x64' && includeWsl ? ['package-wsl', 'wsl-packaged-cli-version'] : [])].map(name => ({ name, result: 'passed' })),
      acceptance: { productionReady: false, institution: 'not-tested', windowsWsl2: 'not-tested' }, artifacts: [] };
    for (const kind of [...definition.artifacts, ...(target === 'linux-x64' && includeWsl ? ['windows-wsl2-payload'] : [])]) {
      const name = `Autograde-IDE-0.5.14-${kind}.zip`, file = path.join(folder, name);
      fs.writeFileSync(file, `synthetic archive for ${kind}`);
      const digest = sha256(file);
      fs.writeFileSync(`${file}.sha256`, `${digest}  ${name}\n`);
      report.artifacts.push({ target: kind, file: name, bytes: fs.statSync(file).size, sha256: digest });
    }
    fs.writeFileSync(path.join(folder, `build-${target}.json`), JSON.stringify(report));
  }
  return directory;
}

function editReport(input, target, change) {
  const file = path.join(input, target, `build-${target}.json`), report = JSON.parse(fs.readFileSync(file, 'utf8'));
  change(report); fs.writeFileSync(file, JSON.stringify(report));
}

test('native host checks reject wrong architecture, OS or Node', () => {
  assert.doesNotThrow(() => assertBuildHost('macos-arm64', expected.upstream, { platform: 'darwin', arch: 'arm64', versions: { node: '24.18.0' } }));
  assert.throws(() => assertBuildHost('linux-x64', expected.upstream, { platform: 'darwin', arch: 'x64', versions: { node: '24.18.0' } }), /native operating system/);
  assert.throws(() => assertBuildHost('linux-x64', expected.upstream, { platform: 'linux', arch: 'arm64', versions: { node: '24.18.0' } }), /native operating system/);
  assert.throws(() => assertBuildHost('linux-x64', expected.upstream, { platform: 'linux', arch: 'x64', versions: { node: '22.0.0' } }), /pinned Node/);
  assert.throws(() => parseOptions(['--target', 'linux-x64']), /Specify/);
});
test('source checking precedes emit, preserves cache reuse and includes Monaco checking', () => {
  const steps = compileSteps('/source', 4096), names = steps.map(([name]) => name);
  assert.ok(names.indexOf('code-oss-source-typecheck') < names.indexOf('code-oss-client-compile'));
  assert.ok(steps.find(([name]) => name === 'code-oss-source-typecheck')[1].includes('/source/src/tsconfig.typecheck.tsbuildinfo'));
  assert.ok(steps.find(([name]) => name === 'code-oss-client-compile')[1].includes('compile-client'));
  assert.ok(names.includes('code-oss-monaco-typecheck'));
});
test('uncommitted source cannot be attested as a candidate', t => {
  const input = fixture(t);
  editReport(input, 'windows-x64', report => { report.sourceDirty = true; });
  assert.throws(() => verifyReports(input, expected), /clean committed source/);
});

test('complete inventory contains all native platforms and optional paired WSL2', t => {
  const builds = verifyReports(fixture(t), expected);
  assert.equal(builds.length, 4);
  assert.equal(builds.flatMap(build => build.report.artifacts).length, 5);
});
test('native Windows is required while WSL2 can be omitted entirely', t => {
  const input = fixture(t, false), builds = verifyReports(input, expected);
  assert.equal(builds.length, 3);
  assert.equal(builds.flatMap(build => build.report.artifacts).length, 3);
  fs.rmSync(path.join(input, 'windows-x64'), { recursive: true });
  assert.throws(() => verifyReports(input, expected), /three native platform/);
});

test('pairing rejects a missing Windows launcher', t => {
  const input = fixture(t);
  fs.rmSync(path.join(input, 'windows-launcher'), { recursive: true });
  assert.throws(() => verifyReports(input, expected), /both the Linux payload and Windows launcher/);
});

test('pairing rejects version skew or missing policy compilation', t => {
  const input = fixture(t);
  editReport(input, 'linux-x64', report => { report.extensionVersion = '0.5.13'; });
  assert.throws(() => verifyReports(input, expected), /same Autograde revision and extension version/);
  editReport(input, 'linux-x64', report => { report.extensionVersion = expected.extensionVersion; report.checks = report.checks.filter(check => check.name !== 'teacher-only-policy-overlay'); });
  assert.throws(() => verifyReports(input, expected), /Missing required build check/);
});

test('pairing rejects modified archive bytes or checksum sidecar', t => {
  const input = fixture(t), target = path.join(input, 'windows-launcher');
  const archive = fs.readdirSync(target).find(file => file.endsWith('.zip'));
  fs.appendFileSync(path.join(target, archive), 'modified');
  assert.throws(() => verifyReports(input, expected), /size\/hash mismatch/);
  const clean = fixture(t), cleanTarget = path.join(clean, 'windows-launcher');
  fs.writeFileSync(path.join(cleanTarget, `${archive}.sha256`), `${'0'.repeat(64)}  ${archive}\n`);
  assert.throws(() => verifyReports(clean, expected), /sidecar mismatch/);
});

test('inventory cannot promote candidates or accept foreign native builds', t => {
  const input = fixture(t);
  editReport(input, 'linux-x64', report => { report.acceptance.productionReady = true; });
  assert.throws(() => verifyReports(input, expected), /cannot certify/);
  editReport(input, 'linux-x64', report => { report.acceptance.productionReady = false; report.host.platform = 'darwin'; });
  assert.throws(() => verifyReports(input, expected), /platform\/architecture mismatch/);
});

test('inventory rejects duplicate payload targets and escaping archive names', t => {
  const input = fixture(t);
  editReport(input, 'linux-x64', report => { report.artifacts[1].target = 'linux-native'; });
  assert.throws(() => verifyReports(input, expected), /duplicate platform artifact/);
  const clean = fixture(t);
  editReport(clean, 'windows-launcher', report => { report.artifacts[0].file = '../outside.zip'; });
  assert.throws(() => verifyReports(clean, expected), /Unsafe/);
});
