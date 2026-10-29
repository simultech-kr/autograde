import { createSeatReporter } from './seat-reporter.mjs';
// Self-contained Mac app runtime. No Git, npm, checkout or system Node dependency.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { loadMachineDiscoveryProfile, machineDiscoveryPath } from './machine-discovery-settings.mjs';
import { learnerEnvironment, verifyRestrictedLaunch } from './learning-profile.mjs';
import { sharedSessionLayout, prepareSharedSession, markSessionLaunching, recordSessionProcess,
  cleanupSharedSession, superviseSharedChild } from './shared-session.mjs';

export function cleanLaunchEnvironment(inherited = process.env) {
  return Object.fromEntries(Object.entries(inherited).filter(([key]) =>
    !/^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_|DYLD_)/.test(key)));
}

function contained(resources, relative, kind) {
  if (typeof relative !== 'string' || !relative || path.isAbsolute(relative) ||
      relative.split('/').some(part => !part || part === '.' || part === '..')) {
    throw new Error('앱 구성 경로가 올바르지 않습니다. 앱을 다시 설치해 주세요.');
  }
  let current = resources;
  for (const component of relative.split('/')) {
    current = path.join(current, component);
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error('앱 실행 경로가 외부 파일에 연결되어 있습니다. 앱을 다시 설치해 주세요.');
  }
  const stat = fs.lstatSync(current);
  if (kind === 'directory' ? !stat.isDirectory() : !stat.isFile()) throw new Error('앱 구성 파일이 올바르지 않습니다. 앱을 다시 설치해 주세요.');
  return current;
}

export function loadMacBundle(resources, { platform = process.platform, arch = process.arch } = {}) {
  if (platform !== 'darwin') throw new Error('이 앱은 macOS용입니다.');
  resources = fs.realpathSync(resources);
  const file = contained(resources, 'autograde/bundle.json', 'file');
  const manifest = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (manifest.schema !== 1 || manifest.arch !== arch || !['arm64', 'x64'].includes(manifest.arch)) {
    throw new Error('이 Mac의 프로세서와 앱이 맞지 않습니다. Apple Silicon 또는 Intel용 앱을 확인해 주세요.');
  }
  if (!/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(manifest.version ?? '') ||
      !['standard', 'restricted'].includes(manifest.policy) ||
      !Array.isArray(manifest.approvedBuiltins) || !manifest.approvedBuiltins.length ||
      manifest.approvedBuiltins.some(name => typeof name !== 'string' || !/^[\w.-]+$/.test(name) || name === '.' || name === '..')) {
    throw new Error('앱 설정을 읽을 수 없습니다. 앱을 다시 설치해 주세요.');
  }
  const source = contained(resources, manifest.editorRoot, 'directory');
  const executable = contained(resources, manifest.executable, 'file');
  const node = contained(resources, manifest.node, 'file');
  fs.accessSync(executable, fs.constants.X_OK);
  fs.accessSync(node, fs.constants.X_OK);
  contained(resources, `${manifest.editorRoot}/out/main.js`, 'file');
  const defaultsPath = contained(resources, 'autograde/runtime-settings.json', 'file');
  const defaults = JSON.parse(fs.readFileSync(defaultsPath, 'utf8'));
  if (!defaults || typeof defaults !== 'object' || Array.isArray(defaults)) throw new Error('앱 기본 설정이 올바르지 않습니다.');
  return { resources, manifest, source, executable, defaults };
}

export function macRuntimeBase(uid = process.getuid()) {
  if (!Number.isSafeInteger(uid) || uid < 0) throw new Error('운영체제 사용자 정보를 확인할 수 없습니다.');
  // macOS user-data Unix sockets have a 103-byte path limit. Keep a stable,
  // short base so a later launch can recover a stopped, owned session.
  return `/private/tmp/ag-lab-${uid}`;
}

export function macLaunchSpec(bundle, layout, inherited = process.env) {
  const socket = path.join(layout.runtime, 'user-data', `${bundle.manifest.version.slice(0, 4)}-shared.sock`);
  if (Buffer.byteLength(socket) >= 103) throw new Error('실습 임시 폴더 경로가 너무 깁니다. 담당자에게 알려 주세요.');
  const temp = path.join(layout.root, 'tmp');
  return { executable: bundle.executable,
    args: [bundle.source, '--user-data-dir', path.join(layout.runtime, 'user-data'),
      '--extensions-dir', path.join(layout.runtime, 'extensions'), '--new-window',
      '--shared-data-dir', path.join(layout.runtime, 'shared-data'),
      '--disable-extension=GitHub.copilot', '--disable-extension=GitHub.copilot-chat',
      '--disable-extension=vscode.vscode-api-tests', layout.workspace],
    options: { cwd: bundle.source, detached: true, shell: false, stdio: 'ignore',
      env: { ...learnerEnvironment(layout, cleanLaunchEnvironment(inherited)),
        TMPDIR: temp, TMP: temp, TEMP: temp, VSCODE_DEV: '1', VSCODE_CLI: '1', NODE_ENV: 'development' } },
  };
}

