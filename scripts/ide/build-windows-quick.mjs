#!/usr/bin/env node
// Local Windows iteration: verified Code-OSS reuse, fresh extension/launcher, no core compile.
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import {execFileSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {lockBuildDirectories} from './build-resume.mjs';
import {inputFingerprint} from './build-inputs.mjs';
import {assertBuildHost, readBuildSource, sha256} from './build-runtime.mjs';
import {validateBaseline} from './windows-quick-baseline.mjs';
import {discoverWindowsBuilds, newQuickOutput} from './windows-quick-discovery.mjs';
import {prepare, verify, verifyCoreOverlay} from '../../ide/code-oss/manage.mjs';
import {packageWindows, windowsWslInstallerFiles, windowsWslSupport} from '../../ide/code-oss/package-windows.mjs';

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const usage = 'Usage: build-windows-quick.mjs [--source EXISTING_CODE_OSS] [--build-output COMPLETED_BUILD_OUTPUT] [--output NEW_DIRECTORY] [--archive]';
const exists = file => {try {return fs.lstatSync(file);} catch (error) {if (error.code === 'ENOENT') return undefined; throw error;}};
const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));

export function parseQuickOptions(args) {
  const options = {archive: false}, seen = new Set();
  for (let index = 0; index < args.length; index++) {
    const flag = args[index];
    if (seen.has(flag)) throw new Error(usage);
    seen.add(flag);
    if (flag === '--archive') {options.archive = true; continue;}
    const key = {'--source':'source', '--build-output':'buildOutput', '--output':'output'}[flag];
    if (!key || !args[index + 1] || args[index + 1].startsWith('--')) throw new Error(usage);
    options[key] = args[++index];
  }
  return options;
}

function assertNewOutput(output) {
  for (const file of [output, `${output}.zip`, `${output}.zip.sha256`]) {
    if (exists(file)) throw new Error(`Quick build requires a new output; existing files are preserved: ${file}`);
  }
}

// Discovery only locates records. Reuse still requires the ordinary locks and
// complete artifact/runtime validation before any extension or launcher work.
function selectBaseline(options, {root, host, pin, dependencies}) {
  const {candidates, rejected} = discoverWindowsBuilds({repository:root, host, pin,
    source:options.source, buildOutput:options.buildOutput});
  const failures = [...rejected];
  const output = options.output === undefined ? newQuickOutput(root) : path.resolve(options.output);
  assertNewOutput(output);
  for (const candidate of candidates) {
    for (const [name, value] of Object.entries({source:candidate.source, buildOutput:candidate.buildOutput})) {
      const info = exists(value);
      if (!info?.isDirectory() || info.isSymbolicLink()) throw new Error(`Existing full-build ${name} directory is required. Run build-runtime.mjs --resume first.`);
    }
    // A lock conflict or unsafe output is not a reason to silently use another
    // source tree. Only an unusable automatically selected baseline is skipped.
    const locks = lockBuildDirectories([candidate.source, candidate.buildOutput, output]);
    const [source, buildOutput, destination] = locks.directories;
    try { assertNewOutput(destination); }
    catch (error) { locks.close(); throw error; }
    try {
      console.log(`[quick] Verifying full build: ${buildOutput}`);
      const started = Date.now();
      const baseline = (dependencies.validateBaseline ?? validateBaseline)({repository:root, source, buildOutput, pin, host});
      (dependencies.verifyCoreOverlay ?? verifyCoreOverlay)(source);
      console.log(`[quick] Baseline verified in ${Date.now() - started} ms`);
      console.log(`[quick] Source: ${source}\n[quick] Output: ${destination}`);
      if (failures.length) console.log(`[quick] Skipped ${failures.length} unusable build record(s).`);
      return {locks, baseline, source, buildOutput, destination};
    } catch (error) {
      locks.close();
      if (options.buildOutput !== undefined) throw error;
      failures.push({directory:buildOutput, reason:error.message});
      console.warn(`[quick] Skipping ${buildOutput}: ${error.message}`);
    }
  }
  const details = failures.slice(0, 6).map(item => `\n- ${item.directory}: ${item.reason}`).join('');
  throw new Error(`No reusable Windows full build found under this repository's dist, build or .cache directories.${details}\nComplete build-runtime.mjs --target windows-x64 first, or resume the previous full build with --resume. Use --build-output COMPLETED_BUILD_OUTPUT to select a build outside these locations.`);
}

