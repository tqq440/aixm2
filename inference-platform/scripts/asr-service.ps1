param(
    [ValidateSet('status', 'start', 'stop')][string]$Action = 'status',
    [ValidateRange(0, 600)][int]$WaitSeconds = 180,
    [string]$PythonPath = (Join-Path $env:USERPROFILE 'miniconda3\envs\project311\python.exe')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'project311 Python was not found. Pass -PythonPath with its full path.'
}
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    & $PythonPath -m backend.modules.agent.agent_cli $Action --wait-seconds $WaitSeconds
    $asrExitCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $asrExitCode
