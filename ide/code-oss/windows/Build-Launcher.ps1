[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [switch]$RunChecks,
    [string]$ExtensionVersion
)
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'Windows에서 실행해 주세요. 학생 PC에는 Node.js나 .NET SDK가 필요하지 않습니다.' }
$target = [IO.Path]::GetFullPath($OutputDirectory)
if (Test-Path -LiteralPath $target) { throw '기존 출력 폴더를 덮어쓰지 않습니다. 새 출력 경로를 지정해 주세요.' }
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe' }
if (-not (Test-Path -LiteralPath $compiler -PathType Leaf)) { throw 'Windows의 .NET Framework 컴파일러를 찾을 수 없습니다. 담당자가 .NET Framework 설치 상태를 확인해 주세요.' }
if ([string]::IsNullOrEmpty($ExtensionVersion)) {
    $package = Join-Path $PSScriptRoot '../../../extensions/vscode/package.json'
    $versionFile = Join-Path $PSScriptRoot 'launcher-version.json'
    if (Test-Path -LiteralPath $package -PathType Leaf) { $ExtensionVersion = (Get-Content -LiteralPath $package -Raw -Encoding UTF8 | ConvertFrom-Json).version }
    elseif (Test-Path -LiteralPath $versionFile -PathType Leaf) { $ExtensionVersion = (Get-Content -LiteralPath $versionFile -Raw -Encoding UTF8 | ConvertFrom-Json).extensionVersion }
    else { throw '실행기 버전 정보가 없습니다. ExtensionVersion을 지정해 주세요.' }
}
if ($ExtensionVersion -notmatch '^\d+\.\d+\.\d+$') { throw 'ExtensionVersion은 major.minor.patch 숫자 형식이어야 합니다.' }
$assemblyVersion = $ExtensionVersion + '.0'
New-Item -ItemType Directory -Path $target | Out-Null
$versionSource = Join-Path $target 'AssemblyInfo.cs'
$versionText = "using System.Reflection;`n[assembly: AssemblyTitle(""Autograde IDE WSL2 Launcher"")]`n[assembly: AssemblyProduct(""Autograde IDE"")]`n[assembly: AssemblyVersion(""$assemblyVersion"")]`n[assembly: AssemblyFileVersion(""$assemblyVersion"")]`n[assembly: AssemblyInformationalVersion(""$ExtensionVersion"")]`n"
[IO.File]::WriteAllText($versionSource, $versionText, (New-Object Text.UTF8Encoding($false)))
$versionJson = @{schema = 1; extensionVersion = $ExtensionVersion; assemblyVersion = $assemblyVersion} | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $target 'launcher-version.json'), $versionJson, (New-Object Text.UTF8Encoding($false)))
$common = @('/nologo', '/utf8output', '/codepage:65001', '/optimize+', '/platform:anycpu', '/reference:System.Web.Extensions.dll')
$core = Join-Path $PSScriptRoot 'LauncherCore.cs'
$gui = Join-Path $target 'Autograde IDE.exe'
& $compiler @common '/target:winexe' '/reference:System.Windows.Forms.dll' '/reference:System.Drawing.dll' "/out:$gui" $versionSource $core (Join-Path $PSScriptRoot 'AutogradeWslLauncher.cs')
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $gui -PathType Leaf)) { throw 'Windows 실행기 컴파일에 실패했습니다. 출력 폴더는 진단을 위해 남겨 두었습니다.' }
if ([Reflection.AssemblyName]::GetAssemblyName($gui).Version.ToString() -ne $assemblyVersion) { throw '컴파일된 실행기 버전이 패키지 버전과 일치하지 않습니다.' }
$checks = Join-Path $target 'Autograde Launcher Checks.exe'
& $compiler @common '/target:exe' "/out:$checks" $versionSource $core (Join-Path $PSScriptRoot 'LauncherChecks.cs')
if ($LASTEXITCODE -ne 0) { throw 'Windows 실행기 시험 프로그램을 컴파일하지 못했습니다.' }
if ($RunChecks) {
    & $checks
    if ($LASTEXITCODE -ne 0) { throw 'Windows 실행기 자체 시험이 실패했습니다. 바로가기를 배포하지 마세요.' }
}
Write-Output $gui