export const extensionChecks = [
  'helpers', 'filesystemFixtures', 'learningEnvironment', 'learningExport', 'learningExportView',
  'sharedLearningSession', 'learningCheckpoint', 'managedExtensions', 'managedExtensionsApi', 'managedExtensionsView',
].map(name => `out/test/${name}.test.js`);

function verifyWslPackage(payload, manifest) {
  const expected = windowsWslSupport(), actual = manifest.wsl;
  if (!actual || actual.launcher !== expected.launcher || actual.installer !== expected.installer
      || actual.runtime !== expected.runtime || !Array.isArray(actual.requires)
      || actual.requires.length !== expected.requires.length
      || new Set(actual.requires).size !== expected.requires.length
      || !expected.requires.every(requirement => actual.requires.includes(requirement))) {
    throw new Error('Missing or invalid required Windows WSL2 launcher metadata');
  }
  const directory = path.join(payload, 'wsl'), info = exists(directory);
  if (!info?.isDirectory() || info.isSymbolicLink()) throw new Error('Required WSL2 package directory must be a real directory');
  for (const filename of ['Autograde IDE.exe', 'Autograde Launcher Checks.exe', 'launcher-version.json', ...windowsWslInstallerFiles]) {
    const file = path.join(directory, filename), stat = exists(file);
    if (!stat?.isFile() || stat.isSymbolicLink() || stat.nlink !== 1 || stat.size < 1) {
      throw new Error(`Missing or invalid required WSL2 package file: ${filename}`);
    }
  }
  const version = read(path.join(directory, 'launcher-version.json'));
  if (version?.schema !== 1 || version.extensionVersion !== manifest.extensionVersion
      || version.assemblyVersion !== `${manifest.extensionVersion}.0`) {
    throw new Error('Required WSL2 launcher version does not match the packaged extension');
  }
  // packageWindows already invokes Build-Launcher.ps1 -RunChecks. This stage
  // verifies that its checked launcher and installer actually reached the payload.
  return {...expected, launcherIncluded:true, runtimeIncluded:false};
}

