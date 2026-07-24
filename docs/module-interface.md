# The `ugreen-nas-autoupdate` module interface / 模块接口

[English](#english) · [中文](#中文)

The factory-test app runs standalone. Submitting a passed unit into your own backend (login, upload screenshots, fill and submit the form, deduct materials) is an **optional** sibling module named `ugreen-nas-autoupdate`. **This repo ships no such module** — you build your own to match the small contract below. When a matching module sits next to the app, the form-entry UI lights up; without it, the app is test-only and that UI stays hidden.

## English

### How the app finds it

The app resolves the module directory in this order, and treats a directory as the module only if `automation/runner.py` exists inside it:

1. the `UGREEN_AUTOUPDATE_ROOT` environment variable
2. `paths.autoupdate_root` in `config/config.yml`
3. an `ugreen-nas-autoupdate` subdirectory next to the exe / project root — this is how a packaged distribution ships the module: drop the folder beside the exe and it just works
4. the sibling directory `../ugreen-nas-autoupdate` (next to this repo — the development/clone layout)

### Expected layout

```
ugreen-nas-autoupdate/
├── automation/
│   ├── __init__.py
│   └── runner.py            # entry point: submit-report | forms refresh | login-ui
├── config/
│   ├── forms.json           # yours; the app reads it, `forms refresh` may rewrite it
│   └── materials.json       # catalog + selection state; both app and module write it
└── state/                    # created at runtime — don't ship, don't commit
    ├── accounts.local.json
    └── bridge_requests/      # the app writes payload files here before invoking you
```

### Packaging allowlist

`build-packages.ps1` does not copy this repository tree and then delete a few known-private folders. It creates the packaged module from an empty directory and copies only:

- `automation/**/*.py`
- `config/forms.json`
- `config/materials.json`
- optional root-level `requirements.txt` and `pyproject.toml`

Everything else is excluded, including nested `.git`, `.venv`, `state`, `.env*`, all hidden names, `*.local.*` overrides, editor backups, caches, logs, temporary files, key files, and symlinks/junctions. The completed distribution is checked recursively against the same allowlist. If your runner needs another code package or static asset, extend and review this allowlist explicitly; do not restore whole-directory copying. Login state is created on the target machine at runtime. Install proprietary labels and other site-specific assets separately.

### The contract

Your module is a Python package the app calls as a subprocess, from the module root:

```
<python> -m automation.runner <command> [args...]      # cwd = module root
```

- `<python>` is the `UGREEN_AUTOUPDATE_PYTHON` environment variable, else the app's own interpreter (or `python` when the app is a frozen exe).
- `submit-report` and `forms refresh` must print **one JSON object on the last line of stdout**; `login-ui`'s stdout is ignored. For every command, a non-zero exit code means failure — stderr (or stdout) is used as the error message.
- No network or shared memory between app and module. The app hands work over as a JSON file on disk and reads a JSON line back; everything else (credentials, backend calls) lives entirely in your module.
- A frozen public exe does not contain a Python runtime for the external module. The current package-B module requires a system/prepared Python 3.11+ interpreter and its dependencies; set `UGREEN_AUTOUPDATE_PYTHON` to the executable path when needed. A replacement module may declare another compatible minimum, but then you must also update the fixed runtime/dependency preflight in `build-packages.ps1`.

### Commands

| Command | When | Timeout |
|---|---|---|
| `submit-report --payload <file.json>` | after capture/measurement validation, before optional cleanup and factory reset | 300 s |
| `forms refresh [--account <name>]` | on app startup, to pull the latest forms + materials | 180 s |
| `login-ui` | when the operator clicks the app's 登录 (login) button | 30 min hard cap — a window still open past that gets killed and the login counts as failed |

`submit-report` receives a payload file the app first writes to `state/bridge_requests/<timestamp>_<sn>.json`. Dispatch on the payload's `type`:

- `type: "submit_report"` — a full report + `form_data` (below). Log into your backend, upload the screenshots, fill the fields, apply the material deduction list, submit. (This is currently the only `type` the app sends.)

At this point the report is still `running`: measurements have passed, but later cleanup or factory-reset verification can still fail the overall factory-test task. Do not treat the module call alone as the final device disposition.

The app deletes its source payload only after the module returns `success` or `already_submitted`, and only if the file identity is unchanged. It keeps failed or malformed requests for diagnosis. The module must treat `--payload` as caller-owned and never delete an arbitrary input path. Apply a bounded retention policy to failed requests and any audit copies because they contain SNs and local screenshot paths.

One error-message contract to know: when `submit-report` fails because the unit has no prior-stage record in your backend, the app recognizes that case **by substring-matching your error message** — it must contain `缺少第一步翻新记录` or `previous refurbishment process` verbatim. Any other wording lands in the generic failure path.

`forms refresh` should re-pull the backend-owned form/material catalog. Preserve `selected_material_codes` and `selected_material_groups` if operators can edit those choices in the app: the app writes those keys back to `config/materials.json`. Write refreshed JSON atomically so another process never observes a partial file.

`login-ui` should pop up **your own login window** — whatever your company's auth looks like; the app knows nothing about it. Authenticate the operator, write the signed-in account (with its token) into `state/accounts.local.json`, mark it active, and return when the window closes. The app then refreshes its submitter dropdown from that file. Never store the password or print the token. Captcha login, SSO, browser login, and pasted-token flows are all valid as long as the state-file contract is met.

The GUI can run several device workers at once, so separate `submit-report` processes may overlap. Treat `account_name` as an exact account selection and fail if it is unavailable; never fall back to another operator's token. Make submission idempotent by form/SN, use cross-process locks around any read-modify-write state, and replace JSON files atomically. A timeout or operator retry can invoke the same logical submission again after the backend accepted the first request, so check for an existing record before creating one and return `already_submitted` when appropriate.

### Files your module provides

The app reads these to build the form and the 物料 (materials) tab — put them in the module's `config/`:

- **`config/forms.json`**

  ```json
  {
    "models": { "2800": "<form_id>", "4800": "<form_id>", "4800Plus": "<form_id>" },
    "forms": {
      "<form_id>": {
        "model_key": "2800", "model_label": "DXP2800", "default_grade": "A",
        "input_fields":  [ { "field": "...", "page_key": "...", "value_key": "...", "...": "..." } ],
        "upload_fields": [ { "field": "...", "page_key": "...", "random_source_dir": false } ],
        "part_groups":   [ { "title": "...", "field": "..." } ],
        "retread_results": { "A": { "relations": ["..."] }, "B": { "...": "..." } },
        "customer": {}, "template": {}, "previous_step": {}, "default_choices": {}
      }
    }
  }
  ```

  Two field semantics that aren't obvious from the names: `input_fields[].page_key`/`value_key` must exactly match a key from the captured-values table below (a typo silently uses that entry's `fallback`, or `""` when no fallback is configured). And an `upload_fields` entry with `random_source_dir: true` is **skipped entirely** unless its `field` appears in the active grade's `retread_results.<grade>.relations` list — that's the mechanism for including a screenshot only for certain grades.

