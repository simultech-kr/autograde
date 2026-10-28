// Windows/Linux deployment templates. Generate on any host; accept on the target OS before use.
import * as path from 'node:path';
const shell = value => `'${value.replaceAll("'", "'\\''")}'`;
const ps = value => `'${value.replaceAll("'", "''")}'`;
const desktop = value => `"${value.replaceAll('\\', '\\\\').replaceAll('"', '\\"').replaceAll('`', '\\`').replaceAll('$', '\\$').replaceAll('%', '%%')}"`;
export function channelDesktopTemplates(platform, root) {
  if (typeof root !== 'string' || /[\x00-\x1f\x7f]/.test(root)) throw new Error('Invalid channel install path');
  if (platform === 'linux') {
    if (!path.posix.isAbsolute(root)) throw new Error('Absolute Linux channel path required');
    const script = `#!/bin/sh\nset -eu\nexec /usr/bin/env -i PATH=/usr/bin:/bin HOME="\${HOME-}" USER="\${USER-}" LOGNAME="\${LOGNAME-}" LANG="\${LANG-}" DISPLAY="\${DISPLAY-}" WAYLAND_DISPLAY="\${WAYLAND_DISPLAY-}" XDG_RUNTIME_DIR="\${XDG_RUNTIME_DIR-}" DBUS_SESSION_BUS_ADDRESS="\${DBUS_SESSION_BUS_ADDRESS-}" ${shell(path.posix.join(root, 'runtime/bin/node'))} ${shell(path.posix.join(root, 'autograde/channel-launcher.mjs'))}\n`;
    return {'start-channel.sh': script, 'autograde-channel.desktop': `[Desktop Entry]\nType=Application\nName=Autograde Channel\nExec=${desktop(path.posix.join(root, 'start-channel.sh'))}\nTerminal=false\nCategories=Education;Development;\n`};
  }
  if (platform === 'win32') {
    if (!path.win32.isAbsolute(root) || root.includes('"')) throw new Error('Absolute Windows channel path required');
    const start = path.win32.join(root, 'start-channel.ps1');
    return {'start-channel.ps1': `$ErrorActionPreference = 'Stop'\nGet-ChildItem Env: | Where-Object { $_.Name -match '^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_)' } | ForEach-Object { Remove-Item -LiteralPath ('Env:' + $_.Name) }\n$node = ${ps(path.win32.join(root, 'runtime/node.exe'))}\n$script = ${ps(path.win32.join(root, 'autograde/channel-launcher.mjs'))}\n$p = Start-Process -FilePath $node -ArgumentList ('"' + $script + '"') -WindowStyle Hidden -PassThru\n$p.WaitForExit()\nif ($p.ExitCode -ne 0) { Add-Type -AssemblyName System.Windows.Forms; [System.Windows.Forms.MessageBox]::Show('승인된 IDE 업데이트 또는 이전 실습 정리를 담당자에게 확인해 주세요.', 'Autograde IDE') | Out-Null; exit 1 }\n`,
      'install-shortcut.ps1': `$ErrorActionPreference = 'Stop'\n$destination = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Autograde Channel.lnk'\nif (Test-Path -LiteralPath $destination) { throw 'Existing shortcut will not be replaced' }\n$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($destination)\n$shortcut.TargetPath = Join-Path $env:SystemRoot 'System32\\WindowsPowerShell\\v1.0\\powershell.exe'\n$shortcut.Arguments = ${ps(`-NoProfile -NonInteractive -WindowStyle Hidden -File "${start}"`)}\n$shortcut.Save()\n`};
  }
  throw new Error('Only Windows and Linux templates are available');
}
