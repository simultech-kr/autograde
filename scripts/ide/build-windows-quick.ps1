[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$Source,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$BuildOutput,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$Output,
    [switch]$Archive
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Resolve user paths against the caller's location, not this script's directory.
# Output need not exist; the Node entry point validates ownership and freshness.
# Every result includes the native launcher and the checked wsl/ launcher and
# installer. WSL2/WSLg and the separately built Linux payload are not installed.
$quickBuildArguments = @(
    (Join-Path $PSScriptRoot 'build-windows-quick.mjs'),
    '--source', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Source),
    '--build-output', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($BuildOutput),
    '--output', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
)
if ($Archive) { $quickBuildArguments += '--archive' }

$nodeCommand = Get-Command node -CommandType Application -ErrorAction Stop
& $nodeCommand.Path @quickBuildArguments
exit $LASTEXITCODE
