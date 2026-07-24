#Requires -Version 5.1
# 一次打两个包。软件本体（exe）完全一样，区别只在带不带 ugreen-nas-autoupdate 模块：
#   A  dist-public\  —— 公开/外部：exe + 公开配置模板，不带模块 → 上传/录表 UI 自动隐藏
#   B  dist-full\    —— 内部产线：同一个 exe + 公开配置模板 + 受控模块文件 + 启动器 → 完整功能
# 两个包都禁止携带真实 config.yml、环境文件、密钥和任何运行态数据；配置只能在目标机本地创建。
# 两个包都带 update-config.json（指向公开库、token 留空，公开库免鉴权）→ 都能自动更新「软件本体 exe」。
# 自更新只替换 exe，不动 ugreen-nas-autoupdate 模块。
#
# 用法：
#   powershell -ExecutionPolicy Bypass -File .\build-packages.ps1
#   powershell -ExecutionPolicy Bypass -File .\build-packages.ps1 -AutoupdateSrc "D:\path\to\ugreen-nas-autoupdate"
#
# 在 Windows 上跑（PyInstaller 只能打目标平台）。

param(
    [string]$AutoupdateSrc = "",
    [string]$UpdateOwner = "lyp04",
    [string]$UpdateRepo  = "ugreen-nas-factory-test"
)

$ErrorActionPreference = 'Stop'
Set-Location -Path $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($AutoupdateSrc)) {
    $moduleCandidates = @(
        (Join-Path $PSScriptRoot "..\ugreen-nas-factory-autoupdate"),
        (Join-Path $PSScriptRoot "..\ugreen-nas-autoupdate"),
        (Join-Path $env:USERPROFILE "ugreen-nas-autoupdate")
    )
    $AutoupdateSrc = $moduleCandidates |
        Where-Object { Test-Path -LiteralPath (Join-Path $_ "automation\runner.py") -PathType Leaf } |
        Select-Object -First 1
}
if ([string]::IsNullOrWhiteSpace($AutoupdateSrc)) {
    throw "找不到内部自动录表模块。请用 -AutoupdateSrc 指向 ugreen-nas-factory-autoupdate。"
}
$AutoupdateSrc = (Resolve-Path -LiteralPath $AutoupdateSrc).Path

if (-not (Test-Path ".venv\Scripts\python.exe")) { Write-Error "未找到 .venv，请先运行 .\install.ps1" }
$python = ".\.venv\Scripts\python.exe"
$pyinstaller = ".\.venv\Scripts\pyinstaller.exe"

# 本脚本始终同时产出公开包和内部完整包。完整包只接受下面的模块契约文件，
# 从空目录逐项复制；源仓库里的 .git/.venv/state/.env 等永远不会进入产物。
$requiredPublicConfig = @("config.example.yml", "selectors.yml")
foreach ($name in $requiredPublicConfig) {
    $source = Join-Path ".\config" $name
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "缺少公开配置文件：$source"
    }
    $sourceItem = Get-Item -LiteralPath $source
    if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "公开配置文件不能是符号链接或 junction：$source"
    }
}
$requiredModuleFiles = @(
    "automation\runner.py",
    "config\forms.json",
    "config\materials.json"
)
foreach ($relative in $requiredModuleFiles) {
    $source = Join-Path $AutoupdateSrc $relative
    if (-not (Test-Path -LiteralPath $source -PathType Leaf)) {
        throw "完整包构建需要自动录表模块契约文件：未找到 $source"
    }
}

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

# 两个包共用：exe + 公开配置模板 + selectors + update-config.json + 固定名称测速源文件
function Copy-Common {
    param([string]$OutDir)
    New-Item -ItemType Directory -Force (Join-Path $OutDir "config") | Out-Null
    Copy-Item -Force "$dist\UGREEN-NAS-Test.exe" (Join-Path $OutDir "UGREEN-NAS-Test.exe")
    Copy-Item -Force -LiteralPath ".\config\config.example.yml" -Destination (Join-Path $OutDir "config\config.example.yml")
    Copy-Item -Force -LiteralPath ".\config\selectors.yml" -Destination (Join-Path $OutDir "config\selectors.yml")
    Write-UpdateConfig $OutDir
    foreach ($p in $testPackages) {
        $source = ".\$p"
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $sourceItem = Get-Item -LiteralPath $source
            if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "测速源文件不能是符号链接或 junction：$source"
            }
            Copy-Item -Force -LiteralPath $source -Destination (Join-Path $OutDir $p)
        }
    }
}

