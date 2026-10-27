#!/usr/bin/env node
// Verify independent native build outputs before creating a candidate index.
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { sha256 } from './build-runtime.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../..');
const read = file => JSON.parse(fs.readFileSync(file, 'utf8'));
const targets = {
  'macos-arm64': { platform: 'darwin', architecture: 'arm64', artifacts: ['macos-native'], checks: ['upstream-commit-and-node', 'teacher-only-policy-overlay', 'code-oss-client-compile', 'native-electron-download', 'overlay-verification', 'package-macos', 'macos-policy-smoke'] },
  'linux-x64': { platform: 'linux', architecture: 'x64', artifacts: ['linux-native'], checks: ['upstream-commit-and-node', 'teacher-only-policy-overlay', 'code-oss-client-compile', 'native-electron-download', 'overlay-verification', 'package-linux', 'linux-packaged-cli-version'] },
  'windows-x64': { platform: 'win32', architecture: 'x64', artifacts: ['windows-native'], checks: ['upstream-commit-and-node', 'teacher-only-policy-overlay', 'code-oss-client-compile', 'native-electron-download', 'overlay-verification', 'package-windows', 'windows-packaged-cli-version', 'windows-native-job-acl-msvc-checks'] },
  'windows-launcher': { platform: 'win32', architecture: 'x64', artifacts: ['windows-wsl2-launcher'], checks: ['windows-launcher-compile', 'windows-launcher-self-tests', 'windows-launcher-version'] },
};
for (const target of ['macos-arm64', 'linux-x64', 'windows-x64']) targets[target].checks.push(
  'code-oss-source-typecheck', 'code-oss-extension-compile', 'code-oss-extension-media', 'code-oss-monaco-typecheck');

function walk(directory) {
  return fs.readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const file = path.join(directory, entry.name);
    if (entry.isSymbolicLink()) throw new Error('Artifact input must not contain symbolic links');
    return entry.isDirectory() ? walk(file) : [file];
  });
}

export function verifyReports(input, expected) {
  const reports = walk(input).filter(file => /^build-(macos-arm64|linux-x64|windows-x64|windows-launcher)\.json$/.test(path.basename(file)));
  const seen = new Set(), filenames = new Set(), result = [];
  for (const file of reports) {
    const report = read(file), original = targets[report.buildTarget];
    const definition = original && { ...original, artifacts: [...original.artifacts], checks: [...original.checks] };
    if (report.buildTarget === 'linux-x64' && report.includeWsl === true) {
      definition.artifacts.push('windows-wsl2-payload'); definition.checks.push('package-wsl', 'wsl-packaged-cli-version');
    }
    if (report.schema !== 1 || report.status !== 'release-candidate' || !definition || seen.has(report.buildTarget)) throw new Error('Invalid or duplicate build report');
    if (report.sourceDirty !== false) throw new Error('Candidate provenance requires a clean committed source checkout');
    seen.add(report.buildTarget);
    if (report.sourceRevision !== expected.sourceRevision || report.extensionVersion !== expected.extensionVersion) throw new Error('Builds must use the same Autograde revision and extension version');
    for (const key of ['repository', 'tag', 'commit', 'node']) if (report.upstream?.[key] !== expected.upstream[key]) throw new Error(`Upstream ${key} mismatch`);
    if (report.host?.platform !== definition.platform || report.host?.architecture !== definition.architecture) throw new Error('Build report platform/architecture mismatch');
    if (report.acceptance?.productionReady !== false || report.acceptance?.institution !== 'not-tested' || report.acceptance?.windowsWsl2 !== 'not-tested') throw new Error('Build automation cannot certify institutional or WSL2 acceptance');
    for (const name of definition.checks) if (!report.checks?.some(check => check.name === name && check.result === 'passed')) throw new Error(`Missing required build check: ${name}`);
    if (!Array.isArray(report.artifacts) || report.artifacts.length !== definition.artifacts.length || new Set(report.artifacts.map(item => item.target)).size !== definition.artifacts.length) throw new Error('Missing or duplicate platform artifact');
    for (const item of report.artifacts) {
      if (!definition.artifacts.includes(item.target) || typeof item.file !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]+$/.test(item.file) || filenames.has(item.file)) throw new Error('Unsafe or duplicate artifact name');
      filenames.add(item.file);
      if (!item.file.includes(`-${expected.extensionVersion}-`)) throw new Error('Artifact filename does not match extension version');
      const archive = path.join(path.dirname(file), item.file);
      if (!fs.statSync(archive).isFile() || fs.statSync(archive).size !== item.bytes || !/^[a-f0-9]{64}$/.test(item.sha256) || sha256(archive) !== item.sha256) throw new Error(`Artifact size/hash mismatch: ${item.file}`);
      const checksum = fs.readFileSync(`${archive}.sha256`, 'utf8').trim();
      if (checksum !== `${item.sha256}  ${item.file}`) throw new Error(`Checksum sidecar mismatch: ${item.file}`);
      item.source = archive;
    }
    result.push({ file, report });
  }
  if (!['macos-arm64', 'linux-x64', 'windows-x64'].every(target => seen.has(target))) throw new Error('All three native platform build reports are required');
  const wslPayload = result.some(build => build.report.buildTarget === 'linux-x64' && build.report.includeWsl === true);
  if (wslPayload !== seen.has('windows-launcher')) throw new Error('Optional Windows WSL2 requires both the Linux payload and Windows launcher');
  return result;
}

