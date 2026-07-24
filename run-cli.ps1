#Requires -Version 5.1
# Run CLI (useful for scripted/batch testing or debugging).
# Examples:
#   powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 test --sn SN123 --nas-ip auto

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "未找到 .venv，请先运行 .\install.ps1"
}

& .\.venv\Scripts\python.exe -m src.cli @args
exit $LASTEXITCODE
