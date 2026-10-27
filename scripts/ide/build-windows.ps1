[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutputDirectory)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Build the Windows launcher on Windows.' }
$repo = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (& git -C $repo status --porcelain --untracked-files=all) { throw 'Candidate builds require a clean committed checkout.' }
$target = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $target) { throw 'Choose a new output directory.' }
$upstream = Get-Content -LiteralPath (Join-Path $repo 'ide/code-oss/upstream.json') -Raw | ConvertFrom-Json
$version = (Get-Content -LiteralPath (Join-Path $repo 'extensions/vscode/package.json') -Raw | ConvertFrom-Json).version
$revision = (& git -C $repo rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0) { throw 'Cannot identify the source revision.' }
New-Item -ItemType Directory -Path $target | Out-Null
$bundle = Join-Path $target 'Autograde Windows WSL2 Launcher'
$source = Join-Path $repo 'ide/code-oss/windows'
$null = & (Join-Path $source 'Build-Launcher.ps1') -OutputDirectory $bundle -RunChecks -ExtensionVersion $version
# Include installer inputs because the existing installer validates and builds
# its own private installation directory before creating the desktop shortcut.
foreach ($name in @('Build-Launcher.ps1', 'Install-Shortcut.ps1', 'LauncherCore.cs', 'LauncherChecks.cs', 'AutogradeWslLauncher.cs', 'launcher.example.json')) {
    Copy-Item -LiteralPath (Join-Path $source $name) -Destination $bundle
}
$archive = Join-Path $target ('Autograde-IDE-' + $version + '-windows-wsl2-launcher.zip')
Compress-Archive -LiteralPath $bundle -DestinationPath $archive -CompressionLevel Optimal
$hash = (Get-FileHash -LiteralPath $archive -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText(($archive + '.sha256'), ($hash + '  ' + [IO.Path]::GetFileName($archive) + "`n"), (New-Object Text.UTF8Encoding($false)))
$report = @{
    schema = 1; status = 'release-candidate'; sourceDirty = $false; buildTarget = 'windows-launcher'; sourceRevision = $revision
    upstream = $upstream; extensionVersion = $version
    host = @{platform = 'win32'; architecture = 'x64'; compiler = '.NET Framework csc'; executableArchitecture = 'anycpu'}
    checks = @(@{name = 'windows-launcher-compile'; result = 'passed'}, @{name = 'windows-launcher-self-tests'; result = 'passed'}, @{name = 'windows-launcher-version'; result = 'passed'})
    artifacts = @(@{target = 'windows-wsl2-launcher'; file = [IO.Path]::GetFileName($archive); bytes = (Get-Item -LiteralPath $archive).Length; sha256 = $hash})
    acceptance = @{institution = 'not-tested'; windowsWsl2 = 'not-tested'; productionReady = $false}
}
[IO.File]::WriteAllText((Join-Path $target 'build-windows-launcher.json'), ($report | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding($false)))
Write-Host ('Built Windows launcher ' + $version + '; WSL2 GUI acceptance remains untested.')
