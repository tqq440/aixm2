param([string]$PythonPath = (Join-Path $env:USERPROFILE 'miniconda3\envs\project311\python.exe'))

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'project311 Python was not found. Pass -PythonPath with its full path.'
}
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    & $PythonPath -m backend.modules.platform.platform_cli status
    $platformExitCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $platformExitCode
