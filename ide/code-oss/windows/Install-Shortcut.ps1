[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Distribution,
    [Parameter(Mandatory = $true)][string]$User,
    [Parameter(Mandatory = $true)][string]$LinuxInstallRoot,
    [string]$InstallDirectory,
    [string]$ShortcutName = 'Autograde IDE (WSL2)',
    [switch]$ReplaceOwnedShortcut
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows의 실습 계정으로 실행해 주세요.' }
if ([string]::IsNullOrEmpty($InstallDirectory)) {
    $release = 'release-' + [DateTime]::UtcNow.ToString('yyyyMMddHHmmss') + '-' + [Guid]::NewGuid().ToString('N').Substring(0, 8)
    $InstallDirectory = Join-Path (Join-Path $env:LOCALAPPDATA 'Autograde\WSLLauncher') $release
}
if ($ShortcutName.IndexOfAny([IO.Path]::GetInvalidFileNameChars()) -ge 0 -or [string]::IsNullOrWhiteSpace($ShortcutName)) { throw '바로가기 이름을 확인해 주세요.' }
$target = [IO.Path]::GetFullPath($InstallDirectory)
if (Test-Path -LiteralPath $target) { throw '기존 설치 폴더를 덮어쓰지 않습니다. 새 경로를 지정해 주세요.' }
$parent = [IO.Path]::GetDirectoryName($target)
if (-not (Test-Path -LiteralPath $parent)) { New-Item -ItemType Directory -Path $parent -Force | Out-Null }
$stage = Join-Path $parent ('.autograde-wsl-stage-' + [Guid]::NewGuid().ToString('N'))
$exe = Join-Path $target 'Autograde IDE.exe'

function Assert-NoActiveLauncher($candidate) {
    $mutex = $null
    $owned = $false
    try {
        try { $mutex = [Threading.Mutex]::OpenExisting($candidate.MutexName()) }
        catch [Threading.WaitHandleCannotBeOpenedException] { return }
        try { $owned = $mutex.WaitOne(0) }
        catch [Threading.AbandonedMutexException] { $owned = $true }
        if (-not $owned) { throw '현재 Autograde 실습이 실행 중입니다. 공용 실습을 종료한 뒤 설치·갱신해 주세요.' }
    } finally {
        if ($owned) { $mutex.ReleaseMutex() }
        if ($null -ne $mutex) { $mutex.Dispose() }
    }
}

try {
$null = & (Join-Path $PSScriptRoot 'Build-Launcher.ps1') -OutputDirectory $stage -RunChecks
# Load bytes to avoid locking the staged executable. Resolve the exact assembly
# types: a second install in the same PowerShell window must not reuse old types.
$assembly = [Reflection.Assembly]::Load([IO.File]::ReadAllBytes((Join-Path $stage 'Autograde IDE.exe')))
$configType = $assembly.GetType('Autograde.WslLauncher.LauncherConfig', $true)
$argumentType = $assembly.GetType('Autograde.WslLauncher.WindowsArguments', $true)
$protocolType = $assembly.GetType('Autograde.WslLauncher.SessionProtocol', $true)
$json = @{schema = 1; distribution = $Distribution; user = $User; installRoot = $LinuxInstallRoot} | ConvertTo-Json -Compress
$config = $configType.GetMethod('Parse').Invoke($null, [object[]]@($json))
Assert-NoActiveLauncher $config
$windowsDirectory = [Environment]::GetFolderPath('Windows')
$wsl = Join-Path $windowsDirectory 'Sysnative\wsl.exe'
if (-not (Test-Path -LiteralPath $wsl -PathType Leaf)) { $wsl = Join-Path $windowsDirectory 'System32\wsl.exe' }
if (-not (Test-Path -LiteralPath $wsl -PathType Leaf)) { throw 'WSL을 찾을 수 없습니다. 담당자가 WSL2·WSLg를 먼저 설치해 주세요.' }

# Check the selected distribution, non-root user, WSL2 GUI, installed runtime and
# Electron sandbox before creating a student-facing shortcut. Never run a shell.
$start = New-Object Diagnostics.ProcessStartInfo
$start.FileName = $wsl
$joinArguments = New-Object object[] 1
$joinArguments[0] = $config.Arguments($true)
$start.Arguments = $argumentType.GetMethod('Join').Invoke($null, $joinArguments)
$start.UseShellExecute = $false
$start.CreateNoWindow = $true
$start.RedirectStandardOutput = $true
$start.RedirectStandardError = $true
$start.StandardOutputEncoding = New-Object Text.UTF8Encoding($false)
$start.StandardErrorEncoding = New-Object Text.UTF8Encoding($false)
foreach ($key in @($start.EnvironmentVariables.Keys)) {
    if ($key -eq 'WSLENV' -or $key -match '^(NODE_|ELECTRON_|VSCODE_|AUTOGRADE_)') { $start.EnvironmentVariables.Remove($key) }
}
$process = New-Object Diagnostics.Process
$process.StartInfo = $start
try {
    if (-not $process.Start()) { throw 'WSL 실행기를 시작할 수 없습니다.' }
    $stdout = $process.StandardOutput.ReadToEndAsync()
    $stderr = $process.StandardError.ReadToEndAsync()
    if (-not $process.WaitForExit(120000)) {
        # This is only the read-only --check subprocess, never an IDE session or
        # the WSL distribution. Do not use wsl --terminate/--shutdown.
        $process.Kill()
        throw 'WSL 환경 확인에 2분 이상 걸렸습니다. 담당자가 WSL 상태를 확인한 뒤 설치를 다시 진행해 주세요.'
    }
    $process.WaitForExit()
    $protocol = $protocolType.GetConstructor([Type[]]@([bool], [string])).Invoke([object[]]@($true, $Distribution))
    foreach ($line in ($stdout.Result -split "`r?`n")) { $protocol.Accept($line) }
    $failure = $protocol.ResultError($process.ExitCode)
    if ($failure) { throw $failure }
} finally { $process.Dispose() }

$configPath = Join-Path $stage 'launcher.json'
[IO.File]::WriteAllText($configPath, $json, (New-Object Text.UTF8Encoding($false)))
$desktop = [Environment]::GetFolderPath('DesktopDirectory')
$shortcutPath = Join-Path $desktop ($ShortcutName + '.lnk')
$shell = New-Object -ComObject WScript.Shell
if (Test-Path -LiteralPath $shortcutPath) {
    if (-not $ReplaceOwnedShortcut) { throw '같은 이름의 바로가기가 있습니다. 기존 Autograde 실습을 종료한 뒤 -ReplaceOwnedShortcut으로 갱신하거나 다른 바로가기 이름을 지정해 주세요.' }
    $existing = $shell.CreateShortcut($shortcutPath)
    $previousDirectory = [IO.Path]::GetDirectoryName($existing.TargetPath)
    $ownership = Join-Path $previousDirectory '.autograde-wsl-launcher.json'
    if (-not (Test-Path -LiteralPath $ownership -PathType Leaf)) { throw '기존 바로가기의 소유 정보를 확인하지 못했습니다. 기존 파일을 덮어쓰지 않습니다.' }
    $old = Get-Content -LiteralPath $ownership -Raw -Encoding UTF8 | ConvertFrom-Json
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($old.schema -ne 1 -or $old.userSid -ne $sid -or $old.executable -ne $existing.TargetPath) { throw '기존 바로가기의 소유 정보가 일치하지 않습니다. 기존 파일을 덮어쓰지 않습니다.' }
    $oldJson = [IO.File]::ReadAllText((Join-Path $previousDirectory 'launcher.json'), [Text.Encoding]::UTF8)
    $oldConfig = $configType.GetMethod('Parse').Invoke($null, [object[]]@($oldJson))
    Assert-NoActiveLauncher $oldConfig
    [IO.File]::Copy($shortcutPath, (Join-Path $stage 'previous-shortcut.lnk'), $false)
}
$owner = @{schema = 1; userSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value; executable = $exe} | ConvertTo-Json -Compress
[IO.File]::WriteAllText((Join-Path $stage '.autograde-wsl-launcher.json'), $owner, (New-Object Text.UTF8Encoding($false)))
# Publish only after checks pass, preserving failed stages for diagnosis without
# occupying the requested final destination or creating a misleading shortcut.
[IO.Directory]::Move($stage, $target)
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $exe
$shortcut.WorkingDirectory = $target
$shortcut.Description = 'Autograde 공용 실습: IDE 종료 후 임시 코드·자료 정리'
$shortcut.IconLocation = $exe + ',0'
$shortcut.WindowStyle = 1
$shortcut.Save()
Write-Host '설치 완료: 학생은 바탕 화면의 Autograde IDE 아이콘을 클릭하면 됩니다.'
Write-Output $shortcutPath
} catch {
    if (Test-Path -LiteralPath $stage) { Write-Warning ('설치가 완료되지 않았습니다. 진단용 빌드 폴더: ' + $stage) }
    elseif (Test-Path -LiteralPath $target) { Write-Warning ('앱 파일은 준비되었지만 바로가기 설치를 확인해 주세요: ' + $target) }
    throw
}
