from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_SELECTED_MATERIAL_GROUPS = {"补充包材", "补充配件"}


def _hidden_process_kwargs() -> dict:
    # Without these flags every `subprocess.run(...)` in a PyInstaller --windowed exe pops a fresh cmd window on Windows.
    if not sys.platform.startswith("win"):
        return {}
    kwargs: dict = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
        startupinfo.wShowWindow = getattr(subprocess, "SW_HIDE", 0)
        kwargs["startupinfo"] = startupinfo
    return kwargs


class FormEntryError(RuntimeError):
    pass


def _project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def _config_autoupdate_root() -> Path | None:
    # 用 config_loader 读：它会 (1) 在没有真实 config.yml 时回退到 config.example.yml，
    # (2) 展开 ${USERPROFILE} 等环境变量——直接 yaml.safe_load 拿到的是未展开的字面量。
    try:
        from .utils.config_loader import load_yaml, resolve_config_path

        data = load_yaml(resolve_config_path(_project_root(), "config.yml")) or {}
    except Exception:
        return None
    raw = ((data.get("paths") or {}).get("autoupdate_root") or "").strip()
    return Path(raw) if raw else None


def autoupdate_root() -> Path:
    candidates: list[Path] = []
    if os.environ.get("UGREEN_AUTOUPDATE_ROOT"):
        candidates.append(Path(os.environ["UGREEN_AUTOUPDATE_ROOT"]))
    config_root = _config_autoupdate_root()
    if config_root is not None:
        candidates.append(config_root)
    candidates.append(_project_root() / "ugreen-nas-autoupdate")         # 模块随包：exe 同级子目录（B 完整包布局，复制即用）
    candidates.append(_project_root().parent / "ugreen-nas-autoupdate")  # 与 factory-test 同级（开发 / 克隆布局）
    for candidate in candidates:
        if (candidate / "automation" / "runner.py").exists():
            return candidate
    raise FormEntryError(
        "找不到自动录表系统，请设置 UGREEN_AUTOUPDATE_ROOT 或在 config/config.yml 的 "
        "paths.autoupdate_root 里配置正确路径"
    )


def autoupdate_available(project_root: Path | None = None) -> bool:
    """能否探测到自动录表系统（ugreen-nas-autoupdate 的 automation/runner.py）。
    探测不到就返回 False —— GUI 据此隐藏全部登录/上传/录表相关 UI。绝不抛异常。
    这样「只带 factory-test、不带上传器」的分发包会自动进入纯测试模式。"""
    try:
        autoupdate_root()  # 成功返回即代表 runner.py 存在（见上）
        return True
    except Exception:
        return False


def resolve_config_path(_project_root: Path | None, name: str) -> Path:
    path = autoupdate_root() / "config" / name
    if not path.exists():
        raise FormEntryError(f"自动录表系统缺少配置: {path}")
    return path


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def resolve_form_id(project_root: Path | None = None, model: str | None = None, form_id: str | None = None) -> str:
    if form_id:
        return form_id
    forms = load_json(resolve_config_path(project_root, "forms.json"))
    model_key = normalize_model_key(model or "2800")
    models = forms.get("models") or {}
    if model_key not in models:
        raise FormEntryError(f"Unsupported form model: {model or model_key}")
    return str(models[model_key])


def available_models(project_root: Path | None = None) -> list[str]:
    forms = load_json(resolve_config_path(project_root, "forms.json"))
    return list((forms.get("models") or {}).keys())


def available_grades(project_root: Path | None = None, model: str | None = None) -> list[str]:
    forms = load_json(resolve_config_path(project_root, "forms.json"))
    form_id = resolve_form_id(project_root, model=model)
    grades = sorted((forms["forms"][form_id].get("retread_results") or {}).keys())
    return grades or ["A"]


def normalize_model_key(model: str | None) -> str:
    compact = "".join(str(model or "").split()).lower()
    if compact in {"4800plus", "4800p", "dxp4800plus", "dxp4800p"}:
        return "4800Plus"
    if compact in {"4800", "dxp4800"}:
        return "4800"
    if compact in {"2800", "dxp2800", "", "auto"}:
        return "2800"
    return str(model or "").strip() or "2800"


