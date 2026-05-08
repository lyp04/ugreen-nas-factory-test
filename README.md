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

自动录表桥接默认查找：

```text
${USERPROFILE}\ugreen-nas-autoupdate
```

如果放在其他位置，请设置 `UGREEN_AUTOUPDATE_ROOT`。

传输测速用文件默认在项目根目录查找：

- `测试5G.rar`
- `测试10G.rar`
- `测试20G.rar`

如果这些文件放在别处，请改 `config/config.yml` 里的 `transfer.source_files`。

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
