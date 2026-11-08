import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {execFileSync} from 'node:child_process';

const here = path.dirname(fileURLToPath(import.meta.url));
test('PowerShell resolves packaged WSL launcher versions before a nearby checkout and rejects damaged markers',
  {skip: process.platform !== 'win32' ? 'Windows PowerShell is unavailable on this host; run Build-Launcher.ps1 -RunChecks on Windows.' : false}, () => {
    const powershell = path.join(process.env.SystemRoot, 'System32/WindowsPowerShell/v1.0/powershell.exe');
    const output = execFileSync(powershell, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
      path.join(here, 'windows/LauncherVersionChecks.ps1')], {encoding: 'utf8'});
    assert.match(output, /WSL launcher version checks passed \(19 assertions\)/);
  });
