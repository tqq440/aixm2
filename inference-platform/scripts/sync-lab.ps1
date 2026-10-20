param(
    [ValidateSet('Check', 'Package', 'Upload', 'Verify')][string]$Action = 'Check',
    [ValidatePattern('^[0-9a-f]{64}$')][string]$BundleId,
    [string]$PythonPath = (Join-Path $env:USERPROFILE 'miniconda3\envs\project311\python.exe')
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'project311 Python was not found. Pass -PythonPath with its full path.'
}
$transferArguments = @('-m', 'backend.modules.deployment.transfer_cli', $Action.ToLowerInvariant())
if ($BundleId) { $transferArguments += @('--bundle-id', $BundleId) }
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    & $PythonPath @transferArguments
    $transferExitCode = $LASTEXITCODE
}
finally { Pop-Location }
exit $transferExitCode
