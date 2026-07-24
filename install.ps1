#Requires -Version 5.1
# One-time setup: create venv, install Python deps.
# Usage:  powershell -ExecutionPolicy Bypass -File .\install.ps1 [-WithChromium]

param(
    [switch]$WithChromium
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

Write-Host "== 检查 Python ==" -ForegroundColor Cyan

$venvPython = ".\.venv\Scripts\python.exe"

function Get-PythonLauncherInfo {
    param(
        [Parameter(Mandatory=$true)][string]$Command,
        [string[]]$PrefixArgs = @()
    )

    $resolved = Get-Command $Command -ErrorAction SilentlyContinue
    if (-not $resolved) { return $null }
    $commandPath = if ($resolved.Path) { $resolved.Path } elseif ($resolved.Source) { $resolved.Source } else { $resolved.Name }

    try {
        $versionOutput = & $commandPath @PrefixArgs --version 2>&1
        $exitCode = $LASTEXITCODE
    } catch {
        return $null
    }
    $versionText = ($versionOutput | Out-String).Trim()
    if ($exitCode -ne 0 -or $versionText -notmatch "(\d+)\.(\d+)") {
        return $null
    }

    # 源码里有 3.10+ 语法（裸 PEP 604 联合类型），低版本会在导入期失败。
    $version = [version]("{0}.{1}" -f $Matches[1], $Matches[2])
    if ($version -lt [version]"3.10") {
        Write-Host "$Command $($PrefixArgs -join ' ') 指向 Python $version，低于所需的 3.10。" -ForegroundColor Yellow
        return $null
    }

    return [pscustomobject]@{
        Command = $commandPath
        PrefixArgs = @($PrefixArgs)
        Version = $version
        VersionText = $versionText
    }
}

Write-Host ""
Write-Host "== 创建虚拟环境 .venv ==" -ForegroundColor Cyan

if (Test-Path -LiteralPath $venvPython -PathType Leaf) {
    $venvInfo = Get-PythonLauncherInfo -Command $venvPython
    if (-not $venvInfo) {
        Write-Error "现有 .venv 无法运行或 Python 版本低于 3.10；请移走该目录后重新运行安装脚本"
    }
    Write-Host "已存在，使用 $($venvInfo.VersionText)"
} else {
    if (Test-Path -LiteralPath ".venv") {
        Write-Error "现有 .venv 不完整（缺少 Scripts\python.exe）；请移走该目录后重新运行安装脚本"
    }

    # python.org 安装通常提供 python；未加入 PATH 时 Windows 的 py 启动器
    # 仍可用。依次探测，且每个候选都实际执行并校验 3.10+，不会把 Store
    # 占位器当成可用解释器。
    $pythonLauncher = Get-PythonLauncherInfo -Command "python"
    if (-not $pythonLauncher) {
        $pythonLauncher = Get-PythonLauncherInfo -Command "py" -PrefixArgs @("-3")
    }
    if (-not $pythonLauncher) {
        Write-Host ""
        Write-Host "未找到可用的 python 或 py -3（需要 Python 3.10+）。" -ForegroundColor Red
        Write-Host "请安装 Python 3.10+ 后再运行本脚本：" -ForegroundColor Yellow
        Write-Host "  方式1：winget install -e --id Python.Python.3.12"
        Write-Host "  方式2：从 https://www.python.org/downloads/windows/ 下载安装包"
        Write-Error "Python 未就绪"
    }

    Write-Host "使用 $($pythonLauncher.VersionText)：$($pythonLauncher.Command) $($pythonLauncher.PrefixArgs -join ' ')"
    $pythonLauncherArgs = @($pythonLauncher.PrefixArgs)
    & $pythonLauncher.Command @pythonLauncherArgs -m venv .venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPython -PathType Leaf)) {
        Write-Error "创建虚拟环境失败"
    }
}

Write-Host ""
Write-Host "== 安装依赖 ==" -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { Write-Error "pip 升级失败" }

& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { Write-Error "requirements.txt 安装失败" }

Write-Host ""
Write-Host "== 安装 Playwright 驱动 ==" -ForegroundColor Cyan
Write-Host "（默认用系统 Edge，不下载 Chromium。如需下载 Chromium 改用 ' .\install.ps1 -WithChromium'）" -ForegroundColor DarkGray

if ($WithChromium) {
    & $venvPython -m playwright install chromium
    if ($LASTEXITCODE -ne 0) { Write-Error "Playwright Chromium 安装失败" }
} else {
    Write-Host "跳过 Chromium 下载"
}

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "下一步：.\run-gui.ps1  启动测试界面"
