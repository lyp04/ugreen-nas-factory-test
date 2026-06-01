from __future__ import annotations

import os
import ipaddress
import json
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from tkinter import messagebox, scrolledtext, ttk

from loguru import logger

FORM_ENTRY_ENABLED = True

if __package__ in {None, ""}:
    package_root = Path(__file__).resolve().parent.parent
    if str(package_root) not in sys.path:
        sys.path.insert(0, str(package_root))
    from src import form_entry
    from src.cli import (
        UNFLASHED_MESSAGE,
        UNFLASHED_TITLE,
        failure_stage_for_error,
        get_project_root,
        is_pool_creation_timeout_error,
        is_unflashed_password_error,
        run_test,
    )
    from src.discovery import ugreen_broadcast
    from src.discovery.discover import find_nas_candidates
    from src.updater import UpdateInfo, UpdateManager, format_version
    from src.utils.browser_control import show_browser_windows, terminate_browser_process
    from src.utils.config_loader import load_configs
    from src.utils import label as label_util
    from src.utils.logger import remove_default_sinks
    from src.utils.sn import (
        is_auto_sn_placeholder,
        is_full_sn_candidate,
        model_key_from_sn,
        normalize_sn,
        same_sn_identity,
        sn_tail,
    )
else:
    from . import form_entry
    from .cli import (
        UNFLASHED_MESSAGE,
        UNFLASHED_TITLE,
        failure_stage_for_error,
        get_project_root,
        is_pool_creation_timeout_error,
        is_unflashed_password_error,
        run_test,
    )
    from .discovery import ugreen_broadcast
    from .discovery.discover import find_nas_candidates
    from .updater import UpdateInfo, UpdateManager, format_version
    from .utils.browser_control import show_browser_windows, terminate_browser_process
    from .utils.config_loader import load_configs
    from .utils import label as label_util
    from .utils.logger import remove_default_sinks
    from .utils.sn import (
        is_auto_sn_placeholder,
        is_full_sn_candidate,
        model_key_from_sn,
        normalize_sn,
        same_sn_identity,
        sn_tail,
    )


@dataclass(slots=True)
class DeviceTask:
    task_id: str
    sn: str
    requested_ip: str
    mode: str
    cleanup_before_finish: bool
    factory_reset_before_finish: bool
    auto_form_entry: bool = False
    auto_seed_previous_step: bool = False
    form_model: str = ""
    form_grade: str = "A"
    form_account_name: str = ""
    actual_ip: str = ""
    browser_pid: int | None = None
    state_code: str = "queued"
    status: str = "排队中"
    progress: int = 0
    current_step: str = "等待启动"
    logs: list[str] = field(default_factory=list)
    shown_alerts: set[str] = field(default_factory=set)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    reserved_ips: set[str] = field(default_factory=set)
    network_interface: str = ""
    attempt: int = 0
    max_attempts: int = 2
    started_monotonic: float = field(default_factory=time.monotonic)
    finished_monotonic: float | None = None
    queue_added_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    interrupted_on_exit: bool = False

    @property
    def display_ip(self) -> str:
        return self.actual_ip or self.requested_ip

    @property
    def elapsed_seconds(self) -> int:
        end = self.finished_monotonic if self.finished_monotonic is not None else time.monotonic()
        return max(0, int(end - self.started_monotonic))

    @property
    def elapsed_display(self) -> str:
        return format_elapsed(self.elapsed_seconds)


@dataclass(slots=True)
class TimingSlice:
    label: str
    seconds: int


LOG_TIME_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\s+\|")
TIMING_COLORS = (
    "#4e79a7",
    "#f28e2b",
    "#e15759",
    "#76b7b2",
    "#59a14f",
    "#edc948",
    "#b07aa1",
    "#ff9da7",
    "#9c755f",
    "#bab0ab",
)
TIMING_MIN_VISIBLE_SECONDS = 20
TIMING_DAY_ROLLOVER_THRESHOLD_SECONDS = 12 * 3600


