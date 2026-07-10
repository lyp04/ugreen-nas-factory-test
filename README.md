# ugreen-nas-factory-test

Automated end-to-end factory testing for UGREEN NAS — a Windows desktop tool that drives the UGOS web UI through the full production checklist.

[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](./LICENSE)
[![Release](https://img.shields.io/github/v/release/lyp04/ugreen-nas-factory-test)](https://github.com/lyp04/ugreen-nas-factory-test/releases)

[English](#ugreen-nas-factory-test) · [中文](#中文)

An operator scans a serial number and the tool takes over: it finds the NAS on the LAN and runs it through first-time setup, firmware update, storage-pool and share creation, an SMB read/write speed test, and a page-by-page screenshot pass (with temperature and fan-speed checks). On success it submits the results to a separate module for form entry into the factory's internal business system, then cleans up and factory-resets the unit. Label printing (SN sticker, nameplate, EAN-13) is a separate, opt-in step — run by hand or turned on to auto-print on pass. It drives real hardware through a real browser, so most of the code is about surviving a UI that shifts between firmware versions and a Windows host that fails in quiet ways.

Model is inferred from the SN prefix: DXP2800 (`HB`), DXP4800 (`EC671`), DXP4800Plus (`EC752`).

> [!WARNING]
> Every account, password, printer name, repo, token, IP, and serial number in this repo and its config examples is a placeholder — swap in your own.

## Features

- LAN auto-discovery of unprovisioned UGREEN NAS (UDP broadcast → mDNS → port scan), then test-on-detect.
- Drives the UGOS Pro web UI end to end with Playwright + system Edge: setup wizard → login → firmware update → pools and shares.
- SMB transfer speed test (upload + download) with Task-Manager throughput screenshots bound to the exact reading; below-threshold auto-fails.
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

   Without a `config.yml` the app falls back to the example, whose admin credentials are the `CHANGE_ME` placeholder — the setup wizard would write *that* to the NAS, so edit it first.

3. Run the GUI (recommended), or the CLI:

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run-gui.ps1
   powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 test --sn <SN> --nas-ip auto
   ```

CLI subcommands: `test`, `cleanup`, and the label commands `print-label` (SN barcode), `print-nameplate`, `print-ean13` (carton / middle-box EAN-13), `print-carton` (turnover-box). `print-label --list-printers` lists Windows print-queue names.

Two things to know before the first run:

- The CLI `test` leaves the unit provisioned when it finishes; pass `--cleanup-before-finish` / `--factory-reset-before-finish` to tear down like the production flow. The GUI has both on by default, and form entry (when the module is present) is GUI-only — the CLI has no flag for it.
- If no transfer source file is in place yet (see `transfer.source_files` below), the first speed test generates a 5–20 GiB random file before it starts. That's a few minutes of apparent silence, not a hang.

**Not building from source?** Grab a pre-built release: download the zip from [Releases](https://github.com/lyp04/ugreen-nas-factory-test/releases), unzip, do the same `config.example.yml` → `config.yml` copy-and-edit (step 2), and double-click the exe. **The exe is unsigned**, so Windows SmartScreen warns on first launch — click **More info** → **Run anyway**.

## Distribution: two packages

`build-packages.ps1` builds two variants that **share one exe** — the only difference is whether the `ugreen-nas-autoupdate` module rides along:

```powershell
powershell -ExecutionPolicy Bypass -File .\build-packages.ps1
```

- `dist-public/` — exe + `config.example.yml`, **no module** → form-entry/upload UI stays hidden. For external / public use. (Any `测试*G.rar` transfer source files sitting on the build machine get copied into both packages — exclude them first if you don't want them shipped.)
- `dist-full/` — the same exe + real `config.yml` + the module + a launcher → full features (form entry to the internal system). Internal only.

Both carry an `update-config.json`, so they self-update the exe from your GitHub releases; the module and the proprietary label data don't change on update. The self-update target repo is baked in at build time — `build-packages.ps1 -UpdateOwner <you> -UpdateRepo <your-repo>` (defaults to the placeholder above), separate from `fault_report.owner`/`repo` in `config.yml`. (`build-exe.ps1` builds just the bare exe.)

## Configuration

Everything lives in `config/config.yml` (copied from `config.example.yml`). The real file is gitignored — credentials, tokens, and printer names stay local; the repo ships only the example. Points you'll customize:

| Setting | What to change |
|---|---|
| `admin.username` / `password` | UGOS admin account the setup wizard writes to the NAS |
| `network.subnet` | `auto` (detect the local subnet) or a fixed CIDR like `192.168.0.0/24` |
| `pages` | which pages get screenshotted, in order — one list for all models |
| `provision.pools` | pool/space names, RAID level, and disk labels per pool; the strings must match the UGOS wizard text exactly — edit here when your model has a different bay count |
| `transfer.source_files` | per-model local file for the SMB write test (5 / 10 / 20 GiB); gitignored, not shipped — provide your own or the tool auto-generates one on first run (slow, and needs ~2× that size in free disk) |
| `transfer.speed_thresholds_mb_s` | pass/fail speed floor per model — calibrate against your disks and network before trusting verdicts |
| `validation.cpu_temp_max_c` | CPU temperature ceiling for the fan-mode pages |
| `browser.channel` | `msedge` (default, system Edge) / `chrome` / `chromium` — `chromium` needs a one-time `install.ps1 -WithChromium` first or the browser launch fails |
| `browser.hidden` | `true` moves the browser off-screen; set `false` to watch it drive the UI when debugging selectors |
| `*_printer.*` | `name` = exact Windows print-queue name (blank = auto-detect by driver); `dpi` must match the physical printer or labels come out the wrong size; `auto_print_on_pass` prints on every PASS |
| `output_dir` | screenshots, logs, and reports — defaults to `./screenshot` next to the exe so a copied folder just works; absolute paths and env vars like `${USERPROFILE}` are fine too |
| `fault_report` | GitHub-issue auto-report; token comes from the `FAULT_REPORT_TOKEN` env var (unset = off); set `owner`/`repo` to your own fork |
| `label_data_file` | path to a gitignored file holding your real P/N + EAN-13 tables — proprietary data, ships only in the internal package. If it's missing, the `print-nameplate` CLI silently falls back to `placeholder_pn` (default `000000`); the GUI warns and skips instead |
| `paths.autoupdate_root` | where the optional `ugreen-nas-autoupdate` module lives (also settable via `UGREEN_AUTOUPDATE_ROOT`; full discovery order in [docs/module-interface.md](./docs/module-interface.md)) |

One knob that looks safe but isn't: `browser.language` stays `zh-CN` — the selectors and pool names are Chinese text matches, so changing the UI language breaks most of them.

Page selectors are in `config/selectors.yml`; update them when the UGOS front end changes. Since the 2026-07 UGOS 1.17 rewrite (iView → Arco), affected selectors are written as comma-separated old-new unions so one config works across firmware generations — when a selector breaks, append the new one, don't replace the old.

## Repository layout

```
src/
├── gui.py            GUI main window (Tkinter), multi-device queue
├── gui_no_form.py    same GUI with form entry force-hidden (sets UGREEN_DISABLE_FORM_ENTRY=1)
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

Implementation notes and the hard-won gotchas — self-update on Windows, keeping speed readings and screenshots in sync, UGOS selector hell, the 30-minute firmware state machine — are in [docs/implementation-notes.md](./docs/implementation-notes.md).

## Requirements

- Windows 10/11, Python 3.10+, system Microsoft Edge (no Chromium download).
- For label printing: a Zebra/Deli printer and its Windows queue name.
- LAN discovery sends UDP broadcasts and mDNS queries — allow the app through the Windows firewall prompt on first run; on managed corporate networks, ask IT to whitelist it if devices stop being found. Behind a TLS-intercepting proxy, `pip` and the GitHub update check need the usual `HTTPS_PROXY` / `SSL_CERT_FILE` setup.
- Optional: fault reporting needs a GitHub token in `FAULT_REPORT_TOKEN`; internal form entry needs the separate `ugreen-nas-autoupdate` module alongside the app.

The code is Windows-only at import time (`browser_control.py` binds Win32 APIs unconditionally), so tests that import the CLI/GUI fail collection on macOS/Linux; the pywin32-backed printing paths degrade more gracefully behind `is_windows()` guards.

## Contributing

Issues and pull requests are welcome. Please keep changes focused and describe the motivation in the PR.

## License

[Apache-2.0](./LICENSE).

---

## 中文

[↑ English](#ugreen-nas-factory-test)

**ugreen-nas-factory-test** 是一个 Windows 桌面工具，自动化完成 UGREEN NAS 的整机出厂测试——驱动 UGOS 网页管理界面，跑完整条产线检查流程。

操作员扫一个序列号，工具就接管后续：在局域网里找到这台 NAS，依次跑初始化向导、固件更新、建存储池和共享、SMB 读写测速、逐页截图采集（含温度和风扇转速判定）。通过后把结果交给一个独立模块去录工厂内部业务系统的表单，再清理、恢复出厂。标签打印（SN 标贴、铭牌、EAN-13）是可选的单独一步——手动打，或开启「通过即自动打印」。它驱动的是真机、走的是真浏览器，所以大部分代码都在应付两件事：跨固件版本会变的 UGOS 界面，和一个各种静默失败的 Windows 环境。

机型由 SN 前缀自动识别：DXP2800（`HB`）、DXP4800（`EC671`）、DXP4800Plus（`EC752`）。

> 仓库和示例配置里出现的账号、密码、打印机名、仓库名、token、IP、序列号都是占位示例，换成你自己的。

### 功能

- 局域网自动发现未初始化的 UGREEN NAS（UDP 广播 → mDNS → 端口扫描），扫到即测。
- 用 Playwright + 系统 Edge 端到端驱动 UGOS Pro 网页：初始化向导 → 登录 → 固件更新 → 建池建共享。
- SMB 传输测速（上传 + 下载），任务管理器实时速率截图与读数严格绑定，低于阈值自动判失败。
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

   不复制也能起：找不到 `config.yml` 时会回退到示例，但示例里管理员账号是 `CHANGE_ME` 占位——初始化向导会把它写成 NAS 管理员密码，所以先改。

3. 启动 GUI（推荐），或用 CLI：

   ```powershell
   powershell -ExecutionPolicy Bypass -File .\run-gui.ps1
   powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 test --sn <SN> --nas-ip auto
   ```

   CLI 子命令：`test`、`cleanup`，以及标签命令 `print-label`（SN 条码）、`print-nameplate`（铭牌）、`print-ean13`（彩盒/中箱 EAN-13）、`print-carton`（周转箱）。`print-label --list-printers` 列出 Windows 打印队列名。

首次跑之前有两件事要知道：

- CLI 的 `test` 跑完默认**不**清池、**不**恢复出厂，要传 `--cleanup-before-finish` / `--factory-reset-before-finish` 才和产线流程一致（GUI 默认两者都开）；自动录表只有 GUI 有，CLI 没有对应参数。
- 如果还没放测速源文件（见下面 `transfer.source_files`），第一次测速前工具会先生成一个 5–20 GiB 的随机文件——会静默等几分钟，不是卡死。

**不想从源码构建**的话，直接用打好的发布包：从 [Releases](https://github.com/lyp04/ugreen-nas-factory-test/releases) 下 zip、解压，同样做一遍 `config.example.yml` → `config.yml` 复制改配置（第 2 步），双击 exe 即可。**exe 未签名**，首次运行 SmartScreen 会拦——点**更多信息** → **仍要运行**。

### 分发：两个包

`build-packages.ps1` 一次打两个包，**软件本体（exe）完全一样**，区别只在带不带 `ugreen-nas-autoupdate` 模块：

- `dist-public/`：exe + `config.example.yml`，**不带模块** → 探测不到上传器，录表/上传界面自动隐藏。给外部 / 公开用。（构建机上如果放着 `测试*G.rar` 测速源文件，两个包都会带上——不想带就先挪走。）
- `dist-full/`：**同一个 exe** + 真实 `config.yml` + 模块 + 启动器 → 完整功能（含内部业务系统录表）。仅供内部产线。

两个包都带 `update-config.json`，会自动更新软件本体（exe）；模块和专有打印数据不随更新变动。自更新指向哪个仓库是构建时定死的——`build-packages.ps1 -UpdateOwner <你> -UpdateRepo <你的仓库>`（默认是上面那个占位仓库），和 `config.yml` 里的 `fault_report.owner`/`repo` 是两套。（`build-exe.ps1` 只打裸 exe。）

### 配置

所有配置在 `config/config.yml`（从 `config.example.yml` 复制）。真实文件不进 git——凭据、token、打印机名只留本地，仓库只带示例。要改的点：

| 配置项 | 改什么 |
|---|---|
| `admin.username` / `password` | 初始化向导写进 NAS 的 UGOS 管理员账号 |
| `network.subnet` | `auto`（自动探测本机网段）或写死某个 CIDR（如 `192.168.0.0/24`） |
| `pages` | 逐页截图清单及顺序——一份列表对所有机型生效 |
| `provision.pools` | 每个池的名称/RAID/硬盘标签，字符串必须和 UGOS 建池向导页面文案逐字一致；换盘位数的机型在这里改 |
| `transfer.source_files` | SMB 写测速用的本机源文件，按机型 5 / 10 / 20 GiB；不进 git、不随包——自己放，或首次运行时工具自动生成（慢，需约 2 倍大小的空闲磁盘） |
| `transfer.speed_thresholds_mb_s` | 各机型测速判定下限——先按你的盘和网络实测校准，再相信判定结果 |
| `validation.cpu_temp_max_c` | 风扇模式页的 CPU 温度判定上限 |
| `browser.channel` | `msedge`（默认，系统 Edge）/ `chrome` / `chromium`——选 `chromium` 要先跑一次 `install.ps1 -WithChromium`，否则浏览器起不来 |
| `browser.hidden` | `true` 把浏览器移到屏幕外；调试选择器时改 `false` 能看到它实际在点什么 |
| `*_printer.*` | `name` = Windows 打印队列的精确名字（留空按驱动自动识别）；`dpi` 必须和物理打印机一致，配错不报错、只会打出尺寸不对的标签；`auto_print_on_pass` 开启后每次 PASS 自动出票 |
| `output_dir` | 截图、日志、报告的输出目录——默认 `./screenshot`（相对 exe，整包复制即用），也可写绝对路径或 `${USERPROFILE}` 这类环境变量 |
| `fault_report` | 故障自动上报；token 从 `FAULT_REPORT_TOKEN` 环境变量读（不设=停用）；fork 自用把 `owner`/`repo` 改成你自己的 |
| `label_data_file` | 指向一个不进 git 的文件，里面是你真实的 P/N + EAN-13 对照表——专有数据，只随内部完整包分发。文件缺失时 `print-nameplate` CLI 会**静默**回退 `placeholder_pn`（默认 `000000`）；GUI 则是警告并跳过 |
| `paths.autoupdate_root` | 可选 `ugreen-nas-autoupdate` 模块所在目录（也可用 `UGREEN_AUTOUPDATE_ROOT` 覆盖；完整查找顺序见 [docs/module-interface.md](./docs/module-interface.md)） |

有一个看着无害但不能动的配置：`browser.language` 保持 `zh-CN`——选择器和池名全是中文文本匹配，单独改语言会让大部分选择器失配。

页面选择器在 `config/selectors.yml`，跟随 UGOS 前端变化更新。自 2026-07 UGOS 1.17 换 UI 框架（iView → Arco）起，受影响的选择器都写成「旧, 新」逗号并集，同一份配置兼容新旧固件——选择器失效时**追加新的、保留旧的**，不要整体替换。

### 目录结构

```
src/
├── gui.py            GUI 主窗口（Tkinter），多机排队
├── gui_no_form.py    同一个 GUI，强制隐藏录表界面（设 UGREEN_DISABLE_FORM_ENTRY=1）
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

工厂实机调试积累的坑点和实现取舍——Windows 自更新、测速读数与截图同步、UGOS 选择器地狱、30 分钟固件状态机——在 [docs/implementation-notes.md](./docs/implementation-notes.md)。

### 环境要求

- Windows 10/11、Python 3.10+、系统 Microsoft Edge（无需下载 Chromium）。
- 标签打印：Zebra/Deli 打印机 + 它的 Windows 队列名。
- 局域网发现要发 UDP 广播和 mDNS 查询——首次运行弹 Windows 防火墙授权时要放行；公司管控网络上如果扫不到设备，让 IT 加白名单。走 TLS 解密代理的内网，`pip` 和 GitHub 更新检查需要常规的 `HTTPS_PROXY` / `SSL_CERT_FILE` 配置。
- 可选：故障上报需要在 `FAULT_REPORT_TOKEN` 里放 GitHub token；内部业务系统录表需要 App 旁边有独立的 `ugreen-nas-autoupdate` 模块。

代码在导入期就是 Windows-only 的（`browser_control.py` 无条件绑定 Win32 API），所以 import 到 CLI/GUI 的测试在 macOS/Linux 上收集即失败；pywin32 支撑的打印路径有 `is_windows()` 守卫，降级得更体面。

### 贡献

欢迎提 Issue 和 PR。改动请保持聚焦，并在 PR 里说明动机。

### 许可证

[Apache-2.0](./LICENSE)。