- **`config/materials.json`**

  ```json
  {
    "forms": {
      "<form_id>": {
        "materials": [ { "code": "...", "name": "...", "group": "...", "default_qty": 1, "required": false } ],
        "selected_material_codes":  ["..."],
        "selected_material_groups": ["..."]
      }
    }
  }
  ```

  Each material's `group` must exactly match a `part_groups[].title` in `forms.json`, or the app will not place it in a deduction group. If `selected_material_groups` is missing or empty, the bridge defaults to selecting the complete `补充包材` and `补充配件` groups; set it explicitly (including `[]` only if your module handles that default deliberately) and review `selected_material_codes` to avoid unintended deductions.

- **`state/accounts.local.json`** — the submitter list the app shows in its dropdown, and where `login-ui` writes the signed-in account:

  ```json
  {
    "active": "<name>",
    "accounts": [
      { "name": "<display name>", "account": "<login id>", "token": "<your backend token>", "base": "<your API base>", "...": "..." }
    ]
  }
  ```

  The app uses `active` and each account's `name`/`account` to fill its dropdown. For `forms refresh` it passes `--account <name>`; for `submit-report` it writes the selection as top-level `account_name` in the payload. It also writes this file: switching accounts updates `active`, and deleting an account removes the whole entry. The app necessarily parses and preserves each whole account object during those updates, but it does not interpret, display, or pass `token`, `base`, or custom fields to the subprocess. Your module reads those fields from its own state file and owns every backend call.

### What `captured_values` actually contains

This is the reference for `input_fields[].page_key` / `value_key` — the keys must match exactly:

| `page_key` | `value_key`s |
|---|---|
| `system_update` | `latest_status`, `ugos_version` |
| `network_interface` | `link_bps` (`"10"` or `"2.5"`) |
| `storage_pool` | `hdd_pool_raid`, `ssd_pool_raid` |
| `hdd_write` / `hdd_read` / `ssd_write` / `ssd_read` | always: `share`, `direction`, `rate`, `rate_mbps`, `threshold_mbps`, `speed_status`, `attempt`; wrapper: `attempts`; when available: `sample_source`, `transfer_complete`, `source_bytes`, `destination_bytes`, `upload_bytes`, `download_bytes`, `remote_bytes`, `transfer_expected_bytes`, `transfer_source_bytes`, `transfer_destination_bytes`, `upload_expected_bytes`, `upload_remote_bytes`. `speed_status` may be `ok`, `below_threshold`, `no_sample`, `seed_incomplete`, `unstable_threshold`, `process_timeout`, `process_failed`, `remote_size_mismatch`, `download_size_mismatch`, or `transfer_incomplete` |
| `resource_monitor`, `fan_normal`, `fan_silent`, `fan_full_speed` | `cpu_temp`, `device_fan_rpm` |

