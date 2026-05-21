# UGREEN NAS Factory Test

UGREEN NAS 出厂测试工具。这个仓库保留 NAS 初始化、系统更新、建池建共享、截图、传输测速、清理和恢复出厂相关流程；自动录表通过桥接接口交给独立的 `ugreen-nas-autoupdate` 项目处理。

## 快速使用

安装依赖：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

启动 GUI：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-gui.ps1
```

运行 CLI：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 test --sn SN123 --nas-ip auto
```

打包可执行文件：

```powershell
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
```

## 本地运行资产

仓库不跟踪运行产物、日志、截图、虚拟环境、PyInstaller 产物和大体积传输测试包。

自动录表桥接路径按优先级查找：

1. 环境变量 `UGREEN_AUTOUPDATE_ROOT`
2. `config/config.yml` 里的 `paths.autoupdate_root`
3. factory-test 项目同级目录 `ugreen-nas-autoupdate`

GUI 启动时会自动调用 `ugreen-nas-autoupdate` 的 `forms refresh` 命令刷新后台表单物料，不依赖外部定时任务。

传输测速用文件默认在项目根目录查找：

- `测试5G.rar`
- `测试10G.rar`
- `测试20G.rar`

如果这些文件放在别处，请改 `config/config.yml` 里的 `transfer.source_files`。

## 自动更新

GUI 启动时会按 `config/update-config.json` 的配置去 GitHub Releases 拉取新版本。模板见 `config/update-config.example.json`：

```json
{
  "enabled": true,
  "owner": "lyp04",
  "repo": "ugreen-nas-factory-test",
  "token": "github_pat_xxx",
  "manifestAsset": "update.json",
  "releaseTag": ""
}
```

- `enabled=false` 或缺少 owner/repo/token 时整个检查会被跳过，不会影响主流程。
- 发布时把 `update.json` 和实际的 exe 一起上传成 release asset。manifest 字段示例：

```json
{
  "packageName": "ugreen-nas-factory-test",
  "versionCode": 2,
  "versionName": "0.2.0",
  "exeAsset": "UGREEN-NAS-Test.exe",
  "sha256": "<exe 的 sha256>",
  "notes": "本次更新内容..."
}
```

- 客户端 `src/version.py` 里的 `VERSION_CODE` 严格小于 `update.json.versionCode` 时才会触发更新提示。
- 用户点 “下载并安装” 后，新 exe 会先校验 SHA-256，再由 `state/updates/updater.ps1` 等待当前进程退出后原地替换 exe 并重启。

## 目录

- `src/`: 出厂测试源码
- `config/`: NAS 页面选择器和测试配置
- `tests/`: 单元测试和 smoke 配置检查
- `scripts/`: 调试辅助脚本

## 验证

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests
python -m src.cli --help
```

## 生成说明

由 Codex 生成。
