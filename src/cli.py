from __future__ import annotations

import json
import ipaddress
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import click
from playwright.sync_api import sync_playwright

from . import form_entry

FORM_ENTRY_ENABLED = True
from .discovery import ugreen_broadcast
from .discovery.discover import _looks_like_ugos, find_nas, find_nas_candidates, wait_until_ready
from .flows import capture as capture_flow
from .flows import cleanup as cleanup_flow
from .flows import login as login_flow
from .flows import provision as provision_flow
from .flows import reset_factory as reset_factory_flow
from .flows import setup_wizard as setup_flow
from .flows import system_update as system_update_flow
from .utils.config_loader import load_configs
from .utils.browser_control import close_managed_context, launch_managed_context
from .utils.desktop import dismiss_desktop_overlays
from .utils import label as label_util
from .utils.logger import logger, setup_logger
from .utils.screenshot import capture_failure, relocate_session_dirs, session_dirs
from .utils.sn import (
    SN_TAIL_LENGTH,
    extract_full_sn,
    is_auto_sn_placeholder,
    model_key_from_sn,
    normalize_sn,
    same_sn_identity,
    sn_matches_text,
    sn_tail,
)


def get_project_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


PROJECT_ROOT = get_project_root()
DEVICE_LOCKS: dict[str, threading.Lock] = {}
DEVICE_LOCKS_GUARD = threading.Lock()
ACTIVE_DEVICE_IPS: set[str] = set()
DISCOVERY_ALLOCATION_GUARD = threading.Lock()
UNINITIALIZED_MARKERS = ("未初始化", "欢迎使用绿联云存储", "我们将引导您完成设备的初始化过程")
STARTING_MARKERS = ("服务启动中", "尝试手动刷新页面")
SETUP_NO_SN_MARKERS = ("命名您的绿联云", "命名您的设备", "序列号：", "设备名")
PAGE_LABELS = {
    "system_update": "系统更新",
    "network_interface": "网络接口",
    "storage_pool": "存储池",
    "hdd_write": "HDD 写入",
    "hdd_read": "HDD 读取",
    "ssd_write": "SSD 写入",
    "ssd_read": "SSD 读取",
    "resource_monitor": "资源监控",
    "fan_normal": "风扇标准模式",
    "fan_silent": "风扇静音模式",
    "fan_full_speed": "风扇全速模式",
}
FORM_MISSING_ACCESSORY_STAGE = "未上传：缺少配件照片"
FAN_RPM_FAILURE_STAGE = "风扇转速异常"
FAN_RPM_KEYS = ("resource_monitor", "fan_normal", "fan_silent", "fan_full_speed")
NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:,\d{3})+|\d+)(?:\.\d+)?")
POOL_CREATION_ERROR_MARKERS = (
    "Storage pool summary did not appear in time after creation",
    "Storage-pool creation dialog did not render any configured disk options in time",
    "Storage manager did not expose any unused healthy disks for pool creation before timeout",
    "No configured disks are available in the storage-pool creation dialog",
    "Storage-pool creation aborted by user",
)
POOL_CREATION_FAILURE_STAGE = "建池失败"
UNFLASHED_TITLE = login_flow.UNFLASHED_TITLE
UNFLASHED_MESSAGE = login_flow.UNFLASHED_MESSAGE
MODEL_KEYS = {"2800", "4800", "4800Plus"}
FORM_SUCCESS_STATUSES = {"success", "already_submitted"}


class TaskCancelled(RuntimeError):
    pass


def _normalize_model_override(model: str | None) -> str | None:
    compact = "".join(str(model or "").split()).lower()
    if compact in {"", "auto", "sn", "sn_auto"}:
        return None
    if compact in {"2800", "dxp2800"}:
        return "2800"
    if compact in {"4800", "dxp4800"}:
        return "4800"
    if compact in {"4800plus", "4800p", "dxp4800plus", "dxp4800p"}:
        return "4800Plus"
    return str(model or "").strip() or None


def _resolve_model_for_sn(sn: str, requested_model: str | None = None) -> tuple[str, str]:
    inferred = model_key_from_sn(sn)
    if inferred:
        return inferred, "sn_prefix"
    if requested_model:
        return requested_model, "requested"
    return "2800", "fallback"


def _update_report_model(report: dict, sn: str, requested_model: str | None = None) -> str:
    model, source = _resolve_model_for_sn(sn, requested_model)
    report["form_model"] = model
    report["model_source"] = source
    if requested_model:
        report["requested_form_model"] = requested_model
    return model


def _read_report_file(report_path: Path) -> dict:
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _merge_resume_report(report: dict, previous: dict) -> None:
    if not previous:
        return
    captured = previous.get("captured")
    if isinstance(captured, dict):
        report["captured"] = dict(captured)
    captured_values = previous.get("captured_values")
    if isinstance(captured_values, dict):
        report["captured_values"] = {
            str(page_key): dict(values)
            for page_key, values in captured_values.items()
            if isinstance(values, dict)
        }
    form_result = previous.get("form_result")
    if isinstance(form_result, dict):
        report["form_result"] = dict(form_result)
    if previous.get("provisioned"):
        report["provisioned"] = True
    if previous.get("provision_started"):
        report["provision_started"] = True
    previous_status = str(previous.get("status") or "").strip()
    if previous_status and previous_status != "success":
        report["resumed_from_status"] = previous_status
        if previous.get("current_stage"):
            report["resumed_from_stage"] = previous.get("current_stage")


def _form_result_is_success(report: dict) -> bool:
    form_result = report.get("form_result")
    if not isinstance(form_result, dict):
        return False
    return str(form_result.get("status") or "").lower() in FORM_SUCCESS_STATUSES


def _has_resume_artifacts(report: dict) -> bool:
    return bool(
        report.get("provisioned")
        or report.get("provision_started")
        or report.get("captured")
        or report.get("captured_values")
    )


def _first_number(value: object) -> float | None:
    match = NUMBER_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _fan_rpm_failures(captured_values: dict) -> list[str]:
    failures: list[str] = []
    for page_key in FAN_RPM_KEYS:
        values = captured_values.get(page_key)
        if not isinstance(values, dict):
            continue
        raw = values.get("device_fan_rpm")
        rpm = _first_number(raw)
        label = _page_label(page_key)
        if rpm is None:
            failures.append(f"{label} 未读取到风扇转速")
        elif rpm <= 0:
            failures.append(f"{label} 风扇转速 {raw}，必须大于 0")
    return failures


def _validate_captured_measurements(captured_values: dict) -> None:
    failures = _fan_rpm_failures(captured_values)
    if failures:
        raise RuntimeError(f"{FAN_RPM_FAILURE_STAGE}: {'；'.join(failures)}")


def _write_report_checkpoint(dirs: dict[str, Path], report: dict, stage: str | None = None) -> None:
    if stage:
        report["current_stage"] = stage
    report["checkpoint_at"] = datetime.now().isoformat()
    _write_json_atomic(dirs["base"] / "test_report.json", report)


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp_path.replace(path)


def is_pool_creation_timeout_error(exc_or_message: object) -> bool:
    message = str(exc_or_message)
    return any(marker in message for marker in POOL_CREATION_ERROR_MARKERS)


def is_unflashed_password_error(exc_or_message: object) -> bool:
    return UNFLASHED_MESSAGE in str(exc_or_message)


def failure_stage_for_error(exc_or_message: object) -> str:
    message = str(exc_or_message)
    if FAN_RPM_FAILURE_STAGE in message:
        return FAN_RPM_FAILURE_STAGE
    if is_pool_creation_timeout_error(exc_or_message):
        return _pool_failure_stage(message)
    if is_unflashed_password_error(exc_or_message):
        return UNFLASHED_TITLE
    capture_match = re.search(r"Capture failed at page '([A-Za-z0-9_]+)'", message)
    if capture_match:
        page_key = capture_match.group(1)
        label = PAGE_LABELS.get(page_key, page_key.replace("_", " "))
        speed_match = re.search(
            r"(\d+(?:\.\d+)?)\s*MB/s\s*<\s*\d+(?:\.\d+)?\s*MB/s",
            message,
        )
        if speed_match:
            return f"{label} 失败：{speed_match.group(1)} MB/s"
        return f"{label} 截图失败"
    if "did not become ready within" in message:
        sec = re.search(r"within\s+(\d+)\s*s", message)
        return f"设备上线超时（{sec.group(1)}s）" if sec else "设备上线超时"
    if "stayed on 'service starting' screen" in message:
        sec = re.search(r"for\s+(\d+)\s*s", message)
        return f"UGOS 卡在服务启动中（{sec.group(1)}s）" if sec else "UGOS 卡在服务启动中"
    if "missing device information" in message:
        return "UGOS 设备信息缺失"
    if "Could not determine whether setup wizard or login page" in message:
        return "UGOS 页面状态未知"
    if "SN未解绑" in message or "请先解绑SN" in message:
        return "SN 未解绑"
    if "already has a submitted record for this form" in message:
        return "表单已有提交记录"
    return "测试失败"


