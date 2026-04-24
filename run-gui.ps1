#Requires -Version 5.1
# Launch the factory test GUI.
# Usage:  powershell -ExecutionPolicy Bypass -File .\run-gui.ps1

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "未找到 .venv，请先运行 .\install.ps1"
}

& .\.venv\Scripts\python.exe -m src.gui_no_form
