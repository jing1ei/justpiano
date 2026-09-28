$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot
if ($env:OS -ne 'Windows_NT') { throw 'Build the Windows app on Windows.' }
if ($env:PYTHON) {
    & $env:PYTHON tools\bootstrap_env.py --venv .venv
} else {
    py -3.11 tools\bootstrap_env.py --venv .venv
}
if ($LASTEXITCODE -ne 0) { throw 'Could not prepare Python 3.11.' }
$python = '.venv\Scripts\python.exe'
& $python -c 'import platform,struct; assert struct.calcsize("P") == 8 and platform.machine().lower() in ("amd64", "x86_64"), "Windows x64 Python is required"'
if ($LASTEXITCODE -ne 0) { throw 'Use Python 3.11 x64 for a Windows-x64 release.' }
& $python -m pip install -r requirements.txt -c constraints-build.txt pyinstaller setuptools
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
& $python -m tools.make_icon
if ($LASTEXITCODE -ne 0) { throw 'Icon generation failed.' }
& $python -m PyInstaller --noconfirm JustPiano.spec
if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
& $python -m tools.check_bundle 'dist\JustPiano\JustPiano.exe'
if ($LASTEXITCODE -ne 0) { throw 'Packaged app startup check failed.' }
$version = & $python -c 'from justpiano import __version__; print(__version__)'
if ($LASTEXITCODE -ne 0) { throw 'Could not determine package version.' }
Compress-Archive -Path 'dist\JustPiano' -DestinationPath "dist\JustPiano-$version-Windows-x64.zip" -Force
Write-Host 'Extract the entire ZIP, then open JustPiano\JustPiano.exe.'
