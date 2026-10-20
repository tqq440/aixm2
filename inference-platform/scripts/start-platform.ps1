param(
    [string]$PythonPath = (Join-Path $env:USERPROFILE 'miniconda3\envs\project311\python.exe'),
    [string]$NodePath = 'D:\develop\node\node.exe',
    [ValidateRange(1024, 65535)][int]$FrontendPort = 5173,
    [ValidateRange(5, 900)][int]$StartupTimeoutSeconds = 180
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'project311 Python was not found. Pass -PythonPath with its full path.'
}
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    & $PythonPath -m backend.modules.platform.platform_cli start --node-path $NodePath --frontend-port $FrontendPort --startup-timeout-seconds $StartupTimeoutSeconds
    $platformExitCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $platformExitCode
