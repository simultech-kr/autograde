"""Exercise argument boundaries at deployment OS entry points."""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / 'scripts' / 'deploy'


def _executable(path, content):
    path.write_text(content)
    path.chmod(0o755)


@pytest.mark.skipif(os.name == 'nt', reason='Bash wrappers run on POSIX hosts')
@pytest.mark.parametrize('filename', ['ubuntu.sh', 'macos.sh'])
def test_shell_syntax(filename):
    subprocess.run(['bash', '-n', str(SCRIPTS / filename)], check=True)


@pytest.mark.skipif(os.name == 'nt', reason='Bash wrapper runs on POSIX hosts')
def test_macos_launcher_preserves_spaces_and_shell_metacharacters_as_arguments(tmp_path):
    scripts = tmp_path / 'source with spaces'
    scripts.mkdir()
    shutil.copy2(SCRIPTS / 'macos.sh', scripts / 'macos.sh')
    (scripts / 'deploy.py').write_text('import json,sys\nprint(json.dumps(sys.argv[1:]))\n')
    tools = tmp_path / 'tools'
    tools.mkdir()
    _executable(tools / 'uname', '#!/bin/sh\nprintf "%s\\n" Darwin\n')
    # The launcher must use its adjacent Python entry and forward an argv list.
    # A profile name resembling shell code must never execute that code.
    (tools / 'python3').symlink_to(sys.executable)
    profile = str(tmp_path / 'profile $(touch UNEXPECTED) with spaces.local')
    args = ['plan', '--profile', profile, '--name', '교수자 이름; echo not-a-command']
    result = subprocess.run(['bash', str(scripts / 'macos.sh'), *args],
                            cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == args
    assert not (tmp_path / 'UNEXPECTED').exists()


@pytest.mark.skipif(os.name == 'nt', reason='Bash wrappers run on POSIX hosts')
@pytest.mark.parametrize(('filename', 'reported_platform'), [('ubuntu.sh', 'Darwin'), ('macos.sh', 'Linux')])
def test_launcher_refuses_wrong_host_before_running_python(tmp_path, filename, reported_platform):
    tools = tmp_path / 'tools'
    tools.mkdir()
    _executable(tools / 'uname', '#!/bin/sh\nprintf "%s\\n" "' + reported_platform + '"\n')
    _executable(tools / 'python3', '#!/bin/sh\ntouch PYTHON_WAS_RUN\n')
    result = subprocess.run(['bash', str(SCRIPTS / filename), 'install', '--profile', 'profile.local'],
                            cwd=tmp_path, capture_output=True, text=True,
                            env={**os.environ, 'PATH': str(tools) + os.pathsep + os.environ['PATH']})
    assert result.returncode == 2
    assert not (tmp_path / 'PYTHON_WAS_RUN').exists()


def test_windows_powershell_parses_when_runtime_is_available(tmp_path):
    powershell = shutil.which('pwsh') or shutil.which('powershell')
    if powershell is None:
        pytest.skip('PowerShell unavailable; Windows/WSL2 live acceptance remains separate')
    parser = tmp_path / 'check.ps1'
    parser.write_text('''param([string]$Target)
$tokens = $null
$parseErrors = $null
$null = [System.Management.Automation.Language.Parser]::ParseFile($Target, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count -gt 0) { $parseErrors | ForEach-Object { Write-Error $_ }; exit 1 }
exit 0
''')
    subprocess.run([powershell, '-NoProfile', '-NonInteractive', '-File', str(parser),
                    '-Target', str(SCRIPTS / 'windows.ps1')], check=True)
