import { createSeatReporter } from './seat-reporter.mjs';
// Native Linux desktop entry point, executed through bundled Linux Node.
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import { learnerEnvironment, verifyRestrictedLaunch } from './learning-profile.mjs';
import { sharedSessionLayout, prepareSharedSession, markSessionLaunching, recordSessionProcess,
  cleanupSharedSession, superviseSharedChild } from './shared-session.mjs';

export class LinuxLaunchError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const fail = (code, message) => { throw new LinuxLaunchError(code, message); };
const linuxFilesystems = new Set([0xef53, 0x9123683e, 0x58465342, 0x01021994, 0x794c7630, 0xf2f52010, 0x2fc12fc1]);

export function assertNativeLinuxStorage(target, statfs = fs.statfsSync) {
  const canonical = fs.realpathSync(target);
  if (!linuxFilesystems.has(Number(statfs(canonical).type) >>> 0)) {
    fail('unsafe_install', '앱과 임시 실습 폴더는 로컬 Linux 파일 시스템에 설치해야 합니다. 네트워크·Windows 공유 폴더에서는 실행하지 않습니다.');
  }
  return canonical;
}

function contained(root, relative, kind = 'file') {
  if (typeof relative !== 'string' || !relative || path.isAbsolute(relative) || /[\\\x00-\x1f\x7f]/.test(relative) ||
      relative.split('/').some(part => !part || part === '.' || part === '..')) {
    fail('bundle_invalid', '앱 구성 경로가 올바르지 않습니다. 담당자에게 앱 재설치를 요청해 주세요.');
  }
  let target = root;
  for (const component of relative.split('/')) {
    target = path.join(target, component);
    if (fs.lstatSync(target).isSymbolicLink()) fail('bundle_invalid', '앱 실행 파일이 다른 경로에 연결되어 있습니다. 앱을 다시 설치해 주세요.');
  }
  const stat = fs.lstatSync(target);
  if (kind === 'directory' ? !stat.isDirectory() : !stat.isFile()) fail('bundle_invalid', '앱 구성 파일을 확인할 수 없습니다.');
  return target;
}

export function loadLinuxBundle(root, { platform = process.platform, arch = process.arch,
  nodeVersion = process.versions.node, statfs = fs.statfsSync } = {}) {
  if (platform !== 'linux') fail('linux_required', 'Linux용 앱은 Linux 데스크톱에서 실행해 주세요.');
  root = assertNativeLinuxStorage(root, statfs);
  const manifest = JSON.parse(fs.readFileSync(contained(root, 'linux-package.json'), 'utf8'));
  if (manifest.schema !== 1 || manifest.platform !== 'linux' || manifest.distribution !== 'linux-desktop-shared-lab' || !['x64', 'arm64'].includes(manifest.arch) ||
      manifest.arch !== arch || manifest.nodeVersion !== nodeVersion || manifest.lifecycle !== 'shared' ||
      !/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(manifest.version ?? '') ||
      !['standard', 'restricted'].includes(manifest.policy) ||
      !Array.isArray(manifest.approvedBuiltins) || !manifest.approvedBuiltins.includes('autograde') || !manifest.approvedBuiltins.includes('autograde-ide-profile') ||
      manifest.approvedBuiltins.some(name => typeof name !== 'string' || !/^[\w-][\w.-]*$/.test(name))) {
    fail('bundle_invalid', 'Linux용 앱의 버전·프로세서 또는 공용 실습 설정이 맞지 않습니다. 담당자에게 맞는 배포본을 요청해 주세요.');
  }
  const source = contained(root, manifest.editorRoot, 'directory');
  const executable = contained(root, manifest.executable);
  const node = contained(root, manifest.node);
  fs.accessSync(executable, fs.constants.X_OK); fs.accessSync(node, fs.constants.X_OK);
  contained(root, `${manifest.editorRoot}/out/main.js`);
  const defaults = JSON.parse(fs.readFileSync(contained(root, 'autograde/runtime-settings.json'), 'utf8'));
  if (!defaults || typeof defaults !== 'object' || Array.isArray(defaults)) fail('bundle_invalid', '앱 기본 설정이 올바르지 않습니다.');
  return { root, manifest, source, executable, defaults };
}

