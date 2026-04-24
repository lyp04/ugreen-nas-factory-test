from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_AUTOUPDATE_ROOT = Path(r"${USERPROFILE}\ugreen-nas-autoupdate")
DEFAULT_SELECTED_MATERIAL_GROUPS = {"补充包材", "补充配件"}


class FormEntryError(RuntimeError):
    pass


def autoupdate_root() -> Path:
    candidates = []
    if os.environ.get("UGREEN_AUTOUPDATE_ROOT"):
        candidates.append(Path(os.environ["UGREEN_AUTOUPDATE_ROOT"]))
    candidates.extend(
        [
            DEFAULT_AUTOUPDATE_ROOT,
            Path(__file__).resolve().parents[2] / "ugreen-nas-autoupdate",
            Path(__file__).resolve().parents[1].parent.parent / "ugreen-nas-autoupdate",
        ]
    )
    for candidate in candidates:
        if (candidate / "automation" / "runner.py").exists():
            return candidate
    raise FormEntryError(
        "找不到自动录表系统，请设置 UGREEN_AUTOUPDATE_ROOT 或放在 "
        r"${USERPROFILE}\ugreen-nas-autoupdate"
    )


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
        path = captured_paths.get(page_key) if page_key else None
        uploads[field] = {**item, "path": path}

    selected_groups = set(material_form.get("selected_material_groups") or DEFAULT_SELECTED_MATERIAL_GROUPS)
    all_materials = material_form.get("materials") or []
    material_groups = []
    for group in form.get("part_groups", []):
        group_title = str(group.get("title") or "")
        group_items = [
            normalize_material(item)
            for item in all_materials
            if str(item.get("group") or "") == group_title and (not selected_groups or group_title in selected_groups)
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
    result = subprocess.run(cmd, cwd=root, text=True, capture_output=True, timeout=300)
    if result.returncode != 0:
        raise FormEntryError((result.stderr or result.stdout or "自动录表系统调用失败").strip())
    text = (result.stdout or "").strip().splitlines()[-1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FormEntryError(f"自动录表系统返回不可解析: {text}") from exc


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
    # The bridge does not authenticate against 内部系统 directly. A tiny transparent
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


def is_previous_step_error(message: str) -> bool:
    return "缺少第一步翻新记录" in str(message) or "previous refurbishment process" in str(message)
