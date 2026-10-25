// WSLg entry point, executed by the Windows icon through the bundled Linux Node.
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { fileURLToPath } from 'node:url';
import { spawn, spawnSync } from 'node:child_process';
import { learnerEnvironment, verifyRestrictedLaunch } from './learning-profile.mjs';
import { sharedSessionLayout, prepareSharedSession, markSessionLaunching, recordSessionProcess,
  cleanupSharedSession, superviseSharedChild } from './shared-session.mjs';

export class WslLaunchError extends Error {
  constructor(code, message) { super(message); this.code = code; }
}
const fail = (code, message) => { throw new WslLaunchError(code, message); };
const linuxFilesystems = new Set([0xef53, 0x9123683e, 0x58465342, 0x01021994, 0x794c7630]);

export function assertLinuxStorage(target, statfs = fs.statfsSync) {
  const canonical = fs.realpathSync(target);
  if (/^\/mnt(?:\/|$)/i.test(canonical) || !linuxFilesystems.has(Number(statfs(canonical).type) >>> 0)) {
    fail('unsafe_install', '앱과 임시 실습 폴더는 WSL 내부 Linux 파일 시스템에 설치해야 합니다. Windows 드라이브나 공유 폴더에서는 실행하지 않습니다.');
  }
  return canonical;
}