def _pool_failure_stage(message: str) -> str:
    missing = re.search(r"has missing disks\s+\[([^\]]+)\]", message)
    if missing:
        disks = re.findall(r"'([^']+)'", missing.group(1))
        if disks:
            return f"建池失败：缺{'、'.join(disks)}"
        return "建池失败：硬盘缺失"
    if "has no configured disks available" in message:
        return "建池失败：硬盘配置不匹配"
    if "Storage pool summary did not appear in time" in message:
        return "建池失败：摘要超时"
    if "did not render any configured disk options" in message:
        return "建池失败：弹窗无可用盘"
    if "did not expose any unused healthy disks" in message:
        return "建池失败：无可用未使用盘"
    return POOL_CREATION_FAILURE_STAGE


class TaskProgress:
    def __init__(
        self,
        callback: Callable[[dict], None] | None,
        task_id: str,
        sn: str,
        total_steps: int,
    ) -> None:
        self.callback = callback
        self.task_id = task_id
        self.sn = sn
        self.total_steps = max(total_steps, 1)
        self.completed_steps = 0
        self.percent = 0
        self.stage = "等待开始"

    def set_stage(self, stage: str, status: str = "running", **payload: object) -> None:
        self.stage = stage
        self._emit("stage", status=status, stage=stage, **payload)

    def complete_step(self, stage: str, status: str = "running", **payload: object) -> None:
        self.stage = stage
        self.completed_steps = min(self.total_steps, self.completed_steps + 1)
        self.percent = int((self.completed_steps / self.total_steps) * 100)
        self._emit("progress", status=status, stage=stage, **payload)

    def finish(self, status: str, stage: str, **payload: object) -> None:
        self.stage = stage
        if status == "success":
            self.completed_steps = self.total_steps
            self.percent = 100
        self._emit("finished", status=status, stage=stage, **payload)

    def update_sn(self, sn: str) -> None:
        self.sn = sn
        self._emit("identity", status="running", stage=self.stage)

    def _emit(self, event_type: str, **payload: object) -> None:
        if self.callback is None:
            return
        event = {
            "type": event_type,
            "task_id": self.task_id,
            "sn": self.sn,
            "completed": self.completed_steps,
            "total": self.total_steps,
            "percent": self.percent,
            **payload,
        }
        try:
            self.callback(event)
        except Exception:
            pass


def _cancel_requested(cancel_requested_cb: Callable[[], bool] | None) -> bool:
    if cancel_requested_cb is None:
        return False
    try:
        return bool(cancel_requested_cb())
    except Exception:
        return False


def _raise_if_cancelled(cancel_requested_cb: Callable[[], bool] | None) -> None:
    if _cancel_requested(cancel_requested_cb):
        raise TaskCancelled("Test cancelled by user")


def _acquire_lock_until_cancelled(lock: threading.Lock, cancel_requested_cb: Callable[[], bool] | None) -> None:
    while True:
        _raise_if_cancelled(cancel_requested_cb)
        if lock.acquire(timeout=1.0):
            return


def _wait_until_ready_cancelable(
    ip: str,
    port: int,
    max_wait: float,
    cancel_requested_cb: Callable[[], bool] | None,
) -> None:
    if cancel_requested_cb is None:
        wait_until_ready(ip, port, max_wait)
        return

    logger.info(f"Waiting for UGOS at {ip}:{port} to be ready (max {max_wait}s)...")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        _raise_if_cancelled(cancel_requested_cb)
        if _looks_like_ugos(ip, port):
            logger.info("UGOS service is ready")
            return
        for _ in range(20):
            _raise_if_cancelled(cancel_requested_cb)
            time.sleep(0.1)
    raise RuntimeError(f"UGOS at {ip}:{port} did not become ready within {max_wait}s")


