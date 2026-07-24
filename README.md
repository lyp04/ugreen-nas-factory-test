# ugreen-nas-factory-test

Automated end-to-end factory testing for UGREEN NAS — a Windows desktop tool that drives the UGOS web UI through the full production checklist.

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Release](https://img.shields.io/github/v/release/lyp04/ugreen-nas-factory-test)](https://github.com/lyp04/ugreen-nas-factory-test/releases)

[English](#ugreen-nas-factory-test) · [中文](#中文)

An operator scans a serial number and the tool takes over: it finds the NAS on the LAN and runs first-time setup, firmware update, storage-pool and share creation, an SMB read/write speed test, and a page-by-page screenshot pass with temperature and fan-speed checks. If a form-entry module is installed, a passed report can be submitted to that module before cleanup and factory reset. Label printing is separate and opt-in.

Model is inferred from the SN prefix: DXP2800 (`HB`), DXP4800 (`EC671`), DXP4800Plus (`EC752`).

> [!WARNING]
> Credentials, printer queues, IPs, serial numbers, backend addresses, and tokens in examples are placeholders. Keep real values in ignored local files or environment variables.

## Features

- LAN auto-discovery of unprovisioned UGREEN NAS (UDP broadcast → mDNS → port scan), then test-on-detect.
- Drives the UGOS Pro web UI end to end with Playwright + system Edge: setup wizard → login → firmware update → pools and shares.
- SMB transfer speed test (upload + download) with Task-Manager throughput screenshots bound to the exact reading; below-threshold, incomplete-process, or remote-size-mismatch results auto-fail.
- Page-by-page screenshot capture (system update, network, storage, HDD/SSD read/write, resource monitor, three fan modes) with CPU-temperature and fan-RPM validation.
- Label printing — SN barcode, nameplate, EAN-13 carton, and turnover-box labels (ZPL/TSPL, rendered as bitmaps for exact sizing and font control).
- Fault auto-report: an unclassified failure packages its logs and screenshots and opens a fingerprint-deduplicated GitHub Issue (opt-in; token from an env var).
- Multi-device queue — scan one and it enqueues and tests while others run.
- Self-update from a public GitHub repo you control (no token needed).

## Architecture

The tool is a self-contained Windows app. On its own it runs the full test and, optionally, reports faults to a GitHub repo you own and self-updates from that repo's releases. One thing is external and optional: a separate `ugreen-nas-autoupdate` module that submits results to the factory's internal business system. When that module sits next to the app — or in a subdirectory next to the exe; [docs/module-interface.md](./docs/module-interface.md) has the exact discovery order — the app detects it and shows the form-entry and login UI; when it doesn't, that UI is hidden and the app is test-only. This repo ships no such module. To build your own, the interface is small: three `automation.runner` subcommands (`submit-report`, `forms refresh`, `login-ui`) plus three JSON files under the module's `config/` and `state/` — all specified in [docs/module-interface.md](./docs/module-interface.md).

```
                 scan SN
   [ operator ] --------> +------------------------------+   drives UGOS web UI    +------------------+
                          |  ugreen-nas-factory-test     | ----------------------> |  UGREEN NAS      |
                          |  (this repo, Windows app)    | <---------------------- |  (UGOS) on LAN   |
                          +--+--------+--------+---------+   Playwright + Edge     +------------------+
                             |        |        |
                 ZPL / TSPL  |        |        | release .exe (self-update) + fault issues
                             v        |        v
                   [ Zebra / Deli ]   |     +---------------------+
                      printers        |     | GitHub repo (yours) |
                                      |     +---------------------+
            optional, form entry —    |
            shown only if present ----+--> +-----------------------+   submit   +-----------------+
                                           | ugreen-nas-autoupdate | ---------> | internal system |
                                           | module (separate)     |            | (you provide)   |
                                           +-----------------------+            +-----------------+
```

## Quick start

Windows 10/11 with Python 3.10+ and Microsoft Edge (bundled since Win10). In order:

1. Install dependencies (creates a `.venv`, installs Playwright):

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

2. Copy the config template and edit it — **this is where you set the admin account, printer names, and scan subnet**:

   ```powershell
   copy config\config.example.yml config\config.yml
   notepad config\config.yml
   ```

   The GUI can open with the example file, but `test` and `cleanup` reject blank or `CHANGE_ME` admin credentials before touching a NAS.

3. Run the GUI (recommended), or the CLI:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run-gui.ps1
   powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 test --sn <SN> --nas-ip auto
   ```

CLI subcommands: `test`, `cleanup`, and the label commands `print-label` (SN barcode), `print-nameplate`, `print-ean13` (carton / middle-box EAN-13), `print-carton` (turnover-box). `print-label --list-printers` lists Windows print-queue names.

Three things to know before the first run:

- The CLI `test` leaves the unit provisioned when it finishes; pass `--cleanup-before-finish` / `--factory-reset-before-finish` to tear down like the production flow. The GUI has both on by default, and form entry (when the module is present) is GUI-only — the CLI has no flag for it.
- Before setup, pool changes, cleanup, or reset, the selected IP must broadcast the requested full SN. A requested factory reset is not counted as complete merely because the submit button was clicked: an isolated browser context re-discovers the same SN (and pre-reset MAC fingerprint when available) and requires two stable first-time-wizard observations. An unconfirmed reset fails as “needs verification” and is not auto-retried.
- If no transfer source file is in place yet (see `transfer.source_files` below), the first speed test generates a 5–20 GiB random file before it starts. That's a few minutes of apparent silence, not a hang.

**Not building from source?** The [GitHub Releases](https://github.com/lyp04/ugreen-nas-factory-test/releases) zip is the public core package only: exe plus public configuration files, with no form-entry module or transfer RARs. Copy `config.example.yml` to `config.yml`, edit it, and run the exe. The exe is unsigned, so Windows SmartScreen may require **More info** → **Run anyway**.

## Distribution: two packages

`build-packages.ps1` builds two local variants that **share one exe**. It requires the private form-entry module source even when you only need A, because one invocation validates and builds both outputs. Pass the module path explicitly when it is not in a recognized sibling location:

```powershell
powershell -ExecutionPolicy Bypass -File .\build-packages.ps1 `
  -AutoupdateSrc "D:\src\ugreen-nas-factory-autoupdate"
```

- `dist-public/` — exe + `config.example.yml`, `selectors.yml`, and generated `update-config.json`; no backend implementation, credentials, or module files. Form-entry UI stays hidden unless the operator separately installs a compatible module. Fixed-name transfer RARs are copied only by this local build when they exist.
- `dist-full/` — the same safe core plus the module allowlist and `启动-完整版.bat`. First launch creates a local `config.yml`, opens it in Notepad, and exits. Later launches verify Python 3.11+, Tkinter, and the current module's `requests` dependency before starting the app; install any extra dependencies declared by a replacement module yourself.

The full package copies only `automation/**/*.py`, `config/forms.json`, `config/materials.json`, and optional `requirements.txt` / `pyproject.toml`. Login creates `state/accounts.local.json` on the target machine; passwords are the module's concern and must never be stored by this app. Real `config.yml`, account state, label data, keys, logs, and virtual environments are excluded. The exact boundary is in [docs/module-interface.md](./docs/module-interface.md#packaging-allowlist).

Both local packages get an empty-token `update-config.json` for EXE-only updates. Selectors and the private module are not replaced by self-update, so rebuild/redeploy B when either contract changes. `build-exe.ps1` is a development build and emits a disabled `update-config.example.json`; only the tag workflow stamps a release version and publishes the public zip.

## Configuration

Routine site settings live in ignored `config/config.yml`, copied from `config.example.yml`. Selectors, update settings, label data, and the optional module have separate files.

| Setting | What to change |
|---|---|
| `admin.username` / `password` | UGOS admin account the setup wizard writes to the NAS |
| `network.subnet` | CIDR used only by the final port-scan fallback; UDP broadcast and mDNS are not restricted by it |
| `network.*_timeout` / `ugos_http_port` | discovery and service-readiness budgets; change only for a known slow network or nonstandard UGOS port |
| `pages` | known capture page keys to run, in order — one list for all models |
| `provision.pools` | pool/space names, RAID level, and disk labels for the supported models; strings must match the UGOS wizard text exactly. A new bay count also requires `MODEL_HDD_DISK_COUNTS` in `src/flows/provision.py` |
| `transfer.source_files` | per-model local file for the SMB write test (5 / 10 / 20 GiB); not committed. A local A/B build copies the three fixed filenames when present; GitHub Releases do not |
| `transfer.speed_thresholds_mb_s` | pass/fail speed floor per model — calibrate against your disks and network before trusting verdicts |
| other `transfer.*` timing values | sampling interval, stable samples, retry count, seed/finish/settle budgets; keep them consistent with source-file size and link speed |
| `validation.cpu_temp_max_c` | CPU temperature ceiling for the fan-mode pages |
| `validation.cpu_temp_recheck_seconds` | per-fan-page wait/re-capture budget before a bad reading is final |
| `browser.channel` | `msedge` (default, system Edge) / `chrome` / `chromium` — `chromium` needs a one-time `install.ps1 -WithChromium` first or the browser launch fails |
| other `browser.*` values | hidden/headless mode, viewport, language, and default timeout. Keep `language: zh-CN` unless selectors are translated too |
| printer sections | `name` is the Windows queue name and `dpi` must match the device. Blank-name discovery only recognizes Zebra/ZPL-style queues reliably; set Deli/EAN-13 queues explicitly. The 4800/4800Plus nameplate uses its fixed 600-dpi 100×37 mm reverse layout and ignores the configurable QR/barcode coordinates |
| `label_printer.auto_print_on_pass` | one PASS-time switch for SN, nameplate, and EAN-13 labels; turnover-box labels remain manual |
| `output_dir` | screenshots, logs, and reports — defaults to `./screenshot` next to the exe so a copied folder just works; absolute paths and env vars like `${USERPROFILE}` are fine too |
| `fault_report` | disabled by default. To opt in, set `enabled: true`, a private `owner`/`repo`, and `FAULT_REPORT_TOKEN` |
| `label_data_file` | path to a gitignored file holding your real P/N + EAN-13 tables — proprietary data is **never packaged automatically**; install it locally and point the target machine's `config.yml` at it. If it's missing, the `print-nameplate` CLI silently falls back to `placeholder_pn` (default `000000`); the GUI warns and skips instead |
| `paths.autoupdate_root` | where the optional `ugreen-nas-autoupdate` module lives (also settable via `UGREEN_AUTOUPDATE_ROOT`; full discovery order in [docs/module-interface.md](./docs/module-interface.md)) |
| `config/update-config.json` | deployed EXE update source, manifest asset, optional private-repo token, and pinned release tag |

One knob that looks safe but isn't: `browser.language` stays `zh-CN` — the selectors and pool names are Chinese text matches, so changing the UI language breaks most of them.

Page selectors are in `config/selectors.yml`; update them when the UGOS front end changes. Since the 2026-07 UGOS 1.17 rewrite (iView → Arco), affected selectors are written as comma-separated old-new unions so one config works across firmware generations — when a selector breaks, append the new one, don't replace the old.

### Code-level extension points

| Change | Files that must stay in sync |
|---|---|
| Add a NAS model | SN mapping in `src/utils/sn.py`; model sets/normalizers in `src/cli.py` and `src/form_entry.py`; disk counts in `src/flows/provision.py`; transfer size/link rules in `src/flows/capture.py`; label layouts/chooser in `src/utils/label.py` and `src/gui.py`; report labels in `src/report/`; `config.example.yml`; and the optional module's form mapping |
| Add or rename a capture page | a new standard navigation-and-screenshot page needs `pages` plus `capture_pages` in `selectors.yml`. Renaming a built-in key or adding special capture/validation behavior also requires its references in `src/measurements.py`, `src/cli.py`, `src/flows/capture.py`, tests, and any module field maps |
| Adapt a UGOS UI revision | append old/new selector unions in `selectors.yml`; setup/provision/reset state-machine changes belong under `src/flows/` |
| Change pass/fail rules | speed and temperature limits are YAML settings; fan-page scope and zero-RPM exceptions are centralized in `src/measurements.py` |
| Change label geometry | `src/utils/label.py`; keep proprietary P/N/EAN values in the external `label_data_file` |
| Change update source | deployed `config/update-config.json`; `fault_report` is independent and must not reuse the update token |
| Add an internal submission backend | implement the module contract below; do not put backend URLs, credentials, or proprietary field maps in this public repo |

## Bring your own login/upload module

The app does not know how your backend authenticates or accepts uploads. It resolves a module path from the environment,
config, the packaged subdirectory, or the sibling development layout, requires `automation/runner.py`, and invokes three
subprocess commands from that module's root:

```text
<python> -m automation.runner login-ui
<python> -m automation.runner forms refresh [--account <name>]
<python> -m automation.runner submit-report --payload <request.json>
```

`login-ui` owns the complete authentication flow: captcha, SSO, browser login, or token entry. It writes the signed-in
accounts to ignored `state/accounts.local.json`, sets `active`, and returns when its window closes. The app displays
`name`/`account`; it preserves the rest of each account object when updating that file but does not interpret, expose, or
pass the token to a module command. Do not store passwords, print tokens to stdout, or put
state in a package.

`forms refresh` owns `config/forms.json` and `config/materials.json`. `submit-report` receives a JSON file containing
the selected account name, report values, screenshot paths, grade, and selected materials. It performs any uploads and
backend submission, then prints one JSON object on the last stdout line. Successful final states are
`{"status":"success"}` and `{"status":"already_submitted"}`; a non-zero process exit is a failure. Keep stdout machine
readable and send diagnostics to stderr without credentials.

The frozen app runs the module with `UGREEN_AUTOUPDATE_PYTHON`, or system `python` if that variable is unset. The bundled
private form-entry module in package B needs a prepared Python 3.11+ interpreter and its dependencies. Discovery order, timeouts, JSON schemas,
captured value keys, account-file behavior, and the packaging allowlist are specified in
[docs/module-interface.md](./docs/module-interface.md).

## Repository layout

```
src/
├── gui.py            GUI main window (Tkinter), multi-device queue
├── cli.py            CLI entry (Click): test / cleanup / print-*
├── measurements.py   CPU-temp / fan-RPM pass-fail rules (shared by cli.py and flows/capture.py)
├── updater.py        self-update from GitHub Releases
├── form_entry.py     bridge to the ugreen-nas-autoupdate module (internal form entry)
├── flows/            login · setup wizard · provision · capture · cleanup · system update · factory reset
├── discovery/        NAS discovery (broadcast → mDNS → port scan)
├── report/           fault-report packaging, fingerprinting, GitHub Issues, redaction
└── utils/            browser control, SMB speed test, ZPL/TSPL label rendering, SN parsing, config
config/               config.yml (yours) + config.example.yml + selectors.yml + update-config.example.json
docs/                 implementation notes + module interface spec
tests/                unit + smoke tests
```

Implementation notes for Windows self-update, speed-reading/screenshot synchronization, UGOS selector compatibility, and the firmware-update state machine are in [docs/implementation-notes.md](./docs/implementation-notes.md).

## Requirements

- Source runs/builds: Windows 10/11, Python 3.10+, and system Microsoft Edge.
- Public release exe: Windows 10/11 and Edge; a separate Python install is not required.
- Package B form entry: Python 3.11+, Tkinter, and the external module's dependencies; set `UGREEN_AUTOUPDATE_PYTHON` when the prepared interpreter is not `python` on PATH.
- For label printing: a Zebra/Deli printer and its Windows queue name.
- LAN discovery sends UDP broadcasts and mDNS queries — allow the app through the Windows firewall prompt on first run; on managed corporate networks, ask IT to whitelist it if devices stop being found. Behind a TLS-intercepting proxy, `pip` and the GitHub update check need the usual `HTTPS_PROXY` / `SSL_CERT_FILE` setup.
- Optional fault reporting needs an explicitly enabled private target and a GitHub token in `FAULT_REPORT_TOKEN`. Internal form entry needs a compatible external module.

The production app is Windows-only, but Win32 window-management and printing helpers degrade safely on other platforms. The platform-neutral unit suite can therefore run on macOS/Linux too; Windows remains the release and packaged-exe validation target.

## Contributing

Issues and pull requests are welcome. Please keep changes focused and describe the motivation in the PR.

## License

[Apache-2.0](./LICENSE).

---

## 中文

[↑ English](#ugreen-nas-factory-test)

**ugreen-nas-factory-test** 是一个 Windows 桌面工具，自动化完成 UGREEN NAS 的整机出厂测试——驱动 UGOS 网页管理界面，跑完整条产线检查流程。

操作员扫一个序列号，工具就会在局域网找到 NAS，依次跑初始化、固件更新、建池建共享、SMB 读写测速和逐页截图判定。安装了录表模块时，通过的报告可先交给模块上传，再清理、恢复出厂。标签打印是独立的可选步骤。

机型由 SN 前缀自动识别：DXP2800（`HB`）、DXP4800（`EC671`）、DXP4800Plus（`EC752`）。

> 示例里的凭据、打印队列、IP、序列号、后端地址和 token 都是占位值。真实值只放在被忽略的本机文件或环境变量里。

### 功能

- 局域网自动发现未初始化的 UGREEN NAS（UDP 广播 → mDNS → 端口扫描），扫到即测。
- 用 Playwright + 系统 Edge 端到端驱动 UGOS Pro 网页：初始化向导 → 登录 → 固件更新 → 建池建共享。
- SMB 传输测速（上传 + 下载），任务管理器实时速率截图与读数严格绑定；低于阈值、复制进程未正常结束或 NAS 端文件大小不一致都会自动判失败。
- 逐页截图采集（系统更新、网口、存储池、HDD/SSD 读写、资源监控、风扇三模式），带 CPU 温度和风扇转速判定。
- 标签打印——SN 条码、铭牌、EAN-13 彩盒、周转箱标贴（ZPL/TSPL，用位图渲染保证精确尺寸和字体）。
- 故障自动上报：未归类的失败会打包日志和截图，按指纹去重地开一个 GitHub Issue（可选，token 走环境变量）。
- 多机排队——扫一个入队一个，边测边扫。
- 从你掌控的公开 GitHub 仓库自更新（不需要 token）。

### 架构

工具本身是自包含的 Windows App，单独就能跑完整测试，并可选地把故障上报到你自己的 GitHub 仓库、从该仓库的 release 自更新。唯一外部且可选的部分是一个独立的 `ugreen-nas-autoupdate` 模块，负责把结果录到工厂内部业务系统：模块放在 App 旁边（或 exe 同级子目录，完整查找顺序见 [docs/module-interface.md](./docs/module-interface.md)）时，App 探测到就显示录表和登录界面；没有模块时，界面隐藏，App 就是纯测试模式。完整数据流见上方 [Architecture](#architecture) 小节的 ASCII 图。本仓库不带这个模块——想自己写一个，接口很小：三条 `automation.runner` 子命令（`submit-report`、`forms refresh`、`login-ui`）加模块 `config/`、`state/` 下的三个 JSON 文件，规格全部在 [docs/module-interface.md](./docs/module-interface.md)。

### 快速开始

Windows 10/11，Python 3.10+，系统自带 Microsoft Edge（Win10 起内置）。按顺序来：

1. 安装依赖（建 `.venv`、装 Playwright）：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```

2. 复制配置模板再改——**管理员账号密码、打印机名、扫描网段都在这里改**：

   ```powershell
   copy config\config.example.yml config\config.yml
   notepad config\config.yml
   ```

   GUI 可以用示例文件启动，但 `test` 和 `cleanup` 会在接触 NAS 前拒绝空值或 `CHANGE_ME` 管理员凭据。

3. 启动 GUI（推荐），或用 CLI：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run-gui.ps1
   powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 test --sn <SN> --nas-ip auto
   ```

   CLI 子命令：`test`、`cleanup`，以及标签命令 `print-label`（SN 条码）、`print-nameplate`（铭牌）、`print-ean13`（彩盒/中箱 EAN-13）、`print-carton`（周转箱）。`print-label --list-printers` 列出 Windows 打印队列名。

首次跑之前有三件事要知道：

- CLI 的 `test` 跑完默认**不**清池、**不**恢复出厂，要传 `--cleanup-before-finish` / `--factory-reset-before-finish` 才和产线流程一致（GUI 默认两者都开）；自动录表只有 GUI 有，CLI 没有对应参数。
- 初始化、建池、清池或恢复出厂前，所选 IP 必须通过广播返回任务要求的完整 SN。恢复出厂也不能只凭“按钮已提交”算完成：程序会在隔离浏览器上下文中按完整 SN（能取得时再加复位前 MAC 指纹）重新发现同一台 NAS，并连续两次确认它回到首次初始化向导；无法确认时会标成“待确认”并停止，不会自动重跑整套测试。
- 如果还没放测速源文件（见下面 `transfer.source_files`），第一次测速前工具会先生成一个 5–20 GiB 的随机文件——会静默等几分钟，不是卡死。

**不想从源码构建**的话，从 [Releases](https://github.com/lyp04/ugreen-nas-factory-test/releases) 下载公开核心包。它只含 exe 和公开配置，不含录表模块与测速 RAR。复制 `config.example.yml` 为 `config.yml` 后再运行。exe 未签名，SmartScreen 可能要求点**更多信息** → **仍要运行**。

### 分发：两个包

`build-packages.ps1` 一次打两个本地包，**软件本体（exe）完全一样**。脚本始终同时校验并生成 A/B，因此即使只要 A，也要提供内部录表模块源码；不在默认同级位置时这样指定：

```powershell
powershell -ExecutionPolicy Bypass -File .\build-packages.ps1 `
  -AutoupdateSrc "D:\src\ugreen-nas-factory-autoupdate"
```

- `dist-public/`：exe + `config.example.yml`、`selectors.yml`、生成的 `update-config.json`；不含后端实现、凭据和模块文件。只有操作员另外安装兼容模块时录表 UI 才会出现。固定名称测速 RAR 只在本地构建机存在时复制。
- `dist-full/`：同一套安全核心 + 模块白名单 + `启动-完整版.bat`。首次启动会创建并打开本机 `config.yml` 后退出；之后会检查 Python 3.11+、Tkinter 和当前模块所需的 `requests`。自建替换模块若有额外依赖，需要另行安装。

完整包只从外部模块复制 `automation/**/*.py`、`config/forms.json`、`config/materials.json`，以及可选的 `requirements.txt` / `pyproject.toml`。登录后才在目标机创建 `state/accounts.local.json`；真实 `config.yml`、账号状态、标签数据、密钥、日志和虚拟环境都不进包。边界见[模块打包白名单](./docs/module-interface.md#打包白名单)。

两个本地包的 `update-config.json` 都不带 token，只更新 exe，不会替换 `selectors.yml` 或私有模块；接口变化后要重新部署 B。`build-exe.ps1` 是开发构建，输出的是停用状态的 `update-config.example.json`。只有 tag workflow 会写入正式版本号并发布公开 zip。

### 配置

日常产线参数放在 `config/config.yml`（从 `config.example.yml` 复制，真实文件不进 git）。页面选择器、自更新、标签数据和可选模块各有独立文件。

| 配置项 | 改什么 |
|---|---|
| `admin.username` / `password` | 初始化向导写进 NAS 的 UGOS 管理员账号 |
| `network.subnet` | 只给最后的端口扫描兜底用；UDP 广播和 mDNS 不受这个 CIDR 限制 |
| `network.*_timeout` / `ugos_http_port` | 发现和服务就绪等待时间；只在已知慢网或非标准 UGOS 端口时改 |
| `pages` | 已支持的截图页 key 及顺序——一份列表对所有机型生效 |
| `provision.pools` | 已支持机型的池名称/RAID/硬盘标签，字符串必须和 UGOS 建池向导文案逐字一致；新增盘位数还要改 `src/flows/provision.py` 的 `MODEL_HDD_DISK_COUNTS` |
| `transfer.source_files` | SMB 写测速源文件，按机型 5 / 10 / 20 GiB；不进 git。本地 A/B 构建会复制存在的三个固定文件名，GitHub Release 不带 |
| `transfer.speed_thresholds_mb_s` | 各机型测速判定下限——先按你的盘和网络实测校准，再相信判定结果 |
| 其他 `transfer.*` 时间参数 | 采样间隔、稳定样本、重试次数、种子/结束/落盘等待；要和源文件大小及链路速度一起校准 |
| `validation.cpu_temp_max_c` | 风扇模式页的 CPU 温度判定上限 |
| `validation.cpu_temp_recheck_seconds` | 每个风扇页读数不合格后的等待/重抓预算 |
| `browser.channel` | `msedge`（默认，系统 Edge）/ `chrome` / `chromium`——选 `chromium` 要先跑一次 `install.ps1 -WithChromium`，否则浏览器起不来 |
| 其他 `browser.*` | 隐藏/headless、视口、语言和默认超时；除非选择器也一起翻译，否则保持 `language: zh-CN` |
| 各打印机段 | `name` 是 Windows 队列名，`dpi` 必须匹配设备。留空自动识别只对 Zebra/ZPL 队列可靠，Deli/EAN-13 建议必填。4800/4800Plus 铭牌固定走 600dpi、100×37 mm 反向布局，不读取可配置的二维码/条码坐标 |
| `label_printer.auto_print_on_pass` | PASS 后统一尝试 SN、铭牌和 EAN-13；周转箱仍手动打印 |
| `output_dir` | 截图、日志、报告的输出目录——默认 `./screenshot`（相对 exe，整包复制即用），也可写绝对路径或 `${USERPROFILE}` 这类环境变量 |
| `fault_report` | 默认关闭；启用时同时设 `enabled: true`、自己的私有 `owner`/`repo` 和 `FAULT_REPORT_TOKEN` |
| `label_data_file` | 指向一个不进 git 的文件，里面是你真实的 P/N + EAN-13 对照表——专有数据**不会自动打包**，请在目标机本地安装并在本机 `config.yml` 里填写路径。文件缺失时 `print-nameplate` CLI 会**静默**回退 `placeholder_pn`（默认 `000000`）；GUI 则是警告并跳过 |
| `paths.autoupdate_root` | 可选 `ugreen-nas-autoupdate` 模块所在目录（也可用 `UGREEN_AUTOUPDATE_ROOT` 覆盖；完整查找顺序见 [docs/module-interface.md](./docs/module-interface.md)） |
| `config/update-config.json` | 部署后的 exe 更新源、manifest 资产、可选私库 token 和固定 release tag |

有一个看着无害但不能动的配置：`browser.language` 保持 `zh-CN`——选择器和池名全是中文文本匹配，单独改语言会让大部分选择器失配。

页面选择器在 `config/selectors.yml`，跟随 UGOS 前端变化更新。自 2026-07 UGOS 1.17 换 UI 框架（iView → Arco）起，受影响的选择器都写成「旧, 新」逗号并集，同一份配置兼容新旧固件——选择器失效时**追加新的、保留旧的**，不要整体替换。

#### 需要改代码的扩展点

| 要改什么 | 必须一起维护的文件 |
|---|---|
| 新增 NAS 机型 | `src/utils/sn.py` 的 SN 映射；`src/cli.py`、`src/form_entry.py` 的机型集合/归一化；`src/flows/provision.py` 的盘数；`src/flows/capture.py` 的测速文件大小/链路规则；`src/utils/label.py`、`src/gui.py` 的标签布局/选项；`src/report/` 的标签规则；示例配置和可选录表模块机型表 |
| 新增或重命名截图页 | 新增普通“导航后截图”页要改 `pages` 和 `selectors.yml` 的 `capture_pages`；重命名内建 key 或增加专门采集/判定时，还要同步 `src/measurements.py`、`src/cli.py`、`src/flows/capture.py`、测试和模块字段映射 |
| 适配新版 UGOS UI | 优先在 `selectors.yml` 追加新旧并集；初始化/建池/复位状态机变化放到 `src/flows/` |
| 调整判定 | 速度和温度阈值走 YAML；风扇页范围、静音 0 RPM 豁免集中在 `src/measurements.py` |
| 调整标签版式 | `src/utils/label.py`；真实 P/N/EAN 放外部 `label_data_file` |
| 更换自更新源 | 部署后的 `config/update-config.json`；它和 `fault_report` 相互独立，不能共用 token |
| 接入内部登录上传 | 实现下述模块契约；后端地址、凭据和专有表单字段不要放进本公开仓库 |

### 自建登录 / 上传模块

App 不知道你的后端怎样登录和上传。它按环境变量、配置、随包子目录、开发同级目录的顺序查找含
`automation/runner.py` 的模块，并在模块根目录调用：

```text
<python> -m automation.runner login-ui
<python> -m automation.runner forms refresh [--account <name>]
<python> -m automation.runner submit-report --payload <request.json>
```

`login-ui` 完整负责验证码、SSO、网页登录或手贴 token。登录成功后写入被忽略的
`state/accounts.local.json`、设置 `active`，窗口关闭即返回。App 只显示账号的 `name` / `account`；更新账号文件时会
保留整条对象，但不会解释、显示或把 token 作为命令参数传给模块。模块不要保存密码、不要把 token 打到 stdout，也不要把 `state` 打进包。

`forms refresh` 维护模块自己的 `config/forms.json` 与 `config/materials.json`。`submit-report` 收到的
JSON 文件含选中账号名、测试读数、截图路径、等级和物料清单；模块完成上传和后端提交后，stdout 最后一行只打印
一个 JSON 对象。成功终态必须是 `{"status":"success"}` 或
`{"status":"already_submitted"}`；进程退出码非 0 视为失败。诊断写 stderr，并先脱敏。

冻结版 App 优先使用 `UGREEN_AUTOUPDATE_PYTHON`，未设置时调用 PATH 上的 `python`。当前 B 包内置模块要求目标机
准备 Python 3.11+ 和模块依赖。模块查找顺序、三个命令的超时、JSON schema、截图读数 key、账号文件读写规则和
打包白名单都在 [docs/module-interface.md](./docs/module-interface.md)。

### 目录结构

```
src/
├── gui.py            GUI 主窗口（Tkinter），多机排队
├── cli.py            CLI 入口（Click）：test / cleanup / print-*
├── measurements.py   CPU 温度 / 风扇转速判定规则（cli.py 与 flows/capture.py 共用）
├── updater.py        从 GitHub Release 自更新
├── form_entry.py     对接 ugreen-nas-autoupdate 模块（内部业务系统录表）
├── flows/            登录 · 初始化向导 · 建池建共享 · 截图采集 · 清理 · 系统更新 · 恢复出厂
├── discovery/        NAS 发现（广播 → mDNS → 端口扫描）
├── report/           故障日志打包、指纹去重、GitHub Issue、脱敏
└── utils/            浏览器控制、SMB 测速、ZPL/TSPL 标签渲染、SN 解析、配置
config/               config.yml（你的）+ config.example.yml + selectors.yml + update-config.example.json
docs/                 实现细节 + 模块接口规格
tests/                单元 + smoke 测试
```

Windows 自更新、测速读数与截图同步、UGOS 选择器兼容和固件更新状态机的实现说明见 [docs/implementation-notes.md](./docs/implementation-notes.md)。

### 环境要求

- 源码运行/构建：Windows 10/11、Python 3.10+、系统 Microsoft Edge。
- 公开 Release exe：Windows 10/11 + Edge，不需要另装 Python。
- B 包录表：Python 3.11+、Tkinter 和外部模块依赖；准备好的解释器不在 PATH 时设置 `UGREEN_AUTOUPDATE_PYTHON`。
- 标签打印：Zebra/Deli 打印机 + 它的 Windows 队列名。
- 局域网发现要发 UDP 广播和 mDNS 查询——首次运行弹 Windows 防火墙授权时要放行；公司管控网络上如果扫不到设备，让 IT 加白名单。走 TLS 解密代理的内网，`pip` 和 GitHub 更新检查需要常规的 `HTTPS_PROXY` / `SSL_CERT_FILE` 配置。
- 可选故障上报必须显式启用、指向私有仓库，并在 `FAULT_REPORT_TOKEN` 里放 GitHub token。内部录表需要兼容的外部模块。

正式应用仍只支持 Windows，但 Win32 窗口管理与打印辅助代码在其他平台会安全降级，因此平台无关的单元测试也能在 macOS/Linux 上运行；发布和打包 exe 仍以 Windows 验证为准。

### 贡献

欢迎提 Issue 和 PR。改动请保持聚焦，并在 PR 里说明动机。

### 许可证

[Apache-2.0](./LICENSE)。
