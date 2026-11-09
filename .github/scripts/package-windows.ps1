param(
    [Parameter(Mandatory)][ValidatePattern('^[A-Za-z][A-Za-z0-9]*$')][string]$Application,
    [Parameter(Mandatory)][ValidatePattern('^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+|(?:a|b|rc)\d+)?$')][string]$Version,
    [Parameter(Mandatory)][ValidateSet('x64', 'x86', 'arm64')][string]$Architecture,
    [Parameter(Mandatory)][string]$SourceDirectory,
    [Parameter(Mandatory)][string]$OutputDirectory
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$source = (Resolve-Path -LiteralPath $SourceDirectory).Path
if (-not (Test-Path -LiteralPath "$source/$Application.exe" -PathType Leaf)) {
    throw "Missing application executable in $source"
}
New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null
$output = (Resolve-Path -LiteralPath $OutputDirectory).Path
$compiler = Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6/ISCC.exe'
if (-not (Test-Path -LiteralPath $compiler)) { throw 'Install Inno Setup 6 before packaging.' }
& $compiler /Qp "/DAppName=$Application" "/DAppVersion=$Version" "/DAppArch=$Architecture" `
    "/DSourceDirectory=$source" "/DOutputDirectory=$output" "$PSScriptRoot/windows.iss"
if ($LASTEXITCODE -ne 0) { throw 'Installer compilation failed.' }
$installer = Join-Path $output "$Application-$Version-Windows-$Architecture.exe"
if (-not (Test-Path -LiteralPath $installer -PathType Leaf)) { throw 'Installer was not produced.' }
# Native/x86 installers are also installed and uninstalled in an isolated folder.
# An ARM64 installer is compiled on x64 CI and exercised on ARM64 hosts.
$canInstall = $Architecture -ne 'arm64' -or $env:PROCESSOR_ARCHITECTURE -eq 'ARM64'
if ($canInstall) {
    $temporary = Join-Path ([IO.Path]::GetTempPath()) ('installer-check-' + [guid]::NewGuid().ToString('N'))
    try {
        $process = Start-Process -FilePath $installer -Wait -PassThru -ArgumentList `
            '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART', '/SP-', "/DIR=`"$temporary`""
        if ($process.ExitCode -ne 0) { throw "Silent installation failed: $($process.ExitCode)" }
        foreach ($file in Get-ChildItem -LiteralPath $source -File -Recurse) {
            $relative = [IO.Path]::GetRelativePath($source, $file.FullName)
            $installed = Join-Path $temporary $relative
            if (-not (Test-Path -LiteralPath $installed) -or
                (Get-FileHash -LiteralPath $installed).Hash -ne (Get-FileHash -LiteralPath $file.FullName).Hash) {
                throw "Installed payload differs: $relative"
            }
        }
        $process = Start-Process -FilePath "$temporary/unins000.exe" -Wait -PassThru `
            -ArgumentList '/VERYSILENT', '/SUPPRESSMSGBOXES', '/NORESTART'
        if ($process.ExitCode -ne 0 -or (Test-Path -LiteralPath "$temporary/$Application.exe")) {
            throw 'Silent uninstallation failed.'
        }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
    }
}
Write-Host "Verified installer: $installer"
