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

One more switch worth knowing: setting `UGREEN_DISABLE_FORM_ENTRY=1` force-hides the form-entry UI even when a module is present (that's all `src/gui_no_form.py` does). Detection wins only when that variable isn't set.

### Expected layout

```
ugreen-nas-autoupdate/
├── automation/
│   ├── __init__.py
│   └── runner.py            # entry point: submit-report | forms refresh | login-ui
├── config/
│   ├── forms.json           # yours; the app only reads it
│   └── materials.json       # yours; the app reads it, `forms refresh` rewrites it
└── state/                    # created at runtime — don't ship, don't commit
    ├── accounts.local.json
    └── bridge_requests/      # the app writes payload files here before invoking you
```

### The contract

Your module is a Python package the app calls as a subprocess, from the module root:

```
<python> -m automation.runner <command> [args...]      # cwd = module root
```

- `<python>` is the `UGREEN_AUTOUPDATE_PYTHON` environment variable, else the app's own interpreter (or `python` when the app is a frozen exe).
- `submit-report` and `forms refresh` must print **one JSON object on the last line of stdout**; `login-ui`'s stdout is ignored. For every command, a non-zero exit code means failure — stderr (or stdout) is used as the error message.
- No network or shared memory between app and module. The app hands work over as a JSON file on disk and reads a JSON line back; everything else (credentials, backend calls) lives entirely in your module.

### Commands

| Command | When | Timeout |
|---|---|---|
| `submit-report --payload <file.json>` | after a unit passes (and, separately, to pre-seed a previous step) | 300 s |
| `forms refresh [--account <name>]` | on app startup, to pull the latest forms + materials | 180 s |
| `login-ui` | when the operator clicks the app's 登录 (login) button | 30 min hard cap — a window still open past that gets killed and the login counts as failed |

`submit-report` receives a payload file the app first writes to `state/bridge_requests/<timestamp>_<sn>.json`. Dispatch on the payload's `type`:

- `type: "submit_report"` — a full report + `form_data` (below). Log into your backend, upload the screenshots, fill the fields, apply the material deduction list, submit.
- `type: "seed_previous_step"` — `{ "sn", "model" }`. **Reserved, not wired up yet**: the current app never sends this type (the GUI checkbox exists but its callback isn't connected). Implement it as a no-op or skip it until a release note says otherwise.

One error-message contract to know: when `submit-report` fails because the unit has no prior-stage record in your backend, the app recognizes that case **by substring-matching your error message** — it must contain `缺少第一步翻新记录` or `previous refurbishment process` verbatim. Any other wording lands in the generic failure path.

`forms refresh` should re-pull your form catalog and **rewrite `config/materials.json` wholesale** (the app treats that file as backend-owned and blows away local edits on the next sync).

`login-ui` should pop up **your own login window** — whatever your company's auth looks like; the app knows nothing about it. Authenticate the operator, write the signed-in account (with its token) into `state/accounts.local.json`, mark it active, and return when the window closes. The app then refreshes its submitter dropdown from that file. This is what the app's 登录 button does: it only asks your module to *show a login window* — you decide what that means (a captcha login, SSO, a pasted token, anything). No credential or backend detail ever lives in the app.

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

  Two field semantics that aren't obvious from the names: `input_fields[].page_key`/`value_key` must exactly match a key from the captured-values table below (a typo silently yields an empty value, not an error). And an `upload_fields` entry with `random_source_dir: true` is **skipped entirely** unless its `field` appears in the active grade's `retread_results.<grade>.relations` list — that's the mechanism for including a screenshot only for certain grades.

- **`config/materials.json`**

  ```json
  {
    "forms": {
      "<form_id>": {
        "materials": [ { "code": "...", "name": "...", "group": "...", "qty": 1 } ],
        "selected_material_codes":  ["..."],
        "selected_material_groups": ["..."]
      }
    }
  }
  ```

- **`state/accounts.local.json`** — the submitter list the app shows in its dropdown, and where `login-ui` writes the signed-in account:

  ```json
  {
    "active": "<name>",
    "accounts": [
      { "name": "<display name>", "account": "<login id>", "token": "<your backend token>", "base": "<your API base>", "...": "..." }
    ]
  }
  ```

  The app uses `active` and each account's `name`/`account` to fill its dropdown and pass the chosen `--account <name>` to `submit-report` / `forms refresh` — and it does write this file: switching accounts updates `active`, deleting an account removes the whole entry. What it never does is read or forward `token`, `base`, or any field you add; those are **yours**, read back by your module when it authenticates. The app never touches your backend.

### What `captured_values` actually contains

This is the reference for `input_fields[].page_key` / `value_key` — the keys must match exactly:

| `page_key` | `value_key`s |
|---|---|
| `system_update` | `latest_status`, `ugos_version` |
| `network_interface` | `link_bps` (`"10"` or `"2.5"`) |
| `storage_pool` | `hdd_pool_raid`, `ssd_pool_raid` |
| `hdd_write` / `hdd_read` / `ssd_write` / `ssd_read` | `share`, `direction`, `rate`, `rate_mbps`, `threshold_mbps`, `speed_status` (`ok` / `below_threshold` / `no_sample` / `seed_incomplete` / `unstable_threshold`), `attempt` |
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

`report` carries a few more bookkeeping fields than shown (`started_at`, `model_source`, …) — ignore what you don't need. Your runner reads this, does the backend work, and prints e.g. `{"status": "ok"}` (or `{"status": "error", "message": "..."}`) as the last stdout line.

### Optional: git self-sync

If your module directory is a git work tree with an upstream, the app fast-forwards it on startup (`git fetch`, then `git reset --hard` **only when the local branch is strictly behind**) so a factory-test release can pull a matching module change along. It's best-effort and silently skipped if the directory isn't a git repo, git isn't on PATH, you're offline, or the local branch has diverged.

---

## 中文

### App 怎么找到模块

App 按下面顺序解析模块目录,且只有目录里存在 `automation/runner.py` 才认定它是模块:

1. 环境变量 `UGREEN_AUTOUPDATE_ROOT`
2. `config/config.yml` 里的 `paths.autoupdate_root`
3. exe / 项目根目录下的 `ugreen-nas-autoupdate` 子目录——成品分发就走这条:把模块文件夹放在 exe 旁边即可
4. 同级目录 `../ugreen-nas-autoupdate`(本仓库旁边,开发克隆布局)

另有一个开关:设环境变量 `UGREEN_DISABLE_FORM_ENTRY=1` 可以在模块存在时也强制隐藏录表界面(`src/gui_no_form.py` 干的就是这一件事)。

### 契约

你的模块是一个 Python 包,App 在模块根目录以子进程方式调用(目录布局见上面英文小节):

```
<python> -m automation.runner <命令> [参数...]      # cwd = 模块根目录
```

- `<python>` 取环境变量 `UGREEN_AUTOUPDATE_PYTHON`,没有就用 App 自己的解释器(冻结成 exe 时用 `python`)。
- `submit-report` 和 `forms refresh` 必须在 **stdout 最后一行打印一个 JSON 对象**;`login-ui` 的 stdout 会被忽略。所有命令都是退出码非 0 视为失败,错误信息取 stderr(或 stdout)。
- App 和模块之间不走网络、不共享内存:App 把任务以 JSON 文件落盘交过去,再读回一行 JSON;其余(凭据、后端调用)全在你的模块里。

### 命令

| 命令 | 何时调用 | 超时 |
|---|---|---|
| `submit-report --payload <file.json>` | 一台测试通过后(以及单独预置上一工步) | 300 秒 |
| `forms refresh [--account <name>]` | App 启动时,拉最新表单 + 物料 | 180 秒 |
| `login-ui` | 操作员点 App 的「登录」按钮时 | 30 分钟硬上限,超时窗口被杀、登录按失败处理 |

`submit-report` 拿到的 payload 文件,App 会先写到 `state/bridge_requests/<时间戳>_<sn>.json`。按 payload 的 `type` 分派:

- `type: "submit_report"` —— 完整报告 + `form_data`(见下)。登录后端、上传截图、填字段、应用扣料清单、提交。
- `type: "seed_previous_step"` —— `{ "sn", "model" }`。**预留、当前未接线**:现版本 App 不会发这个 type(GUI 有复选框但回调没接上),先实现成空操作或干脆不管。

错误文案契约:`submit-report` 因「后端没有该 SN 的上一工步记录」而失败时,App 靠**子串匹配你的错误信息**识别这种情况——信息里必须原样含有 `缺少第一步翻新记录` 或 `previous refurbishment process`,换别的措辞就会落进普通失败分支。

`forms refresh` 应重新拉表单目录,并**整个重写 `config/materials.json`**(App 把它当后端所有,下次同步会清掉本地改动)。

`login-ui` 应弹出**你自己的登录窗口**——你公司用什么认证都行,App 完全不知道。认证操作员后,把登录成功的账号(含 token)写进 `state/accounts.local.json` 并设为 active,窗口关闭后返回;App 随后从该文件刷新提交人下拉框。这就是 App「登录」按钮的全部含义:它只让你的模块*弹个登录窗*,窗里做什么(验证码登录 / SSO / 手贴 token …)由你决定。**任何凭据、后端细节都不在 App 里。**

### 模块要提供的文件

App 读这些来搭表单和「物料」标签页,放在模块的 `config/` 下:`config/forms.json`(机型→form_id、每个 form 的字段/上传/物料分组/等级等)、`config/materials.json`(每个 form 的物料清单 + 选中扣料的编码/分组)、以及 `state/accounts.local.json`(提交人列表,也是 `login-ui` 写登录账号的地方)。App 用 `active` 和每个账号的 `name`/`account` 填下拉框、把选中的 `--account <name>` 传给 submit-report/forms refresh,**并且会写这个文件**(切换账号改 `active`、删除账号移除整条);它从不读取或转发 `token`/`base` 等凭据字段,那些是你模块自己的,App 从不碰你的后端。结构见上面英文小节;`input_fields` 的 `page_key`/`value_key` 必须精确匹配英文小节「What `captured_values` actually contains」那张表(拼错不报错、只会静默拿到空值),`link_bps` 是按机型推算的、不取页面实测值。

### `submit_report` payload

结构见上面英文小节。你的 runner 读它、做后端那套(登录 / 按 `uploads` 传对应截图 / 填 `inputs` / 按 `material_groups` 扣料 / 提交),然后在 stdout 最后一行打印结果,如 `{"status": "ok"}` 或 `{"status": "error", "message": "..."}`。

### 可选:git 自同步

如果模块目录是带 upstream 的 git 工作树,App 启动时会帮它 fast-forward(`git fetch`,**仅当本地严格落后时** `git reset --hard`),这样 factory-test 发个版就能把配套的模块改动一起带上。尽力而为:不是 git 仓库 / git 不在 PATH / 离线 / 本地已分叉,都会静默跳过。