function contained(root, relative, kind = 'file') {
  if (typeof relative !== 'string' || !relative || path.isAbsolute(relative) || relative.includes('\\') ||
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

export function loadWslBundle(root, { platform = process.platform, arch = process.arch,
  nodeVersion = process.versions.node, statfs = fs.statfsSync } = {}) {
  if (platform !== 'linux') fail('wsl_missing', 'Windows의 WSL2 실행 아이콘으로 열어 주세요. Linux용 실행 환경이 필요합니다.');
  root = assertLinuxStorage(root, statfs);
  const manifest = JSON.parse(fs.readFileSync(contained(root, 'wsl-package.json'), 'utf8'));
  if (manifest.schema !== 1 || manifest.platform !== 'linux' || !['x64', 'arm64'].includes(manifest.arch) ||
      manifest.arch !== arch || manifest.nodeVersion !== nodeVersion || manifest.lifecycle !== 'shared' ||
      !/^\d+\.\d+\.\d+(?:[-+][\w.-]+)?$/.test(manifest.version ?? '') ||
      !['standard', 'restricted'].includes(manifest.policy) ||
      !Array.isArray(manifest.approvedBuiltins) || !manifest.approvedBuiltins.includes('autograde') ||
      manifest.approvedBuiltins.some(name => typeof name !== 'string' || !/^[\w-][\w.-]*$/.test(name))) {
    fail('bundle_invalid', 'WSL용 앱의 버전·프로세서 또는 공용 실습 설정이 맞지 않습니다. 담당자에게 맞는 배포본을 요청해 주세요.');
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

export function cleanWslEnvironment(inherited = process.env) {
  return Object.fromEntries(Object.entries(inherited).filter(([name]) =>
    !/^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_|LD_|DYLD_)/.test(name) &&
    !['CHROME_DEVEL_SANDBOX', 'CHROME_USER_DATA_DIR', 'WSLENV', 'BASH_ENV', 'ENV',
      'SSH_AUTH_SOCK', 'GPG_AGENT_INFO'].includes(name)));
}

function isWslgSocket(file) {
  try {
    const canonical = fs.realpathSync(file);
    return canonical.startsWith('/mnt/wslg/') && fs.statSync(canonical).isSocket();
  } catch { return false; }
}

export function inspectWslEnvironment({ platform = process.platform, release = os.release(), uid = process.getuid?.(),
  env = process.env, socketExists = isWslgSocket } = {}) {
  if (platform !== 'linux' || !/microsoft/i.test(release)) fail('wsl_missing', 'WSL2가 준비되지 않았습니다. 담당자에게 WSL2 설치 상태를 확인해 달라고 요청해 주세요.');
  if (!/microsoft.*wsl2/i.test(release)) fail('wsl1_unsupported', 'WSL1에서는 공용 실습 앱을 실행할 수 없습니다. 담당자가 WSL2 배포판을 준비해야 합니다.');
  if (!Number.isSafeInteger(uid) || uid <= 0) fail('root_user', '관리자(root) 대신 실습용 일반 Linux 사용자로 실행해야 합니다. 담당자에게 아이콘 설정 확인을 요청해 주세요.');
  const display = /^:(\d{1,4})(?:\.\d+)?$/.exec(env.DISPLAY ?? '');
  if (display && socketExists(`/tmp/.X11-unix/X${display[1]}`)) return { gui: 'x11', uid, distribution: env.WSL_DISTRO_NAME ?? '' };
  if (typeof env.XDG_RUNTIME_DIR === 'string' && path.isAbsolute(env.XDG_RUNTIME_DIR) &&
      /^[\w.-]+$/.test(env.WAYLAND_DISPLAY ?? '') && !['.', '..'].includes(env.WAYLAND_DISPLAY) &&
      socketExists(path.join(env.XDG_RUNTIME_DIR, env.WAYLAND_DISPLAY))) return { gui: 'wayland', uid, distribution: env.WSL_DISTRO_NAME ?? '' };
  fail('gui_unavailable', 'WSLg 그래픽 환경에 연결할 수 없습니다. 담당자에게 Windows·WSL 업데이트와 그래픽 환경 확인을 요청해 주세요.');
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

export function checkWslSandbox(bundle, { env = process.env, probe = spawnSync, helperTrusted = secureSandboxHelper } = {}) {
  const helper = path.join(path.dirname(bundle.executable), 'chrome-sandbox');
  if (helperTrusted(helper)) return { sandbox: 'setuid-helper', helper };
  // Probe without changing the host's AppArmor/user-namespace policy. This is a
  // prerequisite check, not proof that Chromium will pass every host policy.
  const checked = probe('/usr/bin/unshare', ['--user', '--map-root-user', '/usr/bin/true'], {
    encoding: 'utf8', timeout: 5000, maxBuffer: 4096, stdio: 'ignore', env: cleanWslEnvironment(env),
  });
  if (checked.status === 0 && !checked.error) return { sandbox: 'user-namespace' };
  fail('sandbox_unavailable', '편집기의 보안 실행 환경을 사용할 수 없습니다. 담당자가 Linux 사용자 네임스페이스 또는 관리되는 Chromium sandbox를 준비해야 합니다. 보안 기능을 끄고 실행하지 않습니다.');
}

export function preflightWsl(bundle, { environment = inspectWslEnvironment(), sandbox = checkWslSandbox(bundle) } = {}) {
  return { event: 'ready', schema: 1, platform: 'wsl2', architecture: bundle.manifest.arch,
    extensionVersion: bundle.manifest.extensionVersion, ...environment, sandbox: sandbox.sandbox,
    lifetime: 'shared', noSandbox: false };
}

export function wslRuntimeBase(uid = process.getuid()) {
  if (!Number.isSafeInteger(uid) || uid <= 0) fail('root_user', '일반 실습용 Linux 사용자로 실행해야 합니다.');
  return `/tmp/ag-wsl-lab-${uid}`;
}

export function wslLaunchSpec(bundle, layout, environment, sandbox, inherited = process.env) {
  if (Buffer.byteLength(path.join(layout.runtime, 'user-data', `${bundle.manifest.version.slice(0, 4)}-shared.sock`)) >= 107) {
    fail('unsafe_install', '임시 실습 폴더 경로가 너무 깁니다. 담당자에게 경로 확인을 요청해 주세요.');
  }
  if (!['x11', 'wayland'].includes(environment.gui)) fail('gui_unavailable', 'WSLg 그래픽 환경을 확인할 수 없습니다.');
  const temp = path.join(layout.root, 'tmp');
  const home = path.join(layout.root, 'home');
  const env = { ...learnerEnvironment(layout, cleanWslEnvironment(inherited)),
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

export async function runWslSession(bundle, { environment = inspectWslEnvironment(), sandbox = checkWslSandbox(bundle),
  runtimeBase = wslRuntimeBase(environment.uid), inherited = process.env,
  statfs = fs.statfsSync, start = spawn, report = () => {} } = {}) {
  // Validate storage before creating a session; the existing shared-session
  // implementation validates every path component and its ownership again.
  let existing = path.resolve(runtimeBase);
  while (!fs.existsSync(existing)) existing = path.dirname(existing);
  assertLinuxStorage(existing, statfs);
  let layout = sharedSessionLayout(runtimeBase, 'shared-wsl-seat', bundle.manifest.policy);
  const spec = wslLaunchSpec(bundle, layout, environment, sandbox, inherited);
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

export function wslFailure(error) {
  if (error instanceof WslLaunchError) return { event: 'error', code: error.code, message: error.message };
  const message = String(error?.message ?? error);
  if (/active|still running|concurrent/i.test(message)) return { event: 'error', code: 'session_failed',
    message: '이전 실습이 아직 실행 중입니다. 기존 IDE에서 공용 실습 종료를 선택한 뒤 다시 열어 주세요.' };
  return { event: 'error', code: 'session_failed', message: '앱 실행 또는 이전 실습 정리를 확인할 수 없습니다. 담당자에게 앱 설치·권한·남아 있는 실습 프로세스 확인을 요청해 주세요. 새 실습을 시작하지 않았거나 종료 시 정리가 완료되지 않았을 수 있습니다.' };
}

export function isWslLauncherEntry(argument, moduleURL = import.meta.url) {
  try { const a = fs.statSync(argument), b = fs.statSync(fileURLToPath(moduleURL)); return a.isFile() && a.dev === b.dev && a.ino === b.ino; }
  catch { return false; }
}

if (isWslLauncherEntry(process.argv[1])) {
  const report = event => process.stdout.write(`${JSON.stringify(event)}\n`);
  try {
    if (process.argv.length > 3 || (process.argv[2] && process.argv[2] !== '--check')) fail('bundle_invalid', '지원하지 않는 실행 옵션입니다. 설치된 아이콘으로 실행해 주세요.');
    const environment = inspectWslEnvironment();
    const bundle = loadWslBundle(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'));
    const sandbox = checkWslSandbox(bundle);
    if (process.argv[2] === '--check') report(preflightWsl(bundle, { environment, sandbox }));
    else {
      const result = await runWslSession(bundle, { environment, sandbox, report });
      if (result.code) fail('session_failed', 'IDE가 정상적으로 종료되지 않았습니다. 임시 실습 파일은 정리했습니다. 담당자에게 WSLg·Linux 라이브러리·Chromium 보안 실행 환경을 확인해 달라고 요청해 주세요.');
    }
  } catch (error) { report(wslFailure(error)); process.exitCode = 1; }
}
