param([int]$Port = 8765, [string]$DataRoot = '')
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw 'Create the virtual environment and install requirements first; see README.md.'
}
$launchArguments = @('-m', 'fusion_memory.cli', 'serve', '--port', "$Port")
if ($DataRoot) { $launchArguments += @('--data-root', $DataRoot) }
& $pythonPath @launchArguments
exit $LASTEXITCODE
