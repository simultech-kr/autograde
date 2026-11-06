import { createSeatReporter } from './seat-reporter.mjs';
// Started only by the native Windows supervisor, already inside its job object.
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';

export function packagePath(root, relative, directory = false) {
  if (typeof relative !== 'string' || !relative || /[\\:\x00-\x1f]/.test(relative) || relative.split('/').some(p => !p || p === '.' || p === '..')) throw new Error('Invalid Windows package path');
  let current = root;
  for (const part of relative.split('/')) {
    current = path.join(current, part);
    if (fs.lstatSync(current).isSymbolicLink()) throw new Error('Linked Windows package path');
  }
  const info = fs.lstatSync(current);
  if (directory ? !info.isDirectory() : !info.isFile()) throw new Error('Missing Windows package component');
  return current;
}

export function loadWindowsPackage(root, host = process) {
  const manifest = JSON.parse(fs.readFileSync(packagePath(root, 'windows-package.json'), 'utf8'));
  if (host.platform !== 'win32' || host.arch !== 'x64' || manifest.schema !== 1 || manifest.platform !== 'win32' || manifest.arch !== 'x64' ||
      manifest.lifecycle !== 'shared' || manifest.policy !== 'standard' || manifest.nodeVersion !== host.versions.node ||
      !/^\d+\.\d+\.\d+$/.test(manifest.extensionVersion ?? '') || !/^[a-f0-9]{40}$/.test(manifest.upstreamCommit ?? '') ||
      !Array.isArray(manifest.approvedBuiltins) || !manifest.approvedBuiltins.includes('autograde')) throw new Error('Windows package/runtime mismatch');
  return { manifest, root, editor: packagePath(root, manifest.editorRoot, true),
    executable: packagePath(root, manifest.executable), node: packagePath(root, manifest.node),
    defaults: JSON.parse(fs.readFileSync(packagePath(root, 'autograde/runtime-settings.json'), 'utf8')) };
}

