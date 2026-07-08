#Requires -Version 5.1
# 一次打两个包：
#   A  dist-public\  —— 入口 src\gui_no_form.py（硬关录表：UGREEN_DISABLE_FORM_ENTRY=1）
#                       只带 config.example.yml、不带上传器；登录/上传 UI 永远隐藏。给别人 / 进公开发布用。
#   B  dist-full\    —— 入口 src\gui.py（自动探测上传器）
#                       带真实 config.yml + ugreen-nas-autoupdate 上传器 + 一个设好环境变量的启动器；完整功能。本地用。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\build-packages.ps1
#   powershell -ExecutionPolicy Bypass -File .\build-packages.ps1 -AutoupdateSrc "D:\path\to\ugreen-nas-autoupdate"
#
# 在 Windows 上跑（PyInstaller 只能打目标平台）。首次跑完请分别双击两个 exe 确认：
# A 里没有账号/上传/录表物料，B 里有。

param(
    [string]$AutoupdateSrc = (Join-Path $env:USERPROFILE "ugreen-nas-autoupdate")
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

# 与 build-exe.ps1 一致的 hidden-import 列表（form_entry 仍打进去，只是运行时不触发）。
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

# 构建一个 onefile exe 到 .\build\<Stem>\UGREEN-NAS-Test.exe（不返回值，靠约定路径拿产物）。
function Build-Exe {
    param([string]$Entry, [string]$Stem)
    $dist = ".\build\$Stem"
    if (Test-Path $dist) { Remove-Item -Recurse -Force $dist }
    $piArgs = @(
        "--name", "UGREEN-NAS-Test", "--onefile", "--windowed", "--noconfirm",
        "--collect-all", "playwright", "--collect-all", "zeroconf", "--collect-all", "barcode",
        "--distpath", $dist, "--workpath", ".\build\_work_$Stem", "--specpath", ".\build\_spec_$Stem"
    )
    foreach ($h in $hiddenImports) { $piArgs += @("--hidden-import", $h) }
    $piArgs += $Entry
    Write-Host ("  pyinstaller " + ($piArgs -join ' ')) -ForegroundColor DarkGray
    & $pyinstaller @piArgs
    if ($LASTEXITCODE -ne 0) { Write-Error "PyInstaller 构建失败（entry=$Entry）" }
    if (-not (Test-Path "$dist\UGREEN-NAS-Test.exe")) { Write-Error "构建后未找到 exe（entry=$Entry）" }
}

function Copy-Common {
    param([string]$OutDir)
    New-Item -ItemType Directory -Force (Join-Path $OutDir "config") | Out-Null
    Copy-Item -Force ".\config\selectors.yml" (Join-Path $OutDir "config\selectors.yml")
    foreach ($p in $testPackages) { if (Test-Path ".\$p") { Copy-Item -Force ".\$p" (Join-Path $OutDir $p) } }
}

# ---------- Package A：公开 / 给别人（硬关上传，不带上传器）----------
Write-Host "== [A] 公开包：入口 gui_no_form.py（硬关录表）==" -ForegroundColor Cyan
Build-Exe "src\gui_no_form.py" "public"
if (Test-Path ".\dist-public") { Remove-Item -Recurse -Force ".\dist-public" }
New-Item -ItemType Directory -Force ".\dist-public" | Out-Null
Copy-Item -Force ".\build\public\UGREEN-NAS-Test.exe" ".\dist-public\UGREEN-NAS-Test.exe"
Copy-Common ".\dist-public"
Copy-Item -Force ".\config\config.example.yml" ".\dist-public\config\config.example.yml"
Write-Host "   -> dist-public\（exe + config.example.yml，无上传器，登录/上传 UI 隐藏）" -ForegroundColor Green

# ---------- Package B：本地完整（带真实配置 + 上传器）----------
Write-Host "== [B] 本地完整包：入口 gui.py（自动探测上传器）==" -ForegroundColor Cyan
Build-Exe "src\gui.py" "full"
if (Test-Path ".\dist-full") { Remove-Item -Recurse -Force ".\dist-full" }
New-Item -ItemType Directory -Force ".\dist-full" | Out-Null
Copy-Item -Force ".\build\full\UGREEN-NAS-Test.exe" ".\dist-full\UGREEN-NAS-Test.exe"
Copy-Common ".\dist-full"
if (Test-Path ".\config\config.yml") {
    Copy-Item -Force ".\config\config.yml" ".\dist-full\config\config.yml"
} else {
    Write-Host "   ! 本地没有 config\config.yml，改带 config.example.yml（记得填真实凭据）" -ForegroundColor Yellow
    Copy-Item -Force ".\config\config.example.yml" ".\dist-full\config\config.yml"
}
if (Test-Path $AutoupdateSrc) {
    Copy-Item -Recurse -Force $AutoupdateSrc ".\dist-full\ugreen-nas-autoupdate"
    $bat = @(
        "@echo off",
        "set ""UGREEN_AUTOUPDATE_ROOT=%~dp0ugreen-nas-autoupdate""",
        "start """" ""%~dp0UGREEN-NAS-Test.exe"""
    ) -join "`r`n"
    Set-Content -Path ".\dist-full\启动-完整版.bat" -Value $bat -Encoding OEM
    Write-Host "   -> dist-full\（exe + 真实 config + 上传器 + 启动器；用「启动-完整版.bat」启动）" -ForegroundColor Green
} else {
    Write-Host "   ! 找不到上传器源目录：$AutoupdateSrc（跳过打包上传器）。" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "A 公开包：dist-public\   （拷走整个目录；双击 exe 即纯测试模式）"
Write-Host "B 完整包：dist-full\     （拷走整个目录；用 启动-完整版.bat 启动带上传器）"
Write-Host "首次务必人工核对：A 里无 账号/上传/录表物料标签；B 里有。" -ForegroundColor Yellow
