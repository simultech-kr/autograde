[CmdletBinding()]
param([Parameter(Mandatory = $true)][string]$OutputDirectory, [Parameter(Mandatory = $true)][string]$ExtensionVersion, [switch]$RunChecks)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT' -or -not [Environment]::Is64BitOperatingSystem) { throw 'Build the x64 native launcher on Windows x64.' }
if ($ExtensionVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid extension version.' }
$target = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $target) { throw 'Choose a new native launcher output directory.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw '.NET Framework compiler is required on the build machine.' }
New-Item -ItemType Directory -Path $target | Out-Null
$versionSource = Join-Path $target 'AssemblyInfo.cs'
$version = $ExtensionVersion + '.0'
$text = "using System.Reflection;`n[assembly: AssemblyTitle(""Autograde IDE Native Windows"")]`n[assembly: AssemblyProduct(""Autograde IDE"")]`n[assembly: AssemblyVersion(""$version"")]`n[assembly: AssemblyFileVersion(""$version"")]`n[assembly: AssemblyInformationalVersion(""$ExtensionVersion"")]`n"
[IO.File]::WriteAllText($versionSource, $text, (New-Object Text.UTF8Encoding($false)))
$common = @('/nologo', '/utf8output', '/codepage:65001', '/langversion:5', '/optimize+', '/platform:x64', '/reference:System.Web.Extensions.dll', '/reference:System.Core.dll', '/reference:System.Net.Http.dll')
$core = Join-Path $PSScriptRoot 'NativeSessionCore.cs'
$policy = Join-Path $PSScriptRoot 'NativeCleanupPolicy.cs'
$policyWindows = Join-Path $PSScriptRoot 'NativeCleanupPolicyWindows.cs'
$diagnostics = Join-Path $PSScriptRoot 'NativeDiagnostics.cs'
$diagnosticLog = Join-Path $PSScriptRoot 'NativeDiagnosticLog.cs'
$app = Join-Path $target 'Autograde IDE.exe'
& $compiler @common '/target:winexe' '/reference:System.Windows.Forms.dll' '/reference:System.Drawing.dll' "/out:$app" $versionSource $core $policy $policyWindows $diagnostics $diagnosticLog (Join-Path $PSScriptRoot 'NativeLauncher.cs')
if ($LASTEXITCODE -ne 0) { throw 'Native Windows launcher compilation failed.' }
if ([Reflection.AssemblyName]::GetAssemblyName($app).Version.ToString() -ne $version) { throw 'Native launcher version mismatch.' }
$checks = Join-Path $target 'Autograde Native Checks.exe'
& $compiler @common '/target:exe' "/out:$checks" $versionSource $core $policy $diagnostics $diagnosticLog (Join-Path $PSScriptRoot 'NativeLauncherChecks.cs')
if ($LASTEXITCODE -ne 0) { throw 'Native Windows check compilation failed.' }
$diagnosticChecks = Join-Path $target 'Autograde Diagnostic Checks.exe'
& $compiler @common '/target:exe' "/out:$diagnosticChecks" $diagnostics (Join-Path $PSScriptRoot 'NativeDiagnosticsChecks.cs')
if ($LASTEXITCODE -ne 0) { throw 'Native diagnostics check compilation failed.' }
& $diagnosticChecks
if ($LASTEXITCODE -ne 0) { throw 'Native diagnostics checks failed.' }
$policyChecks = Join-Path $target 'Autograde Policy Checks.exe'
& $compiler @common '/target:exe' "/out:$policyChecks" $policy (Join-Path $PSScriptRoot 'NativeCleanupPolicyChecks.cs')
if ($LASTEXITCODE -ne 0) { throw 'Native cleanup policy check compilation failed.' }
& $policyChecks
if ($LASTEXITCODE -ne 0) { throw 'Native cleanup policy checks failed.' }
if ($RunChecks) { & $checks; if ($LASTEXITCODE -ne 0) { throw 'Native Windows lifecycle checks failed.' } }
Write-Output $app
