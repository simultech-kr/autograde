#!/usr/bin/env node
// Real macOS Code-OSS smoke test. Only a temporary app clone and a synthetic
// shared session are modified; no server, credentials, or student work is used.
// Usage: node ide/code-oss/check-extension-policy.mjs /path/to/Autograde\ IDE.app
import assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { createHash } from 'node:crypto';
import { execFileSync, spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { loadMacBundle, macLaunchSpec } from './mac-launcher.mjs';
import { sharedSessionLayout, prepareSharedSession, markSessionLaunching, recordSessionProcess,
  cleanupSharedSession, superviseSharedChild, processAlive, processGroupAlive } from './shared-session.mjs';

function write(file, value) {
  fs.mkdirSync(path.dirname(file), {recursive: true});
  fs.writeFileSync(file, typeof value === 'object' ? JSON.stringify(value, null, 2) : value);
}

function probeVsix(root) {
  const input = path.join(root, 'probe-input'), output = path.join(root, 'policy-probe.vsix');
  write(path.join(input, 'extension/package.json'), {name: 'policy-probe', publisher: 'autograde-test', version: '1.0.0',
    displayName: 'Local policy smoke probe', engines: {vscode: '^1.92.0'}, description: 'Synthetic offline test only'});
  write(path.join(input, 'extension.vsixmanifest'), `<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"><Metadata><Identity Publisher="autograde-test" Id="policy-probe" Version="1.0.0"/><DisplayName>Local policy smoke probe</DisplayName><Description>Synthetic offline test only</Description></Metadata><Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets></PackageManifest>`);
  write(path.join(input, '[Content_Types].xml'), '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="vsixmanifest" ContentType="text/xml"/></Types>');
  execFileSync('/usr/bin/zip', ['-q', '-r', output, 'extension', 'extension.vsixmanifest', '[Content_Types].xml'], {cwd: input});
  return fs.readFileSync(output);
}

const userHarness = `const vscode = require('vscode');
const assert = require('node:assert/strict');
async function denied(action) {
  try { await action(); } catch (error) {
    assert.match(String(error.message), /교수자가 관리|instructor/, 'must reject with the policy error');
    return {denied: true, message: error.message};
  }
  throw new Error('Ordinary user extension unexpectedly installed a VSIX');
}
exports.activate = () => ({
  async probe(file, hash) {
    const uri = vscode.Uri.file(file);
    return {
      publicInstall: await denied(() => vscode.commands.executeCommand('workbench.extensions.installExtension', uri)),
      privateInstall: await denied(() => vscode.commands.executeCommand('_autograde.installCourseExtension', uri, hash)),
    };
  }
});
`;

function builtinHarness(config) {
  return `const vscode = require('vscode');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const config = ${JSON.stringify(config)};
const report = value => fs.writeFileSync(config.result, JSON.stringify(value, null, 2));
async function run() {
  const result = {ok: false, checks: {}, editorVersion: vscode.version};
  try {
    const caller = vscode.extensions.getExtension('autograde-test.policy-caller');
    assert.ok(caller, 'synthetic ordinary user extension is scanned');
    const user = await caller.activate();
    Object.assign(result.checks, await user.probe(config.file, config.hash));
    assert.equal(vscode.extensions.getExtension('autograde-test.policy-probe'), undefined, 'denied commands did not install');
    const commands = await vscode.commands.getCommands(false);
    assert.equal(commands.includes('_autograde.installCourseExtension'), false, 'private bridge is not publicly registered');
    result.checks.privateCommandHidden = true;
    try {
      await vscode.commands.executeCommand('_autograde.installCourseExtension', vscode.Uri.file(config.file), '0'.repeat(64));
      throw new Error('Wrong package hash unexpectedly installed');
    } catch (error) {
      assert.match(String(error.message), /교수자가 관리|instructor/);
      result.checks.wrongHash = {denied: true, message: error.message};
    }
    await vscode.commands.executeCommand('_autograde.installCourseExtension', vscode.Uri.file(config.file), config.hash);
    const directories = fs.readdirSync(config.extensions).filter(name => name.startsWith('autograde-test.policy-probe-'));
    assert.equal(directories.length, 1, 'one instructor-approved extension was installed');
    const installed = JSON.parse(fs.readFileSync(path.join(config.extensions, directories[0], 'package.json'), 'utf8'));
    assert.equal(installed.publisher + '.' + installed.name, 'autograde-test.policy-probe');
    assert.equal(installed.version, '1.0.0');
    result.checks.builtinPrivateInstall = {installed: true, id: 'autograde-test.policy-probe', version: installed.version};
    assert.equal(fs.readdirSync(config.runtime).some(name => name.startsWith('course-extension-verified-')), false, 'verified snapshot was removed');
    result.checks.privateSnapshotCleaned = true;
    result.ok = true;
  } catch (error) { result.error = error.stack || String(error); }
  report(result);
  void vscode.commands.executeCommand('workbench.action.quit');
}
exports.activate = () => { setTimeout(() => void run(), 100); };
`;
}

const pause = ms => new Promise(resolve => setTimeout(resolve, ms));
export async function checkExtensionPolicy(app, {timeoutMs = 90_000, shutdownTimeoutMs = 3000} = {}) {
  if (process.platform !== 'darwin') throw new Error('This packaged-app smoke test runs on macOS');
  app = fs.realpathSync(path.resolve(app));
  const root = fs.mkdtempSync('/private/tmp/ag-pol-');
  const clone = path.join(root, 'Smoke IDE.app');
  // APFS clones are independent writable files, never hard links into distribution.
  execFileSync('/bin/cp', ['-cR', app, clone]);
  const bundle = loadMacBundle(path.join(clone, 'Contents/Resources'));
  const resultFile = path.join(root, 'result.json');
  const layout = prepareSharedSession(sharedSessionLayout(path.join(root, 's'), 'policy-smoke', 'standard'), {
    ...bundle.defaults, 'security.workspace.trust.enabled': false, 'git.enabled': false,
  });
  fs.mkdirSync(path.join(layout.root, 'tmp'), {mode: 0o700});
  const staging = fs.mkdtempSync(path.join(layout.runtime, 'extension-install-'));
  const bytes = probeVsix(root), file = path.join(staging, 'policy-probe.vsix');
  fs.writeFileSync(file, bytes, {mode: 0o600, flag: 'wx'});
  const config = {file, hash: createHash('sha256').update(bytes).digest('hex'), result: resultFile,
    extensions: path.join(layout.runtime, 'extensions'), runtime: layout.runtime};
  const commonManifest = {version: '1.0.0', engines: {vscode: '^1.92.0'}, main: './policy-smoke.cjs',
    activationEvents: ['*'], extensionKind: ['workspace']};
  const builtin = path.join(bundle.source, 'extensions/autograde');
  write(path.join(builtin, 'package.json'), {...commonManifest, name: 'autograde-vscode', publisher: 'autograde'});
  write(path.join(builtin, 'policy-smoke.cjs'), builtinHarness(config));
  const user = path.join(config.extensions, 'autograde-test.policy-caller-1.0.0');
  write(path.join(user, 'package.json'), {...commonManifest, name: 'policy-caller', publisher: 'autograde-test'});
  write(path.join(user, 'policy-smoke.cjs'), userHarness);
  const spec = macLaunchSpec(bundle, layout);
  spec.args.push('--skip-welcome', '--skip-release-notes', '--disable-workspace-trust');
  let cliResult;
  try {
    const cli = spawnSync(spec.executable, [path.join(bundle.source, 'out/cli.js'), bundle.source,
      '--user-data-dir', path.join(layout.runtime, 'user-data'), '--extensions-dir', config.extensions,
      '--install-extension', file, '--force'], {
      cwd: bundle.source, env: {...spec.options.env, ELECTRON_RUN_AS_NODE: '1'},
      encoding: 'utf8', timeout: 30_000, maxBuffer: 4 * 1024 * 1024,
    });
    const output = `${cli.stdout || ''}${cli.stderr || ''}`;
    write(path.join(root, 'cli.log'), output);
    if (cli.error) throw cli.error;
    assert.notEqual(cli.status, 0, 'ordinary CLI must reject direct VSIX installation');
    assert.match(output, /교수자가 관리|instructor/, 'CLI must reject with the installation policy error');
    assert.equal(fs.readdirSync(config.extensions).some(name => name.startsWith('autograde-test.policy-probe-')), false);
    cliResult = {denied: true, exitCode: cli.status};
  } catch (error) {
    cleanupSharedSession(layout);
    throw error;
  }
  const log = fs.openSync(path.join(root, 'editor.log'), 'w');
  markSessionLaunching(layout);
  const child = spawn(spec.executable, spec.args, {...spec.options, stdio: ['ignore', log, log]});
  fs.closeSync(log);
  let spawnError, exited = false;
  child.once('error', error => {spawnError = error;});
  child.once('exit', () => {exited = true;});
  const shutdown = new AbortController();
  const completion = superviseSharedChild(child, layout, {shutdownTimeoutMs, shutdownSignal: shutdown.signal});
  void completion.catch(() => {});
  if (child.pid) recordSessionProcess(layout, child.pid);
  process.stdout.write(`Synthetic policy smoke artifacts: ${root}\n`);
  const deadline = Date.now() + timeoutMs;
  let result;
  try {
    while (!fs.existsSync(resultFile)) {
      if (spawnError) throw spawnError;
      if (exited) throw new Error(`Code-OSS exited before smoke completion; see ${root}/editor.log`);
      if (Date.now() >= deadline) throw new Error(`Code-OSS smoke timed out; see ${root}/editor.log`);
      await pause(250);
    }
    result = JSON.parse(fs.readFileSync(resultFile, 'utf8'));
    result.checks.cliInstall = cliResult;
    result.app = app; result.artifacts = root;
    write(resultFile, result);
    assert.equal(result.ok, true, result.error);
    return result;
  } finally {
    shutdown.abort();
    let shutdownError;
    try {
      await completion;
      assert.equal(processAlive(child.pid), false, 'synthetic editor stopped');
      assert.equal(processGroupAlive(child.pid), false, 'synthetic editor children stopped');
      assert.equal(fs.existsSync(layout.root), false, 'shared session payload removed');
      assert.equal(fs.existsSync(path.join(layout.base, '.autograde-shared-lock.json')), false, 'shared session lock removed');
    } catch (error) { shutdownError = error; }
    const shutdownResult = {
      editorProcessesStopped: Boolean(child.pid || spawnError) && !processAlive(child.pid) && !processGroupAlive(child.pid),
      sharedSessionCleaned: !shutdownError && !fs.existsSync(layout.root) && !fs.existsSync(path.join(layout.base, '.autograde-shared-lock.json')),
      ...(shutdownError ? {error: shutdownError.message} : {}),
    };
    write(path.join(root, 'shutdown.json'), shutdownResult);
    if (result) {
      Object.assign(result.checks, shutdownResult);
      if (shutdownError) { result.ok = false; result.shutdownError = shutdownError.message; }
      write(resultFile, result);
    }
    if (shutdownError) throw shutdownError;
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  if (process.argv.length !== 3) {
    process.stderr.write('Usage: node ide/code-oss/check-extension-policy.mjs /path/to/Autograde IDE.app\n');
    process.exitCode = 2;
  } else {
    try { process.stdout.write(`${JSON.stringify(await checkExtensionPolicy(process.argv[2]), null, 2)}\n`); }
    catch (error) { process.stderr.write(`${error.stack || error}\n`); process.exitCode = 1; }
  }
}