def build_report_form_data(
    report: dict[str, Any],
    project_root: Path | None = None,
    form_id: str | None = None,
    model: str | None = None,
    grade: str | None = None,
) -> dict[str, Any]:
    root = project_root or Path.cwd()
    resolved_form_id = resolve_form_id(root, model=model or report.get("form_model"), form_id=form_id)
    forms = load_json(resolve_config_path(root, "forms.json"))
    materials = load_json(resolve_config_path(root, "materials.json"))
    form = forms["forms"][resolved_form_id]
    material_form = materials.get("forms", {}).get(resolved_form_id, {})
    model_key = normalize_model_key(model or report.get("form_model") or form.get("model_key"))
    selected_grade = str(grade or form.get("default_grade") or "A").upper()
    retread_results = form.get("retread_results") or {}
    retread_result = retread_results.get(selected_grade) or retread_results.get(form.get("default_grade") or "A") or {}
    active_relations = set(retread_result.get("relations") or [])

    captured = report.get("captured_values") or {}
    inputs = {}
    for item in form.get("input_fields", []):
        field = item["field"]
        value = resolve_input_value(item, captured, model_key=model_key)
        inputs[field] = {**item, "value": value}

    uploads = {}
    captured_paths = report.get("captured") or {}
    for item in form.get("upload_fields", []):
        field = item["field"]
        page_key = str(item.get("page_key") or "")
        if item.get("random_source_dir") and field not in active_relations:
            continue
        path = captured_paths.get(page_key) if page_key else None
        uploads[field] = {**item, "path": path}

    selected_groups = set(material_form.get("selected_material_groups") or DEFAULT_SELECTED_MATERIAL_GROUPS)
    selected_codes = {str(code) for code in material_form.get("selected_material_codes") or []}
    all_materials = material_form.get("materials") or []
    material_groups = []
    for group in form.get("part_groups", []):
        group_title = str(group.get("title") or "")
        candidates = [
            normalize_material(item)
            for item in all_materials
            if str(item.get("group") or "") == group_title
        ]
        group_items = candidates if group_title in selected_groups else [
            item for item in candidates if item.get("code") in selected_codes
        ]
        material_groups.append({**group, "items": group_items})

    return {
        "form_id": resolved_form_id,
        "sn": str(report.get("sn") or ""),
        "model_key": form.get("model_key") or model_key,
        "model_label": form.get("model_label") or "",
        "grade": selected_grade,
        "customer": form.get("customer") or {},
        "template": form.get("template") or {},
        "previous_step": form.get("previous_step") or {},
        "choices": dict(form.get("default_choices") or {}),
        "retread_result": retread_result,
        "inputs": inputs,
        "uploads": uploads,
        "material_groups": material_groups,
        "bridge": {"autoupdate_root": str(autoupdate_root())},
    }


def submit_report(
    report: dict[str, Any],
    project_root: Path,
    form_id: str | None = None,
    model: str | None = None,
    grade: str | None = None,
    account_name: str | None = None,
) -> dict[str, Any]:
    form_data = build_report_form_data(report, project_root, form_id=form_id, model=model, grade=grade)
    report["form_data"] = form_data
    payload = {
        "type": "submit_report",
        "created_at": datetime.now().isoformat(),
        "account_name": account_name or get_active_account_name(project_root),
        "report": report,
        "form_data": form_data,
    }
    return _run_autoupdate_bridge(payload)


def seed_previous_step_for_task(project_root: Path, sn: str, model: str, account_name: str | None = None) -> dict[str, Any]:
    payload = {
        "type": "seed_previous_step",
        "created_at": datetime.now().isoformat(),
        "account_name": account_name or get_active_account_name(project_root),
        "sn": sn,
        "model": normalize_model_key(model),
    }
    return _run_autoupdate_bridge(payload)