/** Dependencies are injectable for filesystem/process integration tests; CLI exposes no bypass flags. */
export function buildWindowsQuick(options, dependencies = {}) {
  const root = dependencies.repository ?? repository;
  const host = dependencies.host ?? {hostname: os.hostname(), platform: process.platform, architecture: process.arch, node: process.versions.node};
  const node = dependencies.node ?? fs.realpathSync(process.execPath);
  const pin = read(path.join(root, 'ide/code-oss/upstream.json'));
  assertBuildHost('windows-x64', pin, {platform: host.platform, arch: host.architecture, versions: {node: host.node}});
  if (typeof options.archive !== 'boolean') throw new Error('Archive option must be a boolean');
  for (const name of ['source', 'buildOutput', 'output']) {
    if (options[name] !== undefined && (typeof options[name] !== 'string' || !options[name])) throw new Error(usage);
  }
  const {locks, baseline, source, destination} = selectBaseline(options, {root, host, pin, dependencies});
  let attempt;
  try {
    const provenance = readBuildSource(root, {local: true});
    const fingerprints = () => Object.fromEntries(['extensions/vscode', 'ide/code-oss', 'scripts/ide'].map(directory => [directory, inputFingerprint(root, [directory])]));
    const inputs = fingerprints(), stages = [];
    const env = Object.fromEntries(Object.entries(process.env).filter(([key]) => !/^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_)/i.test(key)));
    const run = dependencies.run ?? ((command, args, cwd, extra = {}) => execFileSync(command, args, {cwd, env, stdio:'inherit', ...extra}));
    const stage = (name, action) => {
      const started = Date.now();
      console.log(`[quick] ${name}`);
      const value = action();
      stages.push({name, result:'passed', milliseconds:Date.now() - started});
      return value;
    };
    console.log('[quick] Reusing verified Code-OSS, native dependencies and Electron. No npm ci or Code-OSS compile.');
    stage('autograde-extension-compile-and-prepare', () => (dependencies.prepare ?? prepare)(source));
    stage('extension-platform-tests', () => run(node, ['--test', ...extensionChecks], path.join(root, 'extensions/vscode')));
    stage('launcher-package-tests', () => run(node, ['--test', 'ide/code-oss/package-windows.test.mjs', 'ide/code-oss/windows-native-bootstrap.test.mjs'], root));
    stage('overlay-verification', () => (dependencies.verify ?? verify)(source));
    attempt = fs.mkdtempSync(path.join(path.dirname(destination), `.${path.basename(destination)}.quick-attempt-`));
    const payload = path.join(attempt, path.basename(destination));
    stage('windows-package-and-native-lifecycle-checks', () => (dependencies.packageWindows ?? packageWindows)({source, node, output:payload, archive:false}));
    const manifest = read(path.join(payload, 'windows-package.json'));
    const wsl = stage('windows-wsl2-launcher-checks', () => verifyWslPackage(payload, manifest));
    const executable = manifest.executable;
    if (typeof executable !== 'string' || !/^editor\/electron\/[^/\\:]+\.exe$/.test(executable)) throw new Error('Invalid packaged Windows executable');
    const app = path.join(payload, 'editor/app');
    stage('packaged-cli-version', () => run(path.join(payload, executable), [path.join(app, 'out/cli.js'), app, '--version'], app,
      {env: {...env, ELECTRON_RUN_AS_NODE:'1'}}));
    stage('reused-runtime-verification', () => baseline.verifyCore());
    if (JSON.stringify(fingerprints()) !== JSON.stringify(inputs)
        || JSON.stringify(readBuildSource(root, {local:true})) !== JSON.stringify(provenance)) {
      throw new Error('Autograde inputs changed during the quick build; retry with a new output');
    }
    // This report travels with the package. Keep build PC names and private paths
    // only in the local baseline proof, never in the distributable folder/ZIP.
    const publicBaseline = Object.fromEntries(['reportSha256', 'checkpointSha256', 'sourceRevision',
      'sourceDirty', 'buildMode', 'upstream', 'artifact'].map(key => [key, baseline.baseline[key]]));
    const report = {schema:'autograde-windows-quick-build-v1', status:'local-fast-build', buildMode:'local-fast',
      productionReady:false, sourceRevision:provenance.sourceRevision, sourceDirty:provenance.sourceDirty,
      host:{platform:host.platform, architecture:host.architecture, node:host.node}, baseline:publicBaseline,
      extensionVersion:manifest.extensionVersion, checks:stages, wsl,
      createdAt:new Date().toISOString(), acceptance:{institution:'not-tested', windowsLab:'not-tested', windowsWsl2:'not-tested'},
      packaging:options.archive ? 'folder-and-zip' : 'folder'};
    fs.writeFileSync(path.join(payload, 'quick-build.json'), `${JSON.stringify(report, null, 2)}\n`, {flag:'wx'});
    let archiveHash;
    if (options.archive) {
      // Archive only the verified payload; temporary attempts and private build state stay outside it.
      run('tar.exe', ['-a', '-cf', `${payload}.zip`, '-C', attempt, path.basename(payload)], root);
      const archiveInfo = fs.lstatSync(`${payload}.zip`);
      if (!archiveInfo.isFile() || archiveInfo.isSymbolicLink() || archiveInfo.nlink !== 1) throw new Error('Invalid quick-build archive');
      archiveHash = sha256(`${payload}.zip`);
    }
    // Nothing is published until all checks succeed. Failed attempts remain for diagnosis.
    if ([destination, `${destination}.zip`, `${destination}.zip.sha256`].some(file => exists(file))) throw new Error('Quick output changed before publication');
    if (options.archive) {
      fs.copyFileSync(`${payload}.zip`, `${destination}.zip`, fs.constants.COPYFILE_EXCL);
      fs.writeFileSync(`${destination}.zip.sha256`, `${archiveHash}  ${path.basename(destination)}.zip\n`, {flag:'wx'});
    }
    fs.renameSync(payload, destination);
    fs.rmSync(attempt, {recursive:true});
    attempt = undefined;
    console.log(`[quick] Ready: ${path.join(destination, 'Autograde IDE.exe')}`);
    return {...report, directory:destination, archive:options.archive ? `${destination}.zip` : null};
  } catch (error) {
    if (attempt) console.error(`[quick] Failed staging files preserved at: ${attempt}`);
    throw error;
  } finally { locks.close(); }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {console.log(JSON.stringify(buildWindowsQuick(parseQuickOptions(process.argv.slice(2))), null, 2));}
  catch (error) {console.error(error.stack || error); process.exitCode = 1;}
}
