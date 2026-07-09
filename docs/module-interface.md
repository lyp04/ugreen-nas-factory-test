# The `ugreen-nas-autoupdate` module interface / 模块接口

[English](#english) · [中文](#中文)

The factory-test app runs standalone. Submitting a passed unit into your own backend (login, upload screenshots, fill and submit the form, deduct materials) is an **optional** sibling module named `ugreen-nas-autoupdate`. **This repo ships no such module** — you build your own to match the small contract below. When a matching module sits next to the app, the form-entry UI lights up; without it, the app is test-only and that UI stays hidden.

## English

### How the app finds it

The app resolves the module directory in this order, and treats a directory as the module only if `automation/runner.py` exists inside it:

1. the `UGREEN_AUTOUPDATE_ROOT` environment variable
2. `paths.autoupdate_root` in `config/config.yml`
3. the sibling directory `../ugreen-nas-autoupdate` (next to this repo)

### The contract

Your module is a Python package the app calls as a subprocess, from the module root:

```
<python> -m automation.runner <command> [args...]      # cwd = module root
```

- `<python>` is the `UGREEN_AUTOUPDATE_PYTHON` environment variable, else the app's own interpreter (or `python` when the app is a frozen exe).
- Each command must print **one JSON object on the last line of stdout**. A non-zero exit code means failure — stderr (or stdout) is used as the error message.
- No network or shared memory between app and module. The app hands work over as a JSON file on disk and reads a JSON line back; everything else (credentials, backend calls) lives entirely in your module.

### Commands

| Command | When | Timeout |
|---|---|---|
| `submit-report --payload <file.json>` | after a unit passes (and, separately, to pre-seed a previous step) | 300 s |
| `forms refresh [--account <name>]` | on app startup, to pull the latest forms + materials | 180 s |
| `login-ui` | when the operator clicks the app's 登录 (login) button | until the window closes |

`submit-report` receives a payload file the app first writes to `state/bridge_requests/<timestamp>_<sn>.json`. Dispatch on the payload's `type`:

- `type: "submit_report"` — a full report + `form_data` (below). Log into your backend, upload the screenshots, fill the fields, apply the material deduction list, submit.
- `type: "seed_previous_step"` — `{ "sn", "model" }`. Optional; pre-seed the previous-step lookup for that unit.

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
        "input_fields":  [ { "field": "...", "page_key": "...", "...": "..." } ],
        "upload_fields": [ { "field": "...", "page_key": "...", "random_source_dir": false } ],
        "part_groups":   [ { "title": "..." } ],
        "retread_results": { "A": { "relations": ["..."] }, "B": { "...": "..." } },
        "customer": {}, "template": {}, "previous_step": {}, "default_choices": {}
      }
    }
  }
  ```

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

  The app only reads `active` and each account's `name`/`account` (to fill the dropdown and pass the chosen `--account <name>` to `submit-report` / `forms refresh`). Everything else — `token`, `base`, and any fields you add — is **yours**; your module reads them back when it authenticates. The app never touches your backend.

### The `submit_report` payload

```json
{
  "type": "submit_report",
  "created_at": "2026-01-01T12:00:00",
  "account_name": "<selected account>",
  "report": {
    "sn": "...",
    "captured": { "<page_key>": "<screenshot file path>" },
    "captured_values": { "<page_key>": { "...": "..." } }
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

Your runner reads this, does the backend work, and prints e.g. `{"status": "ok"}` (or `{"status": "error", "message": "..."}`) as the last stdout line.

### Optional: git self-sync

If your module directory is a git work tree with an upstream, the app fast-forwards it on startup (`git fetch`, then `git reset --hard` **only when the local branch is strictly behind**) so a factory-test release can pull a matching module change along. It's best-effort and silently skipped if the directory isn't a git repo, git isn't on PATH, you're offline, or the local branch has diverged.

---

## 中文

### App 怎么找到模块

App 按下面顺序解析模块目录,且只有目录里存在 `automation/runner.py` 才认定它是模块:

1. 环境变量 `UGREEN_AUTOUPDATE_ROOT`
2. `config/config.yml` 里的 `paths.autoupdate_root`
3. 同级目录 `../ugreen-nas-autoupdate`(本仓库旁边)

### 契约

你的模块是一个 Python 包,App 在模块根目录以子进程方式调用:

```
<python> -m automation.runner <命令> [参数...]      # cwd = 模块根目录
```

- `<python>` 取环境变量 `UGREEN_AUTOUPDATE_PYTHON`,没有就用 App 自己的解释器(冻结成 exe 时用 `python`)。
- 每个命令必须在 **stdout 最后一行打印一个 JSON 对象**。退出码非 0 视为失败,错误信息取 stderr(或 stdout)。
- App 和模块之间不走网络、不共享内存:App 把任务以 JSON 文件落盘交过去,再读回一行 JSON;其余(凭据、后端调用)全在你的模块里。

### 命令

| 命令 | 何时调用 | 超时 |
|---|---|---|
| `submit-report --payload <file.json>` | 一台测试通过后(以及单独预置上一工步) | 300 秒 |
| `forms refresh [--account <name>]` | App 启动时,拉最新表单 + 物料 | 180 秒 |
| `login-ui` | 操作员点 App 的「登录」按钮时 | 直到窗口关闭 |

`submit-report` 拿到的 payload 文件,App 会先写到 `state/bridge_requests/<时间戳>_<sn>.json`。按 payload 的 `type` 分派:

- `type: "submit_report"` —— 完整报告 + `form_data`(见下)。登录后端、上传截图、填字段、应用扣料清单、提交。
- `type: "seed_previous_step"` —— `{ "sn", "model" }`,可选,给这台预置上一工步查表。

`forms refresh` 应重新拉表单目录,并**整个重写 `config/materials.json`**(App 把它当后端所有,下次同步会清掉本地改动)。

`login-ui` 应弹出**你自己的登录窗口**——你公司用什么认证都行,App 完全不知道。认证操作员后,把登录成功的账号(含 token)写进 `state/accounts.local.json` 并设为 active,窗口关闭后返回;App 随后从该文件刷新提交人下拉框。这就是 App「登录」按钮的全部含义:它只让你的模块*弹个登录窗*,窗里做什么(验证码登录 / SSO / 手贴 token …)由你决定。**任何凭据、后端细节都不在 App 里。**

### 模块要提供的文件

App 读这些来搭表单和「物料」标签页,放在模块的 `config/` 下:`config/forms.json`(机型→form_id、每个 form 的字段/上传/物料分组/等级等)、`config/materials.json`(每个 form 的物料清单 + 选中扣料的编码/分组)、以及 `state/accounts.local.json`(提交人列表,也是 `login-ui` 写登录账号的地方)。**App 只读 `active` 和每个账号的 `name`/`account`(填下拉框、把选中的 `--account <name>` 传给 submit-report/forms refresh);`token`/`base` 等其余字段是你模块自己的,App 从不碰你的后端。** 结构见上面英文小节。

### `submit_report` payload

结构见上面英文小节。你的 runner 读它、做后端那套(登录 / 按 `uploads` 传对应截图 / 填 `inputs` / 按 `material_groups` 扣料 / 提交),然后在 stdout 最后一行打印结果,如 `{"status": "ok"}` 或 `{"status": "error", "message": "..."}`。

### 可选:git 自同步

如果模块目录是带 upstream 的 git 工作树,App 启动时会帮它 fast-forward(`git fetch`,**仅当本地严格落后时** `git reset --hard`),这样 factory-test 发个版就能把配套的模块改动一起带上。尽力而为:不是 git 仓库 / git 不在 PATH / 离线 / 本地已分叉,都会静默跳过。