export async function runMacSession(bundle, { runtimeBase = macRuntimeBase(), inherited = process.env,
  start = spawn, report = () => {}, discoveryProfilePath = machineDiscoveryPath() } = {}) {
  // Public machine choices live outside the disposable student seat and the
  // signed bundle. Never accept a profile path from inherited environment.
  const discovery = loadMachineDiscoveryProfile(discoveryProfilePath);
  let layout = sharedSessionLayout(runtimeBase, 'shared-mac-seat', bundle.manifest.policy);
  const spec = macLaunchSpec(bundle, layout, inherited);
  if (discovery) spec.options.env.AUTOGRADE_DISCOVERY_PROFILE_PATH = discovery.path;
  layout = prepareSharedSession(layout, {...bundle.defaults, 'autograde.discovery.enabled': true, ...discovery?.settings});
  try {
    // Do not silently start a student next to retained data from an older mode.
    if (layout.preservedLegacyProfiles) throw new Error('이전 개인 실습 폴더가 남아 있습니다. 담당자가 보관 위치를 정리한 뒤 다시 시작해 주세요.');
    verifyRestrictedLaunch(layout, bundle.source, new Set(bundle.manifest.approvedBuiltins));
    fs.mkdirSync(path.join(layout.root, 'tmp'), { mode: 0o700 });
  } catch (error) { cleanupSharedSession(layout); throw error; }
  markSessionLaunching(layout);
  let child;
  try { child = start(spec.executable, spec.args, spec.options); }
  catch (error) { cleanupSharedSession(layout, { spawnFailed: true }); throw error; }
  const completion = superviseSharedChild(child, layout);
  // Attach the rejection handler before recording the process, so startup errors
  // cannot abandon the live supervisor and its cleanup obligation.
  let recordingError;
  try { if (child.pid) recordSessionProcess(layout, child.pid); }
  catch (error) { recordingError = error; child.kill('SIGTERM'); }
  if (!recordingError && child.pid) report({ event: 'ide-started', pid: child.pid });
  const result = await completion;
  if (recordingError) throw recordingError;
  report({ event: 'session-cleaned' });
  return result;
}

export function macFailureMessage(error) {
  const reason = String(error?.message ?? error);
  if (/active|still running|still alive|still active|concurrent/i.test(reason)) {
    return '이 Mac에서 이전 실습이 아직 실행 중입니다. 기존 Autograde IDE에서 공용 실습 종료를 선택한 뒤 다시 열어 주세요.';
  }
  if (/ownership|owner|lease|\block\b|interrupted|cleanup|private \(0700\)|symlink|permissions|EACCES|EPERM/i.test(reason)) {
    return '이전 실습의 종료 또는 임시 파일 정리를 확인할 수 없습니다. 학생 정보 보호를 위해 새 실습을 시작하지 않았습니다. 담당자에게 확인을 요청해 주세요.';
  }
  if (/ENOENT|ENOEXEC|EFTYPE|spawn/i.test(reason)) return '필요한 앱 실행 파일이 없거나 실행할 수 없습니다. 앱 전체를 다시 복사해 주세요.';
  // Application-generated localized errors are safe to show. Never forward
  // arbitrary child logs, credentials or student source into a persistent log.
  if (/[가-힣]/.test(reason)) return reason.slice(0, 400);
  return 'Autograde IDE를 시작하거나 실습을 정리하지 못했습니다. 담당자에게 앱 설치와 이전 실습 종료 상태를 확인해 달라고 요청해 주세요.';
}

export function isMacLauncherEntry(argument, moduleURL = import.meta.url) {
  if (!argument) return false;
  try {
    // Finder can use /tmp instead of /private/tmp and decomposed Unicode names.
    // Node canonicalizes import.meta.url independently; text equality is unsafe.
    const invoked = fs.statSync(argument), module = fs.statSync(fileURLToPath(moduleURL));
    return invoked.isFile() && invoked.dev === module.dev && invoked.ino === module.ino;
  } catch { return false; }
}

if (isMacLauncherEntry(process.argv[1])) {
  let seatReporter;
  const report = event => { seatReporter?.event(event); process.stdout.write(`${JSON.stringify(event)}\n`); };
  try {
    const bundle = loadMacBundle(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'));
    seatReporter = createSeatReporter({ version: bundle.manifest.version, extensionVersion: bundle.manifest.extensionVersion ?? bundle.manifest.version });
    const result = await runMacSession(bundle, { report });
    // A crashed IDE is still cleaned, but the student needs a visible notice.
    if (result.code) throw new Error('IDE가 정상적으로 종료되지 않았습니다. 임시 실습 파일은 정리했습니다. 서버에 제출한 코드와 직접 내보낸 파일을 확인해 주세요.');
  } catch (error) { report({ event: 'error', message: macFailureMessage(error) }); process.exitCode = 1; }
  await seatReporter?.close();
}