def run_test(
    sn: str,
    nas_ip: str = "auto",
    mode: str = "setup",
    setup_file_log: bool = True,
    cleanup_before_finish: bool = False,
    factory_reset_before_finish: bool = False,
    auto_form_entry: bool = False,
    form_model: str | None = None,
    form_grade: str = "A",
    form_account_name: str | None = None,
    progress_cb: Callable[[dict], None] | None = None,
    confirm_disk_shortage_cb: Callable[[dict], bool] | None = None,
    confirm_previous_step_cb: Callable[[dict], bool] | None = None,
    resolve_form_grade_cb: Callable[[dict], str] | None = None,
    cancel_requested_cb: Callable[[], bool] | None = None,
    task_id: str | None = None,
) -> dict:
    sn = normalize_sn(sn)
    if len(sn_tail(sn)) < 4:
        raise ValueError("SN must contain at least the last 4 alphanumeric characters")

    requested_form_model = _normalize_model_override(form_model)
    form_model, form_model_source = _resolve_model_for_sn(sn, requested_form_model)
    form_features_enabled = FORM_ENTRY_ENABLED and form_entry is not None
    if not form_features_enabled:
        auto_form_entry = False
        form_grade = ""
        form_account_name = None

    config, selectors = load_configs(PROJECT_ROOT)
    output_root = (PROJECT_ROOT / config["output_dir"]).resolve()
    dirs = session_dirs(output_root, sn)
    previous_report = _read_report_file(dirs["base"] / "test_report.json")
    if setup_file_log:
        setup_logger(dirs["sn_root"], sn=sn)

    page_keys = list(config["pages"])
    total_steps = (
        3
        + len(page_keys)
        + int(auto_form_entry)
        + int(cleanup_before_finish or factory_reset_before_finish)
        + int(factory_reset_before_finish)
    )
    progress = TaskProgress(progress_cb, task_id or sn, sn, total_steps)
    report: dict = {
        "sn": sn,
        "mode": mode,
        "started_at": datetime.now().isoformat(),
        "cleanup_before_finish": cleanup_before_finish or factory_reset_before_finish,
        "factory_reset_before_finish": factory_reset_before_finish,
        "auto_form_entry": auto_form_entry,
        "form_model": form_model,
        "model_source": form_model_source,
        "form_grade": form_grade,
        "form_account_name": form_account_name,
        "status": "running",
    }
    _merge_resume_report(report, previous_report)
    if requested_form_model:
        report["requested_form_model"] = requested_form_model
    final_success_stage = "测试完成"
    if not form_features_enabled:
        report["form_skip_reason"] = "form_entry_feature_disabled"
    elif not auto_form_entry:
        report["form_skip_reason"] = "auto_form_entry_disabled"
    ip = ""
    port = config["network"]["ugos_http_port"]
    device_lock: threading.Lock | None = None
    device_lock_acquired = False
    reserved_ips: set[str] = set()

    def checkpoint(stage: str | None = None) -> None:
        _write_report_checkpoint(dirs, report, stage)

    with logger.contextualize(task_id=task_id or sn, device_sn=sn):
        _raise_if_cancelled(cancel_requested_cb)
        progress.set_stage("等待设备上线", status="running", nas_ip=nas_ip)
        checkpoint("等待设备上线")
        try:
            ip, reserved_ips, discovered_sn = _resolve_ip_for_task(
                sn,
                nas_ip,
                config,
                progress,
                cancel_requested_cb,
            )
            _raise_if_cancelled(cancel_requested_cb)
            sn, dirs = _upgrade_task_sn(
                current_sn=sn,
                discovered_sn=discovered_sn,
                output_root=output_root,
                dirs=dirs,
                progress=progress,
                report=report,
                setup_file_log=setup_file_log,
            )
            report["nas_ip"] = ip
            if reserved_ips:
                report["nas_reserved_ips"] = sorted(reserved_ips)
            device_lock = _device_lock_for_ip(ip)
            if not device_lock.acquire(blocking=False):
                logger.info(f"NAS {ip}: another task is active, waiting on hold")
                progress.set_stage("等待同一 NAS 上一个任务完成", status="running", nas_ip=ip)
                _acquire_lock_until_cancelled(device_lock, cancel_requested_cb)
            device_lock_acquired = True
            logger.info(f"NAS {ip}: device lock acquired")
            progress.set_stage("等待设备上线", status="running", nas_ip=ip)
            _wait_until_ready_cancelable(ip, port, config["network"]["service_ready_timeout"], cancel_requested_cb)
            progress.complete_step("设备已连接", nas_ip=ip)
            checkpoint("设备已连接")

            _raise_if_cancelled(cancel_requested_cb)

            nas_url = f"http://{ip}:{port}"
            with sync_playwright() as pw:
                browser_session = launch_managed_context(pw, config["browser"], task_id or sn)
                if browser_session.browser_pid is not None:
                    _emit_browser_event(progress_cb, "browser_ready", browser_pid=browser_session.browser_pid)

                context = browser_session.context
                page = browser_session.page
                page.set_default_timeout(config["browser"]["default_timeout_ms"])

                try:
                    _raise_if_cancelled(cancel_requested_cb)
                    progress.set_stage("执行初始化或登录", status="running", nas_ip=ip)
                    resolved_mode = mode
                    if mode == "setup":
                        try:
                            discovered_sn = setup_flow.run(page, nas_url, config["admin"], selectors, sn=sn)
                            sn, dirs = _upgrade_task_sn(
                                current_sn=sn,
                                discovered_sn=discovered_sn,
                                output_root=output_root,
                                dirs=dirs,
                                progress=progress,
                                report=report,
                                setup_file_log=setup_file_log,
                            )
                        except setup_flow.SetupAlreadyRegistered:
                            logger.info("Setup mode fallback: device is already registered, switching to login flow")
                            login_flow.run(page, nas_url, config["admin"], selectors)
                            sn, dirs = _upgrade_task_sn(
                                current_sn=sn,
                                discovered_sn=_extract_full_sn_from_page(page, sn),
                                output_root=output_root,
                                dirs=dirs,
                                progress=progress,
                                report=report,
                                setup_file_log=setup_file_log,
                            )
                            resolved_mode = "login"
                    else:
                        login_flow.run(page, nas_url, config["admin"], selectors)
                        sn, dirs = _upgrade_task_sn(
                            current_sn=sn,
                            discovered_sn=_extract_full_sn_from_page(page, sn),
                            output_root=output_root,
                            dirs=dirs,
                            progress=progress,
                            report=report,
                            setup_file_log=setup_file_log,
                        )

                    _raise_if_cancelled(cancel_requested_cb)
                    browser_session, page = _ensure_browser_session(
                        pw=pw,
                        browser_session=browser_session,
                        page=page,
                        browser_cfg=config["browser"],
                        task_id=task_id or sn,
                        progress_cb=progress_cb,
                        nas_url=nas_url,
                        admin=config["admin"],
                        selectors=selectors,
                    )

                    _raise_if_cancelled(cancel_requested_cb)
                    sn, dirs = _upgrade_task_sn_from_settings_if_needed(
                        page=page,
                        selectors=selectors,
                        current_sn=sn,
                        output_root=output_root,
                        dirs=dirs,
                        progress=progress,
                        report=report,
                        setup_file_log=setup_file_log,
                    )
                    report["resolved_mode"] = resolved_mode
                    form_model = _update_report_model(report, sn, requested_form_model)
                    logger.info(f"SN {sn} model resolved as {form_model} ({report.get('model_source')})")
                    progress.complete_step("初始化完成", nas_ip=ip, resolved_mode=resolved_mode)
                    checkpoint("初始化完成")

                    progress.set_stage("检查系统更新", status="running", nas_ip=ip)
                    _raise_if_cancelled(cancel_requested_cb)
                    if system_update_flow.run_update_first(page, nas_url, config["admin"], selectors):
                        report["system_update_before_provision"] = "installed"
                        progress.set_stage("系统更新完成", status="running", nas_ip=ip)
                        browser_session, page = _ensure_browser_session(
                            pw=pw,
                            browser_session=browser_session,
                            page=page,
                            browser_cfg=config["browser"],
                            task_id=task_id or sn,
                            progress_cb=progress_cb,
                            nas_url=nas_url,
                            admin=config["admin"],
                            selectors=selectors,
                        )
                    checkpoint("系统更新检查完成")

                    _raise_if_cancelled(cancel_requested_cb)
                    try:
                        dismiss_desktop_overlays(
                            page,
                            selectors,
                            max_rounds=8,
                            completion_wait_ms=6_000 if resolved_mode == "setup" else 0,
                            idle_wait_ms=6_000 if resolved_mode == "setup" else 0,
                        )
                    except Exception as exc:
                        logger.warning(f"Desktop overlay cleanup interrupted; reopening browser before capture: {exc}")
                    browser_session, page = _ensure_browser_session(
                        pw=pw,
                        browser_session=browser_session,
                        page=page,
                        browser_cfg=config["browser"],
                        task_id=task_id or sn,
                        progress_cb=progress_cb,
                        nas_url=nas_url,
                        admin=config["admin"],
                        selectors=selectors,
                    )

                    progress.set_stage("准备共享目录", status="running", nas_ip=ip)
                    _raise_if_cancelled(cancel_requested_cb)
                    reset_existing_pools = not _has_resume_artifacts(report)
                    report["provision_started"] = True
                    checkpoint("准备共享目录")
                    provision_flow.run(
                        page,
                        config,
                        selectors,
                        config["admin"],
                        confirm_disk_shortage_cb=confirm_disk_shortage_cb,
                        model=form_model,
                        reset_existing_pools=reset_existing_pools,
                    )
                    report["provisioned"] = True
                    progress.complete_step("共享目录已就绪", nas_ip=ip)
                    checkpoint("共享目录已就绪")

                    _raise_if_cancelled(cancel_requested_cb)
                    captured_values: dict[str, dict[str, str]] = {
                        str(page_key): dict(values)
                        for page_key, values in (report.get("captured_values") or {}).items()
                        if isinstance(values, dict)
                    }
                    report["captured_values"] = captured_values
                    report["captured"] = dict(report.get("captured") or {})
                    transfer_config = dict(config.get("transfer") or {})
                    transfer_config["model"] = form_model or "2800"

                    def capture_progress(event: dict) -> None:
                        _handle_capture_progress(progress, event, cancel_requested_cb)
                        if event.get("type") != "capture_page_completed":
                            return
                        page_key = str(event.get("page_key") or "")
                        screenshot = str(event.get("screenshot") or "")
                        if not page_key or not screenshot:
                            return
                        report.setdefault("captured", {})[page_key] = screenshot
                        values = event.get("values")
                        if isinstance(values, dict) and values:
                            captured_values[page_key] = {str(key): str(value) for key, value in values.items()}
                            report["captured_values"] = captured_values
                        checkpoint(f"截图完成：{_page_label(page_key)}")

                    saved = capture_flow.run(
                        page,
                        nas_url,
                        sn,
                        page_keys,
                        selectors,
                        dirs["screenshots"],
                        config["admin"],
                        transfer_config,
                        progress_cb=capture_progress,
                        capture_values=captured_values,
                    )
                    report["captured"] = saved
                    report["captured_values"] = captured_values
                    _validate_captured_measurements(captured_values)
                    checkpoint("截图完成")

                    if auto_form_entry:
                        _raise_if_cancelled(cancel_requested_cb)
                        progress.set_stage("自动录表", status="running", nas_ip=ip)
                        if _form_result_is_success(report):
                            result = report["form_result"]
                            logger.info(
                                f"Auto form entry already completed in checkpoint: {result.get('status')}"
                            )
                        else:
                            selected_grade = str(form_grade or report.get("form_grade") or "A").strip().upper()
                            if resolve_form_grade_cb is not None:
                                selected_grade = str(
                                    resolve_form_grade_cb(
                                        {
                                            "sn": sn,
                                            "model": form_model,
                                            "grade": selected_grade,
                                        }
                                    )
                                    or selected_grade
                                ).strip().upper()
                            if selected_grade not in {"A", "B"}:
                                selected_grade = "A"
                            report["form_grade"] = selected_grade
                            result = form_entry.submit_report(
                                report,
                                PROJECT_ROOT,
                                model=form_model,
                                grade=selected_grade,
                                account_name=form_account_name,
                            )
                            report["form_result"] = result
                        if not _form_result_is_success(report):
                            raise RuntimeError(
                                f"自动录表未成功: {report.get('form_result')}"
                            )
                        progress.complete_step("自动录表完成", nas_ip=ip, form_status=result.get("status"))
                        checkpoint("自动录表完成")

                    if cleanup_before_finish or factory_reset_before_finish:
                        browser_session, page = _ensure_browser_session(
                            pw=pw,
                            browser_session=browser_session,
                            page=page,
                            browser_cfg=config["browser"],
                            task_id=task_id or sn,
                            progress_cb=progress_cb,
                            nas_url=nas_url,
                            admin=config["admin"],
                            selectors=selectors,
                        )
                        progress.set_stage("清理存储池", status="running", nas_ip=ip)
                        _raise_if_cancelled(cancel_requested_cb)
                        deleted = cleanup_flow.run(page, selectors, config["admin"])
                        report["deleted_pools"] = deleted
                        progress.complete_step("清理完成", nas_ip=ip)
                        checkpoint("清理完成")

                        if factory_reset_before_finish:
                            progress.set_stage("恢复出厂设置", status="running", nas_ip=ip)
                            _raise_if_cancelled(cancel_requested_cb)
                            reset_factory_flow.run(page, selectors, config["admin"])
                            report["factory_reset"] = "initiated"
                            progress.complete_step("恢复出厂设置已触发", nas_ip=ip)

                    report["status"] = "success"
                    report["current_stage"] = final_success_stage
                    progress.finish("success", final_success_stage, nas_ip=ip)
                except TaskCancelled as exc:
                    logger.info(f"Test cancelled: {exc}")
                    report["status"] = "cancelled"
                    report["error"] = str(exc)
                    progress.finish("cancelled", "用户中断", nas_ip=ip or nas_ip, error=str(exc))
                    raise
                except Exception as exc:
                    if _cancel_requested(cancel_requested_cb):
                        logger.info(f"Test cancelled: {exc}")
                        report["status"] = "cancelled"
                        report["error"] = "Test cancelled by user"
                        progress.finish("cancelled", "用户中断", nas_ip=ip or nas_ip, error=str(exc))
                        raise TaskCancelled("Test cancelled by user") from exc
                    logger.error(f"Test failed: {exc}")
                    failure_stage = failure_stage_for_error(exc)
                    report["status"] = "failed"
                    report["error"] = str(exc)
                    report["current_stage"] = failure_stage
                    progress.finish("failed", failure_stage, nas_ip=ip or nas_ip, error=str(exc))
                    capture_failure(page, sn, step="cli_top", dest_dir=dirs["screenshots"])
                    raise
                finally:
                    report["finished_at"] = datetime.now().isoformat()
                    _write_json_atomic(dirs["base"] / "test_report.json", report)
                    close_managed_context(browser_session)
                    _emit_browser_event(progress_cb, "browser_closed")
        except TaskCancelled as exc:
            if report.get("status") == "running":
                report["status"] = "cancelled"
                report["error"] = str(exc)
                progress.finish("cancelled", "用户中断", nas_ip=ip or nas_ip, error=str(exc))
                report["finished_at"] = datetime.now().isoformat()
                _write_json_atomic(dirs["base"] / "test_report.json", report)
            raise
        except Exception as exc:
            if report.get("status") == "running":
                failure_stage = failure_stage_for_error(exc)
                report["status"] = "failed"
                report["error"] = str(exc)
                report["current_stage"] = failure_stage
                progress.finish("failed", failure_stage, nas_ip=ip or nas_ip, error=str(exc))
                report["finished_at"] = datetime.now().isoformat()
                _write_json_atomic(dirs["base"] / "test_report.json", report)
            raise
        finally:
            if device_lock_acquired and device_lock is not None:
                logger.info(f"NAS {ip}: device lock released")
                device_lock.release()
            _release_reserved_ips(reserved_ips)

    logger.info(f"Test complete for {sn}")
    return report


