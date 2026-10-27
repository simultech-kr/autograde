// Operator-invoked desktop registration. Runs with the package's bundled Node.
import * as fs from 'node:fs';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadLinuxBundle, isLinuxLauncherEntry } from './linux-launcher.mjs';

function safeAbsolute(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value) || /[\x00-\x1f\x7f]/.test(value)) throw new Error('Use an absolute path without control characters');
  return value;
}
function present(file) { try { return fs.lstatSync(file); } catch (e) { if (e.code === 'ENOENT') return undefined; throw e; } }

// Desktop Entry string escaping is applied before Exec argument unquoting.
// https://specifications.freedesktop.org/desktop-entry/latest/exec-variables.html
export function desktopExec(file) {
  safeAbsolute(file);
  // '=' is forbidden in the executable path; field codes inside quotes are
  // unspecified, so refuse '%' rather than relying on desktop-specific parsing.
  if (/[=%]/.test(file)) throw new Error('Desktop executable paths cannot contain = or %');
  const quoted = file.replace(/[\\"`$]/g, character => `\\${character}`);
  return `"${quoted.replaceAll('\\', '\\\\')}"`;
}
function desktopValue(value) { return safeAbsolute(value).replaceAll('\\', '\\\\'); }

export function desktopEntry(root) {
  const executable = path.join(safeAbsolute(root), 'bin/autograde-ide');
  return `[Desktop Entry]\nType=Application\nVersion=1.0\nName=Autograde IDE\nComment=Shared classroom programming workspace\nComment[ko]=공용 실습용 프로그래밍 환경\nExec=${desktopExec(executable)} --desktop\nIcon=${desktopValue(path.join(root, 'share/icons/autograde-ide.svg'))}\nTerminal=false\nStartupNotify=false\nCategories=Education;Development;IDE;\n`;
}

function realDirectoryChain(target, create = false) {
  let current = path.parse(target).root;
  for (const part of path.relative(current, target).split(path.sep).filter(Boolean)) {
    current = path.join(current, part);
    if (!present(current) && create) fs.mkdirSync(current, { mode: 0o755 });
    const info = fs.lstatSync(current);
    if (!info.isDirectory() || info.isSymbolicLink()) throw new Error('Desktop destination must use real directories, not links');
  }
}

export function installDesktop({ root, applicationsDir, desktopDir }, { platform = process.platform, uid = process.getuid?.(), load = loadLinuxBundle } = {}) {
  if (platform !== 'linux' || !Number.isSafeInteger(uid) || uid <= 0) throw new Error('Run desktop registration as the ordinary Linux desktop user');
  root = fs.realpathSync(safeAbsolute(root));
  load(root);
  const entry = desktopEntry(root);
  const executable = path.join(root, 'bin/autograde-ide'), icon = path.join(root, 'share/icons/autograde-ide.svg');
  for (const file of [executable, icon]) {
    realDirectoryChain(path.dirname(file));
    if (!fs.lstatSync(file).isFile()) throw new Error('Desktop launcher and icon must be regular files');
  }
  fs.accessSync(executable, fs.constants.X_OK);
  applicationsDir = path.resolve(safeAbsolute(applicationsDir));
  if (desktopDir) desktopDir = path.resolve(safeAbsolute(desktopDir));
  const destinations = [...new Set([applicationsDir, desktopDir].filter(Boolean))];
  if (!destinations.length) throw new Error('An applications directory is required');
  for (const folder of destinations) {
    realDirectoryChain(folder, true);
    const info = fs.lstatSync(folder);
    if (info.uid !== uid || (info.mode & 0o022)) throw new Error('Desktop destination must belong to this user and not be writable by other users');
    if (present(path.join(folder, 'autograde-ide.desktop'))) throw new Error('An Autograde desktop entry already exists; existing files are never replaced');
  }
  const created = [];
  try {
    for (const folder of destinations) {
      const file = path.join(folder, 'autograde-ide.desktop');
      fs.writeFileSync(file, entry, { flag: 'wx', mode: folder === desktopDir ? 0o700 : 0o644 });
      const info = fs.lstatSync(file); created.push({ file, dev: info.dev, ino: info.ino });
    }
  } catch (error) {
    for (const owned of created) {
      const info = present(owned.file);
      if (info?.dev === owned.dev && info?.ino === owned.ino) fs.unlinkSync(owned.file);
    }
    throw error;
  }
  return { event: 'desktop-installed', files: created.map(item => item.file), package: root };
}

export function parseDesktopOptions(args, env = process.env) {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  const home = safeAbsolute(env.HOME);
  const dataHome = env.XDG_DATA_HOME ? safeAbsolute(env.XDG_DATA_HOME) : path.join(home, '.local/share');
  const options = { root, applicationsDir: path.join(dataHome, 'applications') };
  const seen = new Set();
  for (let i = 0; i < args.length; i++) {
    const key = { '--applications-dir': 'applicationsDir', '--desktop-dir': 'desktopDir' }[args[i]];
    if (!key || seen.has(key) || !args[i + 1]) throw new Error('Usage: install-autograde-desktop [--applications-dir ABSOLUTE_DIR] [--desktop-dir ABSOLUTE_DIR]');
    seen.add(key); options[key] = safeAbsolute(args[++i]);
  }
  return options;
}

if (isLinuxLauncherEntry(process.argv[1], import.meta.url)) {
  try { console.log(JSON.stringify(installDesktop(parseDesktopOptions(process.argv.slice(2))))); }
  catch (error) { console.error(`Desktop registration failed: ${error.message}`); process.exitCode = 1; }
}
