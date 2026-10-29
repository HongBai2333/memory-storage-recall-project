param([string]$Output = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$datasetPath = Join-Path $projectRoot 'evaluation\datasets\text-foundation-v1'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create the virtual environment and install this project first; see README.md.'
}
if (-not $Output) {
    $runSuffix = [Guid]::NewGuid().ToString('N').Substring(0, 12)
    $Output = Join-Path $projectRoot "artifacts\evaluation\pilot-$runSuffix"
}
& $pythonPath -X utf8 -m fusion_memory.evaluation.cli run `
    --dataset $datasetPath --experiment (Join-Path $datasetPath 'experiment.json') --output $Output
exit $LASTEXITCODE