def run_cleanup(sn: str, nas_ip: str = "auto", setup_file_log: bool = True) -> dict:
    config, selectors = load_configs(PROJECT_ROOT)
    output_root = (PROJECT_ROOT / config["output_dir"]).resolve()
    dirs = session_dirs(output_root, sn)
    if setup_file_log:
        setup_logger(dirs["sn_root"], sn=sn)

    ip = _resolve_ip(nas_ip, config)
    port = config["network"]["ugos_http_port"]
    device_lock = _device_lock_for_ip(ip)
    device_lock_acquired = False
    if not device_lock.acquire(blocking=False):
        logger.info(f"NAS {ip}: another task is active, waiting on hold")
        _acquire_lock_until_cancelled(device_lock, None)
    device_lock_acquired = True
    logger.info(f"NAS {ip}: device lock acquired")
    wait_until_ready(ip, port, config["network"]["service_ready_timeout"])

    nas_url = f"http://{ip}:{port}"
    report: dict = {"sn": sn, "nas_ip": ip, "started_at": datetime.now().isoformat()}

    with sync_playwright() as pw:
        browser_session = launch_managed_context(pw, config["browser"], f"cleanup_{sn}")
        context = browser_session.context
        page = browser_session.page
        page.set_default_timeout(config["browser"]["default_timeout_ms"])

        try:
            login_flow.run(page, nas_url, config["admin"], selectors)
            dismiss_desktop_overlays(page, selectors, max_rounds=2, completion_wait_ms=0)
            deleted = cleanup_flow.run(page, selectors, config["admin"])
            report["deleted_pools"] = deleted
            report["status"] = "success"
        except Exception as exc:
            report["status"] = "failed"
            report["error"] = str(exc)
            capture_failure(page, sn, step="cleanup_top", dest_dir=dirs["screenshots"])
            raise
        finally:
            report["finished_at"] = datetime.now().isoformat()
            (dirs["base"] / "cleanup_report.json").write_text(
                json.dumps(report, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            close_managed_context(browser_session)
            if device_lock_acquired:
                logger.info(f"NAS {ip}: device lock released")
                device_lock.release()
                device_lock_acquired = False

    logger.info(f"Cleanup complete for {sn}")
    return report


def run_smoke(nas_ip: str, setup_file_log: bool = True) -> dict:
    config, selectors = load_configs(PROJECT_ROOT)
    output_root = (PROJECT_ROOT / config["output_dir"]).resolve()
    smoke_dirs = session_dirs(output_root, "_smoke")
    if setup_file_log:
        setup_logger(smoke_dirs["sn_root"], sn="_smoke")

    unresolved = _find_todo_selectors(selectors)
    if unresolved:
        logger.warning(f"{len(unresolved)} selectors are still TODO:")
        for path in unresolved:
            logger.warning(f"  - {path}")
    else:
        logger.info("All selectors in selectors.yml are filled in")

    port = config["network"]["ugos_http_port"]
    nas_url = f"http://{nas_ip}:{port}"

    with sync_playwright() as pw:
        browser_session = launch_managed_context(pw, config["browser"], "_smoke")
        context = browser_session.context
        page = browser_session.page
        page.set_default_timeout(5000)

        try:
            page.goto(nas_url, wait_until="domcontentloaded")
            results = _probe_selectors(page, selectors)
            hit = sum(1 for v in results.values() if v)
            total = len(results)
            logger.info(f"Selector probe: {hit}/{total} visible")
            for key, visible in results.items():
                logger.info(f"  {'OK  ' if visible else 'MISS'}  {key}")
        finally:
            close_managed_context(browser_session)

    return {
        "todos": unresolved,
        "hits": hit,
        "total": total,
        "missing": [k for k, v in results.items() if not v],
    }


def _resolve_ip(nas_ip: str, config: dict) -> str:
    if nas_ip and nas_ip.lower() != "auto":
        return nas_ip
    return find_nas(
        subnet=config["network"]["subnet"],
        port=config["network"]["ugos_http_port"],
        discovery_timeout=config["network"]["discovery_timeout"],
    )


@dataclass(frozen=True)
class CandidateSelection:
    ip: str
    reserved_ips: frozenset[str]
    reason: str
    full_sn: str | None = None


def _resolve_ip_for_task(
    sn: str,
    nas_ip: str,
    config: dict,
    progress: TaskProgress,
    cancel_requested_cb: Callable[[], bool] | None = None,
) -> tuple[str, set[str], str | None]:
    if nas_ip and nas_ip.lower() != "auto":
        return nas_ip, set(), None

    auto_discovery_timeout = float(
        config["network"].get("auto_discovery_timeout", config["network"]["service_ready_timeout"])
    )
    deadline = datetime.now().timestamp() + auto_discovery_timeout
    last_error: Exception | None = None
    target_tail = sn_tail(sn)

    while datetime.now().timestamp() < deadline:
        _raise_if_cancelled(cancel_requested_cb)
        with DISCOVERY_ALLOCATION_GUARD:
            excluded = set(ACTIVE_DEVICE_IPS)
            if excluded:
                logger.info(f"Auto discovery excluding active NAS IPs: {sorted(excluded)}")
            try:
                candidates = find_nas_candidates(
                    subnet=config["network"]["subnet"],
                    port=config["network"]["ugos_http_port"],
                    discovery_timeout=config["network"]["discovery_timeout"],
                    exclude=excluded,
                )
                selection = _select_candidate_for_sn(
                    sn=sn,
                    candidates=candidates,
                    port=config["network"]["ugos_http_port"],
                    browser_cfg=config.get("browser") or {},
                )
                if selection:
                    reserved_ips = set(selection.reserved_ips)
                    ACTIVE_DEVICE_IPS.update(reserved_ips)
                    logger.info(
                        f"Auto discovery assigned NAS IP {selection.ip} for SN tail {target_tail} "
                        f"({selection.reason}); reserved IPs: {sorted(reserved_ips)}"
                    )
                    return selection.ip, reserved_ips, selection.full_sn
                last_error = RuntimeError(
                    f"No discovered UGOS NAS matched SN tail {target_tail}; candidates: {candidates}"
                )
            except Exception as exc:
                last_error = exc

        progress.set_stage("等待匹配 SN 的 NAS 上线", status="running", nas_ip="auto")
        logger.info(f"Auto discovery found no matching unused NAS yet; retrying: {last_error}")
        for _ in range(30):
            _raise_if_cancelled(cancel_requested_cb)
            time.sleep(0.1)

    raise RuntimeError(f"No unused UGOS NAS matching SN tail {target_tail} became available before timeout: {last_error}")


def _select_candidate_for_sn(
    sn: str,
    candidates: list[str],
    port: int,
    browser_cfg: dict,
) -> CandidateSelection | None:
    target_tail = sn_tail(sn)
    if not candidates:
        return None

    logger.info(f"Auto discovery matching SN tail {target_tail} against candidates: {candidates}")
    identity_texts = _probe_candidate_identity_texts(candidates, port, browser_cfg)
    matched_ips: list[str] = []
    fallback_ips: list[str] = []
    setup_no_sn_ips: list[str] = []

    for ip, text in identity_texts.items():
        preview = _identity_preview(text)
        full_sn = extract_full_sn(text, sn)
        explicit_full_sn = extract_full_sn(text)
        if sn_matches_text(sn, text):
            logger.info(f"Auto discovery candidate {ip} matched SN tail {target_tail}: {preview}")
            matched_ips.append(ip)
            if full_sn:
                logger.info(f"Auto discovery learned full SN {full_sn} from {ip}")
            continue
        if explicit_full_sn and not same_sn_identity(sn, explicit_full_sn):
            logger.info(
                f"Auto discovery candidate {ip} exposed SN {explicit_full_sn}, "
                f"which does not match SN tail {target_tail}: {preview}"
            )
            continue
        if _is_uninitialized_identity_text(text):
            logger.info(
                f"Auto discovery candidate {ip} has no visible SN yet; setup page is uninitialized: {preview}"
            )
            fallback_ips.append(ip)
            continue
        if _is_setup_without_full_sn_identity_text(text):
            logger.info(
                f"Auto discovery candidate {ip} is on setup page but full SN is not visible yet: {preview}"
            )
            setup_no_sn_ips.append(ip)
            continue
        if _is_starting_identity_text(text):
            logger.info(f"Auto discovery candidate {ip} is still starting; waiting before assignment: {preview}")
            continue
        logger.info(f"Auto discovery candidate {ip} did not match SN tail {target_tail}: {preview}")

    if matched_ips:
        reserved = _reserve_neighboring_aliases(matched_ips, fallback_ips + setup_no_sn_ips)
        full_sn = _first_full_sn_for_tail(identity_texts, sn, matched_ips)
        selected_ip = _preferred_matched_ip(matched_ips, identity_texts)
        reason = "matched visible SN"
        if selected_ip != matched_ips[0]:
            interface = _identity_field(identity_texts.get(selected_ip, ""), "interface")
            reason = f"matched visible SN; preferred {interface or 'better'} interface"
        return CandidateSelection(selected_ip, frozenset(reserved), reason, full_sn)

    fallback_groups = _candidate_ip_groups(fallback_ips + setup_no_sn_ips)
    if fallback_groups:
        if len(fallback_groups) > 1:
            logger.info(
                "Auto discovery found multiple uninitialized/setup candidates without a visible SN; "
                f"waiting for UGREEN broadcast or page identity instead of guessing: {fallback_groups}"
            )
            return None
        selected_group = fallback_groups[0]
        if len(selected_group) > 1:
            logger.info(
                f"Auto discovery treating consecutive uninitialized IPs as one dual-port NAS: {selected_group}"
            )
        else:
            logger.info(
                f"Auto discovery assigning uninitialized NAS without visible SN: {selected_group[0]}"
            )
        return CandidateSelection(
            selected_group[0],
            frozenset(selected_group),
            "uninitialized page has no visible SN",
        )

    return None


def _first_full_sn_for_tail(identity_texts: dict[str, str], sn: str, ips: list[str]) -> str | None:
    for ip in ips:
        full_sn = extract_full_sn(identity_texts.get(ip, ""), sn)
        if full_sn:
            return full_sn
    return None


def _preferred_matched_ip(matched_ips: list[str], identity_texts: dict[str, str]) -> str:
    if not matched_ips:
        return ""
    ordered_ips = _sort_ip_strings(matched_ips)
    return max(
        ordered_ips,
        key=lambda ip: (_identity_port_score(identity_texts.get(ip, "")), -ordered_ips.index(ip)),
    )


def _identity_port_score(text: str) -> int:
    normalized = text.lower().replace(" ", "")
    model = _identity_field(text, "model").lower().replace(" ", "")
    interface = _identity_field(text, "interface").lower()
    score = 0
    if "10000" in normalized or "10gb" in normalized or "10g" in normalized:
        score += 100
    if "dxp4800plus" in model and interface == "eth0":
        score += 50
    elif interface == "eth0":
        score += 5
    return score


def _identity_field(text: str, key: str) -> str:
    prefix = f"{key}="
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith(prefix.lower()):
            return stripped[len(prefix):].strip()
    return ""


def _is_uninitialized_identity_text(text: str) -> bool:
    return any(marker in text for marker in UNINITIALIZED_MARKERS)


def _is_starting_identity_text(text: str) -> bool:
    return any(marker in text for marker in STARTING_MARKERS)


def _is_setup_without_full_sn_identity_text(text: str) -> bool:
    return any(marker in text for marker in SETUP_NO_SN_MARKERS) and extract_full_sn(text) is None


def _reserve_neighboring_aliases(matched_ips: list[str], alias_candidates: list[str]) -> list[str]:
    reserved = set(matched_ips)
    aliases = set(alias_candidates)
    for match in matched_ips:
        for alias in aliases:
            if _are_consecutive_ips(match, alias) or _are_consecutive_ips(alias, match):
                reserved.add(alias)
    return _sort_ip_strings(list(reserved))


def _candidate_ip_groups(ips: list[str]) -> list[str]:
    sorted_ips = _sort_ip_strings(ips)
    groups: list[list[str]] = []
    index = 0
    while index < len(sorted_ips):
        current = sorted_ips[index]
        if index + 1 < len(sorted_ips) and _are_consecutive_ips(current, sorted_ips[index + 1]):
            groups.append([current, sorted_ips[index + 1]])
            index += 2
        else:
            groups.append([current])
            index += 1
    return groups


def _sort_ip_strings(ips: list[str]) -> list[str]:
    def key(ip: str):
        try:
            return (0, ipaddress.ip_address(ip))
        except ValueError:
            return (1, ip)

    return sorted(dict.fromkeys(ips), key=key)


def _are_consecutive_ips(left: str, right: str) -> bool:
    try:
        left_ip = ipaddress.ip_address(left)
        right_ip = ipaddress.ip_address(right)
    except ValueError:
        return False
    return left_ip.version == right_ip.version and int(right_ip) == int(left_ip) + 1


def _probe_candidate_identity_texts(candidates: list[str], port: int, browser_cfg: dict) -> dict[str, str]:
    results: dict[str, str] = {}
    broadcast_texts = _probe_ugreen_broadcast_identity_texts(candidates)
    if candidates and all(_broadcast_text_has_sn(broadcast_texts.get(ip, "")) for ip in candidates):
        return {ip: broadcast_texts[ip] for ip in candidates}

    launch_kwargs: dict = {"headless": True}
    channel = browser_cfg.get("channel") or None
    if channel and channel != "chromium":
        launch_kwargs["channel"] = channel

    viewport = browser_cfg.get("viewport") or [1280, 720]
    language = browser_cfg.get("language") or "zh-CN"

    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch_kwargs)
        try:
            for ip in candidates:
                parts: list[str] = []
                if ip in broadcast_texts:
                    parts.append(broadcast_texts[ip])
                page = browser.new_page(
                    locale=language,
                    viewport={"width": int(viewport[0]), "height": int(viewport[1])},
                )
                try:
                    page.goto(f"http://{ip}:{port}", wait_until="commit", timeout=8_000)
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5_000)
                    except Exception:
                        pass
                    page.wait_for_timeout(2_000)
                    body_text = page.locator("body").inner_text(timeout=5_000)
                    parts.append(f"{page.title()}\n{body_text}")
                except Exception as exc:
                    parts.append(f"probe-error: {exc}")
                finally:
                    page.close()
                results[ip] = "\n".join(parts)
        finally:
            browser.close()
    return results