export function finalize(input, output) {
  input = fs.realpathSync(input); output = path.resolve(output);
  if (fs.existsSync(output)) throw new Error('Choose a new candidate output directory');
  const expected = { upstream: read(path.join(root, 'ide/code-oss/upstream.json')),
    extensionVersion: read(path.join(root, 'extensions/vscode/package.json')).version,
    sourceRevision: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root, encoding: 'utf8' }).trim() };
  const builds = verifyReports(input, expected);
  const manifest = { schema: 1, product: 'Autograde IDE', status: 'release-candidate', ...expected,
    createdAt: new Date().toISOString(), run: { id: process.env.GITHUB_RUN_ID ?? null, attempt: process.env.GITHUB_RUN_ATTEMPT ?? null },
    distribution: 'workflow-artifact-repository-visibility', productionReady: false,
    acceptance: { institution: 'not-tested', windowsNative: 'not-tested', windowsWsl2: 'not-tested', linuxDesktop: 'not-tested', macosSigning: 'ad-hoc-only',
      requiredBeforeDeployment: ['Institution configuration and server/extension compatibility review', 'Real target desktop launch, teaching workflow and shared-session cleanup', 'Windows native MSVC/SDK and instructor-extension workflow acceptance', 'Optional WSL2/WSLg acceptance when that target is selected', 'Institution signing/notarization and software license review'] },
    windows: { default: 'windows-native', runtime: 'Windows x64 + operator-provisioned MSVC and Windows SDK',
      optional: builds.some(build => build.report.buildTarget === 'windows-launcher') ? ['windows-wsl2'] : [],
      optionalWslRequiredArtifactTargets: ['windows-wsl2-payload', 'windows-wsl2-launcher'] },
    builds: [], artifacts: [] };
  fs.mkdirSync(output, { recursive: true });
  for (const { file, report } of builds) {
    const reportName = path.basename(file);
    fs.copyFileSync(file, path.join(output, reportName), fs.constants.COPYFILE_EXCL);
    manifest.builds.push({ target: report.buildTarget, report: reportName, sha256: sha256(file), checks: report.checks, host: report.host });
    for (const { source, ...item } of report.artifacts) {
      fs.copyFileSync(source, path.join(output, item.file), fs.constants.COPYFILE_EXCL);
      fs.copyFileSync(`${source}.sha256`, path.join(output, `${item.file}.sha256`), fs.constants.COPYFILE_EXCL);
      manifest.artifacts.push(item);
    }
  }
  fs.writeFileSync(path.join(output, 'artifact-manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`, { flag: 'wx' });
  const sums = fs.readdirSync(output).filter(name => !name.endsWith('.sha256')).sort().map(name => `${sha256(path.join(output, name))}  ${name}\n`).join('');
  fs.writeFileSync(path.join(output, 'SHA256SUMS'), sums, { flag: 'wx' });
  return manifest;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  try {
    if (process.argv.length !== 4) throw new Error('Usage: finalize-artifacts.mjs INPUT_DIRECTORY NEW_OUTPUT_DIRECTORY');
    console.log(JSON.stringify(finalize(process.argv[2], process.argv[3]), null, 2));
  } catch (error) { console.error(error.stack || error); process.exitCode = 1; }
}

export { targets as buildTargets };
