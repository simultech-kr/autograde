[CmdletBinding()]
param(
    [ValidateNotNullOrEmpty()][string]$Source,
    [ValidateNotNullOrEmpty()][string]$BuildOutput,
    [ValidateNotNullOrEmpty()][string]$Output,
    [switch]$Archive
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# Resolve user paths against the caller's location, not this script's directory.
# Omitted paths are discovered by Node; output need not exist. The Node entry
# point validates the completed baseline and output ownership and freshness.
# Every result includes the native launcher and the checked wsl/ launcher and
# installer. WSL2/WSLg and the separately built Linux payload are not installed.
$quickBuildArguments = @(
    (Join-Path $PSScriptRoot 'build-windows-quick.mjs')
)
if ($PSBoundParameters.ContainsKey('Source')) {
    $quickBuildArguments += '--source', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Source)
}
if ($PSBoundParameters.ContainsKey('BuildOutput')) {
    $quickBuildArguments += '--build-output', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($BuildOutput)
}
if ($PSBoundParameters.ContainsKey('Output')) {
    $quickBuildArguments += '--output', $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Output)
}
if ($Archive) { $quickBuildArguments += '--archive' }

$nodeCommand = Get-Command node -CommandType Application -ErrorAction Stop
& $nodeCommand.Path @quickBuildArguments
exit $LASTEXITCODE