def _broadcast_text_has_sn(text: str) -> bool:
    return any(line.startswith("SN=") and len(normalize_sn(line[3:])) > SN_TAIL_LENGTH for line in text.splitlines())


def _probe_ugreen_broadcast_identity_texts(candidates: list[str]) -> dict[str, str]:
    candidate_set = set(candidates)
    texts: dict[str, str] = {}
    try:
        hits = ugreen_broadcast.discover(timeout=1.5)
    except Exception as exc:
        logger.info(f"UGREEN broadcast identity probe failed: {exc}")
        return texts

    for hit in hits:
        if hit.address not in candidate_set:
            continue
        fields = [f"IP={hit.address}"]
        if hit.sn:
            fields.append(f"SN={hit.sn}")
        if hit.mac:
            fields.append(f"MAC={hit.mac}")
        for key, value in sorted(hit.data.items()):
            if key in {"ip", "sn", "mac", "pair"}:
                continue
            if isinstance(value, (str, int, float, bool)):
                fields.append(f"{key}={value}")
        text = "UGREEN broadcast\n" + "\n".join(fields)
        texts[hit.address] = text
        logger.info(f"UGREEN broadcast identity for {hit.address}: {_identity_preview(text)}")
    return texts


def _identity_preview(text: str, limit: int = 160) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit] if compact else "<empty>"


