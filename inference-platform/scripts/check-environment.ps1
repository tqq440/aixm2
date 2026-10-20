param(
    [string]$PythonPath = (Join-Path $env:USERPROFILE 'miniconda3\envs\project311\python.exe'),
    [string]$NodePath = 'D:\develop\node\node.exe'
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'project311 Python was not found. Pass -PythonPath with its full path.'
}
$projectRoot = Split-Path -Parent $PSScriptRoot
Push-Location -LiteralPath $projectRoot
try {
    & $PythonPath -m backend.modules.maintenance.check_environment --node-path $NodePath
    $runExitCode = $LASTEXITCODE
}
finally {
    Pop-Location
}
exit $runExitCode
