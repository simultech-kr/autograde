$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'Resolve-LauncherVersion.ps1')

$fixture = Join-Path ([IO.Path]::GetTempPath()) ('autograde-launcher-version-' + [Guid]::NewGuid().ToString('N'))
$launcherRoot = Join-Path $fixture 'repository/ide/code-oss/windows'
$package = Join-Path $fixture 'repository/extensions/vscode/package.json'
$marker = Join-Path $launcherRoot 'launcher-version.json'
$encoding = New-Object Text.UTF8Encoding($false)
function Assert-Version([string]$expected, [string]$explicitVersion = '') {
    $actual = Resolve-AutogradeLauncherVersion -LauncherRoot $launcherRoot -ExtensionVersion $explicitVersion
    if ($actual -cne $expected) { throw "Expected launcher version $expected, received $actual." }
}
function Assert-Rejected([scriptblock]$action) {
    $rejected = $false
    try { $null = & $action } catch { $rejected = $true }
    if (-not $rejected) { throw 'An invalid launcher version was accepted.' }
}
try {
    New-Item -ItemType Directory -Path $launcherRoot -Force | Out-Null
    New-Item -ItemType Directory -Path (Split-Path -Parent $package) -Force | Out-Null
    [IO.File]::WriteAllText($package, '{"version":"9.9.9"}', $encoding)
    [IO.File]::WriteAllText($marker, '{"schema":1,"extensionVersion":"0.5.22","assemblyVersion":"0.5.22.0"}', $encoding)
    # A package inside a checkout must keep its version even after git pull.
    Assert-Version '0.5.22'
    Assert-Version '0.5.23' '0.5.23'
    Assert-Rejected { Resolve-AutogradeLauncherVersion -LauncherRoot $launcherRoot -ExtensionVersion 'invalid' }
    Remove-Item -LiteralPath $package
    Assert-Version '0.5.22'
    Remove-Item -LiteralPath $marker
    Assert-Version '0.5.23' '0.5.23'
    Assert-Rejected { Resolve-AutogradeLauncherVersion -LauncherRoot $launcherRoot }
    [IO.File]::WriteAllText($package, '{"version":"9.9.9"}', $encoding)
    Assert-Version '9.9.9'

    foreach ($invalid in @(
        '{broken',
        '{"schema":2,"extensionVersion":"0.5.22","assemblyVersion":"0.5.22.0"}',
        '{"schema":"1","extensionVersion":"0.5.22","assemblyVersion":"0.5.22.0"}',
        '{"schema":1,"extensionVersion":"invalid","assemblyVersion":"invalid.0"}',
        '{"schema":1,"extensionVersion":"0.5.22","assemblyVersion":"0.5.21.0"}',
        '[{"schema":1,"extensionVersion":"0.5.22","assemblyVersion":"0.5.22.0"}]'
    )) {
        [IO.File]::WriteAllText($marker, $invalid, $encoding)
        Assert-Rejected { Resolve-AutogradeLauncherVersion -LauncherRoot $launcherRoot }
        Assert-Rejected { Resolve-AutogradeLauncherVersion -LauncherRoot $launcherRoot -ExtensionVersion '0.5.23' }
    }
    Write-Output 'WSL launcher version checks passed (19 assertions).'
} finally {
    if (Test-Path -LiteralPath $fixture) { Remove-Item -LiteralPath $fixture -Recurse -Force }
}