// Start from the desktop essentials, excluding inherited tokens, Node injection,
// shell startup scripts, IDE profiles, tool histories and language startup hooks.
export function cleanLinuxEnvironment(inherited = process.env) {
  const allowed = /^(?:PATH|HOME|USER|LOGNAME|SHELL|LANG|LANGUAGE|LC_[A-Z_]+|TERM|COLORTERM|TZ|DISPLAY|WAYLAND_DISPLAY|XAUTHORITY|XDG_RUNTIME_DIR|XDG_SESSION_TYPE|XDG_CURRENT_DESKTOP|XDG_SESSION_DESKTOP|DESKTOP_SESSION|XDG_CONFIG_DIRS|XDG_DATA_DIRS|DBUS_SESSION_BUS_ADDRESS|GTK_IM_MODULE|QT_IM_MODULE|XMODIFIERS|PULSE_SERVER|PIPEWIRE_REMOTE)$/;
  return Object.fromEntries(Object.entries(inherited).filter(([name]) => allowed.test(name)));
}

function isLocalSocket(file) {
  try { return fs.statSync(file).isSocket(); } catch { return false; }
}
function privateRuntimeDirectory(folder, uid) {
  try {
    const stat = fs.lstatSync(folder);
    return stat.isDirectory() && !stat.isSymbolicLink() && stat.uid === uid && !(stat.mode & 0o077);
  } catch { return false; }
}

export function inspectLinuxEnvironment({ platform = process.platform, release = os.release(), uid = process.getuid?.(),
  env = process.env, socketExists = isLocalSocket, runtimeDirectory = privateRuntimeDirectory } = {}) {
  if (platform !== 'linux' || /microsoft/i.test(release)) fail('linux_required', '이 배포본은 Linux 데스크톱용입니다. Windows에서는 WSL2용 실행 아이콘을 사용해 주세요.');
  if (!Number.isSafeInteger(uid) || uid <= 0) fail('root_user', '관리자(root) 대신 실습용 일반 Linux 사용자로 실행해야 합니다.');
  if (typeof env.XDG_RUNTIME_DIR === 'string' && path.isAbsolute(env.XDG_RUNTIME_DIR) &&
      runtimeDirectory(env.XDG_RUNTIME_DIR, uid) &&
      /^[\w.-]+$/.test(env.WAYLAND_DISPLAY ?? '') && !['.', '..'].includes(env.WAYLAND_DISPLAY) &&
      socketExists(path.join(env.XDG_RUNTIME_DIR, env.WAYLAND_DISPLAY))) return { gui: 'wayland', uid };
  const display = /^:(\d{1,4})(?:\.\d+)?$/.exec(env.DISPLAY ?? '');
  if (display && socketExists(`/tmp/.X11-unix/X${display[1]}`)) return { gui: 'x11', uid };
  fail('gui_unavailable', '로컬 Linux 그래픽 세션에 연결할 수 없습니다. X11 또는 Wayland 데스크톱에 로그인한 뒤 실행해 주세요.');
}

export function checkLinuxLibraries(bundle, { env = process.env, probe = spawnSync } = {}) {
  const checked = probe('/usr/bin/ldd', [bundle.executable], {
    encoding: 'utf8', timeout: 10000, maxBuffer: 65536, env: { ...cleanLinuxEnvironment(env), LC_ALL: 'C' }, shell: false,
  });
  if (checked.error || checked.status !== 0 || /\bnot found\b/.test(`${checked.stdout ?? ''} ${checked.stderr ?? ''}`)) {
    fail('libraries_unavailable', '편집기에 필요한 Linux 시스템 라이브러리를 확인할 수 없습니다. 담당자가 배포본에 맞는 Electron 런타임 라이브러리를 설치해야 합니다.');
  }
  return { libraries: 'available' };
}

function secureSandboxHelper(file) {
  try {
    const stat = fs.lstatSync(file);
    if (!stat.isFile() || stat.uid !== 0 || (stat.mode & 0o7777) !== 0o4755) return false;
    let parent = path.dirname(file);
    while (true) {
      const info = fs.lstatSync(parent);
      if (!info.isDirectory() || info.uid !== 0 || (info.mode & 0o022)) return false;
      if (parent === path.dirname(parent)) break;
      parent = path.dirname(parent);
    }
    return true;
  } catch { return false; }
}

export function checkLinuxSandbox(bundle, { env = process.env, probe = spawnSync, helperTrusted = secureSandboxHelper } = {}) {
  const helper = path.join(path.dirname(bundle.executable), 'chrome-sandbox');
  if (helperTrusted(helper)) return { sandbox: 'setuid-helper', helper };
  // Probe without changing the host's AppArmor/user-namespace policy. This is a
  // prerequisite check, not proof that Chromium will pass every host policy.
  const checked = probe('/usr/bin/unshare', ['--user', '--map-root-user', '/usr/bin/true'], {
    encoding: 'utf8', timeout: 5000, maxBuffer: 4096, stdio: 'ignore', env: cleanLinuxEnvironment(env),
  });
  if (checked.status === 0 && !checked.error) return { sandbox: 'user-namespace' };
  fail('sandbox_unavailable', '편집기의 보안 실행 환경을 사용할 수 없습니다. 담당자가 Linux 사용자 네임스페이스 또는 관리되는 Chromium sandbox를 준비해야 합니다. 보안 기능을 끄고 실행하지 않습니다.');
}

