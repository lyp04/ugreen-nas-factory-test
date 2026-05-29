# UGREEN NAS 出厂测试工具

Windows 桌面工具，自动化完成 UGREEN NAS 的出厂全流程测试：系统初始化、固件更新、建池建共享、SMB 传输测速、截图采集、标签打印和恢复出厂设置。测试结果通过桥接接口交给独立的 `ugreen-nas-autoupdate` 项目自动录入 内部系统 表单。

已支持机型：DXP2800（2 盘位）、DXP4800（4 盘位）、DXP4800Plus（4 盘位 + M.2）。

当前版本 0.1.0。

## 功能

- 局域网自动发现 NAS（mDNS / 端口扫描 / UGREEN 广播），扫到即测
- 浏览器自动驾驶 UGOS 管理页面（Playwright + Edge），完成向导 → 登录 → 固件更新 → 建池建共享
- SMB 传输测速（上传 + 下载），抓取 Windows 任务管理器实时速率截图，低于阈值自动判定失败
- 逐页截图采集（系统更新、网口、存储池、读写速率、资源监控、风扇模式），绑定 CPU 温度 / 风扇转速等指标
- CPU 温度超限（> 60 ℃）全机型自动判定测试失败
- 标签打印：SN 条码标签（模版二）、铭牌标贴（模版一）、69 码 EAN-13（模版五）、周转箱标贴（模版六）
- 测试通过后自动录表到 内部系统（通过 `ugreen-nas-autoupdate` 桥接）
- GUI 支持多台 NAS 排队并发测试，扫码枪扫一个排一个
- App 自更新（从 GitHub Release 拉取新版本，SHA-256 校验后原地替换）

## 标签打印功能

本工具支持 4 种标签模版，覆盖从单机到整箱的全部标贴需求：

| 模版 | 尺寸 | 内容 | 打印机 | 每次份数 |
|------|------|------|--------|----------|
| 模版二（SN 标签） | 42×25mm | Code 128 条码 + SN 文字 | Zebra ZD888 (203dpi) | 2（彩盒+中箱） |
| 模版一（铭牌标贴） | 83.7×36.7mm | QR 码 + Code 128 + P/N + SN | Zebra ZT610 (600dpi) | 1 |
| 模版五（69 码） | 50×30mm | EAN-13 条码 + Model + P/N 标题 | Deli DL-888T (203dpi) | 2（彩盒+中箱） |
| 模版六（周转箱） | 102×152mm | PID + QR + 4×SN 条码 + PO + 日期 | Zebra ZD888 (203dpi) | 2 |

### 触发方式

- **手动打印：** GUI 选中设备 → 点击「打印标签」→ 勾选需要的模版 → 确认
- **自动打印：** 勾选「自动打印标签」复选框后，测试通过即自动打印已勾选的模版
- **CLI 打印：** `run-cli print-label --sn xxx`、`print-nameplate`、`print-ean13`、`print-carton`

### P/N 和 EAN-13 自动查表

SN 前缀自动识别机型（2800 / 4800 / 4800Plus），结合 A/B 等级从内置查表得到 P/N 和 EAN-13：

| 机型 | 等级 | P/N | EAN-13 |
|------|------|-----|--------|
| DXP2800 | A | 00000 | 6900000000000 |
| DXP2800 | B | 00001 | 6900000000001 |
| DXP4800 | A | 00002 | 6900000000002 |
| DXP4800 | B | 00003 | 6900000000003 |
| DXP4800Plus | A | 00004 | 6900000000004 |
| DXP4800Plus | B | 00005 | 6900000000005 |

新机型上线时需要在 `src/utils/label.py` 的 `NAMEPLATE_PN_TABLE` 和 `NAMEPLATE_EAN13_TABLE` 字典中添加对应条目。

### 周转箱标贴特殊逻辑

- 必须选中恰好 4 个**同机型**的 SN 才能打印（否则按钮灰色不可选）
- QR 码内容格式：`SN*{PID}*{QTY}*{PO}*{DATE}*{流水号}`
- 流水号按天计数、跨天清零，存储在 `output_dir` 下的 `carton_seq.json`
- 打印失败不会消耗序号（`peek_carton_seq` 只读，`commit_carton_print` 才提交）
- 审计日志写入 `carton_log.jsonl`

