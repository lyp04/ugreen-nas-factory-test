#Requires -Version 5.1
# One-time setup: create venv, install Python deps.
# Usage:  powershell -ExecutionPolicy Bypass -File .\install.ps1 [-WithChromium]

param(
    [switch]$WithChromium
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

Write-Host "== 检查 Python ==" -ForegroundColor Cyan

function Assert-RealPython {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) {
        Write-Host "未找到 python 命令。" -ForegroundColor Red
        if (Get-Command py -ErrorAction SilentlyContinue) {
            Write-Host "但检测到 py 启动器——多半是安装时没勾选 'Add Python to PATH'。" -ForegroundColor Yellow
            Write-Host "可以把已装的 Python 加入 PATH，或手动执行：py -3 -m venv .venv 后重跑本脚本。" -ForegroundColor Yellow
        }
        return $false
    }

    $versionText = & python --version 2>&1
    $exit = $LASTEXITCODE
    $isStub = $cmd.Source -like "*WindowsApps*" -and ($exit -ne 0 -or -not $versionText)

    if ($isStub -or $exit -ne 0) {
        Write-Host "检测到 python.exe 是 Microsoft Store 占位器（未实际安装）。" -ForegroundColor Red
        Write-Host "Source: $($cmd.Source)"
        return $false
    }

    Write-Host $versionText

    # 源码里有 3.10+ 语法（裸 PEP 604 联合类型），低版本会在导入期抛出一条
    # 看不出因果的 TypeError——在这里就把版本挡下来。
    if ("$versionText" -match "(\d+)\.(\d+)") {
        $ver = [version]("{0}.{1}" -f $Matches[1], $Matches[2])
        if ($ver -lt [version]"3.10") {
            Write-Host "Python $ver 过低，本项目需要 3.10+。" -ForegroundColor Red
            return $false
        }
    }
    return $true
}

if (-not (Assert-RealPython)) {
    Write-Host ""
    Write-Host "请安装 Python 3.10+ 后再运行本脚本：" -ForegroundColor Yellow
    Write-Host "  方式1：winget install -e --id Python.Python.3.12"
    Write-Host "  方式2：从 https://www.python.org/downloads/windows/ 下载安装包，安装时务必勾选 'Add Python to PATH'"
    Write-Host ""
    Write-Host "如果已装 Python 但仍报此错，请关闭 Store 别名：设置 → 应用 → 高级应用设置 → 应用执行别名 → 关闭 python.exe / python3.exe" -ForegroundColor DarkGray
    Write-Error "Python 未就绪"
}

Write-Host ""
Write-Host "== 创建虚拟环境 .venv ==" -ForegroundColor Cyan
if (-not (Test-Path ".venv")) {
    & python -m venv .venv
    if (-not $?) { Write-Error "创建虚拟环境失败" }
} else {
    Write-Host "已存在，跳过"
}

Write-Host ""
Write-Host "== 安装依赖 ==" -ForegroundColor Cyan
& .\.venv\Scripts\python.exe -m pip install --upgrade pip
if (-not $?) { Write-Error "pip 升级失败" }

& .\.venv\Scripts\pip.exe install -r requirements.txt
if (-not $?) { Write-Error "requirements.txt 安装失败" }

Write-Host ""
Write-Host "== 安装 Playwright 驱动 ==" -ForegroundColor Cyan
Write-Host "（默认用系统 Edge，不下载 Chromium。如需下载 Chromium 改用 ' .\install.ps1 -WithChromium'）" -ForegroundColor DarkGray

if ($WithChromium) {
    & .\.venv\Scripts\playwright.exe install chromium
    if (-not $?) { Write-Error "Playwright Chromium 安装失败" }
} else {
    Write-Host "跳过 Chromium 下载"
}

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "下一步：.\run-gui.ps1  启动测试界面"