export function preflightLinux(bundle, { environment = inspectLinuxEnvironment(), sandbox = checkLinuxSandbox(bundle), libraries = checkLinuxLibraries(bundle) } = {}) {
  return { event: 'ready', schema: 1, platform: 'linux-desktop', architecture: bundle.manifest.arch,
    extensionVersion: bundle.manifest.extensionVersion, ...environment, sandbox: sandbox.sandbox,
    lifetime: 'shared', noSandbox: false, ...libraries };
}

export function linuxRuntimeBase(uid = process.getuid()) {
  if (!Number.isSafeInteger(uid) || uid <= 0) fail('root_user', '일반 실습용 Linux 사용자로 실행해야 합니다.');
  return `/tmp/ag-linux-lab-${uid}`;
}

export function linuxLaunchSpec(bundle, layout, environment, sandbox, inherited = process.env) {
  if (Buffer.byteLength(path.join(layout.runtime, 'user-data', `${bundle.manifest.version.slice(0, 4)}-shared.sock`)) >= 107) {
    fail('unsafe_install', '임시 실습 폴더 경로가 너무 깁니다. 담당자에게 경로 확인을 요청해 주세요.');
  }
  if (!['x11', 'wayland'].includes(environment.gui)) fail('gui_unavailable', 'Linux 데스크톱 그래픽 환경을 확인할 수 없습니다.');
  const temp = path.join(layout.root, 'tmp');
  const home = path.join(layout.root, 'home');
  const desktopEnv = cleanLinuxEnvironment(inherited);
  // X11 normally finds ~/.Xauthority; HOME below is disposable. Keep the
  // original authority path so changing HOME does not break authenticated X11.
  if (!desktopEnv.XAUTHORITY && inherited.HOME && path.isAbsolute(inherited.HOME)) {
    const authority = path.join(inherited.HOME, '.Xauthority');
    if (fs.existsSync(authority)) desktopEnv.XAUTHORITY = authority;
  }
  const env = { ...learnerEnvironment(layout, desktopEnv),
    TMPDIR: temp, TMP: temp, TEMP: temp,
    HOME: home, ZDOTDIR: home, HISTFILE: path.join(home, '.shell_history'),
    PYTHON_HISTORY: path.join(home, '.python_history'), PYTHONHISTFILE: path.join(home, '.python_history'),
    LESSHISTFILE: path.join(home, '.lesshst'),
    XDG_CONFIG_HOME: path.join(layout.root, 'config'), XDG_CACHE_HOME: path.join(layout.root, 'cache'),
    XDG_DATA_HOME: path.join(layout.root, 'data'), XDG_STATE_HOME: path.join(layout.root, 'state'),
    VSCODE_DEV: '1', VSCODE_CLI: '1', NODE_ENV: 'development',
  };
  if (sandbox.sandbox === 'setuid-helper') env.CHROME_DEVEL_SANDBOX = sandbox.helper;
  return { executable: bundle.executable,
    args: [bundle.source, '--user-data-dir', path.join(layout.runtime, 'user-data'),
      '--extensions-dir', path.join(layout.runtime, 'extensions'), '--new-window',
      '--shared-data-dir', path.join(layout.runtime, 'shared-data'), `--ozone-platform=${environment.gui}`,
      '--disable-extension=GitHub.copilot', '--disable-extension=GitHub.copilot-chat',
      '--disable-extension=vscode.vscode-api-tests', layout.workspace],
    options: { cwd: bundle.source, env, stdio: 'ignore', detached: true, shell: false },
  };
}