$forbiddenDistributionSegments = @(
    ".git", ".venv", "venv", "state", "log", "logs", "tmp", "temp",
    "build", "dist", "apk", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"
)
$forbiddenDistributionExtensions = @(
    ".key", ".pem", ".pfx", ".p12", ".ppk", ".kdbx", ".log", ".tmp",
    ".bak", ".backup", ".old", ".orig", ".save", ".swp", ".swo"
)

function Get-NormalizedRelativePath {
    param([string]$RootPath, [string]$FullName)
    return $FullName.Substring($RootPath.Length).TrimStart([char[]]"\/").Replace('\', '/')
}

function Test-ForbiddenRelativePath {
    param([string]$RelativePath)
    foreach ($segment in @($RelativePath.Replace('\', '/') -split '/')) {
        if ($forbiddenDistributionSegments -contains $segment -or $segment.StartsWith(".") -or
            $segment -match '(?i)\.local\.' -or
            $segment -match '(?i)(?:^|\.)(?:bak|backup|old|orig|save|swp|swo)(?:\.|$)' -or
            $segment -match '~$' -or $segment -match '^#.*#$') {
            return $true
        }
    }
    return $false
}

function Assert-NoEmbeddedCredential {
    param([string]$Path, [string]$RelativePath)

    $text = [System.IO.File]::ReadAllText($Path)
    if ($text -match '(?i)https?://[^/\s:@]+:[^@\s/]+@') {
        throw "分发文件含带账号密码的 URL：$RelativePath"
    }
    $assignmentPattern = @'
(?im)(?:["']?(?:password|passwd|pwd|token|cookie|authorization|client_secret|api_key|access_key)["']?\s*[:=]\s*)(?:[rubf]*["'])([^"'{}\r\n$]{8,})(?:["'])
'@
    foreach ($match in [regex]::Matches($text, $assignmentPattern)) {
        $value = $match.Groups[1].Value.Trim()
        if ($value -and
            $value -notmatch '^(?i)(?:CHANGE_ME|REDACTED|PLACEHOLDER|EXAMPLE|<[^>]+>)$') {
            throw "分发文件疑似硬编码凭据：$RelativePath"
        }
    }
}

function Copy-AutoupdateModuleAllowlist {
    param([string]$SourceRoot, [string]$DestinationRoot)

    $sourceRootItem = Get-Item -LiteralPath $SourceRoot
    if (($sourceRootItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
        throw "自动录表模块根目录不能是符号链接或 junction：$SourceRoot"
    }
    $sourceRootPath = (Resolve-Path -LiteralPath $SourceRoot).Path.TrimEnd('\', '/')
    $automationRoot = Join-Path $sourceRootPath "automation"
    $moduleConfigRoot = Join-Path $sourceRootPath "config"
    foreach ($sourceDirectory in @($automationRoot, $moduleConfigRoot)) {
        $sourceDirectoryItem = Get-Item -LiteralPath $sourceDirectory
        if (($sourceDirectoryItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "自动录表模块白名单目录不能是符号链接或 junction：$sourceDirectory"
        }
    }

    New-Item -ItemType Directory -Force $DestinationRoot | Out-Null
    $sourceItems = @(Get-ChildItem -LiteralPath $automationRoot -Recurse -Force)
    foreach ($sourceItem in $sourceItems) {
        if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "自动录表模块白名单路径不能包含符号链接或 junction：$($sourceItem.FullName)"
        }
    }
    foreach ($sourceFile in @($sourceItems | Where-Object { -not $_.PSIsContainer -and $_.Extension -ieq ".py" })) {
        $relative = Get-NormalizedRelativePath $sourceRootPath $sourceFile.FullName
        if (Test-ForbiddenRelativePath $relative) { continue }
        $target = Join-Path $DestinationRoot $relative
        New-Item -ItemType Directory -Force (Split-Path -Parent $target) | Out-Null
        Copy-Item -Force -LiteralPath $sourceFile.FullName -Destination $target
    }

    foreach ($relative in @("config\forms.json", "config\materials.json")) {
        $source = Join-Path $sourceRootPath $relative
        $sourceItem = Get-Item -LiteralPath $source
        if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "自动录表模块配置不能是符号链接：$source"
        }
        $target = Join-Path $DestinationRoot $relative
        New-Item -ItemType Directory -Force (Split-Path -Parent $target) | Out-Null
        Copy-Item -Force -LiteralPath $source -Destination $target
    }

    # 依赖声明是唯一允许从模块根目录带入的可选文件；依赖本身仍须在目标机安装。
    foreach ($name in @("requirements.txt", "pyproject.toml")) {
        $source = Join-Path $sourceRootPath $name
        if (Test-Path -LiteralPath $source -PathType Leaf) {
            $sourceItem = Get-Item -LiteralPath $source
            if (($sourceItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
                throw "自动录表模块依赖声明不能是符号链接：$source"
            }
            Copy-Item -Force -LiteralPath $source -Destination (Join-Path $DestinationRoot $name)
        }
    }
}

function Assert-DistributionTreeSafe {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [switch]$AllowModule,
        [switch]$AllowLauncher
    )

    $rootPath = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\', '/')
    $publicFiles = @(
        "UGREEN-NAS-Test.exe",
        "config/config.example.yml",
        "config/selectors.yml",
        "config/update-config.json"
    ) + $testPackages
    $modulePrefix = "ugreen-nas-autoupdate/"
    $privateKeyNames = @("id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "authorized_keys")

    foreach ($item in Get-ChildItem -LiteralPath $rootPath -Recurse -Force) {
        $relative = Get-NormalizedRelativePath $rootPath $item.FullName
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "分发目录含符号链接或 junction：$relative"
        }
        if (Test-ForbiddenRelativePath $relative) {
            throw "分发目录含禁用路径：$relative"
        }

        if ($item.PSIsContainer) {
            $allowedDirectory = $relative -eq "config"
            if ($AllowModule) {
                $allowedDirectory = $allowedDirectory -or
                    $relative -eq "ugreen-nas-autoupdate" -or
                    $relative -eq "ugreen-nas-autoupdate/config" -or
                    $relative -eq "ugreen-nas-autoupdate/automation" -or
                    $relative.StartsWith("ugreen-nas-autoupdate/automation/")
            }
            if (-not $allowedDirectory) {
                throw "分发目录含非白名单目录：$relative"
            }
            continue
        }

        if ($item.Name -ieq "config.yml" -or $item.Name -match '(?i)^\.env(?:\..+)?$') {
            throw "分发目录含真实配置或环境文件：$relative"
        }
        if ($privateKeyNames -contains $item.Name -or $forbiddenDistributionExtensions -contains $item.Extension) {
            throw "分发目录含密钥、日志或临时文件：$relative"
        }

        $allowedFile = $publicFiles -contains $relative
        if ($AllowLauncher -and $relative -eq "启动-完整版.bat") {
            $allowedFile = $true
        }
        if ($AllowModule -and $relative.StartsWith($modulePrefix)) {
            $allowedFile = $relative -in @(
                "ugreen-nas-autoupdate/config/forms.json",
                "ugreen-nas-autoupdate/config/materials.json",
                "ugreen-nas-autoupdate/requirements.txt",
                "ugreen-nas-autoupdate/pyproject.toml"
            ) -or (
                $relative.StartsWith("ugreen-nas-autoupdate/automation/") -and
                $item.Extension -ieq ".py"
            )
        }
        if (-not $allowedFile) {
            throw "分发目录含非白名单文件：$relative"
        }

        if ($item.Length -le 16777216 -and $item.Extension -in @(".py", ".json", ".yml", ".yaml", ".txt", ".toml", ".bat", ".ps1")) {
            $text = [System.IO.File]::ReadAllText($item.FullName)
            if ($text -match '-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----') {
                throw "分发目录含私钥内容：$relative"
            }
            Assert-NoEmbeddedCredential -Path $item.FullName -RelativePath $relative
        }
    }
}

# ---------- Package A：公开 / 外部（不带上传器模块）----------
Write-Host "== [A] 公开包（不带上传器模块）==" -ForegroundColor Cyan
if (Test-Path ".\dist-public") { Remove-Item -Recurse -Force ".\dist-public" }
New-Item -ItemType Directory -Force ".\dist-public" | Out-Null
Copy-Common ".\dist-public"
Assert-DistributionTreeSafe ".\dist-public"
Write-Host "   -> dist-public\（exe + config.example.yml + update-config.json；无模块→上传 UI 隐藏；自更新开）" -ForegroundColor Green

# ---------- Package B：内部完整（带上传器模块）----------
Write-Host "== [B] 完整包（带上传器模块）==" -ForegroundColor Cyan
if (Test-Path ".\dist-full") { Remove-Item -Recurse -Force ".\dist-full" }
New-Item -ItemType Directory -Force ".\dist-full" | Out-Null
Copy-Common ".\dist-full"
$moduleOut = ".\dist-full\ugreen-nas-autoupdate"
Copy-AutoupdateModuleAllowlist -SourceRoot $AutoupdateSrc -DestinationRoot $moduleOut
# 启动器只在目标机创建本地 config.yml。首次运行会复制模板、打开记事本并退出，
# 防止占位账号直接启动测试；填写并保存后再次运行才启动 App。
$bat = @(
    "@echo off",
    "setlocal",
    "if not exist ""%~dp0config\config.yml"" (",
    "  copy /Y ""%~dp0config\config.example.yml"" ""%~dp0config\config.yml"" >nul",
    "  if errorlevel 1 exit /b 1",
    "  echo 已从公开模板创建本机配置。请填写 config.yml，保存后重新运行本启动器。",
    "  start """" notepad.exe ""%~dp0config\config.yml""",
    "  exit /b 2",
    ")",
    "if not defined UGREEN_AUTOUPDATE_PYTHON (",
    "  for /f ""delims="" %%P in ('where.exe python.exe 2^>nul') do if not defined UGREEN_AUTOUPDATE_PYTHON set ""UGREEN_AUTOUPDATE_PYTHON=%%P""",
    ")",
    "if not defined UGREEN_AUTOUPDATE_PYTHON (",
    "  echo 内部录表模块需要 Python 3.11+。请安装 Python，或设置 UGREEN_AUTOUPDATE_PYTHON 为 python.exe 的完整路径。",
    "  pause",
    "  exit /b 3",
    ")",
    """%UGREEN_AUTOUPDATE_PYTHON%"" -c ""import sys, requests, tkinter; assert sys.version_info >= (3, 11)"" >nul 2>nul",
    "if errorlevel 1 (",
    "  echo 内部录表模块依赖未就绪。请运行：""%UGREEN_AUTOUPDATE_PYTHON%"" -m pip install -r ""%~dp0ugreen-nas-autoupdate\requirements.txt""",
    "  pause",
    "  exit /b 4",
    ")",
    "set ""UGREEN_AUTOUPDATE_ROOT=%~dp0ugreen-nas-autoupdate""",
    "start """" ""%~dp0UGREEN-NAS-Test.exe"""
) -join "`r`n"
Set-Content -Path ".\dist-full\启动-完整版.bat" -Value $bat -Encoding OEM
Assert-DistributionTreeSafe ".\dist-full" -AllowModule -AllowLauncher
Write-Host "   -> dist-full\（exe + 公开配置模板 + 模块契约白名单 + 启动器；不含真实配置和运行态数据）" -ForegroundColor Green

Write-Host ""
Write-Host "== 完成 ==" -ForegroundColor Green
Write-Host "A 公开包：dist-public\   （无模块，上传 UI 隐藏；自更新开）"
Write-Host "B 完整包：dist-full\     （首次运行启动器会创建并打开本机 config.yml；填写后再次启动）"
Write-Host "真实配置、labels.yml、模块 state/.env/.git/.venv、日志、密钥和临时文件一律不打包；需要的数据请在目标机另行安装。"
Write-Host "注意：本脚本不 stamp 版本号，包内 exe 版本 = src/version.py 的默认值，首次启动会自更新到公开库最新 release。" -ForegroundColor Yellow
