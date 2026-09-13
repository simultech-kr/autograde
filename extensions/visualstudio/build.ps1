param([ValidateSet('Debug', 'Release')][string]$Configuration = 'Release')
$ErrorActionPreference = 'Stop'
if ($env:OS -ne 'Windows_NT') { throw 'VSIX packaging requires Windows.' }
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
if (!(Test-Path $vswhere)) { throw 'Install Visual Studio 2022/2026 and the Visual Studio extension development workload.' }
$vsPath = & $vswhere -latest -products '*' -requires Microsoft.Component.MSBuild Microsoft.VisualStudio.Component.CoreEditor -property installationPath
if (!$vsPath) { throw 'A Visual Studio installation with MSBuild and CoreEditor is required.' }
$msbuild = Join-Path $vsPath 'MSBuild\Current\Bin\MSBuild.exe'
Push-Location $PSScriptRoot
try {
    & dotnet run --project Autograde.Checks/Autograde.Checks.csproj --configuration $Configuration
    if ($LASTEXITCODE -ne 0) { throw 'Core checks failed; VSIX packaging stopped.' }
    & $msbuild Autograde.VisualStudio/Autograde.VisualStudio.csproj /restore /t:Rebuild /m "/p:Configuration=$Configuration" /p:DeployExtension=false /nologo
    if ($LASTEXITCODE -ne 0) { throw 'Visual Studio extension build failed.' }
    $vsix = Join-Path $PSScriptRoot "Autograde.VisualStudio\bin\$Configuration\net472\Autograde.VisualStudio.vsix"
    if (!(Test-Path $vsix)) { throw 'VSIX was not generated. Do not distribute a compile-only DLL.' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $archive = [System.IO.Compression.ZipFile]::OpenRead($vsix)
    try {
        $names = @($archive.Entries | ForEach-Object { $_.FullName })
        foreach ($required in @('extension.vsixmanifest', 'Autograde.VisualStudio.dll', 'Autograde.VisualStudio.pkgdef', 'Autograde.Core.dll', 'Newtonsoft.Json.dll')) {
            if ($names -notcontains $required) { throw "VSIX dependency missing: $required" }
        }
    } finally { $archive.Dispose() }
    Write-Host "VSIX built (installation smoke tests still required): $vsix"
    Get-FileHash -Algorithm SHA256 $vsix
} finally { Pop-Location }