def format_elapsed(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def build_timing_slices(logs: list[str]) -> list[TimingSlice]:
    entries: list[tuple[int, str]] = []
    day_offset = 0
    previous_second: int | None = None
    for raw in logs:
        for line in str(raw or "").splitlines():
            match = LOG_TIME_RE.match(line)
            if not match:
                continue
            second = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
            absolute_second = second + day_offset
            if previous_second is not None and absolute_second < previous_second:
                if previous_second - absolute_second > TIMING_DAY_ROLLOVER_THRESHOLD_SECONDS:
                    day_offset += 24 * 3600
                    absolute_second = second + day_offset
                else:
                    absolute_second = previous_second
            previous_second = absolute_second
            entries.append((absolute_second, _log_message_text(line)))

    if len(entries) < 2:
        return []

    durations: dict[str, int] = {}
    current = "准备"
    update_index = 0
    active_update = False
    for index, (second, message) in enumerate(entries[:-1]):
        current, update_index, active_update = _timing_phase_after_message(
            message,
            current,
            update_index,
            active_update,
        )
        duration = max(0, entries[index + 1][0] - second)
        if duration:
            durations[current] = durations.get(current, 0) + duration

    return [TimingSlice(label, seconds) for label, seconds in durations.items() if seconds > 0]


def _compact_timing_slices(
    slices: list[TimingSlice],
    limit: int = 7,
    min_visible_seconds: int = TIMING_MIN_VISIBLE_SECONDS,
) -> list[TimingSlice]:
    visible = sorted(
        (item for item in slices if item.seconds >= min_visible_seconds),
        key=lambda item: item.seconds,
        reverse=True,
    )
    other_seconds = sum(item.seconds for item in slices if item.seconds < min_visible_seconds)
    if len(visible) > limit - 1 and other_seconds > 0:
        cutoff = limit - 1
    else:
        cutoff = limit
    if len(visible) > cutoff:
        other_seconds += sum(item.seconds for item in visible[cutoff:])
        visible = visible[:cutoff]
    if other_seconds > 0:
        visible.append(TimingSlice("其他", other_seconds))
    return visible


def _log_message_text(line: str) -> str:
    parts = line.split("|", 2)
    return parts[2].strip() if len(parts) == 3 else line.strip()


def _timing_phase_after_message(
    message: str,
    current: str,
    update_index: int,
    active_update: bool,
) -> tuple[str, int, bool]:
    if "Setup wizard ->" in message or "[Page " in message or "Setup page reports" in message:
        return "首次设置", update_index, False
    if "Setup wizard complete" in message or "Setup mode fallback" in message:
        return "准备更新", update_index, False
    if "Logging in at" in message and not active_update:
        return "登录", update_index, False
    if "Login succeeded" in message and not active_update:
        return "准备更新", update_index, False

    if (
        "System update:" in message
        or "System update is ready" in message
        or "System update flow finished" in message
    ):
        starts_update = any(
            marker in message
            for marker in (
                "existing update/reboot screen detected",
                "clicked; waiting for desktop to return",
                "continuing update via",
                "update notice is visible; continuing update",
                "installation page is visible",
                "clicked after download",
                "waiting for installation/reboot to start",
                "page disconnected/reloading after confirmation",
            )
        )
        if starts_update and not active_update:
            update_index += 1
            active_update = True
        if active_update:
            current = f"更新{update_index}"
        elif "download/update is still in progress" in message or "download started" in message:
            current = "下载更新"
        else:
            current = "检查更新"

        if any(
            marker in message
            for marker in (
                "verifying latest status after update/reboot",
                "update page now reports latest version",
                "already latest",
                "System update flow finished",
            )
        ):
            active_update = False
            if "latest version" in message or "System update flow finished" in message:
                current = "准备建池"
            else:
                current = "检查更新"
        return current, update_index, active_update

    if message.startswith("Provisioning:"):
        return "建池共享", update_index, False
    if any(
        marker in message
        for marker in (
            "Capturing ",
            "Captured ",
            "Skipping existing ",
            "hdd_write:",
            "hdd_read:",
            "ssd_write:",
            "ssd_read:",
            "transfer speed attempt",
        )
    ):
        return "截图传输", update_index, False
    if message.startswith("自动录表") or message.lower().startswith("form entry"):
        return "自动录表", update_index, False
    if "cleanup" in message.lower() or "factory reset" in message.lower() or "恢复出厂" in message:
        return "收尾清理", update_index, False
    if "次测试失败" in message or "retry" in message.lower():
        return "重试等待", update_index, False
    return current, update_index, active_update


def sort_ip_strings(ips) -> list[str]:
    def key(ip: str):
        try:
            return (0, ipaddress.ip_address(ip))
        except ValueError:
            return (1, ip)

    return sorted(dict.fromkeys(str(ip).strip() for ip in ips if ip and str(ip).strip()), key=key)


LANGUAGE_CODES_BY_LABEL = {"中文": "zh", "English": "en", "Español (México)": "es-MX"}
LANGUAGE_OPTIONS = tuple(LANGUAGE_CODES_BY_LABEL)

UI_TEXT = {
    "zh": {
        "app_title": "UGREEN NAS 出厂测试",
        "ready": "就绪",
        "queue_summary_empty": "连接设备：0 台",
        "select_device_log_sentence": "选择左侧设备查看右侧日志。",
        "no_logs": "该设备当前还没有日志。",
        "test_params": "测试参数",
        "timing_analysis": "耗时分析",
        "sn_label": "SN/后4位:",
        "nas_ip_label": "NAS IP:",
        "or_label": "or",
        "auto_scan": "自动入队",
        "finish_actions": "结束前操作:",
        "cleanup_pools": "清理存储池",
        "factory_reset": "恢复出厂设置",
        "auto_form_entry": "自动录表",
        "auto_print_label": "自动打印标签",
        "grade_prompt_title": "请选择等级",
        "grade_prompt_body": "检测到设备就绪，需要选择测试等级才能继续：",
        "grade_a_button": "A 类（L0 翻新）",
        "grade_b_button": "B 类（L1 翻新）",
        "form_settings": "录表配置:",
        "model_auto": "机型按 SN 自动识别",
        "grade": "等级",
        "account": "账号",
        "switch_account": "切换账号",
        "add_account": "添加账号",
        "delete_account": "删除账号",
        "auto_seed_previous": "缺第一步时自动补录",
        "add_to_queue": "添加到队列",
        "print_label": "打印标签",
        "label_nameplate": "铭牌标签",
        "label_sn": "SN 标签",
        "label_ean13": "69 码",
        "label_shipping": "船运标签",
        "label_carton": "周转箱标签",
        "print_chooser_title": "选择要打印的标签",
        "print_chooser_for": "将为 {count} 个 SN 打印：",
        "cancel": "取消",
        "open_screenshot_dir": "打开截图目录",
        "remove_finished": "移除本台",
        "show_browser": "显示浏览器",
        "cancel_task": "中断任务",
        "retry_task": "重试任务",
        "connected_devices": "连接设备",
        "tree_status": "状态",
        "tree_elapsed": "运行时间",
        "tree_progress": "进度",
        "tree_step": "当前步骤",
        "sound": "提示音",
        "success_count": "今日成功 {count} 台",
        "failed_count": "今日失败 {count} 台",
        "language_zh": "中文",
        "language_en": "English",
        "language_es_mx": "Español (México)",
        "auto_form_off": "自动录表未开启，本次不会提交表单。",
        "auto_scan_waiting_form_grade": "请选择 A/B，或关闭自动录表后再自动入队。",
        "auto_scan_waiting_form_config": "自动入队等待录表配置完成。",
        "queue_summary": "连接设备：{total} 台，活跃 {active} 台",
        "status_summary": "运行中 {running} | On Hold {on_hold} | 重试 {retrying} | 排队 {queued} | 完成 {success} | 失败 {failed}",
        "tab_log": "日志",
        "tab_materials": "物料",
        "tab_measurements": "测试数据",
        "measure_select_device": "选择左侧设备查看测试数据。",
        "measure_no_data": "暂无测试数据，等待测试运行后再查看。",
        "measure_section_system": "系统",
        "measure_section_storage": "存储池",
        "measure_section_transfer": "传输测速",
        "measure_section_fan": "温度 / 风扇",
        "measure_ugos_version": "UGOS 版本",
        "measure_link_bps": "网口速率",
        "measure_hdd_pool": "HDD 存储池",
        "measure_ssd_pool": "SSD 存储池",
        "measure_hdd_write": "HDD 写入",
        "measure_hdd_read": "HDD 读取",
        "measure_ssd_write": "SSD 写入",
        "measure_ssd_read": "SSD 读取",
        "measure_fan_normal": "普通模式",
        "measure_fan_silent": "安静模式",
        "measure_fan_full": "全速模式",
        "measure_threshold": "阈值",
        "materials_select_device": "选择左侧设备查看录表物料。",
        "materials_no_form": "本台设备未启用自动录表。",
        "materials_no_report": "该设备还未提交录表。",
        "materials_no_config": "录表配置缺少该机型物料信息。",
        "materials_group_count": "已选 {selected} / 候选 {total}",
        "materials_group_disabled": "未启用",
        "materials_status_selected": "已选",
        "materials_status_unselected": "未选",
        "materials_status_missing": "缺料",
        "materials_status_pending": "待录入",
        "materials_status_group_disabled": "未启用",
        "materials_submission_pending": "录表未完成",
        "materials_summary_missing": "缺料 {count} 项",
        "materials_mode_fresh_full_retry": "每次重取物料，全选后反选缺料",
        "materials_col_name": "名称",
        "materials_col_code": "编码",
        "materials_col_qty": "数量",
        "materials_col_status": "状态",
        "update_available_title": "发现新版本",
        "update_available_body": "当前版本: {current}\n最新版本: {latest}",
        "update_install_button": "下载并安装",
        "update_later_button": "稍后",
        "update_progress_title": "更新进度",
        "update_failed_title": "更新失败",
        "update_installing_title": "更新已就绪",
        "update_installing_body": "新版本 {version} 已下载并校验，点击确定后应用将关闭。请重新启动应用进入新版本。",
    },
    "en": {
        "app_title": "UGREEN NAS Factory Test",
        "ready": "Ready",
        "queue_summary_empty": "Connected devices: 0",
        "select_device_log_sentence": "Select a device on the left to view logs.",
        "no_logs": "This device has no logs yet.",
        "test_params": "Test Parameters",
        "timing_analysis": "Timing",
        "sn_label": "SN / Last 4:",
        "nas_ip_label": "NAS IP:",
        "or_label": "or",
        "auto_scan": "Auto queue",
        "finish_actions": "Finish actions:",
        "cleanup_pools": "Clean storage pools",
        "factory_reset": "Factory reset",
        "auto_form_entry": "Auto form entry",
        "auto_print_label": "Auto print label",
        "grade_prompt_title": "Choose grade",
        "grade_prompt_body": "A device is ready for test. Select a grade to continue:",
        "grade_a_button": "Grade A (L0 refurb)",
        "grade_b_button": "Grade B (L1 refurb)",
        "form_settings": "Form settings:",
        "model_auto": "Model auto-detected by SN",
        "grade": "Grade",
        "account": "Account",
        "switch_account": "Switch account",
        "add_account": "Add account",
        "delete_account": "Delete account",
        "auto_seed_previous": "Auto-fill step 1 if missing",
        "add_to_queue": "Add to queue",
        "print_label": "Print label",
        "label_nameplate": "Nameplate",
        "label_sn": "SN sticker",
        "label_ean13": "EAN-13 (69)",
        "label_shipping": "Shipping label",
        "label_carton": "Turnover-box label",
        "print_chooser_title": "Choose label to print",
        "print_chooser_for": "Printing for {count} SN(s):",
        "cancel": "Cancel",
        "open_screenshot_dir": "Open screenshot folder",
        "remove_finished": "Remove this device",
        "show_browser": "Show browser",
        "cancel_task": "Cancel task",
        "retry_task": "Retry task",
        "connected_devices": "Connected Devices",
        "tree_status": "Status",
        "tree_elapsed": "Elapsed",
        "tree_progress": "Progress",
        "tree_step": "Current step",
        "sound": "Sound",
        "success_count": "Today done {count}",
        "failed_count": "Today failed {count}",
        "language_zh": "中文",
        "language_en": "English",
        "language_es_mx": "Español (México)",
        "auto_form_off": "Auto form entry is off; this run will not submit the form.",
        "auto_scan_waiting_form_grade": "Select A/B, or turn off auto form entry before auto queue starts.",
        "auto_scan_waiting_form_config": "Auto queue is waiting for form settings.",
        "queue_summary": "Connected devices: {total}, active {active}",
        "status_summary": "Running {running} | On Hold {on_hold} | Retrying {retrying} | Queued {queued} | Done {success} | Failed {failed}",
        "tab_log": "Log",
        "tab_materials": "Materials",
        "tab_measurements": "Measurements",
        "measure_select_device": "Select a device on the left to view its measurements.",
        "measure_no_data": "No measurements captured yet for this device.",
        "measure_section_system": "System",
        "measure_section_storage": "Storage pools",
        "measure_section_transfer": "Transfer speed",
        "measure_section_fan": "Temperature / fan",
        "measure_ugos_version": "UGOS version",
        "measure_link_bps": "Link speed",
        "measure_hdd_pool": "HDD pool",
        "measure_ssd_pool": "SSD pool",
        "measure_hdd_write": "HDD write",
        "measure_hdd_read": "HDD read",
        "measure_ssd_write": "SSD write",
        "measure_ssd_read": "SSD read",
        "measure_fan_normal": "Normal mode",
        "measure_fan_silent": "Silent mode",
        "measure_fan_full": "Full-speed mode",
        "measure_threshold": "threshold",
        "materials_select_device": "Select a device on the left to view submitted materials.",
        "materials_no_form": "Auto form entry was not enabled for this device.",
        "materials_no_report": "Form has not been submitted yet.",
        "materials_no_config": "No material config found for this model.",
        "materials_group_count": "{selected} kept / {total} candidates",
        "materials_group_disabled": "Not enabled",
        "materials_status_selected": "Kept",
        "materials_status_unselected": "Skipped",
        "materials_status_missing": "Out of stock",
        "materials_status_pending": "Pending",
        "materials_status_group_disabled": "Not enabled",
        "materials_submission_pending": "Submission incomplete",
        "materials_summary_missing": "{count} out of stock",
        "materials_mode_fresh_full_retry": "Fresh material list, submit all then remove out-of-stock items",
        "materials_col_name": "Name",
        "materials_col_code": "Code",
        "materials_col_qty": "Qty",
        "materials_col_status": "Status",
        "update_available_title": "New version available",
        "update_available_body": "Current: {current}\nLatest:  {latest}",
        "update_install_button": "Download and install",
        "update_later_button": "Later",
        "update_progress_title": "Update progress",
        "update_failed_title": "Update failed",
        "update_installing_title": "Update ready",
        "update_installing_body": "Version {version} downloaded and verified. The app will close — please re-launch it to enter the new version.",
    },
    "es-MX": {
        "app_title": "Prueba de fábrica UGREEN NAS",
        "ready": "Listo",
        "queue_summary_empty": "Dispositivos conectados: 0",
        "select_device_log_sentence": "Selecciona un equipo a la izquierda para ver la bitácora.",
        "no_logs": "Este dispositivo aún no tiene registros.",
        "test_params": "Parámetros de prueba",
        "timing_analysis": "Tiempos",
        "sn_label": "SN / últimos 4:",
        "nas_ip_label": "IP del NAS:",
        "or_label": "o",
        "auto_scan": "Agregar automáticamente",
        "finish_actions": "Acciones finales:",
        "cleanup_pools": "Limpiar pools de almacenamiento",
        "factory_reset": "Restablecer de fábrica",
        "auto_form_entry": "Captura automática",
        "auto_print_label": "Imprimir etiqueta auto",
        "grade_prompt_title": "Elegir grado",
        "grade_prompt_body": "Dispositivo listo para pruebas. Elige el grado para continuar:",
        "grade_a_button": "Grado A (refurb L0)",
        "grade_b_button": "Grado B (refurb L1)",
        "form_settings": "Configuración de captura:",
        "model_auto": "Modelo detectado por SN",
        "grade": "Grado",
        "account": "Cuenta",
        "switch_account": "Cambiar cuenta",
        "add_account": "Añadir cuenta",
        "delete_account": "Eliminar cuenta",
        "auto_seed_previous": "Rellenar paso 1 si falta",
        "add_to_queue": "Añadir a la cola",
        "print_label": "Imprimir etiqueta",
        "label_nameplate": "Placa",
        "label_sn": "Etiqueta SN",
        "label_ean13": "EAN-13 (69)",
        "label_shipping": "Etiqueta de envío",
        "label_carton": "Etiqueta de caja",
        "print_chooser_title": "Elegir etiqueta a imprimir",
        "print_chooser_for": "Imprimiendo para {count} SN(s):",
        "cancel": "Cancelar",
        "open_screenshot_dir": "Abrir carpeta de capturas",
        "remove_finished": "Quitar este equipo",
        "show_browser": "Mostrar navegador",
        "cancel_task": "Cancelar tarea",
        "retry_task": "Reintentar",
        "connected_devices": "Dispositivos conectados",
        "tree_status": "Estado",
        "tree_elapsed": "Tiempo",
        "tree_progress": "Progreso",
        "tree_step": "Paso actual",
        "sound": "Sonido",
        "success_count": "Hoy completadas {count}",
        "failed_count": "Hoy fallidas {count}",
        "language_zh": "中文",
        "language_en": "English",
        "language_es_mx": "Español (México)",
        "auto_form_off": "La captura automática está desactivada; esta ejecución no enviará el formulario.",
        "auto_scan_waiting_form_grade": "Selecciona A/B, o desactiva la captura automática para iniciar la cola.",
        "auto_scan_waiting_form_config": "La cola automática espera la configuración de captura.",
        "queue_summary": "Dispositivos conectados: {total}, activos {active}",
        "status_summary": "En curso {running} | En espera {on_hold} | Reintentando {retrying} | En cola {queued} | Completadas {success} | Fallidas {failed}",
        "tab_log": "Bitácora",
        "tab_materials": "Materiales",
        "tab_measurements": "Mediciones",
        "measure_select_device": "Selecciona un equipo a la izquierda para ver sus mediciones.",
        "measure_no_data": "Aún no hay mediciones para este equipo.",
        "measure_section_system": "Sistema",
        "measure_section_storage": "Pools de almacenamiento",
        "measure_section_transfer": "Velocidad de transferencia",
        "measure_section_fan": "Temperatura / ventilador",
        "measure_ugos_version": "Versión UGOS",
        "measure_link_bps": "Velocidad de enlace",
        "measure_hdd_pool": "Pool HDD",
        "measure_ssd_pool": "Pool SSD",
        "measure_hdd_write": "Escritura HDD",
        "measure_hdd_read": "Lectura HDD",
        "measure_ssd_write": "Escritura SSD",
        "measure_ssd_read": "Lectura SSD",
        "measure_fan_normal": "Modo normal",
        "measure_fan_silent": "Modo silencioso",
        "measure_fan_full": "Velocidad máxima",
        "measure_threshold": "mínimo",
        "materials_select_device": "Selecciona un equipo a la izquierda para ver los materiales enviados.",
        "materials_no_form": "La captura automática no está activa para este equipo.",
        "materials_no_report": "El formulario aún no se ha enviado.",
        "materials_no_config": "No se encontró configuración de materiales para este modelo.",
        "materials_group_count": "{selected} de {total} candidatos",
        "materials_group_disabled": "No activado",
        "materials_status_selected": "Registrado",
        "materials_status_unselected": "Omitido",
        "materials_status_missing": "Sin stock",
        "materials_status_pending": "Pendiente",
        "materials_status_group_disabled": "No activado",
        "materials_submission_pending": "Envío incompleto",
        "materials_summary_missing": "{count} sin stock",
        "materials_mode_fresh_full_retry": "Lista actualizada, enviar todo y retirar faltantes",
        "materials_col_name": "Nombre",
        "materials_col_code": "Código",
        "materials_col_qty": "Cantidad",
        "materials_col_status": "Estado",
        "update_available_title": "Nueva versión disponible",
        "update_available_body": "Versión actual: {current}\nÚltima versión: {latest}",
        "update_install_button": "Descargar e instalar",
        "update_later_button": "Más tarde",
        "update_progress_title": "Progreso de la actualización",
        "update_failed_title": "Error de actualización",
        "update_installing_title": "Actualización lista",
        "update_installing_body": "La versión {version} está descargada y verificada. La app se cerrará; por favor vuelve a abrirla para entrar a la nueva versión.",
    },
}


class FactoryTestGUI:
    STATUS_TEXT = {
        "queued": "排队中",
        "running": "进行中",
        "transfer": "传输中",
        "on_hold": "On Hold",
        "cancelling": "\u4e2d\u65ad\u4e2d",
        "cancelled": "\u5df2\u4e2d\u65ad",
        "retrying": "重试中",
        "success": "已完成",
        "failed": "失败",
    }
    STATUS_TEXT_EN = {
        "queued": "Queued",
        "running": "Running",
        "transfer": "Transferring",
        "on_hold": "On Hold",
        "cancelling": "Cancelling",
        "cancelled": "Cancelled",
        "retrying": "Retrying",
        "success": "Done",
        "failed": "Failed",
    }
    STATUS_TEXT_ES_MX = {
        "queued": "En cola",
        "running": "En curso",
        "transfer": "Transfiriendo",
        "on_hold": "En espera",
        "cancelling": "Cancelando",
        "cancelled": "Cancelada",
        "retrying": "Reintentando",
        "success": "Completada",
        "failed": "Fallida",
    }

    ACTIVE_STATES = {"queued", "running", "transfer", "on_hold", "retrying", "cancelling"}
    ROW_SUCCESS_TAG = "row_success"
    ROW_FAILED_TAG = "row_failed"
    ROW_SUCCESS_FG = "#16803c"
    ROW_SUCCESS_BG = "#ecf8ef"
    ROW_FAILED_FG = "#b42318"
    ROW_FAILED_BG = "#fdecec"
    DAILY_STATS_FILE = "gui_daily_stats.json"
    QUEUE_STATE_FILE = "gui_queue_state.json"
    AUTO_SCAN_INTERVAL_MS = 15_000
    AUTO_SCAN_RETRY_MS = 5_000
    TIMING_CHART_REFRESH_MS = 1_000
    MATERIALS_REFRESH_MS = 3_000
    MEASURE_REFRESH_MS = 3_000
    ROLLOVER_IP_PROBE_TIMEOUT = 0.5
    ROLLOVER_MAX_IP_PROBE_WORKERS = 32

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.geometry("1260x760")
        self.root.minsize(1040, 620)

        self.project_root = get_project_root()
        self.form_entry_enabled = FORM_ENTRY_ENABLED
        try:
            self.config, _ = load_configs(self.project_root)
        except Exception:
            self.config = {"output_dir": "./screenshot"}

        self.ui_queue: queue.Queue[dict] = queue.Queue()
        self.devices: dict[str, DeviceTask] = {}
        self.workers: dict[str, threading.Thread] = {}
        self.task_counter = 0
        self.selected_task_id: str | None = None
        self.selected_task_ids: list[str] = []
        self.daily_stats_date, self.daily_stats_devices = self._load_daily_stats()

        self.sn_var = tk.StringVar()
        self.manual_ip_var = tk.StringVar()
        self.auto_scan_var = tk.BooleanVar(value=True)
        self.flow_mode_var = tk.StringVar(value="setup")
        self.cleanup_before_finish_var = tk.BooleanVar(value=True)
        self.factory_reset_before_finish_var = tk.BooleanVar(value=True)
        self.auto_form_entry_var = tk.BooleanVar(value=FORM_ENTRY_ENABLED)
        _label_cfg = (self.config.get("label_printer") or {}) if isinstance(self.config, dict) else {}
        self.auto_print_label_var = tk.BooleanVar(value=bool(_label_cfg.get("auto_print_on_pass", False)))
        self.auto_seed_previous_step_var = tk.BooleanVar(value=False)
        self.form_grade_var = tk.StringVar(value="")
        self.form_account_var = tk.StringVar()
        self.sound_enabled_var = tk.BooleanVar(value=True)
        self.language_var = tk.StringVar(value=LANGUAGE_OPTIONS[0])
        self.status_var = tk.StringVar(value=self._t("ready"))
        self.queue_summary_var = tk.StringVar(value=self._t("queue_summary_empty"))
        self.success_count_var = tk.StringVar(value=self._t("success_count", count=0))
        self.failed_count_var = tk.StringVar(value=self._t("failed_count", count=0))
        self._text_widgets: dict[str, tk.Widget] = {}

        self.sn_entry: ttk.Entry | None = None
        self.start_btn: ttk.Button | None = None
        self.remove_current_btn: ttk.Button | None = None
        self.show_browser_btn: ttk.Button | None = None
        self.cancel_btn: ttk.Button | None = None
        self.auto_scan_check: ttk.Checkbutton | None = None
        self.cleanup_check: ttk.Checkbutton | None = None
        self.factory_reset_check: ttk.Checkbutton | None = None
        self.auto_form_entry_check: ttk.Checkbutton | None = None
        self.auto_print_label_check: ttk.Checkbutton | None = None
        self.auto_seed_previous_step_check: ttk.Checkbutton | None = None
        self.sound_check: ttk.Checkbutton | None = None
        self.language_combo: ttk.Combobox | None = None
        self.form_grade_radios: list[ttk.Radiobutton] = []
        self.form_account_combo: ttk.Combobox | None = None
        self.device_tree: ttk.Treeview | None = None
        self.log_view: scrolledtext.ScrolledText | None = None
        self.log_notebook: ttk.Notebook | None = None
        self.log_tab_frame: ttk.Frame | None = None
        self.materials_tab_frame: ttk.Frame | None = None
        self.materials_tree: ttk.Treeview | None = None
        self.materials_status_var = tk.StringVar(value="")
        self.materials_status_label: ttk.Label | None = None
        self.materials_refresh_after_id: str | None = None
        self.measure_tab_frame: ttk.Frame | None = None
        self.measure_canvas: tk.Canvas | None = None
        self.measure_inner: ttk.Frame | None = None
        self.measure_window: int | None = None
        self.measure_status_var = tk.StringVar(value="")
        self.measure_status_label: ttk.Label | None = None
        self.measure_refresh_after_id: str | None = None
        self.timing_canvas: tk.Canvas | None = None
        self.timing_chart_after_id: str | None = None
        self.daily_rollover_after_id: str | None = None
        self.auto_scan_worker: threading.Thread | None = None
        self.auto_scan_after_id: str | None = None

        self.update_manager = UpdateManager(self.project_root, self.ui_queue)
        self._pending_update: UpdateInfo | None = None
        self._update_install_active = False

        self._build_layout()
        self._restore_queue_state()
        self._refresh_status_counts()
        if self.form_entry_enabled:
            self._refresh_form_accounts()
            self.root.after(0, self._refresh_form_materials_on_startup)
        self._attach_logger_sink()
        self._refresh_action_states()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(0, self._on_auto_scan_toggle)
        self.root.after(100, self._drain_ui_queue)
        self.root.after(1000, self._refresh_elapsed_times)
        self._schedule_daily_rollover()
        self.root.after(500, self.update_manager.check_on_startup)

    def _language_code(self) -> str:
        try:
            return LANGUAGE_CODES_BY_LABEL.get(self.language_var.get(), "zh")
        except Exception:
            return "zh"

    def _t(self, key: str, **kwargs) -> str:
        text = UI_TEXT.get(self._language_code(), UI_TEXT["zh"]).get(key, UI_TEXT["zh"].get(key, key))
        return text.format(**kwargs) if kwargs else text

    def _register_text(self, key: str, widget):
        self._text_widgets[key] = widget
        return widget

    def _status_text(self, status_code: str) -> str:
        source = {
            "zh": self.STATUS_TEXT,
            "en": self.STATUS_TEXT_EN,
            "es-MX": self.STATUS_TEXT_ES_MX,
        }.get(self._language_code(), self.STATUS_TEXT)
        return source.get(status_code, status_code)

    def _timestamped(self, key: str, **kwargs) -> str:
        return f"{datetime.now().strftime('%H:%M:%S')}  {self._t(key, **kwargs)}"

    def _today_key(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _daily_stats_path(self) -> Path:
        return self.project_root / "state" / self.DAILY_STATS_FILE

    def _queue_state_path(self) -> Path:
        return self.project_root / "state" / self.QUEUE_STATE_FILE

    def _load_daily_stats(self) -> tuple[str, dict[str, dict]]:
        today = self._today_key()
        devices: dict[str, dict] = {}
        path = self._daily_stats_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}
        if str(data.get("date") or "") == today and isinstance(data.get("devices"), dict):
            devices.update(dict(data["devices"]))
        devices.update(self._daily_stats_from_output_folders(today))
        return today, devices

    def _daily_stats_from_output_folders(self, today: str) -> dict[str, dict]:
        try:
            output_root = self._output_root()
        except Exception as exc:
            logger.warning(f"Could not scan daily output folders: {exc}")
            return {}
        if not output_root.exists():
            return {}

        devices: dict[str, dict] = {}
        try:
            folders = list(output_root.iterdir())
        except Exception as exc:
            logger.warning(f"Could not list daily output folders: {exc}")
            return {}
        for sn_root in folders:
            if not sn_root.is_dir():
                continue
            entry = self._daily_entry_from_output_folder(sn_root, today)
            if entry is None:
                continue
            key, payload = entry
            devices[key] = payload
        return devices

    def _daily_entry_from_output_folder(self, sn_root: Path, today: str) -> tuple[str, dict] | None:
        try:
            if self._path_created_date(sn_root) != today:
                return None
        except Exception:
            return None

        report = self._read_report(sn_root / "test_report.json")
        if not report:
            return None
        status = str(report.get("status") or "").lower()
        if status not in {"success", "failed"}:
            return None

        report_sn = normalize_sn(str(report.get("sn") or ""))
        folder_sn = normalize_sn(sn_root.name)
        sn = report_sn or folder_sn
        key = self._daily_output_entry_key(sn, sn_root)
        return key, {
            "sn": sn,
            "status": status,
            "updated_at": str(report.get("finished_at") or report.get("started_at") or ""),
            "source": "output_folder",
            "folder": str(sn_root),
        }

    def _daily_output_entry_key(self, sn: str, sn_root: Path) -> str:
        normalized_sn = normalize_sn(sn)
        tail = sn_tail(normalized_sn)
        if normalized_sn and not is_auto_sn_placeholder(normalized_sn) and len(tail) >= 4:
            return f"sn:{tail}"
        return f"folder:{sn_root.name}"

    def _path_created_date(self, path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_ctime).strftime("%Y-%m-%d")

    def _path_created_timestamp(self, path: Path) -> str:
        return datetime.fromtimestamp(path.stat().st_ctime).isoformat(timespec="seconds")

    def _save_daily_stats(self) -> None:
        try:
            path = self._daily_stats_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "date": self.daily_stats_date,
                        "devices": self.daily_stats_devices,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"Could not save daily GUI stats: {exc}")

    def _ensure_daily_stats_current(self) -> None:
        today = self._today_key()
        if getattr(self, "daily_stats_date", "") == today:
            return
        self.daily_stats_date, self.daily_stats_devices = self._load_daily_stats()
        self._save_daily_stats()

    def _detect_present_task_ips(self, tasks) -> set[str]:
        task_ips = set()
        for task in tasks:
            task_ips.update(self._task_identity_ips(task))
        task_ips = {ip for ip in task_ips if self._auto_scan_ip_allowed(ip, None)}
        if not task_ips:
            return set()

        present = set()
        try:
            for hit in self._broadcast_scan_hits(1.0):
                if hit.address in task_ips:
                    present.add(hit.address)
        except Exception:
            pass

        remaining = sorted(task_ips - present)
        if not remaining:
            return present

        network = self.config.get("network") or {}
        port = int(network.get("ugos_http_port") or 9999)
        max_workers = min(self.ROLLOVER_MAX_IP_PROBE_WORKERS, max(1, len(remaining)))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(self._ip_port_present, ip, port): ip for ip in remaining}
            for future in as_completed(futures):
                ip = futures[future]
                try:
                    if future.result():
                        present.add(ip)
                except Exception:
                    pass
        return present

    def _ip_port_present(self, ip: str, port: int) -> bool:
        try:
            with socket.create_connection((ip, port), timeout=self.ROLLOVER_IP_PROBE_TIMEOUT):
                return True
        except OSError:
            return False

    def _keep_task_after_daily_rollover(self, task: DeviceTask, present_ips: set[str]) -> bool:
        return task.state_code in self.ACTIVE_STATES or bool(self._task_identity_ips(task) & present_ips)

    def _milliseconds_until_next_midnight(self) -> int:
        now = datetime.now()
        next_midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return max(1000, int((next_midnight - now).total_seconds() * 1000))

    def _schedule_daily_rollover(self) -> None:
        if self.daily_rollover_after_id is not None:
            try:
                self.root.after_cancel(self.daily_rollover_after_id)
            except Exception:
                pass
        self.daily_rollover_after_id = self.root.after(
            self._milliseconds_until_next_midnight(),
            self._handle_daily_rollover,
        )

    def _handle_daily_rollover(self) -> None:
        self.daily_rollover_after_id = None
        self.daily_stats_date, self.daily_stats_devices = self._load_daily_stats()
        self._save_daily_stats()

        removed_selected = self.selected_task_id is not None
        present_ips = self._detect_present_task_ips(self.devices.values())
        kept_devices = {
            task_id: task
            for task_id, task in self.devices.items()
            if self._keep_task_after_daily_rollover(task, present_ips)
        }
        if self.device_tree is not None:
            for task_id in list(self.devices):
                if task_id not in kept_devices and self.device_tree.exists(task_id):
                    self.device_tree.delete(task_id)
        self.devices = kept_devices
        self.workers = {
            task_id: worker
            for task_id, worker in self.workers.items()
            if task_id in self.devices
        }
        if removed_selected and self.selected_task_id not in self.devices:
            self.selected_task_id = None
            self._set_log_contents(self._t("select_device_log_sentence"))
            self._clear_timing_chart()
            self._clear_materials_tab(self._t("materials_select_device"))
            self._clear_measurements_tab(self._t("measure_select_device"))

        self._refresh_summary()
        self._refresh_action_states()
        self._save_queue_state(merge_existing=False)
        self._schedule_daily_rollover()

    def _load_queue_state_records(self) -> list[dict]:
        today = self._today_key()
        records: list[dict] = []
        path = self._queue_state_path()
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}
        if isinstance(data.get("devices"), list):
            records = [record for record in data["devices"] if isinstance(record, dict)]
        cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        file_date_is_today = str(data.get("date") or "") == today
        records = [
            record
            for record in records
            if self._queue_record_in_window(record, cutoff, file_date_is_today)
        ]
        return self._merge_queue_records_with_output(records, today)

    def _queue_record_in_window(self, record: dict, cutoff: datetime, file_date_is_today: bool) -> bool:
        # File dated today means rollover/in-day save already filtered, so trust every record (lets 在网 devices kept overnight survive restart).
        if file_date_is_today:
            return True
        ctime = self._sn_folder_ctime(normalize_sn(str(record.get("sn") or "")))
        return ctime is not None and datetime.fromtimestamp(ctime) >= cutoff

    def _sn_folder_ctime(self, sn: str) -> float | None:
        if not sn:
            return None
        try:
            path = self._output_root() / sn
            raw = self._path_created_timestamp(path)
        except (OSError, FileNotFoundError):
            return None
        try:
            return datetime.fromisoformat(raw).timestamp()
        except (ValueError, TypeError):
            return None

    def _merge_queue_records_with_output(self, records: list[dict], today: str) -> list[dict]:
        merged_by_key: dict[str, dict] = {}

        def add_or_update(record: dict, prefer_output: bool = False) -> None:
            key = self._queue_record_identity_key(record)
            if not key:
                return
            if key not in merged_by_key:
                merged_by_key[key] = dict(record)
                return
            if not prefer_output:
                merged_by_key[key].update(record)
                return
            existing = dict(merged_by_key[key])
            task_id = str(existing.get("task_id") or "").strip()
            existing.update(record)
            if task_id:
                existing["task_id"] = task_id
            merged_by_key[key] = existing

        for record in records:
            add_or_update(record)
        for record in self._queue_records_from_output_folders(today):
            add_or_update(record, prefer_output=True)
        return self._sort_queue_records(list(merged_by_key.values()))

    def _queue_records_from_output_folders(self, today: str) -> list[dict]:
        try:
            output_root = self._output_root()
        except Exception as exc:
            logger.warning(f"Could not scan queue output folders: {exc}")
            return []
        try:
            folders = sorted(
                (path for path in output_root.iterdir() if path.is_dir()),
                key=lambda path: path.stat().st_ctime,
            )
        except Exception as exc:
            logger.warning(f"Could not list queue output folders: {exc}")
            return []

        records: list[dict] = []
        for index, sn_root in enumerate(folders, start=1):
            record = self._queue_record_from_output_folder(sn_root, today, index)
            if record is not None:
                records.append(record)
        return records

    def _queue_record_from_output_folder(self, sn_root: Path, today: str, index: int) -> dict | None:
        try:
            if self._path_created_date(sn_root) != today:
                return None
        except Exception:
            return None

        report = self._read_report(sn_root / "test_report.json")
        if not report:
            return None
        status = str(report.get("status") or "").lower()
        if status not in {"success", "failed"}:
            return None

        sn = normalize_sn(str(report.get("sn") or sn_root.name))
        if not sn:
            return None
        nas_ip = str(report.get("nas_ip") or "auto").strip() or "auto"
        current_step = self._current_step_from_report(status, report)
        order_at = self._report_order_timestamp(report, sn_root)
        return {
            "task_id": f"{sn}-{index:03d}",
            "sn": sn,
            "requested_ip": nas_ip,
            "actual_ip": nas_ip if nas_ip.lower() != "auto" else "",
            "mode": str(report.get("mode") or "setup"),
            "cleanup_before_finish": bool(report.get("cleanup_before_finish", True)),
            "factory_reset_before_finish": bool(report.get("factory_reset_before_finish", True)),
            "auto_form_entry": bool(report.get("auto_form_entry", False)),
            "auto_seed_previous_step": False,
            "form_model": str(report.get("form_model") or model_key_from_sn(sn) or ""),
            "form_grade": str(report.get("form_grade") or "A"),
            "form_account_name": str(report.get("form_account_name") or ""),
            "state_code": status,
            "progress": 100 if status == "success" else 0,
            "current_step": current_step,
            "reserved_ips": [] if nas_ip.lower() == "auto" else [nas_ip],
            "network_interface": str(report.get("network_interface") or ""),
            "attempt": self._safe_int(report.get("attempt"), 1),
            "max_attempts": self._safe_int(report.get("max_attempts"), 2),
            "elapsed_seconds": self._report_elapsed_seconds(report),
            "started_at": str(report.get("started_at") or ""),
            "finished_at": str(report.get("finished_at") or ""),
            "queue_order_at": order_at,
        }

    def _queue_record_identity_key(self, record: dict) -> str:
        sn = normalize_sn(str(record.get("sn") or ""))
        tail = sn_tail(sn)
        if sn and not is_auto_sn_placeholder(sn) and len(tail) >= 4:
            return f"sn:{tail}"
        task_id = str(record.get("task_id") or "").strip()
        return f"task:{task_id}" if task_id else ""

    def _report_elapsed_seconds(self, report: dict) -> int:
        try:
            started = datetime.fromisoformat(str(report.get("started_at") or ""))
            finished = datetime.fromisoformat(str(report.get("finished_at") or ""))
        except Exception:
            return 0
        return max(0, int((finished - started).total_seconds()))

    def _report_order_timestamp(self, report: dict, sn_root: Path) -> str:
        try:
            return self._path_created_timestamp(sn_root)
        except Exception:
            pass
        for key in ("started_at", "finished_at"):
            value = str(report.get(key) or "").strip()
            if value:
                return value
        return ""

    def _sort_queue_records(self, records: list[dict]) -> list[dict]:
        return sorted(records, key=self._queue_record_sort_key)

    def _queue_record_sort_key(self, record: dict) -> tuple[int, float, int, str]:
        task_id = str(record.get("task_id") or "")
        sn = normalize_sn(str(record.get("sn") or ""))
        counter = self._task_counter_from_id(task_id)
        ctime = self._sn_folder_ctime(sn)
        if ctime is not None:
            return (0, ctime, counter, sn)
        raw_value = str(
            record.get("queue_added_at")
            or record.get("queue_order_at")
            or record.get("started_at")
            or record.get("finished_at")
            or ""
        ).strip()
        try:
            timestamp = datetime.fromisoformat(raw_value).timestamp()
            return (1, timestamp, counter, sn)
        except (ValueError, TypeError):
            return (2, 0.0, counter, sn)

    def _save_queue_state(self, merge_existing: bool = True) -> None:
        try:
            path = self._queue_state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            records_by_id: dict[str, dict] = {}
            if merge_existing:
                existing_records = self._load_queue_state_records()
                if not self.devices and not existing_records and path.exists():
                    return
                for record in existing_records:
                    task_id = str(record.get("task_id") or "").strip()
                    if task_id:
                        records_by_id[task_id] = record
            for task in self.devices.values():
                records_by_id[task.task_id] = self._queue_record_for_task(task)
            records = self._sort_queue_records(list(records_by_id.values()))
            path.write_text(
                json.dumps(
                    {
                        "date": self._today_key(),
                        "devices": records,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning(f"Could not save GUI queue state: {exc}")

    def _queue_record_for_task(self, task: DeviceTask) -> dict[str, object]:
        return {
            "task_id": task.task_id,
            "sn": task.sn,
            "requested_ip": task.requested_ip,
            "actual_ip": task.actual_ip,
            "mode": task.mode,
            "cleanup_before_finish": task.cleanup_before_finish,
            "factory_reset_before_finish": task.factory_reset_before_finish,
            "auto_form_entry": task.auto_form_entry,
            "auto_seed_previous_step": task.auto_seed_previous_step,
            "form_model": task.form_model,
            "form_grade": task.form_grade,
            "form_account_name": task.form_account_name,
            "state_code": task.state_code,
            "progress": task.progress,
            "current_step": task.current_step,
            "reserved_ips": sort_ip_strings(task.reserved_ips),
            "network_interface": task.network_interface,
            "attempt": task.attempt,
            "max_attempts": task.max_attempts,
            "elapsed_seconds": task.elapsed_seconds,
            "queue_added_at": task.queue_added_at,
            "queue_order_at": task.queue_added_at,
            "interrupted_on_exit": task.interrupted_on_exit,
        }

    def _task_from_queue_record(self, record: dict) -> DeviceTask | None:
        task_id = str(record.get("task_id") or "").strip()
        sn = normalize_sn(str(record.get("sn") or ""))
        requested_ip = str(record.get("requested_ip") or "auto").strip() or "auto"
        if not task_id or not sn:
            return None

        original_state = str(record.get("state_code") or "cancelled")
        current_step = str(record.get("current_step") or "上次队列恢复")
        was_interrupted = original_state in self.ACTIVE_STATES or bool(record.get("interrupted_on_exit"))
        if original_state in self.ACTIVE_STATES:
            state_code = "cancelled"
            current_step = f"上次退出时未完成：{current_step}"
        else:
            state_code = original_state

        queue_added_at = str(record.get("queue_added_at") or record.get("queue_order_at") or "").strip()

        task = DeviceTask(
            task_id=task_id,
            sn=sn,
            requested_ip=requested_ip,
            mode=str(record.get("mode") or "setup"),
            cleanup_before_finish=bool(record.get("cleanup_before_finish", True)),
            factory_reset_before_finish=bool(record.get("factory_reset_before_finish", True)),
            auto_form_entry=bool(record.get("auto_form_entry", False)),
            auto_seed_previous_step=bool(record.get("auto_seed_previous_step", False)),
            form_model=str(record.get("form_model") or ""),
            form_grade=str(record.get("form_grade") or "A"),
            form_account_name=str(record.get("form_account_name") or ""),
            actual_ip=str(record.get("actual_ip") or ""),
            state_code=state_code,
            progress=self._safe_int(record.get("progress"), 0),
            current_step=current_step,
            reserved_ips=set(sort_ip_strings(self._queue_record_ips(record, requested_ip))),
            network_interface=str(record.get("network_interface") or ""),
            attempt=self._safe_int(record.get("attempt"), 0),
            max_attempts=self._safe_int(record.get("max_attempts"), 2),
            queue_added_at=queue_added_at,
            interrupted_on_exit=was_interrupted,
        )
        task.status = self._status_text(task.state_code)
        elapsed_seconds = max(0, self._safe_int(record.get("elapsed_seconds"), 0))
        now = time.monotonic()
        task.started_monotonic = now - elapsed_seconds
        if task.state_code not in self.ACTIVE_STATES:
            task.finished_monotonic = now
        disk_logs = self._load_task_logs_from_disk(sn)
        if disk_logs:
            task.logs.extend(disk_logs)
        task.logs.append(
            f"{datetime.now().strftime('%H:%M:%S')} | INFO    | 已从上次队列恢复\n"
        )
        return task

    def _load_task_logs_from_disk(self, sn: str) -> list[str]:
        try:
            log_path = self._output_root() / sn / "run.log"
        except Exception:
            return []
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return []
        lines: list[str] = []
        loguru_re = re.compile(
            r"^\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2})(?:\.\d+)?\s*\|\s*(\S+)\s*\|\s*[^|]*?-\s*(.*)$"
        )
        for raw in text.splitlines():
            match = loguru_re.match(raw)
            if match:
                time_part, level_part, message = match.groups()
                lines.append(f"{time_part} | {level_part:<7} | {message}\n")
            elif raw.strip():
                lines.append(raw + "\n")
        return lines

    def _queue_record_ips(self, record: dict, fallback_ip: str) -> list[str]:
        reserved = record.get("reserved_ips")
        if isinstance(reserved, list):
            return [str(ip) for ip in reserved]
        return [fallback_ip]

    def _safe_int(self, value, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    def _restore_queue_state(self) -> None:
        restored = 0
        max_counter = self.task_counter
        interrupted: list[DeviceTask] = []
        for record in self._load_queue_state_records():
            task = self._task_from_queue_record(record)
            if task is None or task.task_id in self.devices:
                continue
            self.devices[task.task_id] = task
            max_counter = max(max_counter, self._task_counter_from_id(task.task_id))
            self._insert_device_row(task, select=False)
            if task.interrupted_on_exit:
                interrupted.append(task)
            restored += 1
        self.task_counter = max_counter
        if restored:
            self._refresh_summary()
        if interrupted:
            self.root.after(0, lambda: self._auto_resume_interrupted_tasks(interrupted))

    def _auto_resume_interrupted_tasks(self, tasks: list[DeviceTask]) -> None:
        candidates = [
            task
            for task in tasks
            if task.task_id in self.devices
            and task.interrupted_on_exit
            and not self._has_active_task_for_same_device(task)
        ]
        if not candidates:
            return
        try:
            present_ips = self._detect_present_task_ips(candidates)
        except Exception as exc:
            logger.warning(f"Could not probe interrupted-task IPs on startup: {exc}")
            return
        for task in candidates:
            if task.task_id not in self.devices or not task.interrupted_on_exit:
                continue
            if self._has_active_task_for_same_device(task):
                continue
            if self._task_identity_ips(task) & present_ips:
                self._resume_interrupted_task(task)

    def _resume_interrupted_task(self, task: DeviceTask) -> None:
        if not task.interrupted_on_exit or task.task_id not in self.devices:
            return
        task.interrupted_on_exit = False
        task.cancel_event = threading.Event()
        task.state_code = "queued"
        task.status = self._status_text("queued")
        task.current_step = "上次未完成，检测到设备在线，自动续跑"
        task.progress = 0
        task.attempt = 0
        task.finished_monotonic = None
        task.started_monotonic = time.monotonic()
        self._refresh_device_row(task)
        self._refresh_device_rows_for_task_identity(task)
        self._append_local_log(task.task_id, "检测到设备仍然在线，自动恢复执行")
        self._refresh_summary()
        self._refresh_action_states()
        self._save_queue_state()
        self._start_task_worker(task)

    def _task_counter_from_id(self, task_id: str) -> int:
        match = re.search(r"-(\d+)$", task_id)
        return int(match.group(1)) if match else 0

    def _build_layout(self) -> None:
        self.root.title(self._t("app_title"))
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        top = ttk.Frame(main)
        top.grid(row=0, column=0, sticky=tk.EW)
        top.columnconfigure(0, weight=1)
        top.columnconfigure(1, weight=0)

        controls = self._register_text("test_params", ttk.LabelFrame(top, text=self._t("test_params"), padding=12))
        controls.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 12))
        controls.columnconfigure(1, weight=1)

        input_frame = ttk.Frame(controls)
        input_frame.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=4)
        self.auto_scan_check = ttk.Checkbutton(
            input_frame,
            text=self._t("auto_scan"),
            variable=self.auto_scan_var,
            command=self._on_auto_scan_toggle,
        )
        self._register_text("auto_scan", self.auto_scan_check)
        self.auto_scan_check.grid(row=0, column=0, sticky=tk.W, padx=(0, 14))
        self._register_text("sn_label", ttk.Label(input_frame, text=self._t("sn_label"))).grid(
            row=0, column=1, sticky=tk.W, padx=(0, 4)
        )
        self.sn_entry = ttk.Entry(input_frame, textvariable=self.sn_var, width=22)
        self.sn_entry.grid(row=0, column=2, sticky=tk.W)
        self.sn_entry.bind("<Return>", self._on_sn_enter)
        # Focusing the SN box means the operator is about to type/scan a SN
        # they want to print individually — clear any queue selection so the
        # print button uses the entry value, not the highlighted device.
        self.sn_entry.bind("<FocusIn>", self._on_sn_entry_focus)
        self.sn_entry.focus()
        self._register_text("or_label", ttk.Label(input_frame, text=self._t("or_label"))).grid(
            row=0, column=3, sticky=tk.W, padx=10
        )
        self._register_text("nas_ip_label", ttk.Label(input_frame, text=self._t("nas_ip_label"))).grid(
            row=0, column=4, sticky=tk.W, padx=(0, 4)
        )
        ttk.Entry(input_frame, textvariable=self.manual_ip_var, width=16).grid(row=0, column=5, sticky=tk.W)

        self._register_text("finish_actions", ttk.Label(controls, text=self._t("finish_actions"))).grid(
            row=1, column=0, sticky=tk.NW, pady=4
        )
        option_frame = ttk.Frame(controls)
        option_frame.grid(row=1, column=1, sticky=tk.EW, pady=4)
        self.cleanup_check = ttk.Checkbutton(
            option_frame,
            text=self._t("cleanup_pools"),
            variable=self.cleanup_before_finish_var,
            command=self._on_cleanup_toggle,
        )
        self._register_text("cleanup_pools", self.cleanup_check)
        self.cleanup_check.pack(side=tk.LEFT, padx=(0, 12))
        self.factory_reset_check = ttk.Checkbutton(
            option_frame,
            text=self._t("factory_reset"),
            variable=self.factory_reset_before_finish_var,
            command=self._on_factory_reset_toggle,
        )
        self._register_text("factory_reset", self.factory_reset_check)
        self.factory_reset_check.pack(side=tk.LEFT, padx=(0, 12))
        self.auto_form_entry_check = ttk.Checkbutton(
            option_frame,
            text=self._t("auto_form_entry"),
            variable=self.auto_form_entry_var,
            command=self._on_auto_form_entry_toggle,
        )
        self._register_text("auto_form_entry", self.auto_form_entry_check)
        self.auto_form_entry_check.pack(side=tk.LEFT, padx=(0, 12))
        self.auto_print_label_check = ttk.Checkbutton(
            option_frame,
            text=self._t("auto_print_label"),
            variable=self.auto_print_label_var,
        )
        self._register_text("auto_print_label", self.auto_print_label_check)
        self.auto_print_label_check.pack(side=tk.LEFT)

        form_config_label = self._register_text("form_settings", ttk.Label(controls, text=self._t("form_settings")))
        form_config_label.grid(row=2, column=0, sticky=tk.W, pady=4)
        form_frame = ttk.Frame(controls)
        form_frame.grid(row=2, column=1, sticky=tk.EW, pady=4)
        self._register_text("model_auto", ttk.Label(form_frame, text=self._t("model_auto"))).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        self._register_text("grade", ttk.Label(form_frame, text=self._t("grade"))).pack(side=tk.LEFT, padx=(0, 4))
        self.form_grade_radios = []
        for grade in ("A", "B"):
            radio = ttk.Radiobutton(
                form_frame,
                text=grade,
                variable=self.form_grade_var,
                value=grade,
                command=self._on_form_grade_changed,
            )
            radio.pack(side=tk.LEFT, padx=(0, 6))
            self.form_grade_radios.append(radio)
        self._register_text("account", ttk.Label(form_frame, text=self._t("account"))).pack(side=tk.LEFT, padx=(0, 4))
        self.form_account_combo = ttk.Combobox(form_frame, textvariable=self.form_account_var, width=24, state="readonly")
        self.form_account_combo.pack(side=tk.LEFT, padx=(0, 6))
        self.form_account_combo.bind("<<ComboboxSelected>>", self._on_switch_account)
        self._register_text(
            "switch_account", ttk.Button(form_frame, text=self._t("switch_account"), command=self._on_switch_account)
        ).pack(side=tk.LEFT, padx=(0, 6))
        self._register_text(
            "add_account", ttk.Button(form_frame, text=self._t("add_account"), command=self._on_add_account)
        ).pack(side=tk.LEFT, padx=(0, 6))
        self._register_text(
            "delete_account", ttk.Button(form_frame, text=self._t("delete_account"), command=self._on_delete_account)
        ).pack(side=tk.LEFT, padx=(0, 12))
        button_row = 3
        if not self.form_entry_enabled:
            self.auto_form_entry_var.set(False)
            if self.auto_form_entry_check is not None:
                self.auto_form_entry_check.pack_forget()
            form_config_label.grid_remove()
            form_frame.grid_remove()
            button_row = 2

        btn_frame = ttk.Frame(controls)
        btn_frame.grid(row=button_row, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        self.start_btn = ttk.Button(btn_frame, text=self._t("add_to_queue"), command=self._on_start_test)
        self._register_text("add_to_queue", self.start_btn)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        if self.form_entry_enabled:
            self.auto_seed_previous_step_check = ttk.Checkbutton(
                btn_frame,
                text=self._t("auto_seed_previous"),
                variable=self.auto_seed_previous_step_var,
            )
            self._register_text("auto_seed_previous", self.auto_seed_previous_step_check)
            self.auto_seed_previous_step_check.pack(side=tk.LEFT, padx=(6, 12))
        self._register_text(
            "open_screenshot_dir", ttk.Button(btn_frame, text=self._t("open_screenshot_dir"), command=self._open_output)
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.remove_current_btn = ttk.Button(
            btn_frame,
            text=self._t("remove_finished"),
            command=self._remove_current_task,
        )
        self._register_text("remove_finished", self.remove_current_btn)
        self.remove_current_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.show_browser_btn = ttk.Button(btn_frame, text=self._t("show_browser"), command=self._on_show_browser)
        self._register_text("show_browser", self.show_browser_btn)
        self.show_browser_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.cancel_btn = ttk.Button(btn_frame, text=self._t("cancel_task"), command=self._on_cancel_task)
        self._register_text("cancel_task", self.cancel_btn)
        self.cancel_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.print_label_btn = ttk.Button(btn_frame, text=self._t("print_label"), command=self._on_print_label)
        self._register_text("print_label", self.print_label_btn)
        self.print_label_btn.pack(side=tk.LEFT)

        timing_frame = self._register_text(
            "timing_analysis",
            ttk.LabelFrame(top, text=self._t("timing_analysis"), padding=8),
        )
        timing_frame.grid(row=0, column=1, sticky=tk.NSEW)
        timing_frame.columnconfigure(0, weight=1)
        timing_frame.rowconfigure(0, weight=1)
        self.timing_canvas = tk.Canvas(timing_frame, width=320, height=152, highlightthickness=0)
        self.timing_canvas.grid(row=0, column=0, sticky=tk.NSEW)
        self._clear_timing_chart()

        content = ttk.Panedwindow(main, orient=tk.HORIZONTAL)
        content.grid(row=1, column=0, sticky=tk.NSEW, pady=(12, 0))

        queue_frame = self._register_text(
            "connected_devices", ttk.LabelFrame(content, text=self._t("connected_devices"), padding=8)
        )
        queue_frame.columnconfigure(0, weight=1)
        queue_frame.rowconfigure(1, weight=1)
        content.add(queue_frame, weight=3)

        ttk.Label(queue_frame, textvariable=self.queue_summary_var).grid(row=0, column=0, sticky=tk.W, pady=(0, 8))

        columns = ("sn", "ip", "status", "elapsed", "progress", "step")
        self.device_tree = ttk.Treeview(queue_frame, columns=columns, show="headings", selectmode="extended")
        self.device_tree.tag_configure(
            self.ROW_SUCCESS_TAG,
            foreground=self.ROW_SUCCESS_FG,
            background=self.ROW_SUCCESS_BG,
        )
        self.device_tree.tag_configure(
            self.ROW_FAILED_TAG,
            foreground=self.ROW_FAILED_FG,
            background=self.ROW_FAILED_BG,
        )
        self.device_tree.heading("sn", text="SN")
        self.device_tree.heading("ip", text="IP")
        self.device_tree.heading("status", text=self._t("tree_status"))
        self.device_tree.heading("elapsed", text=self._t("tree_elapsed"))
        self.device_tree.heading("progress", text=self._t("tree_progress"))
        self.device_tree.heading("step", text=self._t("tree_step"))
        self.device_tree.column("sn", width=190, anchor=tk.W, stretch=False)
        self.device_tree.column("ip", width=130, anchor=tk.W, stretch=False)
        self.device_tree.column("status", width=90, anchor=tk.CENTER, stretch=False)
        self.device_tree.column("elapsed", width=90, anchor=tk.CENTER, stretch=False)
        self.device_tree.column("progress", width=80, anchor=tk.CENTER, stretch=False)
        self.device_tree.column("step", width=330, anchor=tk.W, stretch=True)
        self.device_tree.grid(row=1, column=0, sticky=tk.NSEW)
        self.device_tree.bind("<<TreeviewSelect>>", self._on_select_device)

        queue_scroll = ttk.Scrollbar(queue_frame, orient=tk.VERTICAL, command=self.device_tree.yview)
        queue_scroll.grid(row=1, column=1, sticky=tk.NS)
        self.device_tree.configure(yscrollcommand=queue_scroll.set)

        log_frame = ttk.Frame(content, padding=(8, 0, 0, 0))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(0, weight=1)
        content.add(log_frame, weight=2)

        self.log_notebook = ttk.Notebook(log_frame)
        self.log_notebook.grid(row=0, column=0, sticky=tk.NSEW)

        self.log_tab_frame = ttk.Frame(self.log_notebook)
        self.log_tab_frame.columnconfigure(0, weight=1)
        self.log_tab_frame.rowconfigure(0, weight=1)
        self.log_notebook.add(self.log_tab_frame, text=self._t("tab_log"))
        self.log_view = scrolledtext.ScrolledText(self.log_tab_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.log_view.grid(row=0, column=0, sticky=tk.NSEW)
        self.log_view.configure(state=tk.DISABLED)
        self._set_log_contents(self._t("select_device_log_sentence"))

        self.materials_tab_frame = ttk.Frame(self.log_notebook)
        self.materials_tab_frame.columnconfigure(0, weight=1)
        self.materials_tab_frame.rowconfigure(1, weight=1)
        self.log_notebook.add(self.materials_tab_frame, text=self._t("tab_materials"))
        self.materials_status_label = ttk.Label(
            self.materials_tab_frame,
            textvariable=self.materials_status_var,
            foreground="#555555",
        )
        self.materials_status_label.grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 6))
        self.materials_tree = ttk.Treeview(
            self.materials_tab_frame,
            columns=("code", "qty", "status"),
            show="tree headings",
            selectmode="browse",
        )
        self.materials_tree.heading("#0", text=self._t("materials_col_name"))
        self.materials_tree.heading("code", text=self._t("materials_col_code"))
        self.materials_tree.heading("qty", text=self._t("materials_col_qty"))
        self.materials_tree.heading("status", text=self._t("materials_col_status"))
        self.materials_tree.column("#0", width=240, anchor=tk.W, stretch=True)
        self.materials_tree.column("code", width=170, anchor=tk.W, stretch=False)
        self.materials_tree.column("qty", width=56, anchor=tk.CENTER, stretch=False)
        self.materials_tree.column("status", width=90, anchor=tk.CENTER, stretch=False)
        self.materials_tree.tag_configure("group_selected", foreground="#1f7a1f", font=("Microsoft YaHei UI", 9, "bold"))
        self.materials_tree.tag_configure("group_unselected", foreground="#777777", font=("Microsoft YaHei UI", 9, "bold"))
        self.materials_tree.tag_configure("item_selected", foreground="#1f7a1f")
        self.materials_tree.tag_configure("item_missing", foreground="#c0392b")
        self.materials_tree.tag_configure("item_pending", foreground="#a87f1c")
        self.materials_tree.grid(row=1, column=0, sticky=tk.NSEW)
        materials_scroll = ttk.Scrollbar(
            self.materials_tab_frame, orient=tk.VERTICAL, command=self.materials_tree.yview
        )
        materials_scroll.grid(row=1, column=1, sticky=tk.NS)
        self.materials_tree.configure(yscrollcommand=materials_scroll.set)
        self.materials_status_var.set(self._t("materials_select_device"))

        self.measure_tab_frame = ttk.Frame(self.log_notebook)
        self.measure_tab_frame.columnconfigure(0, weight=1)
        self.measure_tab_frame.rowconfigure(1, weight=1)
        self.log_notebook.add(self.measure_tab_frame, text=self._t("tab_measurements"))
        self.measure_status_label = ttk.Label(
            self.measure_tab_frame,
            textvariable=self.measure_status_var,
            foreground="#555555",
        )
        self.measure_status_label.grid(row=0, column=0, columnspan=2, sticky=tk.W, padx=(4, 0), pady=(4, 6))
        self.measure_canvas = tk.Canvas(
            self.measure_tab_frame,
            highlightthickness=0,
            borderwidth=0,
            background=self.measure_tab_frame.winfo_toplevel().cget("background"),
        )
        self.measure_canvas.grid(row=1, column=0, sticky=tk.NSEW)
        measure_scroll = ttk.Scrollbar(
            self.measure_tab_frame, orient=tk.VERTICAL, command=self.measure_canvas.yview
        )
        measure_scroll.grid(row=1, column=1, sticky=tk.NS)
        self.measure_canvas.configure(yscrollcommand=measure_scroll.set)
        self.measure_inner = ttk.Frame(self.measure_canvas, padding=(4, 0, 4, 8))
        self.measure_window = self.measure_canvas.create_window((0, 0), window=self.measure_inner, anchor="nw")
        self.measure_inner.bind(
            "<Configure>",
            lambda _e: self.measure_canvas.configure(scrollregion=self.measure_canvas.bbox("all")),
        )
        self.measure_canvas.bind(
            "<Configure>",
            lambda e: self.measure_canvas.itemconfigure(self.measure_window, width=e.width),
        )

        def _on_measure_enter(_e):
            self.measure_canvas.bind_all("<MouseWheel>", _on_measure_wheel)

        def _on_measure_leave(_e):
            self.measure_canvas.unbind_all("<MouseWheel>")

        def _on_measure_wheel(e):
            self.measure_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")

        self.measure_canvas.bind("<Enter>", _on_measure_enter)
        self.measure_canvas.bind("<Leave>", _on_measure_leave)
        self.measure_status_var.set(self._t("measure_select_device"))

        self.log_notebook.bind("<<NotebookTabChanged>>", self._on_log_tab_changed)

        status_bar = ttk.Frame(main, relief=tk.SUNKEN, padding=(6, 2))
        status_bar.grid(row=2, column=0, sticky=tk.EW, pady=(8, 0))
        ttk.Label(status_bar, textvariable=self.status_var).pack(side=tk.LEFT)
        self.language_combo = ttk.Combobox(
            status_bar,
            textvariable=self.language_var,
            values=LANGUAGE_OPTIONS,
            width=12,
            state="readonly",
        )
        self.language_combo.pack(side=tk.RIGHT)
        self.language_combo.bind("<<ComboboxSelected>>", self._on_language_changed)
        self.sound_check = ttk.Checkbutton(
            status_bar,
            text=self._t("sound"),
            variable=self.sound_enabled_var,
            command=self._on_sound_toggle,
        )
        self._register_text("sound", self.sound_check)
        self.sound_check.pack(side=tk.RIGHT, padx=(0, 12))
        status_counts = ttk.Frame(status_bar)
        status_counts.pack(side=tk.RIGHT, padx=(0, 10))
        tk.Label(
            status_counts,
            textvariable=self.success_count_var,
            foreground=self.ROW_SUCCESS_FG,
            padx=4,
        ).pack(side=tk.LEFT)
        tk.Label(
            status_counts,
            textvariable=self.failed_count_var,
            foreground=self.ROW_FAILED_FG,
            padx=4,
        ).pack(side=tk.LEFT)

    def _on_language_changed(self, _event=None) -> None:
        self._apply_language()

    def _apply_language(self) -> None:
        self.root.title(self._t("app_title"))
        for key, widget in self._text_widgets.items():
            try:
                widget.configure(text=self._t(key))
            except Exception:
                pass
        if self.device_tree is not None:
            self.device_tree.heading("status", text=self._t("tree_status"))
            self.device_tree.heading("elapsed", text=self._t("tree_elapsed"))
            self.device_tree.heading("progress", text=self._t("tree_progress"))
            self.device_tree.heading("step", text=self._t("tree_step"))
        if self.log_notebook is not None:
            if self.log_tab_frame is not None:
                self.log_notebook.tab(self.log_tab_frame, text=self._t("tab_log"))
            if self.materials_tab_frame is not None:
                self.log_notebook.tab(self.materials_tab_frame, text=self._t("tab_materials"))
            if self.measure_tab_frame is not None:
                self.log_notebook.tab(self.measure_tab_frame, text=self._t("tab_measurements"))
        if self.materials_tree is not None:
            self.materials_tree.heading("#0", text=self._t("materials_col_name"))
            self.materials_tree.heading("code", text=self._t("materials_col_code"))
            self.materials_tree.heading("qty", text=self._t("materials_col_qty"))
            self.materials_tree.heading("status", text=self._t("materials_col_status"))
        for task in self.devices.values():
            task.status = self._status_text(task.state_code)
            self._refresh_device_row(task)
        self._refresh_summary()
        if self.selected_task_id is None:
            self._clear_materials_tab(self._t("materials_select_device"))
            self._clear_measurements_tab(self._t("measure_select_device"))
        else:
            task = self.devices.get(self.selected_task_id)
            if task is not None:
                self._refresh_materials_tab(task)
                self._refresh_measurements_tab(task)
        self._refresh_action_states()

    def _on_sound_toggle(self) -> None:
        if self.sound_enabled_var.get():
            self._play_completion_sound(success=True)

    def _attach_logger_sink(self) -> None:
        remove_default_sinks()
        logger.add(
            self._log_sink,
            level="INFO",
            format="{time:HH:mm:ss} | {level:<7} | {message}",
        )

    def _log_sink(self, message) -> None:
        record = message.record
        task_id = record["extra"].get("task_id")
        if not task_id:
            return
        self.ui_queue.put({"type": "log", "task_id": task_id, "message": str(message)})

    def _drain_ui_queue(self) -> None:
        try:
            while True:
                event = self.ui_queue.get_nowait()
                self._handle_ui_event(event)
        except queue.Empty:
            pass
        self.root.after(100, self._drain_ui_queue)

    def _handle_ui_event(self, event: dict) -> None:
        event_type = event.get("type")
        if event_type == "log":
            self._handle_log_event(event)
        elif event_type == "local_log":
            self._append_local_log(str(event.get("task_id")), str(event.get("message") or ""))
        elif event_type == "attempt_started":
            self._handle_attempt_started(event)
        elif event_type in {"stage", "progress", "finished", "identity"}:
            self._handle_task_event(event)
        elif event_type == "browser_ready":
            self._handle_browser_ready(event)
        elif event_type == "browser_closed":
            self._handle_browser_closed(event)
        elif event_type == "thread_done":
            self.workers.pop(str(event.get("task_id")), None)
            self._refresh_summary()
            self._refresh_action_states()
        elif event_type == "auto_scan_done":
            self._finish_auto_scan(event)
        elif event_type == "form_material_refresh":
            self.status_var.set(str(event.get("message") or ""))
        elif event_type == "confirm_previous_step":
            self._handle_confirm_previous_step(event)
        elif event_type == "confirm_disk_shortage":
            self._handle_confirm_disk_shortage(event)
        elif event_type == "resolve_form_grade":
            self._handle_resolve_form_grade(event)
        elif event_type == "refresh_task_settings":
            self._handle_refresh_task_settings(event)
        elif event_type == "update_available":
            self._handle_update_available(event)
        elif event_type == "update_progress":
            self._handle_update_progress(event)
        elif event_type == "update_failed":
            self._handle_update_failed(event)
        elif event_type == "update_installing":
            self._handle_update_installing(event)

    def _handle_update_available(self, event: dict) -> None:
        update = event.get("update")
        if not isinstance(update, UpdateInfo):
            return
        if self._pending_update is not None or self._update_install_active:
            return
        self._pending_update = update
        current = format_version()
        latest = f"{update.version_name} ({update.version_code})"
        body = self._t("update_available_body", current=current, latest=latest)
        if update.notes:
            body = f"{body}\n\n{update.notes}"
        title = self._t("update_available_title")
        try:
            confirm = messagebox.askyesno(
                title,
                body,
                icon=messagebox.INFO,
                detail=self._t("update_install_button"),
            )
        except tk.TclError:
            self._pending_update = None
            return
        if not confirm:
            self._pending_update = None
            return
        self._update_install_active = True
        self.update_manager.download_and_install(update)

    def _handle_update_progress(self, event: dict) -> None:
        self.status_var.set(str(event.get("message") or ""))

    def _handle_update_failed(self, event: dict) -> None:
        self._pending_update = None
        self._update_install_active = False
        try:
            messagebox.showerror(
                self._t("update_failed_title"),
                str(event.get("message") or ""),
            )
        except tk.TclError:
            pass

    def _handle_update_installing(self, event: dict) -> None:
        self._pending_update = None
        version_name = str(event.get("version_name") or "")
        try:
            messagebox.showinfo(
                self._t("update_installing_title"),
                self._t("update_installing_body", version=version_name),
            )
        except tk.TclError:
            pass
        try:
            self.root.after(100, self._on_close)
        except tk.TclError:
            pass

    def _handle_log_event(self, event: dict) -> None:
        task = self.devices.get(str(event.get("task_id")))
        if task is None:
            return
        message = str(event.get("message") or "")
        if message and not message.endswith("\n"):
            message += "\n"
        task.logs.append(message)
        if task.task_id == self.selected_task_id:
            self._append_log(message)
            self._schedule_timing_chart_refresh(task)

    def _handle_confirm_previous_step(self, event: dict) -> None:
        reply = event.get("reply")
        done = event.get("done")
        task = self.devices.get(str(event.get("task_id")))
        event_sn = normalize_sn(str(event.get("sn") or ""))
        if task is not None and event_sn and (
            same_sn_identity(task.sn, event_sn) or is_auto_sn_placeholder(task.sn)
        ):
            task.sn = event_sn
        sn = event_sn or (task.sn if task is not None else "")
        answer = messagebox.askyesno(
            "缺少第一步",
            f"SN {sn} 缺少第一步翻新记录。是否自动录入第一步后继续第二步？",
        )
        if isinstance(reply, dict):
            reply["answer"] = bool(answer)
        if done is not None:
            done.set()

    def _handle_resolve_form_grade(self, event: dict) -> None:
        reply = event.get("reply")
        done = event.get("done")
        task = self.devices.get(str(event.get("task_id")))
        prompted_sn = str(event.get("sn") or (task.sn if task is not None else ""))
        # Live-read the current selector before anything else — this is the
        # whole reason the worker calls us instead of trusting task.form_grade.
        grade = str(self.form_grade_var.get() or "").strip().upper()
        if grade not in {"A", "B"}:
            # Fall back to whatever the task was queued with, then prompt the
            # operator if we still don't have a real choice. Never silently
            # default to "A" — earlier behaviour did, and it shipped wrong
            # P/Ns when the operator forgot to pick a grade.
            queued = str((task.form_grade if task is not None else "") or "").strip().upper()
            if queued in {"A", "B"}:
                grade = queued
            else:
                grade = self._ensure_grade_selected(reason_sn=prompted_sn) or ""
        if grade not in {"A", "B"}:
            grade = "A"  # last-resort safety — never crash the test
        if task is not None:
            task.form_grade = grade
        if isinstance(reply, dict):
            reply["grade"] = grade
        if done is not None:
            done.set()

    def _handle_refresh_task_settings(self, event: dict) -> None:
        """Update a queued task's auto-form / grade / account from current GUI vars.

        Called from the worker thread (via the ui_queue + done event pattern)
        right before ``run_test`` so toggles the operator made after queuing
        actually take effect.
        """
        reply = event.get("reply")
        done = event.get("done")
        task = self.devices.get(str(event.get("task_id")))
        if task is None:
            if done is not None:
                done.set()
            return
        live_auto = bool(self.form_entry_enabled and self.auto_form_entry_var.get())
        live_grade = self._form_grade_choice()
        live_account = self.form_account_var.get().strip()
        if isinstance(reply, dict):
            reply["auto_form_entry"] = live_auto
            reply["form_grade"] = live_grade
            reply["form_account_name"] = live_account
        if done is not None:
            done.set()

    def _ensure_grade_selected(self, *, reason_sn: str = "") -> str | None:
        """Make sure form_grade_var holds A or B. Pops a modal if it doesn't.

        Returns "A"/"B" on success, or None if the operator cancels. Already-
        selected grades are returned without prompting. Safe to call from the
        main thread only — uses ``wait_window``.
        """
        current = self._form_grade_choice()
        if current in {"A", "B"}:
            return current

        dlg = tk.Toplevel(self.root)
        dlg.title(self._t("grade_prompt_title"))
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()
        # Make the dialog refuse to be dismissed by the X — operator must pick.
        dlg.protocol("WM_DELETE_WINDOW", lambda: None)

        body = ttk.Frame(dlg, padding=16)
        body.pack()
        msg = self._t("grade_prompt_body")
        if reason_sn:
            msg = f"{msg}\nSN: {reason_sn}"
        ttk.Label(body, text=msg, justify=tk.LEFT).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 12)
        )

        picked = {"grade": None}

        def _choose(grade: str) -> None:
            picked["grade"] = grade
            self.form_grade_var.set(grade)
            dlg.destroy()

        ttk.Button(
            body, text=self._t("grade_a_button"), width=20, command=lambda: _choose("A")
        ).grid(row=1, column=0, padx=4, pady=4)
        ttk.Button(
            body, text=self._t("grade_b_button"), width=20, command=lambda: _choose("B")
        ).grid(row=1, column=1, padx=4, pady=4)

        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.lift()
        dlg.focus_set()
        self.root.wait_window(dlg)
        return picked["grade"]

    def _handle_confirm_disk_shortage(self, event: dict) -> None:
        reply = event.get("reply")
        done = event.get("done")
        task = self.devices.get(str(event.get("task_id")))
        sn = task.sn if task is not None else str(event.get("sn") or "")
        pool_name = str(event.get("pool_name") or "")
        expected = "\u3001".join(str(disk) for disk in event.get("expected_disks") or [])
        available = "\u3001".join(str(disk) for disk in event.get("available_disks") or [])
        visible = "\u3001".join(str(disk) for disk in event.get("visible_disks") or [])
        visible_display = visible or "\u65e0"
        missing = "\u3001".join(str(disk) for disk in event.get("missing_disks") or [])
        fallback = str(event.get("fallback_raid") or "")
        if bool(event.get("m2_missing")):
            messagebox.showerror(
                str(event.get("title") or "M.2未插入"),
                str(event.get("message") or "请确认是否插入固态硬盘"),
            )
            if isinstance(reply, dict):
                reply["answer"] = False
            if done is not None:
                done.set()
            return
        if not bool(event.get("can_continue", True)):
            messagebox.showerror(
                "\u786c\u76d8\u672a\u8bc6\u522b",
                f"SN {sn}\n"
                f"{pool_name} \u6ca1\u6709\u68c0\u6d4b\u5230\u4efb\u4f55\u914d\u7f6e\u786c\u76d8\u3002\n\n"
                f"\u5e94\u6709\uff1a{expected}\n"
                f"\u5f53\u524d\u53ef\u89c1\uff1a{visible_display}\n"
                f"\u7f3a\u5c11\uff1a{missing}\n\n"
                "\u8bf7\u68c0\u67e5\u786c\u76d8\u662f\u5426\u63d2\u597d\u3001\u662f\u5426\u88ab\u7cfb\u7edf\u8bc6\u522b\uff0c"
                "\u5904\u7406\u540e\u91cd\u65b0\u6d4b\u8bd5\u3002",
            )
            if isinstance(reply, dict):
                reply["answer"] = False
            if done is not None:
                done.set()
            return
        answer = messagebox.askyesno(
            "\u786c\u76d8\u6570\u91cf\u4e0d\u8db3",
            f"SN {sn}\n"
            f"{pool_name} \u68c0\u6d4b\u5230\u7f3a\u76d8\u3002\n\n"
            f"\u5e94\u6709\uff1a{expected}\n"
            f"\u53ef\u7528\uff1a{available}\n"
            f"\u7f3a\u5c11\uff1a{missing}\n\n"
            f"\u662f\u5426\u7ee7\u7eed\uff1f\u7ee7\u7eed\u5c06\u4f7f\u7528 {fallback} \u521b\u5efa\u5b58\u50a8\u6c60\uff1b\u53d6\u6d88\u5c06\u4e2d\u65ad\u672c\u6b21\u6d4b\u8bd5\u3002",
        )
        if isinstance(reply, dict):
            reply["answer"] = bool(answer)
        if done is not None:
            done.set()

    def _handle_attempt_started(self, event: dict) -> None:
        task = self.devices.get(str(event.get("task_id")))
        if task is None:
            return
        task.attempt = int(event.get("attempt") or task.attempt or 1)
        task.max_attempts = int(event.get("max_attempts") or task.max_attempts)
        task.state_code = "running"
        task.status = self._status_text("running")
        task.progress = 0
        task.current_step = f"第 {task.attempt}/{task.max_attempts} 次测试启动"
        task.finished_monotonic = None
        self._refresh_device_row(task)
        self._refresh_summary()
        self._refresh_action_states()
        self._save_queue_state()

    def _handle_task_event(self, event: dict) -> None:
        task = self.devices.get(str(event.get("task_id")))
        if task is None:
            return

        status_code = str(event.get("status") or task.state_code)
        previous_sn = task.sn
        task.state_code = status_code
        task.status = self._status_text(status_code)
        event_sn = normalize_sn(str(event.get("sn") or ""))
        if event_sn and event_sn != task.sn and (
            same_sn_identity(task.sn, event_sn) or is_auto_sn_placeholder(task.sn)
        ):
            task.sn = event_sn
        task.current_step = str(event.get("stage") or task.current_step)
        task.progress = int(event.get("percent") or task.progress)

        nas_ip = str(event.get("nas_ip") or "").strip()
        if nas_ip and nas_ip.lower() != "auto":
            task.actual_ip = nas_ip

        if event.get("type") == "finished" and status_code == "success":
            task.progress = 100
            task.finished_monotonic = time.monotonic()
            self._play_completion_sound(success=True)
            if self.auto_print_label_var.get() and task.sn:
                try:
                    self._submit_label_print(task.sn, silent=True)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"auto-print SN sticker failed for SN={task.sn}: {exc}")
                try:
                    self._submit_nameplate_print(task.sn, silent=True)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"auto-print nameplate failed for SN={task.sn}: {exc}")
                try:
                    self._submit_ean13_print(task.sn, silent=True)
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"auto-print ean13 failed for SN={task.sn}: {exc}")
        if event.get("type") == "finished" and status_code == "failed":
            task.finished_monotonic = time.monotonic()
            self._show_failure_alert_if_needed(task, str(event.get("error") or ""))
            self._play_completion_sound(success=False)
        if event.get("type") == "finished" and status_code == "cancelled":
            task.finished_monotonic = time.monotonic()
            self._play_completion_sound(success=False)
        if event.get("type") == "finished" and status_code in {"success", "failed"}:
            self._record_daily_task_result(task, status_code)

        self._refresh_device_row(task)
        if previous_sn != task.sn:
            self._refresh_device_rows_for_sn(previous_sn)
        self._refresh_device_rows_for_task_identity(task)
        self._refresh_summary()
        self._refresh_action_states()
        self._save_queue_state()
        if (
            event.get("type") == "finished"
            and task.task_id == self.selected_task_id
            and status_code in {"success", "failed"}
        ):
            self._refresh_materials_tab(task)
            self._schedule_materials_refresh()
            self._refresh_measurements_tab(task)
            self._schedule_measurements_refresh()

    def _show_failure_alert_if_needed(self, task: DeviceTask, error: str) -> None:
        if is_unflashed_password_error(error):
            if UNFLASHED_MESSAGE in task.shown_alerts:
                return
            task.shown_alerts.add(UNFLASHED_MESSAGE)
            messagebox.showerror(UNFLASHED_TITLE, UNFLASHED_MESSAGE)
            return

        message = "SN\u672a\u89e3\u7ed1\uff0c\u8bf7\u5148\u89e3\u7ed1SN"
        if message not in error or message in task.shown_alerts:
            previous_step = "缺少第一步翻新记录"
            if previous_step not in error or previous_step in task.shown_alerts:
                return
            task.shown_alerts.add(previous_step)
            messagebox.showerror("缺少第一步", "请先在系统里录入第一步后再自动录入第二步。")
            return
        task.shown_alerts.add(message)
        messagebox.showerror("SN\u672a\u89e3\u7ed1", message)

    def _has_completed_output_for_sn(self, sn: str) -> bool:
        normalized = normalize_sn(sn)
        if not normalized or is_auto_sn_placeholder(normalized):
            return False
        try:
            output_root = self._output_root()
        except Exception:
            return False
        if not output_root.exists():
            return False

        for sn_root in output_root.iterdir():
            if not sn_root.is_dir() or not same_sn_identity(sn_root.name, normalized):
                continue
            if self._sn_output_has_success_report(sn_root, normalized):
                return True
        return False

    def _sn_output_has_images(self, sn_root: Path) -> bool:
        image_dir = sn_root / "图片"
        if not image_dir.is_dir():
            return False
        return any(path.is_file() and path.suffix.lower() == ".png" and "_FAIL_" not in path.name for path in image_dir.iterdir())

    def _sn_output_has_success_report(self, sn_root: Path, sn: str) -> bool:
        report = self._read_report(sn_root / "test_report.json")
        if not report or str(report.get("status") or "").lower() != "success":
            return False
        report_sn = normalize_sn(str(report.get("sn") or ""))
        if not (report_sn and same_sn_identity(report_sn, sn)):
            return False
        if bool(report.get("auto_form_entry")):
            form_result = report.get("form_result") if isinstance(report.get("form_result"), dict) else {}
            form_status = str(form_result.get("status") or "").lower()
            return form_status in {"success", "already_submitted"}
        return True

    def _read_report(self, report_path: Path) -> dict | None:
        try:
            return json.loads(report_path.read_text(encoding="utf-8-sig"))
        except Exception:
            return None

    def _current_step_from_report(self, status: str, report: dict) -> str:
        current_stage = str(report.get("current_stage") or "").strip()
        error = str(report.get("error") or "").strip()
        if status == "failed":
            derived = failure_stage_for_error(error) if error else ""
            if derived and derived != "测试失败":
                return derived
            if current_stage and "完成" not in current_stage:
                return current_stage
            return derived or error or current_stage or "Restored from output report"
        return current_stage or error or "Restored from output report"

    def _play_completion_sound(self, success: bool) -> None:
        if not self.sound_enabled_var.get():
            return
        try:
            if sys.platform.startswith("win"):
                import winsound

                pattern = ((880, 120), (1175, 160)) if success else ((440, 180), (330, 240))
                for frequency, duration in pattern:
                    winsound.Beep(frequency, duration)
            else:
                self.root.bell()
        except Exception:
            try:
                self.root.bell()
            except Exception:
                pass

    def _handle_browser_ready(self, event: dict) -> None:
        task = self.devices.get(str(event.get("task_id")))
        if task is None:
            return
        browser_pid = event.get("browser_pid")
        task.browser_pid = int(browser_pid) if browser_pid else None
        self._append_local_log(task.task_id, "浏览器已在后台启动，可按“显示浏览器”查看")
        self._refresh_action_states()

    def _handle_browser_closed(self, event: dict) -> None:
        task = self.devices.get(str(event.get("task_id")))
        if task is None:
            return
        task.browser_pid = None
        self._refresh_action_states()

    def _refresh_device_row(self, task: DeviceTask) -> None:
        if self.device_tree is None or not self.device_tree.exists(task.task_id):
            return
        self.device_tree.item(
            task.task_id,
            values=(
                task.sn,
                task.display_ip,
                task.status,
                task.elapsed_display,
                f"{task.progress}%",
                task.current_step,
            ),
            tags=self._row_tags_for_task(task),
        )

    def _insert_device_row(self, task: DeviceTask, select: bool) -> None:
        if self.device_tree is None:
            return
        self.device_tree.insert(
            "",
            tk.END,
            iid=task.task_id,
            values=(task.sn, task.display_ip, task.status, task.elapsed_display, f"{task.progress}%", task.current_step),
            tags=self._row_tags_for_task(task),
        )
        if select:
            self.device_tree.selection_set(task.task_id)
            self.device_tree.focus(task.task_id)

    def _row_tags_for_task(self, task: DeviceTask) -> tuple[str, ...]:
        if task.state_code == "success":
            return (self.ROW_SUCCESS_TAG,)
        needs_retry = task.state_code == "failed" or task.interrupted_on_exit
        if needs_retry and not self._has_later_task_for_same_device(task):
            return (self.ROW_FAILED_TAG,)
        return ()

    def _has_later_task_for_same_device(self, task: DeviceTask) -> bool:
        after_current = False
        for other in self.devices.values():
            if other.task_id == task.task_id:
                after_current = True
                continue
            if after_current and self._same_device_identity(task, other):
                return True
        return False

    def _same_device_identity(self, left: DeviceTask, right: DeviceTask) -> bool:
        if same_sn_identity(left.sn, right.sn):
            return True
        if is_auto_sn_placeholder(left.sn) or is_auto_sn_placeholder(right.sn):
            return bool(self._task_identity_ips(left) & self._task_identity_ips(right))
        return False

    def _task_identity_ips(self, task: DeviceTask) -> set[str]:
        ips = {
            str(task.requested_ip or "").strip(),
            str(task.actual_ip or "").strip(),
            *(str(ip or "").strip() for ip in task.reserved_ips),
        }
        return {ip for ip in ips if ip and ip.lower() != "auto"}

    def _refresh_device_rows_for_sn(self, sn: str) -> None:
        normalized = normalize_sn(sn)
        if not normalized:
            return
        for task in self.devices.values():
            if same_sn_identity(task.sn, normalized) or task.sn == normalized:
                self._refresh_device_row(task)

    def _refresh_device_rows_for_task_identity(self, target: DeviceTask) -> None:
        for task in self.devices.values():
            if task.task_id == target.task_id or self._same_device_identity(task, target):
                self._refresh_device_row(task)

    def _daily_task_keys(self, task: DeviceTask) -> list[str]:
        keys: list[str] = []
        normalized_sn = normalize_sn(task.sn)
        tail = sn_tail(normalized_sn)
        if normalized_sn and not is_auto_sn_placeholder(normalized_sn) and len(tail) >= 4:
            keys.append(f"sn:{tail}")
        keys.extend(f"ip:{ip}" for ip in sorted(self._task_identity_ips(task)))
        if not keys:
            keys.append(f"task:{task.task_id}")
        return keys

    def _preferred_daily_task_key(self, task: DeviceTask) -> str:
        keys = self._daily_task_keys(task)
        return next((key for key in keys if key.startswith("sn:")), keys[0])

    def _daily_entry_key_for_task(self, task: DeviceTask) -> str | None:
        for key in self._daily_task_keys(task):
            if key in self.daily_stats_devices:
                return key
        return None

    def _mark_daily_task_retry_pending(self, task: DeviceTask) -> None:
        self._ensure_daily_stats_current()
        existing_key = self._daily_entry_key_for_task(task)
        if existing_key is None:
            return
        entry = dict(self.daily_stats_devices.get(existing_key) or {})
        if str(entry.get("status") or "") != "failed":
            return
        preferred_key = self._preferred_daily_task_key(task)
        if preferred_key != existing_key:
            self.daily_stats_devices.pop(existing_key, None)
        entry.update(self._daily_task_payload(task, "retrying"))
        self.daily_stats_devices[preferred_key] = entry
        self._save_daily_stats()
        self._refresh_status_counts()

    def _record_daily_task_result(self, task: DeviceTask, status_code: str) -> None:
        if status_code not in {"success", "failed"}:
            return
        self._ensure_daily_stats_current()
        existing_key = self._daily_entry_key_for_task(task)
        preferred_key = self._preferred_daily_task_key(task)
        entry = dict(self.daily_stats_devices.get(existing_key or preferred_key) or {})
        if existing_key and existing_key != preferred_key:
            self.daily_stats_devices.pop(existing_key, None)
        entry.update(self._daily_task_payload(task, status_code))
        self.daily_stats_devices[preferred_key] = entry
        self._save_daily_stats()

    def _daily_task_payload(self, task: DeviceTask, status_code: str) -> dict[str, str]:
        return {
            "sn": task.sn,
            "ip": task.display_ip,
            "status": status_code,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
        }

    def _daily_status_counts(self) -> tuple[int, int]:
        self._ensure_daily_stats_current()
        success = sum(1 for entry in self.daily_stats_devices.values() if entry.get("status") == "success")
        failed = sum(1 for entry in self.daily_stats_devices.values() if entry.get("status") == "failed")
        return success, failed

    def _refresh_elapsed_times(self) -> None:
        for task in self.devices.values():
            if task.state_code in self.ACTIVE_STATES:
                self._refresh_device_row(task)
        self._refresh_status_counts()
        self.root.after(1000, self._refresh_elapsed_times)

    def _append_local_log(self, task_id: str, message: str) -> None:
        task = self.devices.get(task_id)
        if task is None:
            return
        line = f"{datetime.now().strftime('%H:%M:%S')} | INFO    | {message}\n"
        task.logs.append(line)
        if task.task_id == self.selected_task_id:
            self._append_log(line)
            self._schedule_timing_chart_refresh(task)

    def _set_log_contents(self, text: str) -> None:
        if self.log_view is None:
            return
        self.log_view.configure(state=tk.NORMAL)
        self.log_view.delete("1.0", tk.END)
        self.log_view.insert(tk.END, text)
        self.log_view.see(tk.END)
        self.log_view.configure(state=tk.DISABLED)

    def _append_log(self, msg: str) -> None:
        if self.log_view is None:
            return
        self.log_view.configure(state=tk.NORMAL)
        self.log_view.insert(tk.END, msg)
        self.log_view.see(tk.END)
        self.log_view.configure(state=tk.DISABLED)

    def _clear_materials_tab(self, status_text: str = "") -> None:
        if self.materials_tree is None:
            return
        for item in self.materials_tree.get_children():
            self.materials_tree.delete(item)
        self.materials_status_var.set(status_text)

    def _clear_measurements_tab(self, status_text: str = "") -> None:
        measure_inner = getattr(self, "measure_inner", None)
        if measure_inner is None:
            return
        for child in measure_inner.winfo_children():
            child.destroy()
        self.measure_status_var.set(status_text)
        measure_canvas = getattr(self, "measure_canvas", None)
        if measure_canvas is not None:
            measure_canvas.yview_moveto(0)

    def _add_measure_section(self, title: str) -> ttk.LabelFrame:
        section = ttk.LabelFrame(self.measure_inner, text=title, padding=(10, 6, 10, 8))
        section.columnconfigure(0, weight=0)
        section.columnconfigure(1, weight=1)
        section.pack(fill=tk.X, padx=2, pady=(0, 8))
        return section

    def _add_measure_row(self, section: ttk.LabelFrame, row_index: int, label: str, value: str, tone: str = "") -> None:
        color_map = {"good": "#1f7a1f", "bad": "#c0392b", "muted": "#777777"}
        value_color = color_map.get(tone, "#222222")
        ttk.Label(section, text=label, foreground="#666666").grid(
            row=row_index, column=0, sticky=tk.W, padx=(0, 16), pady=2
        )
        ttk.Label(
            section,
            text=value,
            foreground=value_color,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).grid(row=row_index, column=1, sticky=tk.W, pady=2)

    def _refresh_measurements_tab(self, task: DeviceTask) -> None:
        if self.measure_inner is None:
            return
        report_path = self._output_root() / task.sn / "test_report.json"
        report = self._read_report(report_path) or {}
        captured = report.get("captured_values") if isinstance(report.get("captured_values"), dict) else None
        if not captured:
            self._clear_measurements_tab(self._t("measure_no_data"))
            return

        self._clear_measurements_tab()
        rendered_any = False

        sys_rows: list[tuple[str, str, str]] = []
        ugos = (captured.get("system_update") or {}).get("ugos_version")
        if ugos:
            sys_rows.append((self._t("measure_ugos_version"), str(ugos), ""))
        link_bps = (captured.get("network_interface") or {}).get("link_bps")
        if link_bps:
            sys_rows.append((self._t("measure_link_bps"), f"{link_bps} Gbps", "good"))
        if sys_rows:
            section = self._add_measure_section(self._t("measure_section_system"))
            for i, (label, value, tone) in enumerate(sys_rows):
                self._add_measure_row(section, i, label, value, tone)
            rendered_any = True

        sp = captured.get("storage_pool") or {}
        storage_rows: list[tuple[str, str, str]] = []
        if sp.get("hdd_pool_raid"):
            storage_rows.append((self._t("measure_hdd_pool"), str(sp["hdd_pool_raid"]), ""))
        if sp.get("ssd_pool_raid"):
            storage_rows.append((self._t("measure_ssd_pool"), str(sp["ssd_pool_raid"]), ""))
        if storage_rows:
            section = self._add_measure_section(self._t("measure_section_storage"))
            for i, (label, value, tone) in enumerate(storage_rows):
                self._add_measure_row(section, i, label, value, tone)
            rendered_any = True

        transfer_rows: list[tuple[str, str, str]] = []
        for key, label_key in (
            ("hdd_write", "measure_hdd_write"),
            ("hdd_read", "measure_hdd_read"),
            ("ssd_write", "measure_ssd_write"),
            ("ssd_read", "measure_ssd_read"),
        ):
            page = captured.get(key) or {}
            rate = page.get("rate")
            if not rate:
                continue
            threshold = page.get("threshold_mbps")
            status = str(page.get("speed_status") or "").lower()
            suffix = (
                f"   ({self._t('measure_threshold')} {threshold} MB/s)" if threshold else ""
            )
            tone = "good" if status == "ok" else ("bad" if status else "")
            transfer_rows.append((self._t(label_key), f"{rate}{suffix}", tone))
        if transfer_rows:
            section = self._add_measure_section(self._t("measure_section_transfer"))
            for i, (label, value, tone) in enumerate(transfer_rows):
                self._add_measure_row(section, i, label, value, tone)
            rendered_any = True

        fan_rows: list[tuple[str, str, str]] = []
        for key, label_key in (
            ("fan_normal", "measure_fan_normal"),
            ("fan_silent", "measure_fan_silent"),
            ("fan_full_speed", "measure_fan_full"),
        ):
            page = captured.get(key) or {}
            temp = page.get("cpu_temp")
            rpm = page.get("device_fan_rpm")
            if not temp and not rpm:
                continue
            parts = [str(p) for p in (temp, rpm) if p]
            fan_rows.append((self._t(label_key), "   |   ".join(parts), ""))
        if fan_rows:
            section = self._add_measure_section(self._t("measure_section_fan"))
            for i, (label, value, tone) in enumerate(fan_rows):
                self._add_measure_row(section, i, label, value, tone)
            rendered_any = True

        if not rendered_any:
            self.measure_status_var.set(self._t("measure_no_data"))
            return

        sn = str(report.get("sn") or task.sn)
        model = str(report.get("form_model") or "")
        parts = [sn]
        if model:
            parts.append(model)
        status_code = str(report.get("status") or "")
        if status_code:
            parts.append(self._status_text(status_code) if status_code in self.STATUS_TEXT else status_code)
        self.measure_status_var.set("  |  ".join(parts))

    def _on_log_tab_changed(self, _event=None) -> None:
        self._schedule_materials_refresh(delay_ms=0)
        self._schedule_measurements_refresh(delay_ms=0)

    def _materials_tab_selected(self) -> bool:
        if self.log_notebook is None or self.materials_tab_frame is None:
            return False
        try:
            return str(self.log_notebook.select()) == str(self.materials_tab_frame)
        except Exception:
            return False

    def _measure_tab_selected(self) -> bool:
        if self.log_notebook is None or self.measure_tab_frame is None:
            return False
        try:
            return str(self.log_notebook.select()) == str(self.measure_tab_frame)
        except Exception:
            return False

    def _schedule_materials_refresh(self, delay_ms: int | None = None) -> None:
        if self.materials_tree is None:
            return
        if not self._materials_tab_selected():
            return
        if self.materials_refresh_after_id is not None:
            return
        delay = self.MATERIALS_REFRESH_MS if delay_ms is None else delay_ms
        self.materials_refresh_after_id = self.root.after(delay, self._run_materials_refresh)

    def _run_materials_refresh(self) -> None:
        self.materials_refresh_after_id = None
        if self.selected_task_id is None or not self._materials_tab_selected():
            return
        task = self.devices.get(self.selected_task_id)
        if task is None:
            return
        self._refresh_materials_tab(task)
        self._schedule_materials_refresh()

    def _schedule_measurements_refresh(self, delay_ms: int | None = None) -> None:
        if self.measure_inner is None:
            return
        if not self._measure_tab_selected():
            return
        if self.measure_refresh_after_id is not None:
            return
        delay = self.MEASURE_REFRESH_MS if delay_ms is None else delay_ms
        self.measure_refresh_after_id = self.root.after(delay, self._run_measurements_refresh)

    def _run_measurements_refresh(self) -> None:
        self.measure_refresh_after_id = None
        if self.selected_task_id is None or not self._measure_tab_selected():
            return
        task = self.devices.get(self.selected_task_id)
        if task is None:
            return
        self._refresh_measurements_tab(task)
        self._schedule_measurements_refresh()

    def _refresh_materials_tab(self, task: DeviceTask) -> None:
        if self.materials_tree is None:
            return
        if not task.auto_form_entry:
            self._clear_materials_tab(self._t("materials_no_form"))
            return
        report_path = self._output_root() / task.sn / "test_report.json"
        report = self._read_report(report_path) or {}
        form_data = report.get("form_data") if isinstance(report.get("form_data"), dict) else None
        if not form_data:
            self._clear_materials_tab(self._t("materials_no_report"))
            return
        material_groups = form_data.get("material_groups") or []
        form_result = report.get("form_result") if isinstance(report.get("form_result"), dict) else {}
        removed_codes = {str(c) for c in (form_result.get("removed_material_codes") or [])}
        form_status = str(form_result.get("status") or "").lower()
        submission_complete = form_status in {"success", "already_submitted"}

        self._clear_materials_tab()
        rendered_any = False
        for group in material_groups:
            title = str(group.get("title") or "")
            if not title:
                continue
            candidates = group.get("items") or []
            total_count = len(candidates)
            if total_count == 0:
                group_tag = "group_unselected"
                group_status = self._t("materials_status_group_disabled")
                count_text = self._t("materials_group_disabled")
                group_node = self.materials_tree.insert(
                    "",
                    tk.END,
                    text=title,
                    values=(count_text, "", group_status),
                    open=False,
                    tags=(group_tag,),
                )
                rendered_any = True
                continue

            kept_codes = [
                str(item.get("code") or "")
                for item in candidates
                if str(item.get("code") or "") and str(item.get("code") or "") not in removed_codes
            ]
            selected_count = len(kept_codes)
            group_tag = "group_selected" if selected_count > 0 else "group_unselected"
            group_status = (
                self._t("materials_status_selected") if selected_count > 0 else self._t("materials_status_unselected")
            )
            count_text = self._t("materials_group_count", selected=selected_count, total=total_count)
            group_node = self.materials_tree.insert(
                "",
                tk.END,
                text=title,
                values=(count_text, "", group_status),
                open=True,
                tags=(group_tag,),
            )
            rendered_any = True
            for item in candidates:
                code = str(item.get("code") or "")
                name = str(item.get("name") or code or "")
                qty = item.get("default_qty") if item.get("default_qty") is not None else item.get("qty")
                qty_text = str(qty) if qty not in (None, "") else ""
                if not submission_complete:
                    item_status = self._t("materials_status_pending")
                    item_tag = "item_pending"
                elif code and code in removed_codes:
                    item_status = self._t("materials_status_missing")
                    item_tag = "item_missing"
                else:
                    item_status = self._t("materials_status_selected")
                    item_tag = "item_selected"
                self.materials_tree.insert(
                    group_node,
                    tk.END,
                    text=name,
                    values=(code, qty_text, item_status),
                    tags=(item_tag,),
                )
        if not rendered_any:
            self.materials_status_var.set(self._t("materials_no_config"))
            return
        grade = str(form_data.get("grade") or "").upper()
        model_label = str(form_data.get("model_label") or form_data.get("model_key") or "")
        parts = [p for p in (model_label, f"{self._t('grade')} {grade}" if grade else "") if p]
        if str(form_result.get("material_selection_mode") or "") == "fresh_full_select_then_retry_remove_missing":
            parts.append(self._t("materials_mode_fresh_full_retry"))
        if not submission_complete:
            parts.append(self._t("materials_submission_pending"))
        elif removed_codes:
            parts.append(self._t("materials_summary_missing", count=len(removed_codes)))
        self.materials_status_var.set(" | ".join(parts))

    def _clear_timing_chart(self) -> None:
        self._cancel_timing_chart_refresh()
        if self.timing_canvas is None:
            return
        self.timing_canvas.delete("all")
        self.timing_canvas.create_text(
            160,
            76,
            text="选择设备后显示耗时",
            fill="#777777",
            font=("Microsoft YaHei UI", 9),
        )

    def _cancel_timing_chart_refresh(self) -> None:
        if self.timing_chart_after_id is None:
            return
        try:
            self.root.after_cancel(self.timing_chart_after_id)
        except Exception:
            pass
        self.timing_chart_after_id = None

    def _schedule_timing_chart_refresh(self, task: DeviceTask) -> None:
        if self.timing_canvas is None or task.task_id != self.selected_task_id:
            return
        if self.timing_chart_after_id is not None:
            return
        self.timing_chart_after_id = self.root.after(
            self.TIMING_CHART_REFRESH_MS, self._run_timing_chart_refresh
        )

    def _run_timing_chart_refresh(self) -> None:
        self.timing_chart_after_id = None
        if self.selected_task_id is None:
            return
        task = self.devices.get(self.selected_task_id)
        if task is None:
            return
        self._refresh_timing_chart(task)

    def _refresh_timing_chart(self, task: DeviceTask) -> None:
        if self.timing_canvas is None:
            return
        slices = build_timing_slices(task.logs)
        canvas = self.timing_canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), int(canvas.cget("width") or 380))
        height = max(canvas.winfo_height(), int(canvas.cget("height") or 152))
        if not slices:
            canvas.create_text(
                width // 2,
                height // 2,
                text="等待更多日志生成耗时图",
                fill="#777777",
                font=("Microsoft YaHei UI", 9),
            )
            return

        total = sum(item.seconds for item in slices)
        display_slices = _compact_timing_slices(slices)
        pie_size = min(112, height - 24)
        pie_left = 12
        pie_top = max(10, (height - pie_size) // 2)
        start_angle = 90.0
        for index, item in enumerate(display_slices):
            extent = -360.0 * (item.seconds / total)
            canvas.create_arc(
                pie_left,
                pie_top,
                pie_left + pie_size,
                pie_top + pie_size,
                start=start_angle,
                extent=extent,
                fill=TIMING_COLORS[index % len(TIMING_COLORS)],
                outline="white",
            )
            start_angle += extent
        canvas.create_oval(pie_left, pie_top, pie_left + pie_size, pie_top + pie_size, outline="#eeeeee")
        canvas.create_text(
            pie_left + pie_size // 2,
            pie_top + pie_size // 2,
            text=format_elapsed(total),
            fill="#333333",
            font=("Microsoft YaHei UI", 9, "bold"),
        )

        legend_x = pie_left + pie_size + 16
        legend_y = 14
        line_height = 18
        for index, item in enumerate(display_slices):
            y = legend_y + index * line_height
            percent = item.seconds / total * 100
            color = TIMING_COLORS[index % len(TIMING_COLORS)]
            canvas.create_rectangle(legend_x, y + 3, legend_x + 10, y + 13, fill=color, outline=color)
            text = f"{item.label} {format_elapsed(item.seconds)} {percent:.0f}%"
            canvas.create_text(
                legend_x + 16,
                y + 8,
                text=text,
                anchor=tk.W,
                fill="#333333",
                font=("Microsoft YaHei UI", 8),
            )

    def _output_root(self):
        output_dir = self.config.get("output_dir", "./screenshot")
        path = (self.project_root / output_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _open_output(self) -> None:
        output_dir = self._output_root()
        if sys.platform.startswith("win"):
            os.startfile(str(output_dir))
        else:
            subprocess.Popen(["xdg-open", str(output_dir)])

    def _on_print_label(self) -> None:
        """Open the label-type chooser for the current SN(s)."""
        sns, source = self._resolve_print_label_sns()
        if not sns:
            if source == "queue_selected_no_sn":
                messagebox.showwarning(
                    "打印标签",
                    "当前选中的设备还没有可打印的完整 SN。\n"
                    "请等待 SN 解析完成，或聚焦上方 SN 框（会取消队列选中）后手动填入。",
                )
                return
            # No queue selected and SN entry empty:铭牌/SN 标贴都依赖 SN，无法打印；
            # 但 69 码只需机型+等级，弹出 6 变体选择器供操作员手动补打。
            self._show_ean13_variant_chooser()
            return
        # Confirm non-full SNs once before opening the chooser.
        for sn in sns:
            if not is_full_sn_candidate(sn):
                if not messagebox.askyesno(
                    "打印标签",
                    f"识别到的不是完整 SN（看起来是后 4 位之类的）：{sn}\n"
                    "确认要按此内容打印吗？",
                ):
                    return
        self._show_print_chooser(sns)

    def _resolve_print_label_sns(self) -> tuple[list[str], str]:
        """Pick the SN(s) to print and a short tag describing where they came from.

        Returns (sns, source) where source is one of:
        ``queue`` (one or more selected with real SNs),
        ``queue_selected_no_sn`` (everything selected lacks a real SN yet),
        ``entry`` (no queue selection, SN entry used), or ``empty``.
        """
        ids = list(self.selected_task_ids or ([self.selected_task_id] if self.selected_task_id else []))
        if ids:
            sns: list[str] = []
            for tid in ids:
                task = self.devices.get(tid)
                if task is None:
                    continue
                queue_sn = normalize_sn(task.sn or "")
                if queue_sn and not is_auto_sn_placeholder(queue_sn) and queue_sn not in sns:
                    sns.append(queue_sn)
            if sns:
                return sns, "queue"
            return [], "queue_selected_no_sn"
        raw = self.sn_var.get().strip() if self.sn_entry is not None else ""
        # The SN box accepts several SNs separated by EN/CN comma/semicolon or
        # whitespace (needed for the 4-SN turnover-box label); a single SN still
        # yields a one-element list.
        entry_sns = label_util.split_carton_sns(raw)
        if entry_sns:
            return entry_sns, "entry"
        return [], "empty"

    def _show_print_chooser(self, sns: list[str]) -> None:
        """Modal with 4 label-type checkboxes + 打印 button.

        铭牌 / SN 标贴 / 69 码 are per-SN (printed once for each SN). 周转箱
        (turnover box) is a single box-level label for exactly 4 same-model SNs,
        so its checkbox is enabled only then; otherwise it's grayed out.
        """
        dlg = tk.Toplevel(self.root)
        dlg.title(self._t("print_chooser_title"))
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()

        body = ttk.Frame(dlg, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(body, text=self._t("print_chooser_for", count=len(sns))).grid(
            row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 4)
        )
        preview = ", ".join(sns[:6])
        if len(sns) > 6:
            preview += f" … (+{len(sns) - 6})"
        ttk.Label(body, text=preview, foreground="#444").grid(
            row=1, column=0, columnspan=2, sticky=tk.W, pady=(0, 10)
        )

        # Checkbox state — default the per-SN types to ticked so the common
        # "print everything" case is one click.
        nameplate_var = tk.BooleanVar(value=True)
        sn_var = tk.BooleanVar(value=True)
        ean13_var = tk.BooleanVar(value=True)
        # 周转箱: one label per box of exactly 4 same-model SNs. Default it on
        # only when eligible; otherwise it's grayed out with a reason.
        carton_ok, carton_reason = self._carton_eligibility(sns)
        carton_var = tk.BooleanVar(value=carton_ok)

        ttk.Checkbutton(body, text=self._t("label_nameplate"), variable=nameplate_var).grid(
            row=2, column=0, sticky=tk.W, padx=4, pady=2
        )
        ttk.Checkbutton(body, text=self._t("label_sn"), variable=sn_var).grid(
            row=2, column=1, sticky=tk.W, padx=4, pady=2
        )
        ttk.Checkbutton(body, text=self._t("label_ean13"), variable=ean13_var).grid(
            row=3, column=0, sticky=tk.W, padx=4, pady=2
        )
        ttk.Checkbutton(
            body, text=self._t("label_carton"), variable=carton_var,
            state=("normal" if carton_ok else "disabled"),
        ).grid(row=3, column=1, sticky=tk.W, padx=4, pady=2)
        if not carton_ok:
            ttk.Label(body, text=f"周转箱：{carton_reason}", foreground="#a00").grid(
                row=4, column=0, columnspan=2, sticky=tk.W, padx=4, pady=(0, 2)
            )
        for col in range(2):
            body.columnconfigure(col, weight=1, minsize=150)

        def _print_each(submit_fn) -> None:
            for sn in sns:
                submit_fn(sn, silent=False)

        def _on_confirm() -> None:
            picks = {
                "nameplate": nameplate_var.get(),
                "sn": sn_var.get(),
                "ean13": ean13_var.get(),
                "carton": carton_var.get(),
            }
            if not any(picks.values()):
                messagebox.showwarning(
                    self._t("print_chooser_title"),
                    "请至少勾选一项标签。",
                    parent=dlg,
                )
                return
            dlg.destroy()
            if picks["nameplate"]:
                _print_each(self._submit_nameplate_print)
            if picks["sn"]:
                _print_each(self._submit_label_print)
            if picks["ean13"]:
                _print_each(self._submit_ean13_print)
            if picks["carton"]:
                self._submit_turnover_print(sns, silent=False)

        button_row = ttk.Frame(body)
        button_row.grid(row=5, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(button_row, text=self._t("cancel"), command=dlg.destroy).pack(
            side=tk.RIGHT, padx=(6, 0)
        )
        ttk.Button(
            button_row, text=self._t("print_label"), command=_on_confirm
        ).pack(side=tk.RIGHT)

        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.lift()
        dlg.focus_set()

    def _submit_label_print(self, sn: str, *, silent: bool) -> bool:
        """Shared print path used by the manual button and auto-print hook.

        ``silent=True`` suppresses dialogs and just logs / updates the status
        bar — used after a successful auto test.
        """
        normalized = normalize_sn(sn)
        if not normalized or is_auto_sn_placeholder(normalized) or not is_full_sn_candidate(normalized):
            if not silent:
                messagebox.showwarning("打印标签", f"SN 不可用于打印：{normalized or sn!r}")
            return False

        cfg = (self.config.get("label_printer") or {}) if isinstance(self.config, dict) else {}
        printer_pref = str(cfg.get("name") or "").strip() or None
        try:
            dpi = int(cfg.get("dpi") or label_util.DEFAULT_DPI)
        except (TypeError, ValueError):
            dpi = label_util.DEFAULT_DPI
        try:
            quantity = max(1, int(cfg.get("quantity") or 2))
        except (TypeError, ValueError):
            quantity = 2

        try:
            zpl = label_util.build_zpl(normalized, dpi=dpi, quantity=quantity)
        except ValueError as exc:
            if silent:
                logger.error(f"auto-print: build_zpl failed for SN={normalized}: {exc}")
            else:
                messagebox.showerror("打印标签", f"生成 ZPL 失败：{exc}")
            return False

        if not label_util.is_windows():
            preview_path = self._output_root() / f"label_{normalized}.png"
            try:
                label_util.render_preview_png(normalized, preview_path)
            except ImportError as exc:
                if not silent:
                    messagebox.showinfo(
                        "打印标签",
                        f"当前不是 Windows，无法发送到打印机。\n\n"
                        f"需要装 python-barcode + Pillow 才能在本机生成预览：\n{exc}",
                    )
                return False
            if not silent:
                messagebox.showinfo(
                    "打印标签（非 Windows 预览）",
                    f"非 Windows 主机不能直接发打印队列。\n已生成预览 PNG：\n{preview_path}",
                )
            return False

        target = label_util.find_label_printer(printer_pref)
        if not target:
            queues = label_util.list_windows_printers()
            names = "\n".join(p["name"] for p in queues) or "(无)"
            if silent:
                logger.error(f"auto-print: no Zebra printer found. Queues: {names}")
            else:
                messagebox.showerror(
                    "打印标签",
                    "未找到 Zebra 打印机。\n"
                    "请在 config.yml 的 label_printer.name 里写明打印队列名。\n\n"
                    f"当前 Windows 上已安装的队列：\n{names}",
                )
            return False

        try:
            label_util.send_to_windows_printer(target, zpl, job_name=f"SN {normalized}")
        except Exception as exc:  # noqa: BLE001
            if silent:
                logger.error(f"auto-print: send to '{target}' failed: {exc}")
            else:
                messagebox.showerror("打印标签", f"发送到打印机失败：{exc}")
            return False

        prefix = "自动" if silent else "已"
        self.status_var.set(
            f"{datetime.now().strftime('%H:%M:%S')}  {prefix}发送 {quantity} 张标签到 {target}（SN={normalized}）"
        )
        return True

    def _submit_nameplate_print(self, sn: str, *, silent: bool) -> bool:
        """Send one nameplate (83.7×36.7mm) to the ZT610 with auto P/N lookup.

        Returns False if P/N can't be resolved (model unknown or grade not
        selected) — caller should treat that as a soft skip, not a hard fail.
        """
        normalized = normalize_sn(sn)
        if not normalized or is_auto_sn_placeholder(normalized) or not is_full_sn_candidate(normalized):
            return False

        model_key = model_key_from_sn(normalized)
        grade = (self.form_grade_var.get() or "").strip().upper() or None
        pn = label_util.lookup_pn(model_key, grade)
        if not pn:
            msg = (
                f"无法解析铭牌 P/N (model={model_key!r} grade={grade!r})。"
                "请先在「等级」里选 A 或 B，并确认 SN 对应的机型在查表里。"
            )
            if silent:
                logger.warning(f"auto-print nameplate skipped: {msg}")
            else:
                messagebox.showwarning("打印铭牌", msg)
            return False

        cfg = (self.config.get("nameplate_printer") or {}) if isinstance(self.config, dict) else {}
        printer_pref = str(cfg.get("name") or "").strip() or None
        try:
            dpi = int(cfg.get("dpi") or label_util.NAMEPLATE_DPI)
        except (TypeError, ValueError):
            dpi = label_util.NAMEPLATE_DPI
        try:
            quantity = max(1, int(cfg.get("quantity") or 1))
        except (TypeError, ValueError):
            quantity = 1
        qr_xy = list(cfg.get("qr_top_left_mm") or label_util.NAMEPLATE_QR_TOP_LEFT_MM)
        strip_xy = list(cfg.get("strip_top_left_mm") or label_util.NAMEPLATE_STRIP_TOP_LEFT_MM)

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
            if silent:
                logger.error(f"auto-print nameplate: build failed for SN={normalized}: {exc}")
            else:
                messagebox.showerror("打印铭牌", f"生成 ZPL 失败：{exc}")
            return False

        if not label_util.is_windows():
            if not silent:
                messagebox.showinfo(
                    "打印铭牌",
                    "非 Windows 主机不能直接发打印队列。CLI dry-run 用 `run-cli print-nameplate --zpl-out` 查看 ZPL。",
                )
            return False

        target = label_util.find_label_printer(printer_pref)
        if not target:
            queues = label_util.list_windows_printers()
            names = "\n".join(p["name"] for p in queues) or "(无)"
            msg = (
                "未找到铭牌打印机。"
                "请在 config.yml 的 nameplate_printer.name 里写明打印队列名。\n\n"
                f"当前 Windows 上已安装的队列：\n{names}"
            )
            if silent:
                logger.error(f"auto-print nameplate: {msg}")
            else:
                messagebox.showerror("打印铭牌", msg)
            return False

        try:
            label_util.send_to_windows_printer(target, zpl, job_name=f"Nameplate {normalized}")
        except Exception as exc:  # noqa: BLE001
            if silent:
                logger.error(f"auto-print nameplate: send to '{target}' failed: {exc}")
            else:
                messagebox.showerror("打印铭牌", f"发送到打印机失败：{exc}")
            return False

        prefix = "自动" if silent else "已"
        self.status_var.set(
            f"{datetime.now().strftime('%H:%M:%S')}  {prefix}发送铭牌×{quantity} 到 {target}（SN={normalized} P/N={pn} {grade}类）"
        )
        return True

    def _do_ean13_print(
        self,
        model_key: str | None,
        grade: str | None,
        *,
        quantity: int | None = None,
        silent: bool,
        sn_note: str | None = None,
    ) -> bool:
        """Build + send N EAN-13 (69 码) labels (50×30mm) to the Deli DL-888T.

        Shared by the SN-driven path (``_submit_ean13_print``) and the manual
        6-variant chooser. P/N and EAN-13 are always looked up together from the
        same (model_key, grade) key so the header P/N, the barcode and the model
        name can never drift apart. ``quantity=None`` falls back to the configured
        default; ``sn_note`` only decorates the status line.
        """
        pn = label_util.lookup_pn(model_key, grade)
        ean13 = label_util.lookup_ean13(model_key, grade)
        if not pn or not ean13:
            msg = (
                f"无法解析 69 码标签数据 (model={model_key!r} grade={grade!r}"
                f" pn={pn!r} ean13={ean13!r})。"
                "请确认机型/等级在查表里。"
            )
            if silent:
                logger.warning(f"auto-print ean13 skipped: {msg}")
            else:
                messagebox.showwarning("打印 69 码", msg)
            return False

        cfg = (self.config.get("ean13_printer") or {}) if isinstance(self.config, dict) else {}
        printer_pref = str(cfg.get("name") or "").strip() or None
        try:
            dpi = int(cfg.get("dpi") or label_util.EAN13_DPI)
        except (TypeError, ValueError):
            dpi = label_util.EAN13_DPI
        if quantity is None:
            try:
                quantity = max(1, int(cfg.get("quantity") or 2))
            except (TypeError, ValueError):
                quantity = 2
        else:
            quantity = max(1, int(quantity))

        try:
            data = label_util.build_ean13_tspl(model_key, pn, ean13, dpi=dpi, quantity=quantity)
        except ValueError as exc:
            if silent:
                logger.error(f"auto-print ean13: build failed (model={model_key} grade={grade}): {exc}")
            else:
                messagebox.showerror("打印 69 码", f"生成 TSPL 失败：{exc}")
            return False

        if not label_util.is_windows():
            if not silent:
                messagebox.showinfo(
                    "打印 69 码",
                    "非 Windows 主机不能直接发打印队列。"
                    "用 `run-cli print-ean13 --zpl-out` 查看内容。",
                )
            return False

        target = label_util.find_label_printer(printer_pref)
        if not target:
            queues = label_util.list_windows_printers()
            names = "\n".join(p["name"] for p in queues) or "(无)"
            msg = (
                f"找不到 69 码打印机 '{printer_pref}'（严格匹配）。\n"
                "请在 config.yml 的 ean13_printer.name 里写准打印队列名。\n\n"
                f"Windows 上已安装的队列：\n{names}"
            )
            if silent:
                logger.error(f"auto-print ean13: {msg}")
            else:
                messagebox.showerror("打印 69 码", msg)
            return False

        try:
            label_util.send_to_windows_printer(target, data, job_name=f"EAN-13 {ean13}")
        except Exception as exc:  # noqa: BLE001
            if silent:
                logger.error(f"auto-print ean13: send to '{target}' failed: {exc}")
            else:
                messagebox.showerror("打印 69 码", f"发送到打印机失败：{exc}")
            return False

        prefix = "自动" if silent else "已"
        src = f"SN={sn_note} " if sn_note else ""
        self.status_var.set(
            f"{datetime.now().strftime('%H:%M:%S')}  {prefix}发送 69 码×{quantity} 到 {target}"
            f"（{src}P/N={pn} EAN={ean13} {grade}类）"
        )
        return True

    def _submit_ean13_print(self, sn: str, *, silent: bool) -> bool:
        """Send N EAN-13 (69 码) labels for an SN.

        Model comes from the SN prefix, grade from the form selection; quantity
        falls back to the configured default. Thin wrapper over
        ``_do_ean13_print``. Used by the print chooser and the auto-print hook.
        """
        normalized = normalize_sn(sn)
        if not normalized or is_auto_sn_placeholder(normalized) or not is_full_sn_candidate(normalized):
            return False
        model_key = model_key_from_sn(normalized)
        grade = (self.form_grade_var.get() or "").strip().upper() or None
        return self._do_ean13_print(
            model_key, grade, quantity=None, silent=silent, sn_note=normalized
        )

    def _carton_eligibility(self, sns: list[str]) -> tuple[bool, str]:
        """Whether ``sns`` can make a 周转箱 label: exactly 4, all same model."""
        n = label_util.CARTON_SN_COUNT
        if len(sns) != n:
            return False, f"需正好 {n} 个 SN（当前 {len(sns)} 个）"
        models = {model_key_from_sn(s) for s in sns}
        if None in models or len(models) != 1:
            return False, f"{n} 个 SN 必须同机型"
        return True, ""

    def _submit_turnover_print(self, sns: list[str], *, silent: bool) -> bool:
        """Print ONE 周转箱 (turnover-box) label for a box of exactly 4 SNs.

        Model comes from the (identical) SN prefixes, grade from the form, PID
        from ``lookup_pn``. The day's running seq is stamped into the QR and the
        box is appended to the local台账 — but the seq is only committed after a
        successful print, so failures don't burn a number.
        """
        clean = [normalize_sn(s) for s in (sns or []) if normalize_sn(s)]
        ok, reason = self._carton_eligibility(clean)
        if not ok:
            if not silent:
                messagebox.showwarning("打印周转箱", reason)
            return False
        model_key = model_key_from_sn(clean[0])
        grade = (self.form_grade_var.get() or "").strip().upper() or None
        pn = label_util.lookup_pn(model_key, grade)
        if not pn:
            if not silent:
                messagebox.showwarning(
                    "打印周转箱",
                    f"无法解析 PID/PN (model={model_key!r} grade={grade!r})。"
                    "请确认机型/等级在查表里。",
                )
            return False

        cfg = (self.config.get("carton_printer") or {}) if isinstance(self.config, dict) else {}
        printer_pref = str(cfg.get("name") or "").strip() or None
        try:
            dpi = int(cfg.get("dpi") or label_util.CARTON_DPI)
        except (TypeError, ValueError):
            dpi = label_util.CARTON_DPI
        try:
            quantity = max(1, int(cfg.get("quantity") or 2))
        except (TypeError, ValueError):
            quantity = 2
        po = str(cfg.get("po_default") or label_util.CARTON_PO_DEFAULT)
        warehouse = str(cfg.get("warehouse") or label_util.CARTON_WAREHOUSE_DEFAULT)

        if not label_util.is_windows():
            if not silent:
                messagebox.showinfo("打印周转箱", "非 Windows 主机不能直接发打印队列。")
            return False
        target = label_util.find_label_printer(printer_pref)
        if not target:
            queues = label_util.list_windows_printers()
            names = "\n".join(p["name"] for p in queues) or "(无)"
            msg = (
                f"找不到周转箱打印机 '{printer_pref}'（严格匹配）。\n"
                "请在 config.yml 的 carton_printer.name 里写准打印队列名。\n\n"
                f"Windows 上已安装的队列：\n{names}"
            )
            if silent:
                logger.error(f"carton: {msg}")
            else:
                messagebox.showerror("打印周转箱", msg)
            return False

        data_dir = self._output_root()
        seq = label_util.peek_carton_seq(data_dir)
        try:
            zpl = label_util.build_turnover_box_zpl(
                model_key, grade, clean,
                seq=seq, pid=pn, po=po, warehouse=warehouse, dpi=dpi, quantity=quantity,
            )
        except ValueError as exc:
            if not silent:
                messagebox.showerror("打印周转箱", f"生成 ZPL 失败：{exc}")
            return False
        try:
            label_util.send_to_windows_printer(
                target, zpl, job_name=f"Carton {model_key}{grade or ''} #{seq:03d}"
            )
        except Exception as exc:  # noqa: BLE001
            if silent:
                logger.error(f"carton: send to '{target}' failed: {exc}")
            else:
                messagebox.showerror("打印周转箱", f"发送到打印机失败：{exc}")
            return False

        committed = label_util.commit_carton_print(
            data_dir,
            {"model": model_key, "grade": grade, "pid": pn, "po": po,
             "sns": clean, "printer": target, "copies": quantity},
        )
        self.status_var.set(
            f"{datetime.now().strftime('%H:%M:%S')}  已发送 周转箱×{quantity} 到 {target}"
            f"（PID={pn} 流水号#{committed:03d} {len(clean)}台 {grade or '?'}类）"
        )
        return True

    def _show_ean13_variant_chooser(self) -> None:
        """Manual 69 码 picker for when no queue is selected and SN is empty.

        Shows all six (model, grade) variants as buttons plus a quantity spinbox
        (+/- arrows and free typing). Clicking a button prints that many copies of
        that variant. Stays open so the operator can print several in a row.
        """
        combos = [
            ("2800", "A"), ("2800", "B"),
            ("4800", "A"), ("4800", "B"),
            ("4800Plus", "A"), ("4800Plus", "B"),
        ]

        dlg = tk.Toplevel(self.root)
        dlg.title("打印 69 码")
        dlg.transient(self.root)
        dlg.resizable(False, False)
        dlg.grab_set()

        body = ttk.Frame(dlg, padding=12)
        body.pack(fill=tk.BOTH, expand=True)
        ttk.Label(
            body,
            text="未选队列、SN 也为空 —— 手动选择要打印的 69 码（点按钮即打印）：",
        ).grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        cfg = (self.config.get("ean13_printer") or {}) if isinstance(self.config, dict) else {}
        try:
            default_qty = max(1, int(cfg.get("quantity") or 2))
        except (TypeError, ValueError):
            default_qty = 2
        qty_var = tk.StringVar(value=str(default_qty))

        def _read_qty() -> int | None:
            raw = (qty_var.get() or "").strip()
            try:
                q = int(raw)
            except ValueError:
                messagebox.showwarning("打印 69 码", f"数量必须是整数：{raw!r}", parent=dlg)
                return None
            if q < 1 or q > 999:
                messagebox.showwarning("打印 69 码", "数量需在 1–999 之间。", parent=dlg)
                return None
            return q

        def _make_handler(mk: str, gr: str):
            def _handler() -> None:
                q = _read_qty()
                if q is None:
                    return
                self._do_ean13_print(mk, gr, quantity=q, silent=False)
            return _handler

        for i, (mk, gr) in enumerate(combos):
            r, c = divmod(i, 2)
            disp = label_util.EAN13_MODEL_DISPLAY.get(mk, mk)
            pn = label_util.lookup_pn(mk, gr)
            txt = f"{disp} {gr}\nP/N {pn}" if pn else f"{disp} {gr}\n(无数据)"
            btn = ttk.Button(body, text=txt, width=18, command=_make_handler(mk, gr))
            if not pn or not label_util.lookup_ean13(mk, gr):
                btn.state(["disabled"])
            btn.grid(row=1 + r, column=c, sticky=tk.EW, padx=4, pady=3, ipady=4)
        for col in range(2):
            body.columnconfigure(col, weight=1, minsize=160)

        qrow = ttk.Frame(body)
        qrow.grid(row=4, column=0, columnspan=2, sticky=tk.W, pady=(10, 0))
        ttk.Label(qrow, text="数量：").pack(side=tk.LEFT)
        ttk.Spinbox(qrow, from_=1, to=999, textvariable=qty_var, width=6).pack(side=tk.LEFT)
        ttk.Label(qrow, text="（点 +/- 或直接输入）", foreground="#666").pack(side=tk.LEFT, padx=(8, 0))

        close_row = ttk.Frame(body)
        close_row.grid(row=5, column=0, columnspan=2, sticky=tk.E, pady=(12, 0))
        ttk.Button(close_row, text="关闭", command=dlg.destroy).pack(side=tk.RIGHT)

        dlg.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - dlg.winfo_width()) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - dlg.winfo_height()) // 3
        dlg.geometry(f"+{max(0, x)}+{max(0, y)}")
        dlg.lift()
        dlg.focus_set()

    def _nas_ip(self) -> str:
        ip = self.manual_ip_var.get().strip()
        return ip or "auto"

    def _on_factory_reset_toggle(self) -> None:
        if self.factory_reset_before_finish_var.get():
            self.cleanup_before_finish_var.set(True)

    def _on_cleanup_toggle(self) -> None:
        if self.factory_reset_before_finish_var.get() and not self.cleanup_before_finish_var.get():
            self.cleanup_before_finish_var.set(True)

    def _on_auto_form_entry_toggle(self) -> None:
        if not self.auto_form_entry_var.get():
            self.status_var.set(self._timestamped("auto_form_off"))
        if self.auto_scan_var.get():
            self._on_auto_scan_toggle()

    def _on_form_grade_changed(self) -> None:
        if self.auto_scan_var.get():
            self._on_auto_scan_toggle()

    def _on_auto_scan_toggle(self) -> None:
        if self.auto_scan_var.get():
            if not self._auto_scan_ready(show_warning=False):
                self._cancel_scheduled_auto_scan()
                return
            self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  自动扫描已开启")
            self._schedule_auto_scan(0)
            return

        self._cancel_scheduled_auto_scan()
        self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  自动扫描已关闭")

    def _cancel_scheduled_auto_scan(self) -> None:
        if self.auto_scan_after_id is not None:
            try:
                self.root.after_cancel(self.auto_scan_after_id)
            except Exception:
                pass
            self.auto_scan_after_id = None

    def _schedule_auto_scan(self, delay_ms: int) -> None:
        if not self.auto_scan_var.get():
            return
        if not self._auto_scan_ready(show_warning=False):
            return
        if self.auto_scan_after_id is not None:
            try:
                self.root.after_cancel(self.auto_scan_after_id)
            except Exception:
                pass
        self.auto_scan_after_id = self.root.after(delay_ms, self._start_auto_scan)

    def _start_auto_scan(self) -> None:
        self.auto_scan_after_id = None
        if not self.auto_scan_var.get():
            return
        if not self._auto_scan_ready(show_warning=False):
            return
        if self.auto_scan_worker and self.auto_scan_worker.is_alive():
            self._schedule_auto_scan(self.AUTO_SCAN_RETRY_MS)
            return

        known_ips = self._known_ips()

        def worker() -> None:
            try:
                devices = self._scan_ugreen_nas_devices(known_ips)
                self.ui_queue.put({"type": "auto_scan_done", "success": True, "devices": devices})
            except Exception as exc:
                self.ui_queue.put({"type": "auto_scan_done", "success": False, "message": str(exc)})

        self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  正在自动扫描 UGREEN NAS")
        self.auto_scan_worker = threading.Thread(target=worker, daemon=True)
        self.auto_scan_worker.start()
        self._refresh_action_states()

    def _scan_ugreen_nas_devices(self, known_ips: set[str]) -> list[dict[str, str]]:
        network = self.config.get("network") or {}
        subnet = str(network.get("subnet") or "192.168.0.0/24")
        port = int(network.get("ugos_http_port") or 9999)
        discovery_timeout = float(network.get("discovery_timeout") or 30)
        allowed_network = self._auto_scan_network(subnet)

        broadcast_hits = [
            hit for hit in self._broadcast_scan_hits(discovery_timeout)
            if self._auto_scan_ip_allowed(hit.address, allowed_network)
        ]
        hits_by_ip = {hit.address: hit for hit in broadcast_hits}

        try:
            candidates = [
                ip
                for ip in find_nas_candidates(
                    subnet=subnet,
                    port=port,
                    discovery_timeout=discovery_timeout,
                    exclude=known_ips,
                )
                if self._auto_scan_ip_allowed(ip, allowed_network)
            ]
        except Exception:
            candidates = []

        for hit in self._broadcast_scan_hits(discovery_timeout):
            if not self._auto_scan_ip_allowed(hit.address, allowed_network):
                continue
            current = hits_by_ip.get(hit.address)
            if current is None or (hit.sn and not current.sn):
                hits_by_ip[hit.address] = hit

        candidate_ips = sort_ip_strings(
            ip for ip in (set(candidates) | set(hits_by_ip)) if self._auto_scan_ip_allowed(ip, allowed_network)
        )
        return self._auto_scan_devices_from_candidates(candidate_ips, hits_by_ip, known_ips)

    def _auto_scan_network(self, subnet: str):
        try:
            return ipaddress.ip_network(subnet, strict=False)
        except ValueError:
            return None

    def _auto_scan_ip_allowed(self, ip: str, network) -> bool:
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast or parsed.is_unspecified:
            return False
        if network is None:
            return True
        try:
            return parsed in network
        except TypeError:
            return False

    def _auto_scan_devices_from_candidates(
        self,
        candidate_ips: list[str],
        hits_by_ip: dict,
        known_ips: set[str],
    ) -> list[dict[str, object]]:
        devices: list[dict[str, object]] = []
        consumed_ips: set[str] = set()
        candidate_ips = [ip for ip in candidate_ips if self._auto_scan_ip_allowed(ip, None)]
        known_ips = set(sort_ip_strings(known_ips))
        for ip in candidate_ips:
            if ip in consumed_ips:
                continue
            group = self._auto_scan_ip_group(ip, candidate_ips, hits_by_ip)
            consumed_ips.update(group)
            if group & known_ips:
                continue
            if self._should_defer_auto_scan_group(group, hits_by_ip):
                continue
            selected_ip = self._auto_scan_representative_ip(group, hits_by_ip)
            sn = self._auto_scan_sn_for_ip(selected_ip, hits_by_ip)
            if is_auto_sn_placeholder(sn):
                continue
            device = {
                "ip": selected_ip,
                "sn": sn,
                "mac": self._auto_scan_mac_for_ip(selected_ip, hits_by_ip),
                "reserved_ips": sort_ip_strings(group),
            }
            interface = self._auto_scan_interface_for_ip(selected_ip, hits_by_ip)
            if interface:
                device["interface"] = interface
            devices.append(device)
        return devices

    def _auto_scan_ip_group(self, ip: str, candidate_ips: list[str], hits_by_ip: dict) -> set[str]:
        candidate_set = set(candidate_ips)
        group = self._broadcast_pair_ip_group(ip, hits_by_ip)
        group.update(self._broadcast_sn_ip_group(ip, hits_by_ip))

        sn = self._auto_scan_sn_for_ip(ip, hits_by_ip)
        if not is_auto_sn_placeholder(sn):
            return group

        for adjacent_ip in (self._previous_ip(ip), self._next_ip(ip)):
            if adjacent_ip and adjacent_ip in candidate_set:
                adjacent_sn = self._auto_scan_sn_for_ip(adjacent_ip, hits_by_ip)
                if self._auto_scan_sns_can_share_device(sn, adjacent_sn):
                    group.add(adjacent_ip)
        return group

    def _broadcast_pair_ip_group(self, ip: str, hits_by_ip: dict) -> set[str]:
        group = {ip}
        for hit in hits_by_ip.values():
            pair = getattr(hit, "data", {}).get("pair") if hit is not None else None
            if not isinstance(pair, dict):
                continue
            pair_ips = {str(pair_ip) for pair_ip in pair}
            if ip == getattr(hit, "address", "") or ip in pair_ips:
                group.update(pair_ips)
        return group

    def _broadcast_sn_ip_group(self, ip: str, hits_by_ip: dict) -> set[str]:
        sn = self._auto_scan_sn_for_ip(ip, hits_by_ip)
        if not is_full_sn_candidate(sn):
            return {ip}
        return {
            hit_ip
            for hit_ip in hits_by_ip
            if normalize_sn(self._auto_scan_sn_for_ip(hit_ip, hits_by_ip)) == sn
        } | {ip}

    def _auto_scan_sn_for_ip(self, ip: str, hits_by_ip: dict) -> str:
        hit = hits_by_ip.get(ip)
        if hit is not None:
            return self._auto_scan_sn(ip, hit.sn if hit else "", hit.mac if hit else "")
        owner = self._auto_scan_pair_owner_for_ip(ip, hits_by_ip)
        if owner is None:
            return self._auto_scan_sn(ip, "", "")
        return self._auto_scan_sn(ip, owner.sn, self._auto_scan_pair_mac_for_ip(ip, owner))

    def _auto_scan_mac_for_ip(self, ip: str, hits_by_ip: dict) -> str:
        hit = hits_by_ip.get(ip)
        if hit is not None:
            return str(hit.mac or "")
        owner = self._auto_scan_pair_owner_for_ip(ip, hits_by_ip)
        if owner is None:
            return ""
        return self._auto_scan_pair_mac_for_ip(ip, owner)

    def _auto_scan_interface_for_ip(self, ip: str, hits_by_ip: dict) -> str:
        hit = hits_by_ip.get(ip)
        if hit is not None:
            interface = str((getattr(hit, "data", {}) or {}).get("interface") or "").lower()
            if interface:
                return interface
        owner = self._auto_scan_pair_owner_for_ip(ip, hits_by_ip)
        if owner is None:
            return ""
        return self._infer_pair_interface(ip, owner)

    def _auto_scan_pair_owner_for_ip(self, ip: str, hits_by_ip: dict):
        for hit in hits_by_ip.values():
            pair = (getattr(hit, "data", {}) or {}).get("pair")
            if isinstance(pair, dict) and ip in {str(pair_ip) for pair_ip in pair}:
                return hit
        return None

    def _auto_scan_pair_mac_for_ip(self, ip: str, hit) -> str:
        pair = (getattr(hit, "data", {}) or {}).get("pair")
        if isinstance(pair, dict):
            return str(pair.get(ip) or "").strip()
        return ""

    def _infer_pair_interface(self, ip: str, hit) -> str:
        data = getattr(hit, "data", {}) or {}
        model = str(data.get("model") or "").lower().replace(" ", "")
        if "dxp4800plus" not in model:
            return ""
        pair = data.get("pair")
        if not isinstance(pair, dict):
            return ""
        pair_ips = sort_ip_strings(str(pair_ip) for pair_ip in pair)
        if len(pair_ips) < 2:
            return ""
        if ip == pair_ips[0]:
            return "eth0"
        if ip == pair_ips[1]:
            return "eth1"
        return ""

    def _auto_scan_sns_can_share_device(self, left: str, right: str) -> bool:
        left_auto = is_auto_sn_placeholder(left)
        right_auto = is_auto_sn_placeholder(right)
        if left_auto and right_auto:
            return True
        return (left_auto and is_full_sn_candidate(right)) or (right_auto and is_full_sn_candidate(left))

    def _should_defer_auto_scan_group(self, group: set[str], hits_by_ip: dict) -> bool:
        for ip in group:
            hit = hits_by_ip.get(ip)
            if hit is None:
                continue
            data = getattr(hit, "data", {}) or {}
            model = str(data.get("model") or "").lower().replace(" ", "")
            if "dxp4800plus" not in model:
                continue
            sn = self._auto_scan_sn_for_ip(ip, hits_by_ip)
            if not is_full_sn_candidate(sn):
                continue
            if any(self._auto_scan_interface_for_ip(group_ip, hits_by_ip) == "eth0" for group_ip in group):
                return False
            logger.info(
                f"Auto-scan saw 4800Plus SN {sn} on {ip} without an eth0 alias; waiting instead of queuing eth1"
            )
            return True
        return False

    def _auto_scan_representative_ip(self, group: set[str], hits_by_ip: dict) -> str:
        ordered_ips = sort_ip_strings(group)
        return max(
            ordered_ips,
            key=lambda ip: (self._auto_scan_port_score(ip, hits_by_ip.get(ip), hits_by_ip), -ordered_ips.index(ip)),
        )

    def _auto_scan_port_score(self, ip: str, hit, hits_by_ip: dict | None = None) -> int:
        owner = hit or (self._auto_scan_pair_owner_for_ip(ip, hits_by_ip or {}) if hits_by_ip is not None else None)
        if owner is None:
            return 0
        score = 10 if hit is not None else 5
        sn = self._auto_scan_sn_for_ip(ip, hits_by_ip or ({ip: hit} if hit is not None else {}))
        if is_full_sn_candidate(sn):
            score += 10
        data = getattr(owner, "data", {}) or {}
        text = " ".join(str(value).lower() for value in data.values())
        model = str(data.get("model") or "").lower().replace(" ", "")
        interface = self._auto_scan_interface_for_ip(ip, hits_by_ip or ({ip: hit} if hit is not None else {}))
        if hit is not None and any(marker in text for marker in ("10000", "10gb", "10g", "10 gb")):
            score += 100 if "dxp4800plus" not in model or interface == "eth0" else 0
        if "dxp4800plus" in model and interface == "eth0":
            score += 200
        elif interface == "eth0":
            score += 5
        return score

    def _next_ip(self, ip: str) -> str | None:
        try:
            parsed = ipaddress.ip_address(ip)
            return str(parsed + 1)
        except ValueError:
            return None

    def _previous_ip(self, ip: str) -> str | None:
        try:
            parsed = ipaddress.ip_address(ip)
            if int(parsed) <= 0:
                return None
            return str(parsed - 1)
        except ValueError:
            return None

    def _broadcast_scan_hits(self, discovery_timeout: float) -> list:
        hits_by_ip = {}
        timeout = min(discovery_timeout, 3.0)
        for _ in range(2):
            try:
                for hit in ugreen_broadcast.discover(timeout=timeout):
                    current = hits_by_ip.get(hit.address)
                    if current is None or (hit.sn and not current.sn):
                        hits_by_ip[hit.address] = hit
            except Exception:
                continue
            if any(hit.sn for hit in hits_by_ip.values()):
                break
        return list(hits_by_ip.values())

    def _auto_scan_sn(self, ip: str, broadcast_sn: str, mac: str) -> str:
        sn = normalize_sn(broadcast_sn)
        if is_full_sn_candidate(sn):
            return sn

        normalized_mac = normalize_sn(mac)
        if len(sn_tail(normalized_mac)) >= 4:
            return f"AUTO{normalized_mac[-8:]}"

        try:
            return f"AUTO{int(ipaddress.ip_address(ip)):08X}"
        except ValueError:
            return f"AUTO{normalize_sn(ip)[-8:]}"

    def _finish_auto_scan(self, event: dict) -> None:
        self.auto_scan_worker = None
        success = bool(event.get("success"))
        added = 0
        skipped = 0
        if success:
            if not self._auto_scan_ready(show_warning=False):
                self._refresh_action_states()
                return
            # If auto-form is on but the operator never picked a grade, block
            # right here with a modal — same as the manual flow's validation
            # popup — so we never silently default to A on a freshly-detected
            # device. Re-read live each time so toggling auto-form-entry off
            # mid-scan correctly skips the prompt.
            if self.form_entry_enabled and self.auto_form_entry_var.get():
                if self._ensure_grade_selected() is None:
                    self.status_var.set(self._timestamped("auto_scan_waiting_form_grade"))
                    self._refresh_action_states()
                    return
            network = self.config.get("network") or {}
            allowed_network = self._auto_scan_network(str(network.get("subnet") or "192.168.0.0/24"))
            for device in event.get("devices") or []:
                ip = str(device.get("ip") or "").strip()
                sn = normalize_sn(str(device.get("sn") or ""))
                if (
                    not ip
                    or not self._auto_scan_ip_allowed(ip, allowed_network)
                    or len(sn_tail(sn)) < 4
                    or is_auto_sn_placeholder(sn)
                ):
                    skipped += 1
                    continue
                reserved_ips = {
                    reserved_ip
                    for reserved_ip in sort_ip_strings(device.get("reserved_ips") or [ip])
                    if self._auto_scan_ip_allowed(reserved_ip, allowed_network)
                }
                if not reserved_ips:
                    skipped += 1
                    continue
                if (
                    any(self._has_known_ip(reserved_ip) for reserved_ip in reserved_ips)
                    or self._has_any_sn(sn)
                    or self._has_completed_output_for_sn(sn)
                ):
                    skipped += 1
                    continue
                if self.factory_reset_before_finish_var.get():
                    self.cleanup_before_finish_var.set(True)
                if (
                    self._add_task_to_queue(
                        sn,
                        ip,
                        select=False,
                        source="auto-scan",
                        reserved_ips=reserved_ips,
                        network_interface=str(device.get("interface") or ""),
                    )
                    is not None
                ):
                    added += 1
            self.status_var.set(
                f"{datetime.now().strftime('%H:%M:%S')}  自动扫描完成，新增 {added} 台，跳过 {skipped} 台"
            )
        else:
            self.status_var.set(
                f"{datetime.now().strftime('%H:%M:%S')}  自动扫描失败：{event.get('message') or ''}"
            )

        self._refresh_action_states()
        if self.auto_scan_var.get() and self._auto_scan_ready(show_warning=False):
            self._schedule_auto_scan(self.AUTO_SCAN_INTERVAL_MS if success else self.AUTO_SCAN_RETRY_MS)

    def _on_sn_enter(self, _event) -> None:
        self._on_start_test()

    def _on_sn_entry_focus(self, _event) -> None:
        """Clear queue selection so the manual SN-print path uses the entry box."""
        if self.device_tree is None:
            return
        current = self.device_tree.selection()
        if current:
            self.device_tree.selection_remove(*current)
        self.selected_task_ids = []
        self.selected_task_id = None
        self._refresh_action_states()

    def _refresh_form_accounts(self) -> None:
        if not self.form_entry_enabled:
            self.form_account_var.set("")
            return
        accounts = form_entry.list_accounts(self.project_root)
        names = [str(account.get("name") or account.get("account") or "") for account in accounts]
        names = [name for name in names if name]
        active = form_entry.get_active_account_name(self.project_root)
        if self.form_account_combo is not None:
            self.form_account_combo.configure(values=names)
        if active:
            self.form_account_var.set(active)
        elif names:
            self.form_account_var.set(names[0])
        self._refresh_form_grade_options()

    def _refresh_form_materials_on_startup(self) -> None:
        if not self.form_entry_enabled:
            return
        account_name = self.form_account_var.get().strip()

        def worker() -> None:
            # Fast-forward the autoupdate sibling repo first so that any
            # persistent config edits committed there (e.g. selected_material_codes)
            # land before the 内部系统 refresh that builds on top of them. Silent
            # no-op when autoupdate_root isn't a git work tree.
            sync_status = form_entry.sync_autoupdate_repo(self.project_root)
            if sync_status.get("status") == "updated":
                before = (sync_status.get("before") or "")[:7]
                after = (sync_status.get("after") or "")[:7]
                self.ui_queue.put(
                    {
                        "type": "form_material_refresh",
                        "message": f"{datetime.now().strftime('%H:%M:%S')}  已同步录表配置仓 {before} → {after}",
                    }
                )
            try:
                result = form_entry.refresh_form_materials(self.project_root, account_name=account_name or None)
                forms = result.get("forms") if isinstance(result, dict) else []
                total = sum(int(item.get("selected_count") or 0) for item in forms if isinstance(item, dict))
                self.ui_queue.put(
                    {
                        "type": "form_material_refresh",
                        "message": f"{datetime.now().strftime('%H:%M:%S')}  已刷新录表物料，当前提交物料 {total} 项",
                    }
                )
            except Exception as exc:
                self.ui_queue.put(
                    {
                        "type": "form_material_refresh",
                        "message": f"{datetime.now().strftime('%H:%M:%S')}  录表物料刷新失败：{exc}",
                    }
                )

        threading.Thread(target=worker, name="form-material-refresh", daemon=True).start()

    def _refresh_form_grade_options(self) -> None:
        if not self.form_entry_enabled:
            return
        if self.form_grade_var.get().strip().upper() not in {"A", "B"}:
            self.form_grade_var.set("")

    def _on_switch_account(self, _event=None) -> None:
        if not self.form_entry_enabled:
            return
        account_name = self.form_account_var.get().strip()
        if not account_name:
            return
        form_entry.set_active_account(self.project_root, account_name)
        self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  当前录表账号：{account_name}")

    def _on_delete_account(self) -> None:
        if not self.form_entry_enabled:
            return
        account_name = self.form_account_var.get().strip()
        if not account_name:
            messagebox.showwarning("未选择账号", "请先选择一个要删除的账号")
            return
        if not messagebox.askyesno("删除账号", f"确定删除录表账号：{account_name}？"):
            return
        result = form_entry.delete_account(self.project_root, account_name)
        if not result.get("removed"):
            messagebox.showwarning(
                "无法删除",
                "这个账号来自上传软件当前登录状态，尚未保存到录表账号列表。请先切换一次保存，或在上传软件里退出该账号。",
            )
            return
        self._refresh_form_accounts()
        active = str(result.get("active") or "")
        if active:
            self.form_account_var.set(active)
        self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  已删除录表账号：{account_name}")

    def _on_add_account(self) -> None:
        if not self.form_entry_enabled:
            return
        dialog = tk.Toplevel(self.root)
        dialog.title("添加录表账号")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)

        account_var = tk.StringVar(value=self.form_account_var.get().strip() or "自动录表系统")

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky=tk.NSEW)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="账号名").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=account_var, width=32).grid(row=0, column=1, sticky=tk.EW, pady=4)
        ttk.Label(
            frame,
            text="账号凭据由自动录表系统管理，这里只保存 factory-test 的显示/选择名称。",
        ).grid(row=1, column=0, columnspan=2, sticky=tk.W, pady=(4, 8))

        def save_account() -> None:
            account = account_var.get().strip()
            if not account:
                messagebox.showwarning("缺少账号名", "请填写一个录表账号名。", parent=dialog)
                return
            try:
                entry = form_entry.add_or_update_account(self.project_root, account=account)
                self._refresh_form_accounts()
                self.form_account_var.set(str(entry.get("name") or account))
                messagebox.showinfo("账号已保存", f"当前录表账号：{self.form_account_var.get()}", parent=dialog)
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky=tk.EW)
        ttk.Button(buttons, text="保存并切换", command=save_account).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.LEFT)
    def _form_grade_choice(self) -> str:
        grade = self.form_grade_var.get().strip().upper()
        return grade if grade in {"A", "B"} else ""

    def _auto_scan_ready(self, show_warning: bool) -> bool:
        if self._validate_form_settings(show_warning=show_warning):
            return True
        if not show_warning and self.auto_form_entry_var.get() and not self._form_grade_choice():
            self.status_var.set(self._timestamped("auto_scan_waiting_form_grade"))
        elif not show_warning:
            self.status_var.set(self._timestamped("auto_scan_waiting_form_config"))
        return False

    def _validate_form_settings(self, show_warning: bool = True) -> bool:
        if not self.form_entry_enabled:
            self.auto_form_entry_var.set(False)
            return True
        if not self.auto_form_entry_var.get():
            self.status_var.set(self._timestamped("auto_form_off"))
            return True
        account_name = self.form_account_var.get().strip()
        if not account_name:
            if show_warning:
                messagebox.showwarning("缺少录表账号", "请先添加或选择一个录表账号。")
            return False
        grade = self._form_grade_choice()
        if not grade:
            self.status_var.set(self._timestamped("auto_scan_waiting_form_grade"))
            if show_warning:
                messagebox.showwarning("未选择等级", "请先选择 A/B，或关闭自动录表。")
            return False
        try:
            for model in form_entry.available_models(self.project_root):
                form_entry.build_report_form_data(
                    {"sn": "PREVIEW"},
                    self.project_root,
                    model=model,
                    grade=grade,
                )
        except Exception as exc:
            if show_warning:
                messagebox.showwarning("录表配置不可用", str(exc))
            return False
        return True

    def _on_start_test(self) -> None:
        if not self._validate_form_settings(show_warning=True):
            return
        sn = normalize_sn(self.sn_var.get().strip())
        nas_ip = self._nas_ip()
        if not sn and nas_ip == "auto":
            messagebox.showwarning("缺少 SN 或 IP", "请先扫码/输入序列号，或填写 NAS IP")
            return
        if not sn:
            sn = self._auto_scan_sn(nas_ip, "", "")
        if len(sn_tail(sn)) < 4:
            messagebox.showwarning("SN 过短", "请输入完整 SN 或最后 4 位 SN")
            return

        if self._has_active_sn(sn):
            messagebox.showwarning("设备已存在", f"SN {sn} 已在队列中运行，请勿重复添加")
            return

        if self.factory_reset_before_finish_var.get():
            self.cleanup_before_finish_var.set(True)

        task = self._add_task_to_queue(sn, nas_ip, select=True, source="manual")
        if task is None:
            return

        self.sn_var.set("")
        if self.sn_entry is not None:
            self.sn_entry.focus_set()

    def _add_task_to_queue(
        self,
        sn: str,
        nas_ip: str,
        select: bool,
        source: str,
        reserved_ips: set[str] | None = None,
        network_interface: str = "",
    ) -> DeviceTask | None:
        auto_form_entry = self.form_entry_enabled and self.auto_form_entry_var.get()
        auto_seed_previous_step = auto_form_entry and self.auto_seed_previous_step_var.get()
        task = DeviceTask(
            task_id=self._next_task_id(sn),
            sn=sn,
            requested_ip=nas_ip,
            mode=self.flow_mode_var.get(),
            cleanup_before_finish=self.cleanup_before_finish_var.get(),
            factory_reset_before_finish=self.factory_reset_before_finish_var.get(),
            auto_form_entry=auto_form_entry,
            auto_seed_previous_step=auto_seed_previous_step,
            form_model=model_key_from_sn(sn) or "",
            form_grade=self._form_grade_choice() if auto_form_entry else "",
            form_account_name=self.form_account_var.get().strip() if auto_form_entry else "",
            reserved_ips=set(reserved_ips or {nas_ip}),
            network_interface=network_interface,
        )
        task.status = self._status_text(task.state_code)
        self.devices[task.task_id] = task
        self._insert_device_row(task, select=select)

        if select:
            self.selected_task_id = task.task_id
            self._set_log_contents("")
        self._append_local_log(
            task.task_id,
            f"SN {task.sn} 已加入队列，来源={source}，模式={task.mode}，IP={task.requested_ip}，"
            f"{f'接口={task.network_interface}，' if task.network_interface else ''}"
            f"自动录表={task.auto_form_entry}，自动补第一步={task.auto_seed_previous_step}，"
            f"机型={task.form_model or 'SN自动识别'}，等级={task.form_grade}，账号={task.form_account_name}",
        )
        self._mark_daily_task_retry_pending(task)
        self._refresh_device_rows_for_task_identity(task)
        self._refresh_summary()
        self._refresh_action_states()
        self._save_queue_state()

        self._start_task_worker(task)
        return task

    def _start_task_worker(self, task: DeviceTask) -> None:
        worker = threading.Thread(target=self._run_test_task, args=(task,), daemon=True)
        self.workers[task.task_id] = worker
        worker.start()

    def _retry_failed_task(self, task: DeviceTask) -> DeviceTask | None:
        if task.state_code != "failed" and not task.interrupted_on_exit:
            return None
        if self._has_active_task_for_same_device(task):
            messagebox.showwarning("设备已在重试", f"SN {task.sn} 已有正在排队或运行的重试任务")
            return None

        retry_ip = task.display_ip or task.requested_ip
        retry_task = DeviceTask(
            task_id=self._next_task_id(task.sn),
            sn=task.sn,
            requested_ip=retry_ip,
            mode=task.mode,
            cleanup_before_finish=task.cleanup_before_finish,
            factory_reset_before_finish=task.factory_reset_before_finish,
            auto_form_entry=task.auto_form_entry,
            auto_seed_previous_step=task.auto_seed_previous_step,
            form_model=task.form_model,
            form_grade=task.form_grade,
            form_account_name=task.form_account_name,
            actual_ip=task.actual_ip,
            reserved_ips=set(task.reserved_ips or {retry_ip}),
            network_interface=task.network_interface,
            max_attempts=task.max_attempts,
        )
        retry_task.status = self._status_text(retry_task.state_code)
        self.devices[retry_task.task_id] = retry_task
        self._insert_device_row(retry_task, select=True)
        self.selected_task_id = retry_task.task_id
        self._set_log_contents("")
        self._append_local_log(
            retry_task.task_id,
            f"SN {retry_task.sn} 从失败任务 {task.task_id} 手动重试，IP={retry_task.requested_ip}",
        )
        self._mark_daily_task_retry_pending(retry_task)
        self._refresh_device_rows_for_task_identity(task)
        self._refresh_summary()
        self._refresh_action_states()
        self._save_queue_state()
        self._start_task_worker(retry_task)
        return retry_task

    def _has_active_task_for_same_device(self, task: DeviceTask) -> bool:
        return any(
            other.task_id != task.task_id
            and other.state_code in self.ACTIVE_STATES
            and self._same_device_identity(task, other)
            for other in self.devices.values()
        )

    def _run_test_task(self, task: DeviceTask) -> None:
        try:
            final_event_sent = False
            current_sn = task.sn
            for attempt in range(1, task.max_attempts + 1):
                if task.cancel_event.is_set():
                    self._emit_task_cancelled(task, "Test cancelled by user")
                    break
                self.ui_queue.put(
                    {
                        "type": "attempt_started",
                        "task_id": task.task_id,
                        "attempt": attempt,
                        "max_attempts": task.max_attempts,
                    }
                )

                def emit(event: dict, attempt: int = attempt) -> None:
                    nonlocal final_event_sent, current_sn
                    event.setdefault("task_id", task.task_id)
                    event.setdefault("sn", task.sn)
                    current_sn = normalize_sn(str(event.get("sn") or current_sn))
                    event["attempt"] = attempt
                    if (
                        event.get("type") == "finished"
                        and event.get("status") == "failed"
                        and attempt < task.max_attempts
                        and not task.cancel_event.is_set()
                        and not is_pool_creation_timeout_error(event.get("error") or "")
                        and not is_unflashed_password_error(event.get("error") or "")
                    ):
                        retry_event = dict(event)
                        retry_event["status"] = "retrying"
                        retry_event["stage"] = f"第 {attempt}/{task.max_attempts} 次失败，准备自动重试"
                        self.ui_queue.put(retry_event)
                        return
                    if event.get("type") == "finished" and event.get("status") in {"success", "failed", "cancelled"}:
                        final_event_sent = True
                    self.ui_queue.put(event)

                # Re-read auto-form / grade / account from the GUI just before
                # we hand off to run_test. The task may have been queued
                # minutes ago with a stale snapshot — the operator might have
                # toggled the checkbox or switched grade since.
                self._sync_task_with_live_settings(task)
                try:
                    run_test(
                        task.sn,
                        task.requested_ip,
                        task.mode,
                        setup_file_log=False,
                        cleanup_before_finish=task.cleanup_before_finish,
                        factory_reset_before_finish=task.factory_reset_before_finish,
                        auto_form_entry=task.auto_form_entry,
                        form_model=task.form_model,
                        form_grade=task.form_grade,
                        form_account_name=task.form_account_name,
                        progress_cb=emit,
                        confirm_disk_shortage_cb=lambda prompt, task=task: self._confirm_disk_shortage(task, prompt),
                        confirm_previous_step_cb=lambda prompt, task=task: self._confirm_auto_seed_previous_step(
                            task,
                            prompt,
                        ),
                        resolve_form_grade_cb=lambda prompt, task=task: self._resolve_current_form_grade(task, prompt),
                        cancel_requested_cb=task.cancel_event.is_set,
                        task_id=task.task_id,
                    )
                    break
                except Exception as exc:
                    if task.cancel_event.is_set() or self._is_user_abort_error(exc):
                        if not final_event_sent:
                            self._emit_task_cancelled(task, str(exc))
                        break
                    if is_pool_creation_timeout_error(exc):
                        if not final_event_sent:
                            self.ui_queue.put(
                                {
                                    "type": "finished",
                                    "task_id": task.task_id,
                                    "sn": current_sn or task.sn,
                                    "status": "failed",
                                    "stage": failure_stage_for_error(exc),
                                    "error": str(exc),
                                }
                            )
                        break
                    if is_unflashed_password_error(exc):
                        if not final_event_sent:
                            self.ui_queue.put(
                                {
                                    "type": "finished",
                                    "task_id": task.task_id,
                                    "sn": current_sn or task.sn,
                                    "status": "failed",
                                    "stage": failure_stage_for_error(exc),
                                    "error": str(exc),
                                }
                            )
                        break
                    if task.auto_form_entry and self._is_previous_step_error(exc):
                        if not final_event_sent:
                            self.ui_queue.put(
                                {
                                    "type": "finished",
                                    "task_id": task.task_id,
                                    "sn": task.sn,
                                    "status": "failed",
                                    "stage": "缺少第一步",
                                    "error": str(exc),
                                }
                            )
                        break
                    if attempt >= task.max_attempts:
                        if not final_event_sent:
                            self.ui_queue.put(
                                {
                                    "type": "finished",
                                    "task_id": task.task_id,
                                    "sn": current_sn or task.sn,
                                    "status": "failed",
                                    "stage": failure_stage_for_error(exc),
                                    "error": str(exc),
                                }
                            )
                        break
                    self.ui_queue.put(
                        {
                            "type": "stage",
                            "task_id": task.task_id,
                            "sn": task.sn,
                            "status": "retrying",
                            "stage": f"第 {attempt}/{task.max_attempts} 次失败，准备自动重试",
                        }
                    )
                    self.ui_queue.put(
                        {
                            "type": "local_log",
                            "task_id": task.task_id,
                            "message": f"第 {attempt}/{task.max_attempts} 次测试失败：{exc}；3 秒后自动重试",
                        }
                    )
                    time.sleep(3)
        except Exception:
            pass
        finally:
            self.ui_queue.put({"type": "thread_done", "task_id": task.task_id})

    def _is_previous_step_error(self, exc: Exception) -> bool:
        message = str(exc)
        return "缺少第一步翻新记录" in message or "previous refurbishment process" in message

    def _emit_task_cancelled(self, task: DeviceTask, error: str) -> None:
        self.ui_queue.put(
            {
                "type": "finished",
                "task_id": task.task_id,
                "sn": task.sn,
                "status": "cancelled",
                "stage": "\u7528\u6237\u4e2d\u65ad",
                "error": error,
            }
        )

    def _is_user_abort_error(self, exc: Exception) -> bool:
        message = str(exc)
        return (
            "Storage-pool creation aborted by user" in message
            or "Test cancelled by user" in message
            or "Previous-step auto seed cancelled by user" in message
        )

    def _confirm_disk_shortage(self, task: DeviceTask, prompt: dict) -> bool:
        reply: dict[str, bool] = {"answer": False}
        done = threading.Event()
        self.ui_queue.put(
            {
                "type": "confirm_disk_shortage",
                "task_id": task.task_id,
                "sn": task.sn,
                "reply": reply,
                "done": done,
                **prompt,
            }
        )
        done.wait()
        return bool(reply.get("answer"))

    def _sync_task_with_live_settings(self, task: DeviceTask) -> None:
        """From the worker thread: pull current auto-form / grade / account
        from the GUI into ``task`` so a mid-queue toggle takes effect."""
        reply: dict[str, object] = {}
        done = threading.Event()
        self.ui_queue.put(
            {
                "type": "refresh_task_settings",
                "task_id": task.task_id,
                "reply": reply,
                "done": done,
            }
        )
        done.wait()
        if "auto_form_entry" in reply:
            task.auto_form_entry = bool(reply["auto_form_entry"])
        if reply.get("form_grade"):
            task.form_grade = str(reply["form_grade"])
        if reply.get("form_account_name"):
            task.form_account_name = str(reply["form_account_name"])

    def _resolve_current_form_grade(self, task: DeviceTask, prompt: dict | None = None) -> str:
        prompt = prompt or {}
        fallback = str(prompt.get("grade") or task.form_grade or "A").strip().upper()
        reply: dict[str, str] = {"grade": fallback}
        done = threading.Event()
        self.ui_queue.put(
            {
                "type": "resolve_form_grade",
                "task_id": task.task_id,
                "sn": prompt.get("sn") or task.sn,
                "grade": fallback,
                "reply": reply,
                "done": done,
            }
        )
        done.wait()
        grade = str(reply.get("grade") or fallback).strip().upper()
        if grade not in {"A", "B"}:
            grade = fallback if fallback in {"A", "B"} else "A"
        if grade != task.form_grade:
            self.ui_queue.put(
                {
                    "type": "local_log",
                    "task_id": task.task_id,
                    "message": f"录表前读取当前等级：{grade}",
                }
            )
        task.form_grade = grade
        return grade

    def _confirm_auto_seed_previous_step(self, task: DeviceTask, prompt: dict | None = None) -> bool:
        prompt = prompt or {}
        if task.auto_seed_previous_step:
            self.ui_queue.put(
                {
                    "type": "local_log",
                    "task_id": task.task_id,
                    "message": "按录表配置自动补录第一步，不弹窗询问",
                }
            )
            return True
        reply: dict[str, bool] = {"answer": False}
        done = threading.Event()
        self.ui_queue.put(
            {
                "type": "confirm_previous_step",
                "task_id": task.task_id,
                "sn": prompt.get("sn") or task.sn,
                "reply": reply,
                "done": done,
            }
        )
        done.wait()
        return bool(reply.get("answer"))

    def _on_cancel_task(self) -> None:
        task = self._selected_task()
        if task is None:
            messagebox.showwarning("\u672a\u9009\u62e9\u8bbe\u5907", "\u8bf7\u5148\u5728\u5de6\u4fa7\u961f\u5217\u4e2d\u9009\u4e2d\u4e00\u53f0\u8bbe\u5907")
            return
        if task.state_code == "failed" or task.interrupted_on_exit:
            self._retry_failed_task(task)
            return
        if task.state_code not in self.ACTIVE_STATES:
            messagebox.showwarning("\u65e0\u6cd5\u4e2d\u65ad", "\u53ea\u80fd\u4e2d\u65ad\u6b63\u5728\u6392\u961f\u6216\u8fd0\u884c\u7684\u4efb\u52a1")
            return
        if task.cancel_event.is_set():
            return
        if not messagebox.askyesno("\u4e2d\u65ad\u4efb\u52a1", f"\u786e\u5b9a\u4e2d\u65ad SN {task.sn} \u7684\u5f53\u524d\u6d4b\u8bd5\uff1f"):
            return

        task.cancel_event.set()
        task.state_code = "cancelling"
        task.status = self._status_text("cancelling")
        task.current_step = "\u6b63\u5728\u4e2d\u65ad"
        self._refresh_device_row(task)
        self._append_local_log(task.task_id, "\u5df2\u624b\u52a8\u8bf7\u6c42\u4e2d\u65ad\uff0c\u6b63\u5728\u5173\u95ed\u540e\u53f0\u6d4f\u89c8\u5668\u5e76\u7b49\u5f85\u4efb\u52a1\u6536\u5c3e")
        if task.browser_pid is not None and terminate_browser_process(task.browser_pid):
            self._append_local_log(task.task_id, "\u5df2\u5173\u95ed\u8be5\u4efb\u52a1\u7684\u540e\u53f0\u6d4f\u89c8\u5668")
            task.browser_pid = None
        self._refresh_action_states()
        self._save_queue_state()

    def _on_show_browser(self) -> None:
        task = self._selected_task()
        if task is None:
            messagebox.showwarning("未选择设备", "请先在左侧队列中选中一台设备")
            return

        if task.browser_pid is None:
            messagebox.showwarning("浏览器不可用", "当前设备还没有可显示的浏览器窗口")
            return

        if show_browser_windows(task.browser_pid):
            self._append_local_log(task.task_id, "已显示浏览器窗口")
            self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  已显示 {task.sn} 的浏览器窗口")
        else:
            messagebox.showwarning("显示失败", "未找到该设备对应的浏览器窗口，可能已经结束")

    def _remove_current_task(self) -> None:
        task = self._selected_task()
        if task is None:
            return
        if task.state_code in self.ACTIVE_STATES:
            messagebox.showwarning("任务仍在运行", "请先中断或等待本台完成后再移除。")
            return
        self._remove_task(task)

    def _remove_task(self, task: DeviceTask) -> None:
        task_id = task.task_id
        self.devices.pop(task_id, None)
        self.workers.pop(task_id, None)
        if self.device_tree is not None and self.device_tree.exists(task_id):
            self.device_tree.delete(task_id)

        if self.selected_task_id == task_id:
            self.selected_task_id = None
            self._set_log_contents(self._t("select_device_log_sentence"))
            self._clear_timing_chart()
            self._clear_materials_tab(self._t("materials_select_device"))
            self._clear_measurements_tab(self._t("measure_select_device"))

        self._refresh_device_rows_for_task_identity(task)
        self._refresh_summary()
        self._refresh_action_states()
        self._save_queue_state(merge_existing=False)

    def _on_select_device(self, _event) -> None:
        if self.device_tree is None:
            return
        selection = self.device_tree.selection()
        # Multi-select (Shift / Ctrl-click) is supported; detail panes follow
        # the first item so they keep showing one device at a time.
        self.selected_task_ids = list(selection)
        self.selected_task_id = selection[0] if selection else None
        self._refresh_action_states()
        if self.selected_task_id is None:
            self._set_log_contents(self._t("select_device_log_sentence"))
            self._clear_timing_chart()
            self._clear_materials_tab(self._t("materials_select_device"))
            self._clear_measurements_tab(self._t("measure_select_device"))
            return

        task = self.devices.get(self.selected_task_id)
        if task is None:
            self._set_log_contents(self._t("select_device_log_sentence"))
            self._clear_timing_chart()
            self._clear_materials_tab(self._t("materials_select_device"))
            self._clear_measurements_tab(self._t("measure_select_device"))
            return

        self._set_log_contents("".join(task.logs) if task.logs else self._t("no_logs"))
        self._cancel_timing_chart_refresh()
        self._refresh_timing_chart(task)
        self._refresh_materials_tab(task)
        self._schedule_materials_refresh(delay_ms=0)
        self._refresh_measurements_tab(task)
        self._schedule_measurements_refresh(delay_ms=0)

    def _refresh_summary(self) -> None:
        queued = sum(1 for task in self.devices.values() if task.state_code == "queued")
        running = sum(1 for task in self.devices.values() if task.state_code == "running")
        transfer = sum(1 for task in self.devices.values() if task.state_code == "transfer")
        on_hold = sum(1 for task in self.devices.values() if task.state_code == "on_hold")
        retrying = sum(1 for task in self.devices.values() if task.state_code == "retrying")
        cancelling = sum(1 for task in self.devices.values() if task.state_code == "cancelling")
        success = sum(1 for task in self.devices.values() if task.state_code == "success")
        failed = sum(1 for task in self.devices.values() if self.ROW_FAILED_TAG in self._row_tags_for_task(task))
        cancelled = sum(1 for task in self.devices.values() if task.state_code == "cancelled")
        active = queued + running + transfer + on_hold + retrying + cancelling

        self.queue_summary_var.set(self._t("queue_summary", total=len(self.devices), active=active))
        self._refresh_status_counts()
        self.status_var.set(
            f"{datetime.now().strftime('%H:%M:%S')}  "
            + self._t(
                "status_summary",
                running=running + transfer,
                on_hold=on_hold,
                retrying=retrying,
                queued=queued,
                success=success,
                failed=failed,
            )
        )

    def _refresh_status_counts(self, success: int | None = None, failed: int | None = None) -> None:
        if success is None or failed is None:
            daily_success, daily_failed = self._daily_status_counts()
            if success is None:
                success = daily_success
            if failed is None:
                failed = daily_failed
        self.success_count_var.set(self._t("success_count", count=success))
        self.failed_count_var.set(self._t("failed_count", count=failed))

    def _refresh_action_states(self) -> None:
        selected = self._selected_task()
        remove_enabled = selected is not None and selected.state_code not in self.ACTIVE_STATES
        if self.remove_current_btn is not None:
            self.remove_current_btn.configure(state=tk.NORMAL if remove_enabled else tk.DISABLED)
        show_enabled = selected is not None and selected.browser_pid is not None
        if self.show_browser_btn is not None:
            self.show_browser_btn.configure(state=tk.NORMAL if show_enabled else tk.DISABLED)
        if self.cancel_btn is not None:
            show_retry = selected is not None and (selected.state_code == "failed" or selected.interrupted_on_exit)
            action_key = "retry_task" if show_retry else "cancel_task"
            cancel_enabled = (
                selected is not None
                and (
                    show_retry
                    or (selected.state_code in self.ACTIVE_STATES and not selected.cancel_event.is_set())
                )
            )
            self.cancel_btn.configure(text=self._t(action_key))
            self.cancel_btn.configure(state=tk.NORMAL if cancel_enabled else tk.DISABLED)

    def _on_close(self) -> None:
        if self.materials_refresh_after_id is not None:
            try:
                self.root.after_cancel(self.materials_refresh_after_id)
            except Exception:
                pass
            self.materials_refresh_after_id = None
        if self.daily_rollover_after_id is not None:
            try:
                self.root.after_cancel(self.daily_rollover_after_id)
            except Exception:
                pass
            self.daily_rollover_after_id = None
        for task in self.devices.values():
            if task.state_code not in self.ACTIVE_STATES:
                continue
            task.cancel_event.set()
            task.state_code = "cancelled"
            task.status = self._status_text("cancelled")
            task.current_step = f"上次退出时未完成：{task.current_step}"
            task.finished_monotonic = time.monotonic()
            task.interrupted_on_exit = True
        self._save_queue_state()
        try:
            self.root.destroy()
        except Exception:
            pass

    def _next_task_id(self, sn: str) -> str:
        while True:
            self.task_counter += 1
            task_id = f"{sn}-{self.task_counter:03d}"
            if task_id not in self.devices:
                return task_id

    def _has_active_sn(self, sn: str) -> bool:
        return any(
            same_sn_identity(task.sn, sn) and task.state_code in self.ACTIVE_STATES
            for task in self.devices.values()
        )

    def _has_any_sn(self, sn: str) -> bool:
        return any(same_sn_identity(task.sn, sn) for task in self.devices.values())

    def _known_ips(self) -> set[str]:
        ips: set[str] = set()
        for task in self.devices.values():
            for ip in (task.requested_ip, task.actual_ip):
                normalized = str(ip or "").strip()
                if normalized and normalized.lower() != "auto":
                    ips.add(normalized)
            for ip in task.reserved_ips:
                normalized = str(ip or "").strip()
                if normalized and normalized.lower() != "auto":
                    ips.add(normalized)
        return ips

    def _has_known_ip(self, ip: str) -> bool:
        ip_text = str(ip or "").strip()
        return ip_text in self._known_ips()

    def _active_task_count(self) -> int:
        return sum(1 for task in self.devices.values() if task.state_code in self.ACTIVE_STATES)

    def _selected_task(self) -> DeviceTask | None:
        if self.selected_task_id is None:
            return None
        return self.devices.get(self.selected_task_id)


def main() -> None:
    root = tk.Tk()
    FactoryTestGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