export async function runLinuxSession(bundle, { environment = inspectLinuxEnvironment(), sandbox = checkLinuxSandbox(bundle),
  runtimeBase = linuxRuntimeBase(environment.uid), inherited = process.env,
  statfs = fs.statfsSync, start = spawn, report = () => {} } = {}) {
  // Validate storage before creating a session; the existing shared-session
  // implementation validates every path component and its ownership again.
  let existing = path.resolve(runtimeBase);
  while (!fs.existsSync(existing)) existing = path.dirname(existing);
  assertNativeLinuxStorage(existing, statfs);
  let layout = sharedSessionLayout(runtimeBase, 'shared-linux-seat', bundle.manifest.policy);
  const spec = linuxLaunchSpec(bundle, layout, environment, sandbox, inherited);
  layout = prepareSharedSession(layout, bundle.defaults);
  try {
    if (layout.preservedLegacyProfiles) fail('session_failed', '이전 개인 실습 폴더가 남아 있습니다. 담당자가 보관 위치를 정리해야 새 실습을 시작할 수 있습니다.');
    verifyRestrictedLaunch(layout, bundle.source, new Set(bundle.manifest.approvedBuiltins));
    for (const name of ['tmp', 'home', 'config', 'cache', 'data', 'state']) fs.mkdirSync(path.join(layout.root, name), { mode: 0o700 });
  } catch (error) { cleanupSharedSession(layout); throw error; }
  markSessionLaunching(layout);
  let child;
  try { child = start(spec.executable, spec.args, spec.options); }
  catch (error) { cleanupSharedSession(layout, { spawnFailed: true }); throw error; }
  const completion = superviseSharedChild(child, layout);
  let recordingError;
  try { if (child.pid) recordSessionProcess(layout, child.pid); }
  catch (error) { recordingError = error; child.kill('SIGTERM'); }
  if (!recordingError && child.pid) report({ event: 'ide-started', pid: child.pid });
  const result = await completion;
  if (recordingError) throw recordingError;
  report({ event: 'session-cleaned' });
  return result;
}

export function linuxFailure(error) {
  if (error instanceof LinuxLaunchError) return { event: 'error', code: error.code, message: error.message };
  const message = String(error?.message ?? error);
  if (/active|still running|concurrent/i.test(message)) return { event: 'error', code: 'session_failed',
    message: '이전 실습이 아직 실행 중입니다. 기존 IDE에서 공용 실습 종료를 선택한 뒤 다시 열어 주세요.' };
  return { event: 'error', code: 'session_failed', message: '앱 실행 또는 이전 실습 정리를 확인할 수 없습니다. 담당자에게 앱 설치·권한·남아 있는 실습 프로세스 확인을 요청해 주세요. 새 실습을 시작하지 않았거나 종료 시 정리가 완료되지 않았을 수 있습니다.' };
}

export function isLinuxLauncherEntry(argument, moduleURL = import.meta.url) {
  try { const a = fs.statSync(argument), b = fs.statSync(fileURLToPath(moduleURL)); return a.isFile() && a.dev === b.dev && a.ino === b.ino; }
  catch { return false; }
}

export function showLinuxFailure(failure, { env = process.env, run = spawnSync, exists = fs.existsSync } = {}) {
  // Use fixed executables and argument arrays; never interpolate paths or error
  // text into a shell. The displayed message contains no editor/student logs.
  const options = { env: cleanLinuxEnvironment(env), stdio: 'ignore', shell: false, timeout: 60000 };
  if (exists('/usr/bin/zenity')) run('/usr/bin/zenity', ['--error', '--title=Autograde IDE', '--text', failure.message], options);
  else if (exists('/usr/bin/kdialog')) run('/usr/bin/kdialog', ['--title', 'Autograde IDE', '--error', failure.message], options);
}

if (isLinuxLauncherEntry(process.argv[1])) {
  let seatReporter;
  const report = event => { seatReporter?.event(event); process.stdout.write(`${JSON.stringify(event)}\n`); };
  try {
    if (process.argv.length > 3 || (process.argv[2] && process.argv[2] !== '--check' && process.argv[2] !== '--desktop')) fail('bundle_invalid', '지원하지 않는 실행 옵션입니다. 설치된 아이콘으로 실행해 주세요.');
    const environment = inspectLinuxEnvironment();
    const bundle = loadLinuxBundle(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'));
    seatReporter = createSeatReporter({ version: bundle.manifest.version, extensionVersion: bundle.manifest.extensionVersion ?? bundle.manifest.version });
    const sandbox = checkLinuxSandbox(bundle);
    const libraries = checkLinuxLibraries(bundle);
    if (process.argv[2] === '--check') report(preflightLinux(bundle, { environment, sandbox, libraries }));
    else {
      const result = await runLinuxSession(bundle, { environment, sandbox, report });
      if (result.code) fail('session_failed', 'IDE가 정상적으로 종료되지 않았습니다. 임시 실습 파일은 정리했습니다. 담당자에게 Linux 데스크톱·Linux 라이브러리·Chromium 보안 실행 환경을 확인해 달라고 요청해 주세요.');
    }
  } catch (error) {
    const failure = linuxFailure(error); report(failure);
    if (process.argv[2] === '--desktop') showLinuxFailure(failure);
    process.exitCode = 1;
  }
  await seatReporter?.close();
}
