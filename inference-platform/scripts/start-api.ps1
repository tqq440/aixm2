param(
    [string]$PythonPath = (Join-Path $env:USERPROFILE 'miniconda3\envs\project311\python.exe')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'project311 Python was not found. Pass -PythonPath with its full path.'
}

$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & $PythonPath -m backend.modules.api.run
    $runExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $runExitCode