def _upgrade_task_sn(
    current_sn: str,
    discovered_sn: str | None,
    output_root: Path,
    dirs: dict[str, Path],
    progress: TaskProgress,
    report: dict,
    setup_file_log: bool,
) -> tuple[str, dict[str, Path]]:
    full_sn = normalize_sn(discovered_sn or "")
    if not full_sn or full_sn == current_sn:
        return current_sn, dirs
    if not same_sn_identity(current_sn, full_sn) and not is_auto_sn_placeholder(current_sn):
        logger.warning(f"Ignoring discovered SN {full_sn}; it does not match input {current_sn}")
        return current_sn, dirs

    logger.info(f"SN {current_sn} resolved to full SN {full_sn}")
    report.setdefault("input_sn", current_sn)
    report["sn"] = full_sn
    dirs = relocate_session_dirs(output_root, dirs, full_sn)
    _merge_resume_report(report, _read_report_file(dirs["base"] / "test_report.json"))
    if setup_file_log:
        setup_logger(dirs["sn_root"], sn=full_sn)
    progress.update_sn(full_sn)
    return full_sn, dirs


def _ensure_browser_session(
    pw,
    browser_session,
    page,
    browser_cfg: dict,
    task_id: str,
    progress_cb: Callable[[dict], None] | None,
    nas_url: str,
    admin: dict,
    selectors: dict,
):
    if _page_is_alive(page):
        return browser_session, page

    logger.warning("Browser page was closed after setup; relaunching and logging in again")
    try:
        close_managed_context(browser_session)
    except Exception:
        pass
    _emit_browser_event(progress_cb, "browser_closed")

    browser_session = launch_managed_context(pw, browser_cfg, task_id)
    if browser_session.browser_pid is not None:
        _emit_browser_event(progress_cb, "browser_ready", browser_pid=browser_session.browser_pid)

    page = browser_session.page
    page.set_default_timeout(browser_cfg["default_timeout_ms"])
    login_flow.run(page, nas_url, admin, selectors)
    return browser_session, page


def _page_is_alive(page) -> bool:
    try:
        if page.is_closed():
            return False
        page.evaluate("() => 1")
        return True
    except Exception:
        return False


def _upgrade_task_sn_from_settings_if_needed(
    page,
    selectors: dict,
    current_sn: str,
    output_root: Path,
    dirs: dict[str, Path],
    progress: TaskProgress,
    report: dict,
    setup_file_log: bool,
) -> tuple[str, dict[str, Path]]:
    if len(normalize_sn(current_sn)) > 4 and not is_auto_sn_placeholder(current_sn):
        return current_sn, dirs

    expected_sn = "" if is_auto_sn_placeholder(current_sn) else current_sn
    discovered_sn = _extract_full_sn_from_page(page, expected_sn)
    if not discovered_sn:
        try:
            discovered_sn = capture_flow.extract_full_sn_from_settings(page, selectors, expected_sn)
        except Exception as exc:
            logger.warning(f"Could not resolve full SN from Control Panel settings: {exc}")

    return _upgrade_task_sn(
        current_sn=current_sn,
        discovered_sn=discovered_sn,
        output_root=output_root,
        dirs=dirs,
        progress=progress,
        report=report,
        setup_file_log=setup_file_log,
    )


def _extract_full_sn_from_page(page, sn: str) -> str | None:
    expected_sn = "" if is_auto_sn_placeholder(sn) else sn
    if not expected_sn:
        return None
    parts: list[str] = []
    try:
        parts.append(page.title())
    except Exception:
        pass
    try:
        parts.append(page.locator("body").inner_text(timeout=2_000))
    except Exception:
        pass
    # AUTO placeholders have no real tail to validate against, so avoid cached
    # or hidden browser state that can belong to a previously opened NAS.
    if expected_sn:
        try:
            parts.append(page.content())
        except Exception:
            pass
        try:
            parts.append(
                page.evaluate(
                    """() => JSON.stringify({
                        text: document.body ? document.body.innerText : "",
                        inputs: Array.from(document.querySelectorAll("input, textarea")).map((el) => el.value || ""),
                        labelled: Array.from(document.querySelectorAll("[title], [aria-label]")).map((el) => [
                            el.getAttribute("title") || "",
                            el.getAttribute("aria-label") || "",
                            el.textContent || "",
                        ]),
                        localStorage: {...localStorage},
                        sessionStorage: {...sessionStorage},
                    })"""
                )
            )
        except Exception:
            pass
    return extract_full_sn("\n".join(parts), expected_sn)


def _release_reserved_ips(ips: set[str]) -> None:
    if not ips:
        return
    with DISCOVERY_ALLOCATION_GUARD:
        ACTIVE_DEVICE_IPS.difference_update(ips)



def _device_lock_for_ip(ip: str) -> threading.Lock:
    with DEVICE_LOCKS_GUARD:
        lock = DEVICE_LOCKS.get(ip)
        if lock is None:
            lock = threading.Lock()
            DEVICE_LOCKS[ip] = lock
        return lock


def _find_todo_selectors(selectors: dict, prefix: str = "") -> list[str]:
    todos: list[str] = []
    for key, value in selectors.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            todos.extend(_find_todo_selectors(value, path))
        elif isinstance(value, str) and value == "TODO":
            todos.append(path)
    return todos


def _probe_selectors(page, selectors: dict, prefix: str = "") -> dict[str, bool]:
    results: dict[str, bool] = {}
    for key, value in selectors.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            results.update(_probe_selectors(page, value, path))
        elif path.endswith(".app"):
            continue
        elif isinstance(value, str) and value and value != "TODO":
            try:
                results[path] = page.locator(value).first.is_visible(timeout=2000)
            except Exception:
                results[path] = False
    return results


def _handle_capture_progress(
    progress: TaskProgress,
    event: dict,
    cancel_requested_cb: Callable[[], bool] | None = None,
) -> None:
    _raise_if_cancelled(cancel_requested_cb)
    event_type = event.get("type")
    page_key = str(event.get("page_key") or "")
    stage_name = f"截图：{_page_label(page_key)}" if page_key else "截图"

    if event_type == "capture_page_started":
        progress.set_stage(stage_name, status="running", page_key=page_key)
    elif event_type == "transfer_waiting":
        progress.set_stage(f"{_page_label(page_key)} 传输排队中", status="on_hold", page_key=page_key)
    elif event_type == "transfer_started":
        progress.set_stage(f"{_page_label(page_key)} 传输中", status="transfer", page_key=page_key)
    elif event_type == "transfer_finished":
        progress.set_stage(stage_name, status="running", page_key=page_key)
    elif event_type == "capture_page_completed":
        progress.complete_step(
            stage_name,
            status="running",
            page_key=page_key,
            screenshot=event.get("screenshot"),
        )
    elif event_type == "capture_page_failed":
        progress.set_stage(f"{_page_label(page_key)} 截图失败", status="running", page_key=page_key)