One exception is resolved in the bridge, not from the page: an input field with `value_key: "link_bps"` gets a value **derived from the model** (4800Plus → `"10"`, others → `"2.5"`), not the on-screen reading — the captured value is kept for the record only. Newer app versions may add keys; ignore ones you don't know.

### The `submit_report` payload

```json
{
  "type": "submit_report",
  "created_at": "2026-01-01T12:00:00",
  "account_name": "<selected account>",
  "report": {
    "sn": "...",
    "nas_ip": "...", "form_model": "...", "form_grade": "A",
    "captured": { "<page_key>": "<screenshot file path>" },
    "captured_values": { "<page_key>": { "<value_key>": "..." } }
  },
  "form_data": {
    "form_id": "...", "sn": "...", "model_key": "...", "grade": "A",
    "customer": {}, "template": {}, "previous_step": {},
    "choices": {}, "retread_result": {},
    "inputs":  { "<field>": { "value": "...", "...": "..." } },
    "uploads": { "<field>": { "path": "<screenshot path>", "page_key": "..." } },
    "material_groups": [ { "title": "...", "items": [ { "code": "...", "...": "..." } ] } ],
    "bridge": { "autoupdate_root": "<module dir>" }
  }
}
```

`report` carries a few more bookkeeping fields than shown (`started_at`, `model_source`, …) — ignore what you don't need. Your runner reads this, does the backend work, and prints either `{"status": "success", ...}` or `{"status": "already_submitted", ...}` as the last stdout line. Other status values are not successful; for an error, exit non-zero and write a sanitized diagnostic to stderr.

### Optional: git self-sync

If your module directory is a git work tree with an upstream, the app fast-forwards it on startup (`git fetch`, then `git reset --hard` **only when the local branch is strictly behind**) so a factory-test release can pull a matching module change along. It's best-effort and silently skipped if the directory isn't a git repo, git isn't on PATH, you're offline, or the local branch has diverged.

---

## 中文

### App 怎么找到模块

App 按下面顺序查找模块目录；只有目录里存在 `automation/runner.py`，才会认定它是模块：

1. 环境变量 `UGREEN_AUTOUPDATE_ROOT`
2. `config/config.yml` 里的 `paths.autoupdate_root`
3. exe / 项目根目录下的 `ugreen-nas-autoupdate` 子目录——成品分发使用这条，把模块文件夹放在 exe 旁边即可
4. 同级目录 `../ugreen-nas-autoupdate`（本仓库旁边，开发克隆布局）

### 打包白名单

`build-packages.ps1` 不会先复制整个模块仓库再删几个顶层目录，而是从空目录只复制：

- `automation/**/*.py`
- `config/forms.json`
- `config/materials.json`
- 模块根目录下可选的 `requirements.txt` 和 `pyproject.toml`

其余内容全部排除，包括任意深度的 `.git`、`.venv`、`state`、`.env*`、所有隐藏名称、`*.local.*` 本地覆盖、编辑器备份、缓存、日志、临时文件、密钥文件及符号链接/junction；产物生成后还会按同一白名单递归复查。runner 若需要其他代码包或静态资源，应显式扩充并审查白名单，不能恢复整目录复制。登录状态在目标机运行时创建；专有标签和现场资产单独安装。

### 契约

你的模块是一个 Python 包。App 在模块根目录以子进程方式调用（目录布局见上面的英文小节）：

```
<python> -m automation.runner <命令> [参数...]      # cwd = 模块根目录
```

- `<python>` 取环境变量 `UGREEN_AUTOUPDATE_PYTHON`；未设置时使用 App 自己的解释器（冻结成 exe 时使用 `python`）。
- `submit-report` 和 `forms refresh` 必须在 **stdout 最后一行打印一个 JSON 对象**；`login-ui` 的 stdout 会被忽略。所有命令都以非 0 退出码表示失败，错误信息取 stderr（没有时才取 stdout）。
- App 和模块之间不走网络、不共享内存。App 把任务以 JSON 文件落盘交给模块，再读回一行 JSON；凭据和后端调用全部留在模块内部。
- 冻结后的公开 exe 不带供外部模块使用的 Python runtime。当前 B 包内置模块要求目标机准备 Python 3.11+ 和模块依赖；需要时把 `UGREEN_AUTOUPDATE_PYTHON` 指到解释器完整路径。自建模块可以声明自己的兼容版本下限，但也要同步修改 `build-packages.ps1` 里固定的运行时/依赖预检。

