$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$bridgePython = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $bridgePython)) {
    $basePython = Get-Command python.exe -ErrorAction Stop
    & $basePython.Source -m venv (Join-Path $PSScriptRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw 'Could not create Python virtual environment.' }
}
& $bridgePython -m pip install --disable-pip-version-check -r (Join-Path $PSScriptRoot 'requirements.txt')
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed. Check network and retry.' }
Write-Host 'Installation complete. Open start.cmd to launch the desktop app.'
