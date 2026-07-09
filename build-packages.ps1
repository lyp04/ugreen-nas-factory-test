#Requires -Version 5.1
# 一次打两个包。软件本体（exe）完全一样，区别只在带不带 ugreen-nas-autoupdate 模块：
#   A  dist-public\  —— 公开/外部：exe + config.example.yml + update-config.json，不带模块 → 上传/录表 UI 自动隐藏
#   B  dist-full\    —— 内部产线：同一个 exe + 真实 config.yml + update-config.json + 上传器模块 + 启动器 → 完整功能
# 两个包都带 update-config.json（指向公开库、token 留空，公开库免鉴权）→ 都能自动更新「软件本体 exe」。
# 自更新只替换 exe，不动 ugreen-nas-autoupdate 模块。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\build-packages.ps1
#   powershell -ExecutionPolicy Bypass -File .\build-packages.ps1 -AutoupdateSrc "D:\path\to\ugreen-nas-autoupdate"
#
# 在 Windows 上跑（PyInstaller 只能打目标平台）。

param(
    [string]$AutoupdateSrc = (Join-Path $env:USERPROFILE "ugreen-nas-autoupdate"),
    [string]$UpdateOwner = "lyp04",
    [string]$UpdateRepo  = "ugreen-nas-factory-test"
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if (-not (Test-Path ".venv\Scripts\python.exe")) { Write-Error "未找到 .venv，请先运行 .\install.ps1" }
$python = ".\.venv\Scripts\python.exe"
$pyinstaller = ".\.venv\Scripts\pyinstaller.exe"
if (-not (Test-Path $pyinstaller)) {
    Write-Host "PyInstaller 未安装，正在安装..." -ForegroundColor Yellow
    & $python -m pip install pyinstaller
    if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller 安装失败" }
}

$hiddenImports = @(
    "PIL", "PIL.Image", "PIL.ImageDraw", "PIL.ImageFont", "PIL.ImageChops", "win32print",
    "src.cli", "src.report", "src.report.reporter", "src.report.github_issues",
    "src.report.collector", "src.report.fingerprint", "src.report.redact",
    "src.gui", "src.updater", "src.version", "src.form_entry",
    "src.flows.setup_wizard", "src.flows.login", "src.flows.capture", "src.flows.provision",
    "src.flows.cleanup", "src.flows.reset_factory",
    "src.discovery.discover", "src.discovery.mdns_scanner", "src.discovery.port_scanner",
    "src.discovery.ugreen_broadcast", "src.utils.label"
)
$testPackages = @("测试5G.rar", "测试10G.rar", "测试20G.rar")

# ---- 构建软件本体 exe（gui.py 自动探测版；A/B 共用同一个 exe）----
$dist = ".\build\app"
if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
$piArgs = @(
    "--name", "UGREEN-NAS-Test", "--onefile", "--windowed", "--noconfirm",
    "--collect-all", "playwright", "--collect-all", "zeroconf", "--collect-all", "barcode",
    "--distpath", $dist, "--workpath", ".\build\_work_app", "--specpath", ".\build\_spec_app"
)
foreach ($h in $hiddenImports) { $piArgs += @("--hidden-import", $h) }
$piArgs += "src\gui.py"
Write-Host "== 构建软件本体 exe（gui.py 自动探测）==" -ForegroundColor Cyan
& $pyinstaller @piArgs
if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller 构建失败" }
if (-not (Test-Path "$dist\UGREEN-NAS-Test.exe")) { Write-Error "构建后未找到 exe" }

# update-config.json：两个包共用（公开库免鉴权自更新；token 留空）
function Write-UpdateConfig {
    param([string]$OutDir)
    $cfg = [ordered]@{
        enabled       = $true
        owner         = $UpdateOwner
        repo          = $UpdateRepo
        token         = ""
        manifestAsset = "update.json"
        releaseTag    = ""
    }
    # 用 .NET WriteAllText 写成 UTF-8 无 BOM —— PS5.1 的 Set-Content -Encoding utf8 会加 BOM，
    # 而 updater 的 json.load 读到 BOM 会报错、自更新静默失效。
    $ucPath = Join-Path (Resolve-Path $OutDir) "config\update-config.json"
    [System.IO.File]::WriteAllText($ucPath, ($cfg | ConvertTo-Json -Depth 4))
}

# 两个包共用：exe + selectors + update-config.json + 测速源文件
function Copy-Common {
    param([string]$OutDir)
    New-Item -ItemType Directory -Force (Join-Path $OutDir "config") | Out-Null
    Copy-Item -Force "$dist\UGREEN-NAS-Test.exe" (Join-Path $OutDir "UGREEN-NAS-Test.exe")
    Copy-Item -Force ".\config\selectors.yml" (Join-Path $OutDir "config\selectors.yml")
    Write-UpdateConfig $OutDir
    foreach ($p in $testPackages) { if (Test-Path ".\$p") { Copy-Item -Force ".\$p" (Join-Path $OutDir $p) } }
}

# ---------- Package A：公开 / 外部（不带上传器模块）----------
Write-Host "== [A] 公开包（不带上传器模块）==" -ForegroundColor Cyan
if (Test-Path ".\dist-public") { Remove-Item -Recurse -Force ".\dist-public" }
New-Item -ItemType Directory -Force ".\dist-public" | Out-Null
Copy-Common ".\dist-public"
Copy-Item -Force ".\config\config.example.yml" ".\dist-public\config\config.example.yml"
Write-Host "   -> dist-public\（exe + config.example.yml + update-config.json；无模块→上传 UI 隐藏；自更新开）" -ForegroundColor Green

# ---------- Package B：内部完整（带上传器模块）----------
Write-Host "== [B] 完整包（带上传器模块）==" -ForegroundColor Cyan
if (Test-Path ".\dist-full") { Remove-Item -Recurse -Force ".\dist-full" }
New-Item -ItemType Directory -Force ".\dist-full" | Out-Null
Copy-Common ".\dist-full"
if (Test-Path ".\config\config.yml") {
    Copy-Item -Force ".\config\config.yml" ".\dist-full\config\config.yml"
} else {
    Write-Host "   ! 本地没有 config\config.yml，改带 config.example.yml（记得填真实凭据）" -ForegroundColor Yellow
    Copy-Item -Force ".\config\config.example.yml" ".\dist-full\config\config.yml"
}
# B 便携化：把 output_dir + autoupdate_root 改成便携值——整包复制走即用，不管源 config 写的是啥。
#   output_dir     → 相对 exe 目录（./screenshot）；
#   autoupdate_root → 清空：模块随包放在 exe 同级子目录，form_entry 的「包内子目录」候选会自动发现，
#                     既不泄漏打包机的用户路径，也不用依赖启动器的环境变量。
$cfgB = ".\dist-full\config\config.yml"
$cfgText = (Get-Content $cfgB -Raw) `
    -replace '(?m)^output_dir:.*$', 'output_dir: "./screenshot"' `
    -replace '(?m)^([ \t]*)autoupdate_root:.*$', '$1autoupdate_root: ""'
[System.IO.File]::WriteAllText((Resolve-Path $cfgB), $cfgText)
# 打印专有数据 + 现有模版文件：现有的直接带进 B（A 公开包不带）。
# labels.yml = 真实 P/N/EAN 对照表（label_data_file 指向它）；config/labels/ = 现有 .ddl/.btw 等模版文件（若有）。
if (Test-Path ".\config\labels.yml") { Copy-Item -Force ".\config\labels.yml" ".\dist-full\config\labels.yml" }
if (Test-Path ".\config\labels")     { Copy-Item -Recurse -Force ".\config\labels" ".\dist-full\config\labels" }
if (Test-Path $AutoupdateSrc) {
    Copy-Item -Recurse -Force $AutoupdateSrc ".\dist-full\ugreen-nas-autoupdate"
    # 别把模块的私有历史 / 运行态凭据 / 构建产物打进包：
    #   .git   = 私库完整历史（可能含更多秘密）
    #   state  = 账号 token / 缓存 / 提交记录（accounts.local.json 等）——账号在目标机运行时登录，不随包分发
    #   build/dist/apk/__pycache__/.pytest_cache = 构建产物
    $moduleOut = ".\dist-full\ugreen-nas-autoupdate"
    foreach ($ex in @(".git", "state", "build", "dist", "apk", "__pycache__", ".pytest_cache")) {
        $exPath = Join-Path $moduleOut $ex
        if (Test-Path $exPath) { Remove-Item -Recurse -Force $exPath }
    }
    Get-ChildItem $moduleOut -Recurse -Directory -Filter "__pycache__" -ErrorAction SilentlyContinue | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
    # 启动器：把 UGREEN_AUTOUPDATE_ROOT 指到随包模块再启动 exe（自更新换 exe 后仍生效）
    $bat = @(
        "@echo off",
        "set ""UGREEN_AUTOUPDATE_ROOT=%~dp0ugreen-nas-autoupdate""",
        "start """" ""%~dp0UGREEN-NAS-Test.exe"""
    ) -join "`r`n"
    Set-Content -Path ".\dist-full\启动-完整版.bat" -Value $bat -Encoding OEM
    Write-Host "   -> dist-full\（exe + 真实 config + update-config.json + 上传器模块 + 启动器；自更新只换 exe、不动模块）" -ForegroundColor Green
} else {
    Write-Host "   ! 找不到上传器源目录：$AutoupdateSrc（跳过打包模块）。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "A 公开包：dist-public\   （无模块，上传 UI 隐藏；自更新开）"
Write-Host "B 完整包：dist-full\     （用 启动-完整版.bat 启动；带模块=完整功能；自更新只换 exe，不动模块）"
Write-Host "注意：本脚本不 stamp 版本号，包内 exe 版本 = src/version.py 的默认值，首次启动会自更新到公开库最新 release。" -ForegroundColor Yellow