### 添加 / 更换打印机

**第一步：查看当前 Windows 打印队列**

```powershell
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 print-label --list-printers
```

输出示例：

```
ZDesigner ZD888-203dpi ZPL (副本 1)    driver=ZDesigner ZD888-203dpi ZPL    port=USB004
ZDesigner ZD888-203dpi ZPL             driver=ZDesigner ZD888-203dpi ZPL    port=USB002
ZDesigner ZT610-600dpi ZPL             driver=ZDesigner ZT610-600dpi ZPL    port=USB001
DeliDL-888T                            driver=Deli DL-888T                  port=USB003
```

**第二步：在 `config/config.yml` 中写入精确名字**

```yaml
# SN 标签（模版二）—— 找到 USB004 上的 ZD888 副本
label_printer:
  name: "ZDesigner ZD888-203dpi ZPL (副本 1)"  # 必须和 --list-printers 输出完全一致
  dpi: 203
  quantity: 2
  auto_print_on_pass: false

# 铭牌（模版一）—— ZT610 工业机
nameplate_printer:
  name: "ZDesigner ZT610-600dpi ZPL"
  dpi: 600
  quantity: 1
  auto_print_on_pass: false
  qr_top_left_mm: [65.0, 5.0]       # QR 码左上角坐标（mm）
  strip_top_left_mm: [6.0, 25.0]    # 条码带左上角坐标（mm）
  placeholder_pn: "000000"           # 查不到 P/N 时的占位

# 69 码（模版五）—— Deli 热敏机
ean13_printer:
  name: "DeliDL-888T"
  dpi: 203
  quantity: 2
  auto_print_on_pass: false

# 周转箱（模版六）—— 第二台 ZD888（不带副本后缀）
carton_printer:
  name: "ZDesigner ZD888-203dpi ZPL"
  dpi: 203
  quantity: 2
  auto_print_on_pass: false
  po_default: "XXXXXXXXXXX"  # PO 占位，后续接真实订单时改
  warehouse: "收料仓"       # 收料仓名称
```

**关键注意事项：**

- `name` 字段**大小写不敏感但必须精确匹配**，不做子串匹配。写错一个字符就会找不到打印机
- 同一型号打印机如果装了多个驱动实例（如 "副本 1"、"副本 2"），必须区分清楚哪个接了哪根 USB 线
- `dpi` 必须和打印机实际分辨率一致，否则标签会整体缩放变形
- `auto_print_on_pass: true` 会在测试通过后立刻打印，适合流水线无人看管场景

**第三步：试打验证**

```powershell
# 干跑（不发打印机），生成 PNG 预览
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 print-label --sn TEST1234 --preview ./test.png --no-print

# 实际打印一份
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 print-label --sn TEST1234 --quantity 1

# 导出 ZPL 原始指令（排查打印机不吃指令的问题）
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 print-label --sn TEST1234 --zpl-out ./test.zpl --no-print
```

### 跨平台开发说明

标签渲染（生成 ZPL/TSPL 指令 + PNG 预览）在 macOS/Linux 上也能跑，只是无法发送到 Windows 打印队列。开发调试时用 `--preview` + `--no-print` 即可在任何平台上预览效果。

---

## 快速使用

安装依赖：

```powershell
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

启动 GUI（推荐）：

```powershell
powershell -ExecutionPolicy Bypass -File .\run-gui.ps1
```

运行 CLI：

```powershell
# 完整测试流程
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 test --sn SN123 --nas-ip auto

# 仅清理存储池
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 cleanup --sn SN123 --nas-ip auto

# 连通性检查
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 smoke --nas-ip 192.168.0.100

# 打印 SN 标签（模版二）
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 print-label --sn SN123

# 打印铭牌标贴（模版一）
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 print-nameplate --sn SN123

# 打印 69 码 EAN-13（模版五）
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 print-ean13 --sn SN123