### 命令

| 命令 | 何时调用 | 超时 |
|---|---|---|
| `submit-report --payload <file.json>` | 截图/读数判定通过后、可选清理和恢复出厂之前 | 300 秒 |
| `forms refresh [--account <name>]` | App 启动时，拉取最新表单和物料 | 180 秒 |
| `login-ui` | 操作员点击 App 的「登录」按钮时 | 30 分钟硬上限；超时会终止窗口，并把登录记为失败 |

`submit-report` 拿到的 payload 文件由 App 先写入 `state/bridge_requests/<时间戳>_<sn>.json`。模块按 payload 的 `type` 分派：

- `type: "submit_report"` —— 完整报告和 `form_data`（见下）。模块负责登录后端、上传截图、填写字段、应用扣料清单并提交。目前 App 只会发送这一种 type。

此时 report 仍是 `running`：读数判定已通过，但后面的清池或恢复出厂确认仍可能让整机任务失败。不能把模块调用成功单独当成最终整机结论。

模块返回 `success` 或 `already_submitted` 后，App 只在确认文件身份未变化时删除自己创建的源 payload；失败或格式错误的请求会保留排查。模块必须把 `--payload` 当作调用方文件，不能删除任意输入路径。失败请求和审计副本都要设置保留上限，因为其中有 SN 和本机截图路径。

错误文案有一项约定：`submit-report` 因「后端没有该 SN 的上一工步记录」而失败时，App 靠**子串匹配错误信息**识别这种情况。信息里必须原样含有 `缺少第一步翻新记录` 或 `previous refurbishment process`；换成其他措辞会进入普通失败分支。

`forms refresh` 应重新拉后端所有的表单/物料目录。若允许操作员在 App 里勾选扣料，刷新时要保留 `selected_material_codes` 和 `selected_material_groups`；App 会把这两项写回 `config/materials.json`。保存刷新结果时要原子替换文件，避免别的进程读到半截 JSON。

`login-ui` 应弹出**你自己的登录窗口**。认证完成后，把登录账号（含 token）写入 `state/accounts.local.json`，设为 active，并在窗口关闭后返回；App 随后从该文件刷新提交人下拉框。不要保存密码，也不要把 token 打到 stdout。验证码、SSO、浏览器登录和手动粘贴 token 都可以，只要满足账号文件契约。

GUI 可以同时跑多台设备，因此会有多个 `submit-report` 进程并发。`account_name` 必须精确匹配账号，找不到就失败，不能换用另一个操作员的 token。后端提交要按表单/SN 幂等；共享状态的读改写要加跨进程锁，JSON 用原子替换。进程超时或人工重试时，同一条业务记录可能再次到达；创建前先查重，已存在时返回 `already_submitted`。

### 模块要提供的文件

App 用下面三个文件构建表单、物料页和提交人列表：

- `config/forms.json`：机型到 form_id 的映射，以及字段、上传项、物料分组和等级。
- `config/materials.json`：各表单的物料目录、选中编码和选中分组。
- `state/accounts.local.json`：提交人列表，也是 `login-ui` 写入登录账号的位置。

App 使用 `active` 和每个账号的 `name` / `account` 填充下拉框；刷新时传 `--account <name>`，提交时把选择写入 payload 顶层的 `account_name`。App **也会写账号文件**：切换账号会更新 `active`，删除账号会移除整条记录。因此它会解析并保留完整账号对象，但不会解释、显示或把 `token` / `base` 作为子进程参数传出；后端调用仍全部由模块完成。

`input_fields` 的 `page_key` / `value_key` 拼错时会使用该项的 `fallback`，未配置 fallback 才得到空值；`link_bps` 按机型推算，不取页面实测值。物料 `group` 必须逐字匹配 `part_groups[].title`；`selected_material_groups` 缺失或为空时，会默认整组选中「补充包材」和「补充配件」。上线前应显式复核，避免误扣料。

### `submit_report` payload

结构见上面的英文小节。runner 读取 payload，完成登录、按 `uploads` 上传截图、填写 `inputs`、按 `material_groups` 扣料并提交，然后在 stdout 最后一行打印 `{"status": "success", ...}` 或 `{"status": "already_submitted", ...}`。其他 status 都不算成功；出错时使用非 0 退出码，并把脱敏诊断写到 stderr。

### 可选：git 自同步

如果模块目录是带 upstream 的 git 工作树，App 启动时会尝试 fast-forward：先执行 `git fetch`，并且**仅当本地严格落后时**执行 `git reset --hard`。不是 git 仓库、git 不在 PATH、离线或本地已经分叉时，都会静默跳过。