export function prepareWindowsSession(bundle, sessionRoot, launcherPid, { parentPid = process.ppid } = {}) {
  if (path.basename(sessionRoot) !== 'current' || path.basename(path.dirname(sessionRoot)) !== 'sessions' || !path.isAbsolute(sessionRoot) ||
      path.resolve(sessionRoot) !== sessionRoot ||
      !Number.isSafeInteger(launcherPid) || launcherPid < 1 || launcherPid !== parentPid) throw new Error('Native supervisor required');
  let current = sessionRoot;
  for (;;) {
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Session path contains a link');
    const parent = path.dirname(current); if (parent === current) break; current = parent;
  }
  const readOwner = file => {
    const info = fs.lstatSync(file);
    if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || info.size > 16384) throw new Error('Native session ownership mismatch');
    return JSON.parse(fs.readFileSync(file, 'utf8'));
  };
  const owner = readOwner(path.join(sessionRoot, '.native-owner.json'));
  const anchor = readOwner(path.join(path.dirname(sessionRoot), '.current-owner.json'));
  const id = owner.sessionId;
  // Older native launchers clean on exit. Never advertise deferred cleanup unless
  // both supervisor ownership records explicitly agree on the startup snapshot.
  const cleanupMode = owner.cleanupMode === undefined ? 'on_exit' : owner.cleanupMode;
  if (owner.schema !== 1 || owner.root !== sessionRoot || !/^[a-f0-9]{24}$/.test(id ?? '') || owner.launcherPid !== launcherPid || !/^S-1-/.test(owner.sid ?? '') ||
      !/^[a-f0-9]{8}:[a-f0-9]{16}$/.test(owner.rootIdentity ?? '') ||
      !['next_start', 'on_exit'].includes(cleanupMode) || owner.cleanupMode !== anchor.cleanupMode ||
      ['schema', 'root', 'sessionId', 'launcherPid', 'launcherStarted', 'sid', 'rootIdentity', 'jobName'].some(field => owner[field] !== anchor[field])) throw new Error('Native session ownership mismatch');
  const workspace = path.join(sessionRoot, 'workspace'), materials = path.join(sessionRoot, 'materials'), runtime = path.join(sessionRoot, 'r'), build = path.join(sessionRoot, 'build');
  for (const relative of ['workspace', 'materials', 'tmp', 'home', 'appdata', 'localappdata', 'build', 'r/user-data/User', 'r/extensions', 'r/shared-data']) fs.mkdirSync(path.join(sessionRoot, relative), { recursive: true });
  fs.writeFileSync(path.join(runtime, 'user-data/User/settings.json'), JSON.stringify({ ...bundle.defaults,
    'terminal.integrated.defaultProfile.windows': 'Autograde MSVC',
    'terminal.integrated.profiles.windows': { 'Autograde MSVC': { path: path.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32/cmd.exe'), args: ['/d'] } },
    'terminal.integrated.cwd': workspace, 'files.defaultLanguage': 'cpp', 'files.hotExit': 'off', 'workbench.localHistory.enabled': false,
    'window.restoreWindows': 'none', 'window.confirmBeforeClose': 'always' }), { flag: 'wx' });
  // User-level tasks continue to work when an assignment opens a subfolder.
  fs.writeFileSync(path.join(runtime, 'user-data/User/tasks.json'), JSON.stringify({ version: '2.0.0', tasks: ['C++17', 'C17'].map(language => (
    { label: `Autograde: Build current ${language} file (MSVC)`, type: 'process', command: 'cl.exe',
      args: ['/nologo', language === 'C17' ? '/TC' : '/TP', language === 'C17' ? '/std:c17' : '/std:c++17', '/EHsc', '/Zi', '${file}',
        `/Fo:${build}${path.sep}`, `/Fd:${build}${path.sep}`, `/Fe:${build}${path.sep}` + '${fileBasenameNoExtension}.exe'],
      options: { cwd: build }, problemMatcher: '$msCompile', group: { kind: 'build', isDefault: language === 'C++17' } }
  )) }), { flag: 'wx' });
  const stat = fs.statSync(sessionRoot);
  fs.writeFileSync(path.join(sessionRoot, '.autograde-session.json'), JSON.stringify({ schema: 1, sessionId: id, root: sessionRoot,
    rootDev: stat.dev, rootIno: stat.ino, launcherPid, phase: 'running', policy: 'standard', cleanupMode }), { flag: 'wx' });
  return { root: sessionRoot, workspace, materials, runtime, id, cleanupMode };
}

function nativeCleanupMode(layout) {
  const cleanupMode = layout.cleanupMode === undefined ? 'on_exit' : layout.cleanupMode;
  if (!['next_start', 'on_exit'].includes(cleanupMode)) throw new Error('Invalid native cleanup mode');
  return cleanupMode;
}

/** Caller must obtain layout from prepareWindowsSession's matching ownership proofs. */
export function windowsSeatReporterOptions(bundle, layout) {
  return { version: bundle.manifest.version, extensionVersion: bundle.manifest.extensionVersion,
    platform: 'win32', architecture: 'x64', cleanupMode: nativeCleanupMode(layout) };
}

export function windowsLaunchSpec(bundle, layout, inherited = process.env) {
  const cleanupMode = nativeCleanupMode(layout);
  const env = Object.fromEntries(Object.entries(inherited).filter(([key]) => !/^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_)/i.test(key)));
  Object.assign(env, { USERPROFILE: path.join(layout.root, 'home'), HOME: path.join(layout.root, 'home'),
    APPDATA: path.join(layout.root, 'appdata'), LOCALAPPDATA: path.join(layout.root, 'localappdata'),
    TEMP: path.join(layout.root, 'tmp'), TMP: path.join(layout.root, 'tmp'),
    AUTOGRADE_LEARNING_POLICY: 'standard', AUTOGRADE_LEARNING_LIFETIME: 'shared',
    AUTOGRADE_SESSION_CLEANUP_MODE: cleanupMode,
    AUTOGRADE_LEARNER_WORKSPACE: layout.workspace, AUTOGRADE_LEARNER_MATERIALS: layout.materials,
    AUTOGRADE_SESSION_ROOT: layout.root, AUTOGRADE_SESSION_ID: layout.id,
    VSCODE_DEV: '1', VSCODE_CLI: '1', NODE_ENV: 'development' });
  return { executable: bundle.executable, args: [bundle.editor, '--user-data-dir', path.join(layout.runtime, 'user-data'),
    '--extensions-dir', path.join(layout.runtime, 'extensions'), '--shared-data-dir', path.join(layout.runtime, 'shared-data'),
    '--new-window', '--disable-extension=GitHub.copilot', '--disable-extension=GitHub.copilot-chat',
    '--disable-extension=vscode.vscode-api-tests', layout.workspace], options: { cwd: bundle.editor, env, shell: false, windowsHide: true, stdio: 'ignore' } };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  let seatReporter;
  try {
    if (process.argv.length !== 4) throw new Error('Native supervisor arguments required');
    const bundle = loadWindowsPackage(path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..'));
    const layout = prepareWindowsSession(bundle, process.argv[2], Number(process.argv[3]));
    seatReporter = createSeatReporter(windowsSeatReporterOptions(bundle, layout));
    const spec = windowsLaunchSpec(bundle, layout);
    const child = spawn(spec.executable, spec.args, spec.options);
    seatReporter.event({ event: 'ide-started' });
    const code = await new Promise((resolve, reject) => { child.once('error', reject); child.once('close', code => resolve(code ?? 1)); });
    process.exitCode = code ? 1 : 0;
  } catch { process.exitCode = 1; }
  // Native supervisor cleans only after this bootstrap exits: leave cleanup unknown.
  await seatReporter?.close();
}