# 打印周转箱标贴（模版六）
powershell -ExecutionPolicy Bypass -File .\run-cli.ps1 print-carton --sn SN1 SN2 SN3 SN4
```

打包可执行文件：

```powershell
powershell -ExecutionPolicy Bypass -File .\build-exe.ps1
```

## 配置

所有配置在 `config/config.yml`，关键项：

| 配置项 | 说明 |
|--------|------|
| `admin.username / password` | UGOS 登录账号 |
| `network.subnet` | NAS 发现的扫描网段 |
| `pages` | 需要截图采集的页面列表 |
| `provision.pools` | 自动建池方案（RAID 级别、磁盘选择） |
| `transfer.source_files` | 各机型测速源文件路径（按 SN 前缀区分机型） |
| `transfer.speed_thresholds_mb_s` | 传输速率通过阈值（MB/s），按机型可覆盖 |
| `validation.cpu_temp_max_c` | CPU 温度上限（℃），超过即判定失败 |
| `label_printer` | SN 条码标签打印机（模版二） |
| `nameplate_printer` | 铭牌标贴打印机（模版一） |
| `ean13_printer` | 69 码 EAN-13 打印机（模版五） |
| `carton_printer` | 周转箱标贴打印机（模版六） |
| `output_dir` | 截图和日志输出目录 |
| `paths.autoupdate_root` | 自动录表系统目录（可被 `UGREEN_AUTOUPDATE_ROOT` 覆盖） |

页面选择器在 `config/selectors.yml`，跟随 UGOS 前端变化更新。

## 项目结构

```
src/
├── gui.py                     GUI 主窗口（Tkinter），多任务排队
├── cli.py                     CLI 入口（Click），test / cleanup / smoke / print-*
├── form_entry.py              内部系统 自动录表桥接
├── updater.py                 App 自更新（GitHub Release）
├── version.py                 版本号
├── flows/
│   ├── login.py               UGOS 登录
│   ├── setup_wizard.py        初始化向导
│   ├── provision.py           建池建共享
│   ├── capture.py             截图采集 + 传输测速（最复杂的模块）
│   ├── cleanup.py             存储池清理
│   ├── system_update.py       固件更新（30 分钟状态机）
│   └── reset_factory.py       恢复出厂设置
├── discovery/
│   ├── discover.py            NAS 发现调度（广播 → mDNS → 端口扫描三级）
│   ├── mdns_scanner.py        mDNS 扫描
│   ├── port_scanner.py        端口扫描
│   └── ugreen_broadcast.py    UGREEN 广播协议
└── utils/
    ├── browser_control.py     Playwright 浏览器管理（隐藏窗口、PID 追踪）
    ├── screenshot.py          截图工具（SN 变更时目录迁移）
    ├── smb_transfer.py        SMB 传输测速（分块拷贝 + 进度文件）
    ├── label.py               ZPL/TSPL 标签渲染（位图条码 + 字体回退）
    ├── sn.py                  SN 解析（机型、等级、P/N 查表）
    ├── config_loader.py       配置加载
    ├── logger.py              日志
    └── retry.py               重试装饰器（指数退避）
