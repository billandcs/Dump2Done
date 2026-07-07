param(
    [string]$HostName = "127.0.0.1",
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv-arm64\Scripts\python.exe"

if (-not (Test-Path -LiteralPath $Python)) {
    throw "ARM64 venv not found: $Python. Install it with: python -m venv .venv-arm64"
}

Set-Location $ProjectRoot
& $Python "src\dump2done\web\server.py" --host $HostName --port $Port
