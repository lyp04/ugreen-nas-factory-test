#Requires -Version 5.1
# Build a standalone .exe of the GUI using PyInstaller.
# Output: dist\UGREEN-NAS-Test.exe plus public configuration templates.
# Usage:  powershell -ExecutionPolicy Bypass -File .\build-exe.ps1

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Error "未找到 .venv，请先运行 .\install.ps1"
}

$python = ".\.venv\Scripts\python.exe"
$pyinstaller = ".\.venv\Scripts\pyinstaller.exe"

$publicConfigFiles = @(
    "config.example.yml",
    "selectors.yml",
    "update-config.example.json"
)
foreach ($name in $publicConfigFiles) {
    $source = Join-Path ".\config" $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "缺少公开配置文件：$source"
    }
    $sourceItem = Get-Item -LiteralPath $source
    if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "公开配置文件不能是符号链接或 junction：$source"
    }
}

function Assert-PublicDistributionSafe {
    param([Parameter(Mandatory = $true)][string]$Root)

    $rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\', '/')
    $allowedFiles = @(
        "UGREEN-NAS-Test.exe",
        "config/config.example.yml",
        "config/selectors.yml",
        "config/update-config.example.json"
    ) + $testPackages
    $forbiddenSegments = @(
        ".git", ".venv", "venv", "state", "log", "logs", "tmp", "temp",
        "build", "dist", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"
    )
    $forbiddenExtensions = @(
        ".key", ".pem", ".pfx", ".p12", ".ppk", ".kdbx", ".log", ".tmp",
        ".bak", ".backup", ".old", ".orig", ".save", ".swp", ".swo"
    )

    foreach ($item in Get-ChildItem -LiteralPath $rootPath -Recurse -Force) {
        $relative = $item.FullName.Substring($rootPath.Length).TrimStart([char[]]"\/").Replace('\', '/')
        $segments = @($relative -split '/')
        foreach ($segment in $segments) {
            if ($forbiddenSegments -contains $segment -or $segment.StartsWith(".") -or
                $segment -match '(?i)\.local\.' -or
                $segment -match '(?i)(?:^|\.)(?:bak|backup|old|orig|save|swp|swo)(?:\.|$)' -or
                $segment -match '~$' -or $segment -match '^#.*#$') {
                throw "分发目录含禁用路径：$relative"
            }
        }

        if ($item.PSIsContainer) {
            if ($relative -ne "config") {
                throw "分发目录含非白名单目录：$relative"
            }
            continue
        }

        if ($allowedFiles -notcontains $relative) {
            throw "分发目录含非白名单文件：$relative"
        }
        if ($item.Name -ieq "config.yml" -or $item.Name -match '(?i)^\.env(?:\..+)?$') {
            throw "分发目录含真实配置或环境文件：$relative"
        }
        if ($forbiddenExtensions -contains $item.Extension) {
            throw "分发目录含密钥、日志或临时文件：$relative"
        }
        if ($item.Extension -in @(".yml", ".yaml", ".json", ".txt", ".ps1")) {
            $text = [System.IO.File]::ReadAllText($item.FullName)
            if ($text -match '-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----') {
                throw "分发目录含私钥内容：$relative"
            }
        }
    }
}

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
    --collect-all barcode `
    --hidden-import "PIL" `
    --hidden-import "PIL.Image" `
    --hidden-import "PIL.ImageDraw" `
    --hidden-import "PIL.ImageFont" `
    --hidden-import "PIL.ImageChops" `
    --hidden-import "win32print" `
    --hidden-import "src.cli" `
    --hidden-import "src.report" `
    --hidden-import "src.report.reporter" `
    --hidden-import "src.report.github_issues" `
    --hidden-import "src.report.collector" `
    --hidden-import "src.report.fingerprint" `
    --hidden-import "src.report.redact" `
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
    --hidden-import "src.utils.label" `
    .\src\gui.py

if (-not $?) { Write-Error "PyInstaller 构建失败" }

Write-Host ""
Write-Host "== 拷贝公开配置白名单到 dist ==" -ForegroundColor Cyan
if (Test-Path ".\dist\config") { Remove-Item -Recurse -Force ".\dist\config" }
New-Item -ItemType Directory -Force ".\dist\config" | Out-Null
$testPackages = @("测试5G.rar", "测试10G.rar", "测试20G.rar")
foreach ($name in $publicConfigFiles) {
    Copy-Item -Force -LiteralPath (Join-Path ".\config" $name) -Destination (Join-Path ".\dist\config" $name)
}
foreach ($package in $testPackages) {
    $packagePath = Join-Path "." $package
    if (Test-Path $packagePath) {
        $packageItem = Get-Item -LiteralPath $packagePath
        if (($packageItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "测速源文件不能是符号链接或 junction：$packagePath"
        }
        Copy-Item -Force -LiteralPath $packagePath -Destination (Join-Path ".\dist" $package)
    }
}
Assert-PublicDistributionSafe ".\dist"

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "产物：dist\UGREEN-NAS-Test.exe + 3 个公开配置模板（不含真实 config.yml）。"
Write-Host "首次运行前，把 config.example.yml 复制为 config.yml 并在目标机本地填写；不要把填写后的文件回传或重新打包。"