def _page_label(page_key: str) -> str:
    return PAGE_LABELS.get(page_key, page_key.replace("_", " ").title())


def _emit_browser_event(progress_cb: Callable[[dict], None] | None, event_type: str, **payload: object) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb({"type": event_type, **payload})
    except Exception:
        pass


@click.group()
def cli() -> None:
    pass


@cli.command()
@click.option("--sn", required=True, help="Serial number from barcode scanner")
@click.option("--nas-ip", default="auto", help="NAS IP, or 'auto' to discover via mDNS/port scan")
@click.option("--mode", type=click.Choice(["setup", "login"]), default="setup")
@click.option("--cleanup-before-finish/--no-cleanup-before-finish", default=False, help="Delete storage pools before finishing")
@click.option(
    "--factory-reset-before-finish/--no-factory-reset-before-finish",
    default=False,
    help="Restore factory settings before finishing",
)
def test(
    sn: str,
    nas_ip: str,
    mode: str,
    cleanup_before_finish: bool,
    factory_reset_before_finish: bool,
) -> None:
    run_test(
        sn,
        nas_ip,
        mode,
        cleanup_before_finish=cleanup_before_finish,
        factory_reset_before_finish=factory_reset_before_finish,
    )


@cli.command()
@click.option("--sn", required=True, help="Serial number from barcode scanner")
@click.option("--nas-ip", default="auto", help="NAS IP, or 'auto' to discover via mDNS/port scan")
def cleanup(sn: str, nas_ip: str) -> None:
    run_cleanup(sn, nas_ip)


@cli.command()
@click.option("--nas-ip", required=True, help="NAS IP to probe")
def smoke(nas_ip: str) -> None:
    run_smoke(nas_ip)


def _load_label_config() -> dict:
    """Read `label_printer` section from config.yml, returning {} on any error."""
    try:
        config, _ = load_configs(PROJECT_ROOT)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"_load_label_config: config load failed ({exc}); using defaults")
        return {}
    section = config.get("label_printer") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


@cli.command("print-label")
@click.option("--sn", help="Serial number to encode (Code 128).")
@click.option(
    "--printer",
    default=None,
    help="Windows printer queue name. If omitted, auto-detect a Zebra-like queue.",
)
@click.option(
    "--preview",
    "preview_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Also render a PNG preview to this path (cross-platform, no printer needed).",
)
@click.option(
    "--zpl-out",
    "zpl_out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write raw ZPL to this file (for hotfolder workflows or inspection).",
)
@click.option(
    "--dpi",
    type=click.IntRange(150, 600),
    default=label_util.DEFAULT_DPI,
    show_default=True,
    help="Printer dpi (203 for Zebra ZD220/ZD420, 300 for ZD420-300dpi etc).",
)
@click.option(
    "--quantity",
    "-n",
    type=click.IntRange(1, 50),
    default=1,
    show_default=True,
    help="Number of copies to print.",
)
@click.option(
    "--no-print",
    is_flag=True,
    default=False,
    help="Skip sending to printer (use with --preview / --zpl-out for dry runs).",
)
@click.option(
    "--list-printers",
    is_flag=True,
    default=False,
    help="Print all installed Windows printer queues then exit.",
)
def print_label(
    sn: str | None,
    printer: str | None,
    preview_path: Path | None,
    zpl_out_path: Path | None,
    dpi: int,
    quantity: int,
    no_print: bool,
    list_printers: bool,
) -> None:
    cfg = _load_label_config()
    if printer is None and cfg.get("name"):
        printer = str(cfg["name"])
    if dpi == label_util.DEFAULT_DPI and cfg.get("dpi"):
        try:
            dpi = int(cfg["dpi"])
        except (TypeError, ValueError):
            pass
    if quantity == 1 and cfg.get("quantity"):
        try:
            quantity = max(1, int(cfg["quantity"]))
        except (TypeError, ValueError):
            pass
    """Generate and print a Code 128 SN label (42×25mm, 模版二 spec).

    Examples:

    \b
        # Manual: print one label
        run-cli print-label --sn HB12345ABCD
        # Dry run with PNG preview (works on macOS/Linux too)
        run-cli print-label --sn HB12345ABCD --preview /tmp/label.png --no-print
        # Inspect ZPL bytes
        run-cli print-label --sn HB12345ABCD --zpl-out /tmp/label.zpl --no-print
    """
    if list_printers:
        printers = label_util.list_windows_printers()
        if not printers:
            click.echo("(no printers found — non-Windows host or pywin32 missing)")
        else:
            for p in printers:
                click.echo(f"{p['name']}\tdriver={p['driver']}\tport={p['port']}")
        return

    if not sn:
        raise click.UsageError("--sn is required (unless using --list-printers)")

    normalized = normalize_sn(sn)
    if not normalized:
        raise click.UsageError(f"SN normalizes to empty: {sn!r}")
    if is_auto_sn_placeholder(normalized):
        raise click.UsageError(f"Refusing to print AUTO placeholder SN: {normalized}")

    zpl = label_util.build_zpl(normalized, dpi=dpi, quantity=quantity)

    if zpl_out_path is not None:
        out = label_util.write_zpl_file(zpl_out_path, zpl)
        click.echo(f"ZPL written: {out}")

    if preview_path is not None:
        try:
            out = label_util.render_preview_png(normalized, preview_path)
            click.echo(f"Preview PNG written: {out}")
        except ImportError as exc:
            raise click.ClickException(str(exc))

    if no_print:
        click.echo("(--no-print set; skipped sending to printer)")
        return

    if not label_util.is_windows():
        click.echo(
            "Non-Windows host: cannot send to printer queue directly. "
            "Use --preview / --zpl-out to inspect, or run on the Windows factory machine."
        )
        return

    target = label_util.find_label_printer(printer)
    if not target:
        hints = "\n  ".join(p["name"] for p in label_util.list_windows_printers()) or "(none)"
        raise click.ClickException(
            "Could not auto-detect a Zebra printer. "
            "Pass --printer explicitly. Installed queues:\n  " + hints
        )
    label_util.send_to_windows_printer(target, zpl, job_name=f"SN {normalized}")
    click.echo(f"Sent {quantity} label(s) for SN={normalized} to '{target}'")


def _load_nameplate_config() -> dict:
    try:
        config, _ = load_configs(PROJECT_ROOT)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"_load_nameplate_config: config load failed ({exc}); using defaults")
        return {}
    section = config.get("nameplate_printer") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


