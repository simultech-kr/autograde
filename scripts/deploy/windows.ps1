[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('plan', 'install', 'init-admin', 'start', 'stop', 'status', 'check', 'backup', 'proxy-start', 'cloud-deploy')]
    [string]$Command,
    [Parameter(Mandatory = $true)][string]$Profile,
    [string]$Distribution = 'Ubuntu',
    [string]$SourceRoot,
    [string]$Username,
    [string]$Name,
    [string]$Destination
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($env:OS -ne 'Windows_NT') { throw 'Run this launcher in Windows PowerShell.' }
if (-not (Get-Command wsl.exe -ErrorAction SilentlyContinue)) {
    throw 'Install WSL2 with an Ubuntu distribution, finish its first-run setup, then run this command again.'
}
if ($Distribution -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') {
    throw 'Use the exact Ubuntu distribution name listed by wsl.exe --list --verbose.'
}

# Do not install a distribution, accept a licence, enable Windows features, or
# change the system's default WSL distribution from a deployment command.
$listing = @(& wsl.exe --list --verbose)
if ($LASTEXITCODE -ne 0) { throw 'Cannot list WSL distributions.' }
$distributionPattern = '^\s*\*?\s*' + [regex]::Escape($Distribution) + '\s+.+?\s+2\s*$'
$found = @($listing | Where-Object { ($_ -replace "`0", '') -match $distributionPattern })
if ($found.Count -ne 1) {
    throw "The selected distribution must already use WSL version 2: $Distribution"
}
$osRelease = @(& wsl.exe --distribution $Distribution --exec cat /etc/os-release)
if ($LASTEXITCODE -ne 0 -or -not ($osRelease -match '^ID=("ubuntu"|ubuntu)$')) {
    throw 'The selected WSL2 distribution must be Ubuntu.'
}

function Convert-ToWslPath([string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value.IndexOf([char]0) -ge 0 -or $Value -match "[`r`n]") {
        throw 'A path is missing or contains control characters.'
    }
    # Linux paths are useful for profiles and backups already stored in WSL.
    if ($Value.StartsWith('/')) { return $Value }
    $absolute = [IO.Path]::GetFullPath($Value)
    $converted = @(& wsl.exe --distribution $Distribution --exec wslpath -a -u $absolute)
    if ($LASTEXITCODE -ne 0 -or $converted.Count -ne 1 -or -not $converted[0].StartsWith('/')) {
        throw 'Cannot convert the supplied Windows path to a WSL path.'
    }
    return $converted[0]
}

$repository = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$pythonEntry = Convert-ToWslPath (Join-Path $PSScriptRoot 'deploy.py')
$profilePath = Convert-ToWslPath $Profile
$arguments = @('--distribution', $Distribution, '--exec', 'python3', $pythonEntry, $Command, '--profile', $profilePath)

if ($Command -in @('install', 'cloud-deploy')) {
    $source = if ([string]::IsNullOrWhiteSpace($SourceRoot)) { $repository } else { $SourceRoot }
    $arguments += @('--source-root', (Convert-ToWslPath $source))
} elseif (-not [string]::IsNullOrWhiteSpace($SourceRoot)) {
    throw '-SourceRoot is supported only by install and cloud-deploy.'
}

if ($Command -eq 'init-admin') {
    if ([string]::IsNullOrWhiteSpace($Username) -or [string]::IsNullOrWhiteSpace($Name)) {
        throw 'init-admin requires -Username and -Name. The password is entered interactively in the terminal.'
    }
    $arguments += @('--username', $Username, '--name', $Name)
} elseif (-not [string]::IsNullOrWhiteSpace($Username) -or -not [string]::IsNullOrWhiteSpace($Name)) {
    throw '-Username and -Name are supported only by init-admin.'
}

if ($Command -eq 'backup') {
    if ([string]::IsNullOrWhiteSpace($Destination)) { throw 'backup requires -Destination.' }
    $arguments += @('--destination', (Convert-ToWslPath $Destination))
} elseif (-not [string]::IsNullOrWhiteSpace($Destination)) {
    throw '-Destination is supported only by backup.'
}

# Separate arguments reach wsl.exe --exec directly. Never interpolate a shell
# command, run bash -c, or weaken PowerShell execution policy for deployment.
& wsl.exe @arguments
exit $LASTEXITCODE
