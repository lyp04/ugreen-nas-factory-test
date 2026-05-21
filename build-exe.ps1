#Requires -Version 5.1
# Build a standalone .exe of the GUI using PyInstaller.
# Output: dist\UGREEN-NAS-Test.exe (config/ folder must ship alongside the exe)
# Usage:  powershell -ExecutionPolicy Bypass -File .\build-exe.ps1

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "未找到 .venv，请先运行 .\install.ps1"
}

$python = ".\.venv\Scripts\python.exe"
$pyinstaller = ".\.venv\Scripts\pyinstaller.exe"

if (-not (Test-Path $pyinstaller)) {
    Write-Host "PyInstaller 未安装，正在安装..." -ForegroundColor Yellow
    & $python -m pip install pyinstaller
    if (-not $?) { Write-Error "PyInstaller 安装失败" }
}

Write-Host "== 清理旧构建 ==" -ForegroundColor Cyan
if (Test-Path "build") { Remove-Item -Recurse -Force "build" }
if (Test-Path "dist")  {
    Remove-Item -Recurse -Force "dist\*" -ErrorAction SilentlyContinue
} else {
    New-Item -ItemType Directory -Force "dist" | Out-Null
}
Get-ChildItem -Path . -Filter "*.spec" | Remove-Item -Force

Write-Host ""
Write-Host "== 构建 EXE ==" -ForegroundColor Cyan
& $pyinstaller `
    --name "UGREEN-NAS-Test" `
    --onefile `
    --windowed `
    --collect-all playwright `
    --collect-all zeroconf `
    --hidden-import "src.cli" `
    --hidden-import "src.gui" `
    --hidden-import "src.updater" `
    --hidden-import "src.version" `
    --hidden-import "src.form_entry" `
    --hidden-import "src.flows.setup_wizard" `
    --hidden-import "src.flows.login" `
    --hidden-import "src.flows.capture" `
    --hidden-import "src.flows.provision" `
    --hidden-import "src.flows.cleanup" `
    --hidden-import "src.flows.reset_factory" `
    --hidden-import "src.discovery.discover" `
    --hidden-import "src.discovery.mdns_scanner" `
    --hidden-import "src.discovery.port_scanner" `
    --hidden-import "src.discovery.ugreen_broadcast" `
    .\src\gui.py

if (-not $?) { Write-Error "PyInstaller 构建失败" }

Write-Host ""
Write-Host "== 拷贝 config 目录到 dist ==" -ForegroundColor Cyan
if (Test-Path ".\dist\config") { Remove-Item -Recurse -Force ".\dist\config" }
New-Item -ItemType Directory -Force ".\dist\config" | Out-Null
Copy-Item -Recurse -Force .\config\* .\dist\config\
foreach ($package in @("测试5G.rar", "测试10G.rar", "测试20G.rar")) {
    $packagePath = Join-Path "." $package
    if (Test-Path $packagePath) {
        Copy-Item -Force -LiteralPath $packagePath -Destination (Join-Path ".\dist" $package)
    }
}

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "产物：dist\UGREEN-NAS-Test.exe（同目录下需保留 dist\config\ ）"
Write-Host "打包给工厂：整个 dist 目录拷走即可。operator 双击 exe 启动。"