@cli.command("print-nameplate")
@click.option("--sn", required=True, help="Serial number (drives barcode + QR + text).")
@click.option(
    "--pn",
    default=None,
    help="Part number override. If omitted, looked up from (SN-derived model, --grade) "
    "via lookup_pn(); falls back to nameplate_printer.placeholder_pn from config.",
)
@click.option(
    "--grade",
    type=click.Choice(["A", "B"], case_sensitive=False),
    default=None,
    help="Refurb grade — drives P/N lookup. A = L0 refurb, B = L1.",
)
@click.option(
    "--printer",
    default=None,
    help="Windows printer queue. Default reads nameplate_printer.name from config.",
)
@click.option(
    "--dpi",
    type=click.IntRange(150, 1200),
    default=None,
    help="Printer dpi (ZT610 = 600). Default reads nameplate_printer.dpi.",
)
@click.option(
    "--quantity",
    "-n",
    type=click.IntRange(1, 20),
    default=None,
    help="Copies. Default reads nameplate_printer.quantity (=1 unless overridden).",
)
@click.option(
    "--qr-x",
    type=float,
    default=None,
    help="Override QR top-left x in mm (for layout iteration).",
)
@click.option(
    "--qr-y",
    type=float,
    default=None,
    help="Override QR top-left y in mm.",
)
@click.option(
    "--strip-x",
    type=float,
    default=None,
    help="Override barcode strip top-left x in mm.",
)
@click.option(
    "--strip-y",
    type=float,
    default=None,
    help="Override barcode strip top-left y in mm.",
)
@click.option(
    "--zpl-out",
    "zpl_out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write raw ZPL to this file (skip printer send).",
)
@click.option(
    "--no-print",
    is_flag=True,
    default=False,
    help="Skip sending to printer (use with --zpl-out for dry runs).",
)
def print_nameplate(
    sn: str,
    pn: str | None,
    grade: str | None,
    printer: str | None,
    dpi: int | None,
    quantity: int | None,
    qr_x: float | None,
    qr_y: float | None,
    strip_x: float | None,
    strip_y: float | None,
    zpl_out_path: Path | None,
    no_print: bool,
) -> None:
    """Print the 模版一 nameplate label (83.7×36.7mm) on the ZT610.

    The nameplate stock is pre-printed by the supplier; we only fill the
    QR (20×20mm) and the barcode+text strip (38×6mm) over the reserved
    white regions.

    Layout iteration:

    \b
        # first cut with config defaults
        run-cli print-nameplate --sn HB670EE00000001A
        # nudge QR 2mm right, strip 1mm down for layout testing
        run-cli print-nameplate --sn HB670EE00000001A --qr-x 62 --strip-y 28
    """
    cfg = _load_nameplate_config()
    if printer is None and cfg.get("name"):
        printer = str(cfg["name"])
    if dpi is None:
        try:
            dpi = int(cfg.get("dpi") or label_util.NAMEPLATE_DPI)
        except (TypeError, ValueError):
            dpi = label_util.NAMEPLATE_DPI
    if quantity is None:
        try:
            quantity = max(1, int(cfg.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
    if pn is None:
        model_key = model_key_from_sn(sn)
        pn = label_util.lookup_pn(model_key, grade)
        if not pn:
            pn = str(cfg.get("placeholder_pn") or "").strip()
        if not pn:
            raise click.UsageError(
                "Could not resolve P/N. Pass --pn explicitly, or pass --grade with a SN "
                "whose model is in lookup_pn(), or set nameplate_printer.placeholder_pn."
            )

    qr_xy = list(cfg.get("qr_top_left_mm") or label_util.NAMEPLATE_QR_TOP_LEFT_MM)
    strip_xy = list(cfg.get("strip_top_left_mm") or label_util.NAMEPLATE_STRIP_TOP_LEFT_MM)
    if qr_x is not None:
        qr_xy[0] = qr_x
    if qr_y is not None:
        qr_xy[1] = qr_y
    if strip_x is not None:
        strip_xy[0] = strip_x
    if strip_y is not None:
        strip_xy[1] = strip_y

    normalized = normalize_sn(sn)
    if not normalized:
        raise click.UsageError(f"SN normalizes to empty: {sn!r}")
    if is_auto_sn_placeholder(normalized):
        raise click.UsageError(f"Refusing to print AUTO placeholder SN: {normalized}")

    try:
        zpl = label_util.build_nameplate_zpl(
            normalized,
            pn,
            dpi=dpi,
            quantity=quantity,
            qr_top_left_mm=(float(qr_xy[0]), float(qr_xy[1])),
            strip_top_left_mm=(float(strip_xy[0]), float(strip_xy[1])),
        )
    except ValueError as exc:
        raise click.ClickException(str(exc))

    if zpl_out_path is not None:
        out = label_util.write_zpl_file(zpl_out_path, zpl)
        click.echo(f"ZPL written: {out}")

    if no_print:
        click.echo("(--no-print set; skipped sending to printer)")
        return

    if not label_util.is_windows():
        click.echo(
            "Non-Windows host: cannot send to printer queue directly. "
            "Use --zpl-out to inspect, or run on the Windows factory machine."
        )
        return

    target = label_util.find_label_printer(printer)
    if not target:
        hints = "\n  ".join(p["name"] for p in label_util.list_windows_printers()) or "(none)"
        raise click.ClickException(
            "Could not auto-detect a Zebra printer. "
            "Pass --printer explicitly. Installed queues:\n  " + hints
        )
    label_util.send_to_windows_printer(target, zpl, job_name=f"Nameplate {normalized}")
    click.echo(
        f"Sent {quantity} nameplate(s) for SN={normalized} P/N={pn} to '{target}' "
        f"(QR@{qr_xy[0]},{qr_xy[1]} strip@{strip_xy[0]},{strip_xy[1]})"
    )


def _load_ean13_config() -> dict:
    try:
        config, _ = load_configs(PROJECT_ROOT)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"_load_ean13_config: config load failed ({exc}); using defaults")
        return {}
    section = config.get("ean13_printer") if isinstance(config, dict) else None
    return section if isinstance(section, dict) else {}


@cli.command("print-ean13")
@click.option("--sn", help="Serial number — drives model lookup via SN prefix.")
@click.option(
    "--grade",
    type=click.Choice(["A", "B"], case_sensitive=False),
    default=None,
    help="Refurb grade — drives P/N and EAN-13 lookup.",
)
@click.option(
    "--model",
    default=None,
    help="Override model key (2800 / 4800 / 4800Plus). Default derived from --sn.",
)
@click.option(
    "--pn",
    default=None,
    help="Override P/N. Default looked up from (model, grade) via lookup_pn().",
)
@click.option(
    "--ean13",
    default=None,
    help="Override EAN-13 (12 or 13 digits). Default looked up via lookup_ean13().",
)
@click.option(
    "--printer",
    default=None,
    help="Windows printer queue. Default reads ean13_printer.name from config.",
)
@click.option(
    "--dpi",
    type=click.IntRange(150, 600),
    default=None,
    help="Printer dpi. Default reads ean13_printer.dpi (203).",
)
@click.option(
    "--quantity",
    "-n",
    type=click.IntRange(1, 20),
    default=None,
    help="Copies. Default reads ean13_printer.quantity (=2 per spec 2PCS).",
)
@click.option(
    "--zpl-out",
    "zpl_out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write raw ZPL to this file (skip printer send).",
)
@click.option(
    "--no-print",
    is_flag=True,
    default=False,
    help="Skip sending to printer (use with --zpl-out for dry runs).",
)
def print_ean13(
    sn: str | None,
    grade: str | None,
    model: str | None,
    pn: str | None,
    ean13: str | None,
    printer: str | None,
    dpi: int | None,
    quantity: int | None,
    zpl_out_path: Path | None,
    no_print: bool,
) -> None:
    """Print the 模版五 EAN-13 (69 码) carton + middle-box label.

    Resolves model from SN prefix (HB→2800 / EC671→4800 / EC752→4800Plus),
    then P/N via lookup_pn and EAN-13 via lookup_ean13. Each can be overridden
    explicitly. Output is 50×30mm at 203 dpi, sent to the Deli DL-888T (or
    whichever queue is configured under ``ean13_printer.name``).
    """
    cfg = _load_ean13_config()
    if printer is None and cfg.get("name"):
        printer = str(cfg["name"])
    if dpi is None:
        try:
            dpi = int(cfg.get("dpi") or label_util.EAN13_DPI)
        except (TypeError, ValueError):
            dpi = label_util.EAN13_DPI
    if quantity is None:
        try:
            quantity = max(1, int(cfg.get("quantity") or 2))
        except (TypeError, ValueError):
            quantity = 2

    if not model and sn:
        model = model_key_from_sn(sn)
    if not model:
        raise click.UsageError("Could not resolve model — pass --sn or --model.")
    if not pn:
        pn = label_util.lookup_pn(model, grade)
    if not pn:
        raise click.UsageError(
            f"Could not resolve P/N for model={model!r} grade={grade!r}. "
            "Pass --pn explicitly or specify --grade with a known SN."
        )
    if not ean13:
        ean13 = label_util.lookup_ean13(model, grade)
    if not ean13:
        raise click.UsageError(
            f"Could not resolve EAN-13 for model={model!r} grade={grade!r}. "
            "Pass --ean13 explicitly — the factory hasn't supplied this SKU yet."
        )

    try:
        data = label_util.build_ean13_tspl(model, pn, ean13, dpi=dpi, quantity=quantity)
    except ValueError as exc:
        raise click.ClickException(str(exc))

    if zpl_out_path is not None:
        out = label_util.write_zpl_file(zpl_out_path, data)
        click.echo(f"Label data written: {out}")

    if no_print:
        click.echo("(--no-print set; skipped sending to printer)")
        return

    if not label_util.is_windows():
        click.echo(
            "Non-Windows host: cannot send to printer queue directly. "
            "Use --zpl-out to inspect, or run on the Windows factory machine."
        )
        return

    target = label_util.find_label_printer(printer)
    if not target:
        hints = "\n  ".join(p["name"] for p in label_util.list_windows_printers()) or "(none)"
        raise click.ClickException(
            f"Printer '{printer}' not found (strict match). Installed queues:\n  " + hints
        )
    label_util.send_to_windows_printer(target, data, job_name=f"EAN-13 {ean13}")
    click.echo(
        f"Sent {quantity} EAN-13 label(s) for {model}/{grade} (P/N={pn}, EAN={ean13}) to '{target}'"
    )


def main() -> None:
    try:
        cli()
    except Exception as exc:
        logger.error(f"Fatal: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