def _run_autoupdate_bridge(payload: dict[str, Any]) -> dict[str, Any]:
    root = autoupdate_root()
    bridge_dir = root / "state" / "bridge_requests"
    bridge_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    sn = "".join(ch for ch in str(payload.get("form_data", {}).get("sn") or payload.get("sn") or "UNKNOWN") if ch.isalnum())
    payload_path = bridge_dir / f"{stamp}_{sn or 'UNKNOWN'}.json"
    payload_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    python = os.environ.get("UGREEN_AUTOUPDATE_PYTHON") or sys.executable
    if getattr(sys, "frozen", False):
        python = os.environ.get("UGREEN_AUTOUPDATE_PYTHON") or "python"
    cmd = [python, "-m", "automation.runner", "submit-report", "--payload", str(payload_path)]
    result = subprocess.run(cmd, cwd=root, text=True, capture_output=True, timeout=300, **_hidden_process_kwargs())
    if result.returncode != 0:
        raise FormEntryError((result.stderr or result.stdout or "自动录表系统调用失败").strip())
    text = (result.stdout or "").strip().splitlines()[-1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormEntryError(f"自动录表系统返回不可解析: {text}") from exc


def sync_autoupdate_repo(project_root: Path | None = None) -> dict[str, Any]:
    """Best-effort fast-forward of the autoupdate sibling repo from its remote.

    The factory-test exe updater only swaps its own binary; persistent edits
    that live in the autoupdate repo (e.g. ``materials.json`` opt-ins) used to
    need a manual ``git pull`` on the factory machine. This function automates
    that pull so a tag bump in factory-test is enough to bring the matching
    autoupdate-repo change along with it.

    Returns a small status dict; never raises. Silent no-op when:

    - ``autoupdate_root`` is not configured / not present
    - the directory is not a git work tree
    - the ``git`` binary is not on PATH
    - ``git fetch`` fails (offline, credential prompt, etc.)
    - the local branch has diverged from the upstream (own commits ahead)

    Mechanics: fetch, then if the local branch is strictly behind upstream,
    ``git reset --hard <upstream>``. The reset is intentional — the only
    tracked file the factory machine touches between startups is
    ``materials.json`` (re-written wholesale by the internal-system refresh that runs
    right after this function), so blowing away its uncommitted edits is
    cheaper than carrying a merge conflict forward. v0.1.14 used
    ``merge --ff-only --autostash`` instead, which left the work tree in
    a conflicted state when the upstream commit also touched
    ``materials.json``.
    """
    result: dict[str, Any] = {"status": "skipped", "reason": "", "before": "", "after": ""}

    try:
        root = autoupdate_root()
    except FormEntryError as exc:
        result["reason"] = f"autoupdate_root unresolved: {exc}"
        return result

    if not (root / ".git").exists():
        result["reason"] = f"{root} is not a git work tree"
        return result

    git_path = shutil.which("git")
    if not git_path:
        result["reason"] = "git binary not on PATH"
        return result

    def run(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [git_path, *args],
            cwd=root,
            text=True,
            capture_output=True,
            timeout=30,
            **_hidden_process_kwargs(),
        )

    try:
        before = run("rev-parse", "HEAD")
        if before.returncode != 0:
            result["reason"] = f"rev-parse failed: {(before.stderr or before.stdout).strip()}"
            return result
        result["before"] = before.stdout.strip()
        result["after"] = result["before"]  # default if we end up no-op

        fetch = run("fetch", "--quiet")
        if fetch.returncode != 0:
            result["reason"] = f"git fetch failed: {(fetch.stderr or fetch.stdout).strip()}"
            return result

        upstream = run("rev-parse", "@{upstream}")
        if upstream.returncode != 0:
            result["reason"] = f"no upstream configured: {(upstream.stderr or upstream.stdout).strip()}"
            return result
        upstream_sha = upstream.stdout.strip()

        if upstream_sha == result["before"]:
            result["status"] = "up_to_date"
            return result

        # is HEAD an ancestor of upstream? if not, the local branch has its
        # own commits and we must not clobber them.
        is_ancestor = run("merge-base", "--is-ancestor", "HEAD", upstream_sha)
        if is_ancestor.returncode != 0:
            result["reason"] = "local branch has diverged from upstream — refusing to reset"
            return result

        reset = run("reset", "--hard", "--quiet", upstream_sha)
        if reset.returncode != 0:
            result["reason"] = f"git reset --hard failed: {(reset.stderr or reset.stdout).strip()}"
            return result

        result["after"] = upstream_sha
        result["status"] = "updated"
    except subprocess.TimeoutExpired:
        result["reason"] = "git operation timed out"
    except OSError as exc:
        result["reason"] = f"git invocation failed: {exc}"

    return result


def refresh_form_materials(project_root: Path | None = None, account_name: str | None = None) -> dict[str, Any]:
    root = autoupdate_root()
    python = os.environ.get("UGREEN_AUTOUPDATE_PYTHON") or sys.executable
    if getattr(sys, "frozen", False):
        python = os.environ.get("UGREEN_AUTOUPDATE_PYTHON") or "python"
    cmd = [python, "-m", "automation.runner", "forms", "refresh"]
    if account_name:
        cmd.extend(["--account", account_name])
    result = subprocess.run(cmd, cwd=root, text=True, capture_output=True, timeout=180, **_hidden_process_kwargs())
    if result.returncode != 0:
        raise FormEntryError((result.stderr or result.stdout or "鑷姩褰曡〃鐗╂枡鍒锋柊澶辫触").strip())
    text = (result.stdout or "").strip().splitlines()[-1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormEntryError(f"鑷姩褰曡〃鐗╂枡鍒锋柊杩斿洖涓嶅彲瑙ｆ瀽: {text}") from exc


def run_login_ui(project_root: Path | None = None) -> None:
    """点「登录」时调 ugreen-nas-autoupdate 弹出 内部系统 登录窗口；阻塞到窗口关闭（在后台线程里调用）。
    登录逻辑/验证码全在模块的 login_ui，app 只负责把窗口叫出来、登录完刷新账号。"""
    root = autoupdate_root()
    python = os.environ.get("UGREEN_AUTOUPDATE_PYTHON") or sys.executable
    if getattr(sys, "frozen", False):
        python = os.environ.get("UGREEN_AUTOUPDATE_PYTHON") or "python"
    result = subprocess.run(
        [python, "-m", "automation.runner", "login-ui"],
        cwd=root, text=True, capture_output=True, timeout=1800, **_hidden_process_kwargs(),
    )
    if result.returncode != 0:
        raise FormEntryError((result.stderr or result.stdout or "登录窗口启动失败").strip())


def list_accounts(project_root: Path | None = None) -> list[dict[str, Any]]:
    data = load_accounts_file(project_root)
    accounts = data.get("accounts") or []
    if accounts:
        return accounts
    return [{"name": "自动录表系统", "account": "autoupdate-bridge"}]


def get_active_account_name(project_root: Path | None = None) -> str:
    data = load_accounts_file(project_root)
    active = str(data.get("active") or "").strip()
    if active:
        return active
    accounts = list_accounts(project_root)
    return str(accounts[0].get("name") or accounts[0].get("account") or "自动录表系统")


def set_active_account(project_root: Path | None, account_name: str) -> None:
    data = load_accounts_file(project_root)
    accounts = data.setdefault("accounts", [])
    if not any((item.get("name") or item.get("account")) == account_name for item in accounts):
        accounts.append({"name": account_name, "account": account_name, "updated_at": datetime.now().isoformat()})
    data["active"] = account_name
    save_accounts_file(project_root, data)


def add_or_update_account(
    project_root: Path | None,
    account: str,
    password: str = "",
    captcha: str = "",
    client_id: str = "autoupdate-bridge",
    base: str | None = None,
) -> dict[str, Any]:
    data = load_accounts_file(project_root)
    existing = None
    accounts = []
    for item in data.get("accounts", []):
        item_name = item.get("name") or item.get("account")
        item_account = item.get("account")
        if account in {item_name, item_account}:
            existing = item
            continue
        accounts.append(item)
    entry = {
        **(existing or {}),
        "name": account,
        "account": (existing or {}).get("account") or account,
        "bridge": True,
        "updated_at": datetime.now().isoformat(),
    }
    accounts.append(entry)
    data["accounts"] = accounts
    data["active"] = account
    save_accounts_file(project_root, data)
    return entry


def delete_account(project_root: Path | None, account_name: str) -> dict[str, Any]:
    data = load_accounts_file(project_root)
    accounts = data.get("accounts") or []
    filtered = [item for item in accounts if (item.get("name") or item.get("account")) != account_name]
    removed = len(filtered) != len(accounts)
    data["accounts"] = filtered
    if data.get("active") == account_name:
        data["active"] = str((filtered[0].get("name") or filtered[0].get("account")) if filtered else "")
    save_accounts_file(project_root, data)
    return {"removed": removed, "active": data.get("active") or ""}


def load_accounts_file(project_root: Path | None = None) -> dict[str, Any]:
    path = accounts_path(project_root)
    if not path.exists():
        return {"active": "", "accounts": []}
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_accounts_file(project_root: Path | None, data: dict[str, Any]) -> None:
    path = accounts_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def accounts_path(project_root: Path | None = None) -> Path:
    return autoupdate_root() / "state" / "accounts.local.json"


def get_login_captcha(base: str | None = None) -> dict[str, str]:
    # The bridge does not authenticate against the internal business system directly. A tiny transparent
    # PNG keeps the existing GUI dialog usable for registering a display name.
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAEElEQVR42mP8"
        "/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
    )
    return {"client": "autoupdate-bridge", "captcha": f"data:image/png;base64,{png}"}


def save_captcha_image(captcha_data_url: str, dest: Path) -> Path:
    data = captcha_data_url.split(",", 1)[1] if "," in captcha_data_url else captcha_data_url
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(base64.b64decode(data))
    return dest


def resolve_input_value(item: dict[str, Any], captured: dict[str, Any], model_key: str | None = None) -> str:
    value_key = str(item.get("value_key") or "")
    page_key = str(item.get("page_key") or "")
    if value_key == "link_bps":
        return link_bps_for_model(model_key)
    if page_key and value_key:
        page_values = captured.get(page_key) or {}
        value = page_values.get(value_key)
        if value:
            return normalize_input(value_key, str(value))
    return str(item.get("fallback") or "")


def link_bps_for_model(model: str | None) -> str:
    return "10" if normalize_model_key(model) == "4800Plus" else "2.5"


def normalize_input(value_key: str, raw: str) -> str:
    value = str(raw or "").strip()
    if value_key == "link_bps":
        compact = value.lower().replace(" ", "")
        if "10g" in compact or "10000000000" in compact:
            return "10"
        if "2.5g" in compact or "2500000000" in compact:
            return "2.5"
    return value


def normalize_material(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "code": item.get("code"),
        "name": item.get("name"),
        "group": item.get("group"),
        "required": bool(item.get("required", False)),
        "default_qty": int(item.get("default_qty", 1)),
    }


def list_replacement_part_candidates(
    project_root: Path | None = None,
    model: str | None = None,
    form_id: str | None = None,
) -> list[dict[str, Any]]:
    # The full per-form candidate list with its current deduction-list membership.
    # GUI feeds this into the 物料 tab so every part is shown (not just the codes
    # currently in selected_material_codes), and color-codes by `selected`.
    root = project_root or Path.cwd()
    resolved_form_id = resolve_form_id(root, model=model, form_id=form_id)
    forms = load_json(resolve_config_path(root, "forms.json"))
    materials = load_json(resolve_config_path(root, "materials.json"))
    form = forms.get("forms", {}).get(resolved_form_id) or {}
    material_form = materials.get("forms", {}).get(resolved_form_id, {})
    selected_groups = set(material_form.get("selected_material_groups") or DEFAULT_SELECTED_MATERIAL_GROUPS)
    selected_codes = {str(code) for code in material_form.get("selected_material_codes") or []}
    all_materials = material_form.get("materials") or []

    groups: list[dict[str, Any]] = []
    for group in form.get("part_groups", []):
        group_title = str(group.get("title") or "")
        group_all_in = group_title in selected_groups
        items: list[dict[str, Any]] = []
        for raw in all_materials:
            if str(raw.get("group") or "") != group_title:
                continue
            normalized = normalize_material(raw)
            code = str(normalized.get("code") or "")
            normalized["selected"] = group_all_in or (bool(code) and code in selected_codes)
            items.append(normalized)
        groups.append(
            {
                "field": group.get("field"),
                "title": group_title,
                "group_all_in": group_all_in,
                "items": items,
            }
        )
    return groups


def toggle_material_deduction(
    project_root: Path | None = None,
    model: str | None = None,
    form_id: str | None = None,
    code: str = "",
    select: bool | None = None,
) -> bool:
    # Add/remove `code` from selected_material_codes in materials.json.
    # If `select` is None, flips. Returns the post-toggle membership.
    code_str = str(code or "").strip()
    if not code_str:
        return False
    root = project_root or Path.cwd()
    resolved_form_id = resolve_form_id(root, model=model, form_id=form_id)
    materials_path = resolve_config_path(root, "materials.json")
    materials = load_json(materials_path)
    forms_map = materials.setdefault("forms", {})
    material_form = forms_map.setdefault(resolved_form_id, {})
    codes = [str(c) for c in (material_form.get("selected_material_codes") or [])]
    currently = code_str in codes
    target = (not currently) if select is None else bool(select)
    if target and not currently:
        codes.append(code_str)
    elif not target and currently:
        codes = [c for c in codes if c != code_str]
    material_form["selected_material_codes"] = codes
    materials_path.write_text(
        json.dumps(materials, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return code_str in codes


def is_previous_step_error(message: str) -> bool:
    return "缺少第一步翻新记录" in str(message) or "previous refurbishment process" in str(message)