config/
├── config.yml                 主配置
├── selectors.yml              UGOS 页面 CSS 选择器
└── update-config.example.json 自更新配置模板
tests/                         单元测试 + smoke 测试
scripts/                       调试辅助脚本（DOM 分析、页面探测）
```

---

## 踩坑记录与实现细节

以下是在工厂实机调试中积累的坑点，改一行可能全盘崩盘，务必通读。

### 1. 自动更新：Windows 上最难搞的部分

自动更新经历了至少 5 轮迭代，每轮修一个静默失败：

**`DETACHED_PROCESS` 会让 PowerShell 的文件 I/O 彻底失效。** 最初用 `DETACHED_PROCESS` 标志启动 swap 脚本（为了不弹控制台窗口），结果脚本根本没执行——PowerShell 在 `DETACHED_PROCESS` 下的 `Add-Content`、文件读写全部静默失败。改用 `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP` 组合：前者隐藏窗口，后者让子进程在父进程退出后继续运行。

**`Move-Item -Force` 会假装成功。** PowerShell 的 `Move-Item -Force` 在作为 detached 子进程运行时，被观察到 cmdlet 层面报告成功，但底层 NTFS 重命名实际没动——新旧 exe 都原封不动。改用 .NET API `[System.IO.File]::Delete()` + `[System.IO.File]::Move()`，失败会直接抛异常。swap 前后都记录 SHA-256 前缀用于事后核对。

**Windows Defender 首次执行扫描和自动重启之间的竞态。** PyInstaller `--onefile` 打包的 exe 启动时会解压 `python312.dll` 到 `_MEI*` 临时目录。Defender 对一个刚写入的 50MB 可执行文件做首次扫描，扫描期间 DLL 被锁住，PyInstaller 的引导程序加载不到 DLL 就报 `ERROR_MOD_NOT_FOUND`（"找不到指定的模块"）。第一版修复加了 3 秒 `Start-Sleep`，但 Defender 扫描时间没有安全上界。最终方案：swap 完成后**不自动重启**，弹窗提示用户手动双击 exe——此时 Defender 已经扫描完毕，再启动不会出错。

**`versionCode` 可能重复。** CI 用 `git rev-list --count HEAD` 算 `versionCode`，对同一个 commit 重新打 tag（比如只改了配置）会产生两个 `versionCode` 相同的 release，updater 会跳过新版本。修复：用 `versionName` 的语义化版本号做 tiebreaker。

> 教训：桌面自更新在 Windows 上是一个雷区。进程创建标志、文件移动、杀毒软件、版本号，每一层都有静默失败模式。一定要记日志、验哈希、宁可失败也不要假装成功。

### 2. 传输测速：截图和数值必须是同一瞬间

**先读值再截图 = 必然对不上。** 磁盘传输速率是突发性的：代码在 A 时刻读到 344.8 MB/s，下一个刷新周期截图时任务管理器已经显示 26.4 MB/s。QA 拿到的截图上写着 26.4，但报告里记着 344.8——无法自圆其说。

修复：**先截图，再从截图对应的 DOM 状态重新读值**。只接受截图后立即读到的数值，确保报告数字和截图画面严格一致。

**风扇转速同理。** 关闭 UI 浮层、截图的过程会引起 CPU 峰值从而拉高风扇转速。如果在关浮层之前读 RPM，截图上显示的是峰值后的高转速，而报告里记的是峰值前的低转速（实测：508 vs 1044 RPM）。修复：先关浮层、等稳定，再读 RPM + 截图。

**测速文件不能用全零数据。** 生成测试文件时用 `os.urandom` 填充，每个 chunk 开头还打了递增的 8 字节计数器。全零或可压缩数据会被 SMB 压缩优化，导致测出虚高速率。`st_size` 检查也会被稀疏文件骗过，所以额外在 5 个偏移量位置抽样验证非零。

**SMB 拷贝用 `WriteThrough` 禁止写缓存。** 输出流的 `FileOptions` 带 `WriteThrough`，否则 OS 写缓存会虚高写入速率。

### 3. UGOS 页面自动化：选择器地狱

UGOS 的前端跨固件版本变化很大，同一个功能在不同版本用不同的 HTML 结构、CSS 框架（ivu/arco）和 iframe 命名方式。代码里充满了多级 fallback：

**iframe 选择器要试三种模式。** 存储管理器和文件管理器的 iframe 在不同固件版本用不同命名：`iframe[name^="storagemgr"]`、`iframe[name*="storagemgr"]`、`iframe[src*="/storagemgr/"]`，代码按顺序尝试，命中任何一个即可。

**"立即更新" 按钮有 6 种写法。** 不同 UGOS 版本的更新通知用不同的标签（链接 / 按钮 / 纯文本）和措辞（"立即更新"、"已经下载并准备好"），代码逐一尝试 6 种定位策略。

**文件管理器有三层弹窗要关。** 首次打开文件管理器可能依次弹出：欢迎引导、"个人文件夹"提示、操作教程。不同固件版本弹出顺序不同，代码做了两轮关闭（第二轮用更短的等待时间），并在教程关不掉时直接通过 JS 删除 `div.mask`、`div.stepElem` 等 DOM 节点。

**共享文件夹名称输入框换过 5 次选择器。** `_fill_share_name` 先试配置的选择器、再试 fallback CSS，最后用 JS 在 `.folder-share-create` 容器里找任何 placeholder 含 "文件夹" 的 input，通过 `focus/value/dispatchEvent(input+change)/blur` 填值。

**任务管理器打开有三级 fallback。** (1) 点击顶栏 CPU/RAM 状态组件；(2) 在顶栏右侧按多个像素偏移量盲点；(3) 直接往 DOM 注入一个 `section.cloud-window-main` iframe 容器强行打开。

**初始化向导页面有两种布局。** 设备名和管理员账号在有的固件版本分两页，在有的版本合在一页。代码检测当前布局后走不同分支。

**更新确认弹窗要勾 checkbox。** 某些固件版本要求在确认更新前勾选"我已阅读"复选框。代码先试 `input[type="checkbox"]`，再按中文关键词（我已、同意、知晓）搜索关联的 label。

### 4. 固件更新：30 分钟状态机

`system_update.py` 的核心是一个 1800 秒（30 分钟）超时的状态机，处理以下状态转换：

- 登录页出现 → 重新登录
- 下载进度 → 轮询等待
- 安装画面 → HTTP 探测 NAS 是否重启完毕（连续 2 次 200 响应才算）
- 桌面出现 → **持续可见 20 秒**才算稳定（防止重启中途短暂闪过桌面的假阳性）
- 更新通知再次弹出 → 点击确认
- 3 分钟无进展 → 刷新页面（先 `page.reload()`，失败则 `page.goto()`）

> 不要缩短 `UPDATE_WAIT_S`（30 分钟）。大版本固件更新 + 重启实测需要 15-20 分钟。

### 5. GUI 队列持久化：四个独立的坑

**队列顺序在升级后乱序。** 老版本没有 `queue_added_at` 时间戳，升级后所有恢复的任务都打上同一个 `NOW`，导致随机排序。修复：用 SN 文件夹的 `st_ctime`（文件系统创建时间）作为主排序键，与操作员在 Windows 资源管理器里看到的顺序一致。

**恢复的队列条目缺日志、物料、重试按钮。** 队列恢复只重建了行项目，没加载 `run.log`，不探测设备是否还在线，也不显示重试按钮。

**午夜翻转丢设备。** 清理函数按文件夹 ctime 判断"昨天的记录"要删掉，但午夜翻转逻辑明确保留了跨天的有效设备。App 重启后这些设备消失了。修复：信任当天快照中的所有记录，只对真正过期的文件做严格截断。

**设置在入队时冻结。** 操作员在队列运行中途切换了 A/B 等级复选框，但测试用的是入队时捕获的旧设置。修复：在 `run_test` 实际开始前从 GUI 实时变量重新同步设置。

### 6. 标签打印：位图渲染而非原生 ZPL 指令

**原生 ZPL `^BC` 做不到精确尺寸。** 对 16 位 SN，Code 128 在 203 dpi 下模块宽度只有整数选项：module=1 出来 26.4mm，module=2 出来 52.8mm，规格要求 38mm。代码改用 python-barcode 高分辨率渲染后缩放到精确尺寸，嵌入 `^GFA` 位图指令。

**位图行末填充位会打出黑边。** Pillow 1-bit 模式按字节对齐行，末尾填充位默认为 0。ZPL 极性是反的（0=白 1=黑），反转后填充位变成 1，右侧会出现一条黑色细线。代码手动清零这些填充位。TSPL 极性又和 ZPL 相反，填充位处理也相反。

**EAN-13 用全位图渲染而非 TSPL 原生指令。** 原生 `BARCODE EAN13` 无法精确控制护栏线延伸长度、HRI 数字字体和位置。代码自绘整个条码符号以满足零售包装的外观规格。

**字体 fallback 链跨三个平台。** 优先用规格指定的汉仪康黑45S，然后 Windows 字体（微软雅黑、黑体、Arial）→ macOS 字体（华文黑体、冬青黑体、苹方）→ Linux 字体（DejaVu）→ Pillow 内置。注意 PIL 打不开 `PingFang.ttc`，所以 CJK 字体用其他替代。

**打印机名必须精确匹配。** 工厂机上有多台 Zebra 和 Deli 打印机，早期用模糊匹配曾静默选到错误的打印队列。现在配置了打印机名后只做大小写不敏感的精确匹配，不做子串匹配。想换打印机先跑 `run-cli print-label --list-printers` 看实际队列名。

**周转箱标贴的序号按天计数、跨天清零。** `peek_carton_seq` 只读不提交，`commit_carton_print` 才递增计数并写 JSONL 审计日志。这样打印失败不会浪费序号。

### 7. Windows 子进程：每个调用点都要处理

PyInstaller `--windowed` 打包的 exe 没有控制台，但 `subprocess.run()` 默认会弹一个 cmd 窗口。项目里有三个独立的调用点（git 拉取、autoupdate 桥接、forms refresh）曾经各自漏掉了 `CREATE_NO_WINDOW` 标志，导致操作员面前不停闪烁黑色窗口。

统一解法：`_hidden_process_kwargs()` 辅助函数，同时设置 `CREATE_NO_WINDOW` 创建标志和 `STARTUPINFO(dwFlags=STARTF_USESHOWWINDOW, wShowWindow=SW_HIDE)`。两者都要——某些 Windows 版本上只有一个不够。所有 `subprocess.run()` 调用点必须使用这个辅助函数。

### 8. 浏览器管理：隐藏但可控

**浏览器隐藏靠离屏定位。** Playwright 的 persistent context 不支持真正的 headless + 有状态，所以用 `--window-position=-32000,-32000` 把窗口移到屏幕外。改成 `(0, 0)` 浏览器就会显示出来干扰操作员。

**PID 查找用 WMI + 命令行过滤。** 通过 PowerShell 查询 `Win32_Process`，按进程名（msedge.exe）和用户数据目录标记匹配，同时排除 `--type=` 的子进程（renderer、GPU 进程）。如果不排除，拿到的 PID 是渲染进程而非主进程，窗口显示/隐藏操作会失效。

**窗口类名过滤。** 用 `EnumWindows` 遍历主进程的所有顶层窗口时，只取 `Chrome_WidgetWin_1` 类名的窗口，过滤掉 DevTools 和 Chromium 内部窗口。

### 9. NAS 发现：三级 fallback + 并发隔离

发现策略按速度递减尝试：UGREEN 广播（2 秒）→ mDNS（5 秒）→ TCP 端口扫描（最慢）。每个发现到的 IP 都做 HTTP 探测确认是 UGOS 而非同端口的其他服务。

**SN 尾号匹配防止测错机。** 同一网段上可能有多台 NAS，用 SN 末四位匹配确保测到对的那台。浏览器存储里可能残留上一台设备的 SN，所以只在有扫码枪输入的预期 SN 尾号时才做存储抽取验证。

**并发测试的 IP 隔离。** GUI 并发跑多台 NAS 时，`exclude` 参数防止同一台 NAS 被两个任务同时认领。`DEVICE_LOCKS` 字典按 IP 加锁，锁获取用 `timeout=1.0` 以便在等待间隙检查取消事件，防止死锁。

### 10. autoupdate 仓库同步：不要用 merge

早期用 `git merge --ff-only --autostash` 同步 `ugreen-nas-autoupdate` 仓库。内部系统 refresh 会本地修改 `materials.json`，上游也经常改这个文件，`--autostash` 的 stash pop 经常冲突，留下合并标记导致下次 refresh 失败。

改用 `git reset --hard <upstream>`：反正 `materials.json` 在 sync 之后立刻就会被 内部系统 refresh 整个重写，本地修改是短命的，丢了无所谓。

另一个教训：工厂机上任何需要手动操作的步骤（比如 `git pull`）都会被遗忘。autoupdate 仓库的 carton 扣减功能发布了好几个版本都"静悄悄地没人用"，因为没有人在工厂机上手动 pull。修复：启动时自动拉取。

### 11. Tkinter 的静默吞异常

**`self` vs `self.root`。** `FactoryTestGUI` 是普通 Python 类不是 `tk.Widget`。把 `self` 传给 `tk.Toplevel()` / `transient()` / `winfo_*()` 会抛 `AttributeError`——但 Tkinter 会静默吞掉这个异常。结果是打印标签按钮点了没反应、没报错、没日志，排查半天。必须用 `self.root`。

**关键输入不能有默认值。** A/B 等级的选择如果默认为 A，操作员不选直接开测，就会用错误的 P/N 打标签发货。修复：弹一个阻塞的模态对话框，不选就不让继续。

### 12. PowerShell 序列化陷阱

**多行字符串变数组。** git 输出的 release notes 是多行的，PowerShell 自动把它变成字符串数组。`ConvertTo-Json` 序列化出 `["line1", "line2"]`，Python 端 `str()` 之后变成 `"['line1', ...]"` 字面量。必须先 `| Out-String` 展平。

### 13. 截图目录与 SN 变更

扫码枪有时先扫到 SN 尾号，后续通过 UGOS 页面或 localStorage 才拿到完整 SN。此时需要迁移截图目录：如果目标目录已存在，做文件级合并，按 `st_mtime` 保留较新的文件。如果源和目标解析到同一路径则跳过。

失败截图文件名含 `_FAIL_` 标记，`capture.py` 靠这个标记判断"已有成功截图则跳过"。删掉标记会导致失败截图被误认为成功，跳过重拍。

Playwright traces 目录在每次会话开始时清空，否则会在工厂机上累积 GB 级文件。

---

## 自动录表

GUI 启动时会自动调用 `ugreen-nas-autoupdate` 的 `forms refresh` 刷新表单物料。录表桥接路径按优先级查找：

1. 环境变量 `UGREEN_AUTOUPDATE_ROOT`
2. `config/config.yml` 里的 `paths.autoupdate_root`
3. 项目同级目录 `ugreen-nas-autoupdate`

录表提交通过文件桥接：把 JSON payload 写到 `state/bridge_requests/`，再调用 autoupdate runner 子进程读取。这样绕开了管道大小限制和包含截图路径的大 payload 编码问题。

PyInstaller 冻结的 exe 里 `sys.executable` 指向 exe 本身而非 Python 解释器，调用 autoupdate 桥接时需要用 `UGREEN_AUTOUPDATE_PYTHON` 环境变量或 fallback 到 `"python"` 找解释器。

## 自动更新

GUI 启动时按 `config/update-config.json` 去 GitHub Releases 拉取新版本。配置模板见 `config/update-config.example.json`。

**私有仓库的下载需要手动处理重定向。** GitHub release asset URL 从 `api.github.com` 302 跳转到 `objects.githubusercontent.com`（AWS S3 后端），S3 拒绝同时带 `Authorization: Bearer` 和 AWS 签名参数的请求。代码手动走最多 5 跳重定向，只在 `api.github.com` 域名的跳转上带 token，跳到其他域名时去掉。

发版只需打 tag：

```bash
git tag v0.2.0 && git push --tags
```

CI 会自动把 tag 名当 `versionName`、`git rev-list --count HEAD` 当 `versionCode`，stamp 进 `src/version.py` 后打包发布。

已部署的 v0.1.0 / v0.1.1 无法自动升级（早期 swap 脚本用 `Move-Item -Force` 会静默 no-op），首次需手动替换 exe。v0.1.6 起 swap 完成后不再自动重启，弹窗提示用户手动双击。

## 工具脚本

`scripts/` 下有几个调试辅助脚本：

- `analyze_html.py` — 分析 UGOS 页面 HTML 结构
- `find_pages.py` — 查找 UGOS 可用页面路径
- `inspect_dom.py` — 检查页面 DOM 元素
- `map_captures.py` — 映射截图采集点

## 验证

```powershell
python -m pip install -r requirements-dev.txt
python -m pytest tests
python -m src.cli --help
```
