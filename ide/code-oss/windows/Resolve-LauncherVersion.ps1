function Resolve-AutogradeLauncherVersion {
    param(
        [Parameter(Mandatory = $true)][string]$LauncherRoot,
        [AllowEmptyString()][string]$ExtensionVersion
    )

    $versionFile = Join-Path $LauncherRoot 'launcher-version.json'
    $packagedVersion = $null
    if (Test-Path -LiteralPath $versionFile) {
        try {
            $text = Get-Content -LiteralPath $versionFile -Raw -Encoding UTF8 -ErrorAction Stop
            $marker = $text | ConvertFrom-Json -ErrorAction Stop
            if (-not $text.TrimStart().StartsWith('{') -or $null -eq $marker -or
                -not ($marker.schema -is [int] -or $marker.schema -is [long]) -or $marker.schema -ne 1 -or
                -not ($marker.extensionVersion -is [string]) -or $marker.extensionVersion -notmatch '^\d+\.\d+\.\d+$' -or
                -not ($marker.assemblyVersion -is [string]) -or $marker.assemblyVersion -cne ($marker.extensionVersion + '.0')) {
                throw 'Invalid packaged launcher version.'
            }
            $packagedVersion = $marker.extensionVersion
        } catch {
            throw 'launcher-version.json is invalid. Restore the complete matching WSL launcher package before installing.'
        }
    }

    if ([string]::IsNullOrEmpty($ExtensionVersion)) {
        if ($null -ne $packagedVersion) { $ExtensionVersion = $packagedVersion }
        else {
            $package = Join-Path $LauncherRoot '../../../extensions/vscode/package.json'
            if (-not (Test-Path -LiteralPath $package -PathType Leaf)) {
                throw 'Launcher version is missing. Supply ExtensionVersion or use a complete package.'
            }
            $ExtensionVersion = (Get-Content -LiteralPath $package -Raw -Encoding UTF8 | ConvertFrom-Json).version
        }
    }
    if ($ExtensionVersion -notmatch '^\d+\.\d+\.\d+$') {
        throw 'ExtensionVersion must use numeric major.minor.patch format.'
    }
    return $ExtensionVersion
}
