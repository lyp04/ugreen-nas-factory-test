from __future__ import annotations

import os
import ipaddress
import json
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from datetime import datetime
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
        run_smoke,
        run_test,
    )
    from src.discovery import ugreen_broadcast
    from src.discovery.discover import find_nas_candidates
    from src.utils.browser_control import show_browser_windows, terminate_browser_process
    from src.utils.config_loader import load_configs
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
        run_smoke,
        run_test,
    )
    from .discovery import ugreen_broadcast
    from .discovery.discover import find_nas_candidates
    from .utils.browser_control import show_browser_windows, terminate_browser_process
    from .utils.config_loader import load_configs
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


def format_elapsed(seconds: int) -> str:
    hours, remainder = divmod(max(0, seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def sort_ip_strings(ips) -> list[str]:
    def key(ip: str):
        try:
            return (0, ipaddress.ip_address(ip))
        except ValueError:
            return (1, ip)

    return sorted(dict.fromkeys(str(ip).strip() for ip in ips if ip and str(ip).strip()), key=key)


LANGUAGE_OPTIONS = ("中文", "English")

UI_TEXT = {
    "zh": {
        "app_title": "UGREEN NAS 出厂测试",
        "ready": "就绪",
        "queue_summary_empty": "连接设备：0 台",
        "select_device_log": "选择左侧设备查看右侧日志",
        "select_device_log_sentence": "选择左侧设备查看右侧日志。",
        "no_logs": "该设备当前还没有日志。",
        "test_params": "测试参数",
        "sn_label": "SN/后4位:",
        "nas_ip_label": "NAS IP:",
        "auto_discovery": "自动发现",
        "manual_input": "手动输入",
        "auto_scan": "自动扫描全网入队",
        "mode_label": "模式:",
        "setup_mode": "首次设置（注册 + 截图）",
        "login_mode": "已注册（登录 + 截图）",
        "finish_actions": "结束前操作:",
        "cleanup_pools": "清理存储池",
        "factory_reset": "恢复出厂设置",
        "auto_form_entry": "自动录表",
        "form_settings": "录表配置:",
        "model_auto": "机型按 SN 自动识别",
        "grade": "等级",
        "account": "账号",
        "switch_account": "切换账号",
        "add_account": "添加账号",
        "delete_account": "删除账号",
        "auto_seed_previous": "缺第一步时自动补录",
        "add_to_queue": "添加到队列",
        "smoke_check": "冒烟检查",
        "open_screenshot_dir": "打开截图目录",
        "remove_finished": "移除已完成",
        "show_browser": "显示浏览器",
        "cancel_task": "中断任务",
        "connected_devices": "连接设备",
        "log": "日志",
        "tree_status": "状态",
        "tree_elapsed": "运行时间",
        "tree_progress": "进度",
        "tree_step": "当前步骤",
        "sound": "提示音",
        "language_zh": "中文",
        "language_en": "English",
        "auto_form_off": "自动录表未开启，本次不会提交表单。",
        "queue_summary": "连接设备：{total} 台，活跃 {active} 台",
        "status_summary": "运行中 {running} | On Hold {on_hold} | 重试 {retrying} | 排队 {queued} | 完成 {success} | 失败 {failed}",
    },
    "en": {
        "app_title": "UGREEN NAS Factory Test",
        "ready": "Ready",
        "queue_summary_empty": "Connected devices: 0",
        "select_device_log": "Select a device on the left to view logs",
        "select_device_log_sentence": "Select a device on the left to view logs.",
        "no_logs": "This device has no logs yet.",
        "test_params": "Test Parameters",
        "sn_label": "SN / Last 4:",
        "nas_ip_label": "NAS IP:",
        "auto_discovery": "Auto discover",
        "manual_input": "Manual IP",
        "auto_scan": "Auto-scan LAN into queue",
        "mode_label": "Mode:",
        "setup_mode": "First setup (register + capture)",
        "login_mode": "Registered (login + capture)",
        "finish_actions": "Finish actions:",
        "cleanup_pools": "Clean storage pools",
        "factory_reset": "Factory reset",
        "auto_form_entry": "Auto form entry",
        "form_settings": "Form settings:",
        "model_auto": "Model auto-detected by SN",
        "grade": "Grade",
        "account": "Account",
        "switch_account": "Switch account",
        "add_account": "Add account",
        "delete_account": "Delete account",
        "auto_seed_previous": "Auto-fill step 1 if missing",
        "add_to_queue": "Add to queue",
        "smoke_check": "Smoke check",
        "open_screenshot_dir": "Open screenshot folder",
        "remove_finished": "Remove completed",
        "show_browser": "Show browser",
        "cancel_task": "Cancel task",
        "connected_devices": "Connected Devices",
        "log": "Log",
        "tree_status": "Status",
        "tree_elapsed": "Elapsed",
        "tree_progress": "Progress",
        "tree_step": "Current step",
        "sound": "Sound",
        "language_zh": "中文",
        "language_en": "English",
        "auto_form_off": "Auto form entry is off; this run will not submit the form.",
        "queue_summary": "Connected devices: {total}, active {active}",
        "status_summary": "Running {running} | On Hold {on_hold} | Retrying {retrying} | Queued {queued} | Done {success} | Failed {failed}",
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

    ACTIVE_STATES = {"queued", "running", "transfer", "on_hold", "retrying", "cancelling"}
    AUTO_SCAN_INTERVAL_MS = 15_000
    AUTO_SCAN_RETRY_MS = 5_000

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
        self.smoke_worker: threading.Thread | None = None
        self.task_counter = 0
        self.selected_task_id: str | None = None

        self.sn_var = tk.StringVar()
        self.ip_mode_var = tk.StringVar(value="auto")
        self.manual_ip_var = tk.StringVar()
        self.auto_scan_var = tk.BooleanVar(value=False)
        self.flow_mode_var = tk.StringVar(value="setup")
        self.cleanup_before_finish_var = tk.BooleanVar(value=True)
        self.factory_reset_before_finish_var = tk.BooleanVar(value=True)
        self.auto_form_entry_var = tk.BooleanVar(value=FORM_ENTRY_ENABLED)
        self.auto_seed_previous_step_var = tk.BooleanVar(value=False)
        self.form_grade_var = tk.StringVar(value="A")
        self.form_account_var = tk.StringVar()
        self.sound_enabled_var = tk.BooleanVar(value=True)
        self.language_var = tk.StringVar(value=LANGUAGE_OPTIONS[0])
        self.status_var = tk.StringVar(value=self._t("ready"))
        self.queue_summary_var = tk.StringVar(value=self._t("queue_summary_empty"))
        self.log_title_var = tk.StringVar(value=self._t("select_device_log"))
        self._text_widgets: dict[str, tk.Widget] = {}

        self.sn_entry: ttk.Entry | None = None
        self.start_btn: ttk.Button | None = None
        self.smoke_btn: ttk.Button | None = None
        self.show_browser_btn: ttk.Button | None = None
        self.cancel_btn: ttk.Button | None = None
        self.auto_scan_check: ttk.Checkbutton | None = None
        self.cleanup_check: ttk.Checkbutton | None = None
        self.factory_reset_check: ttk.Checkbutton | None = None
        self.auto_form_entry_check: ttk.Checkbutton | None = None
        self.auto_seed_previous_step_check: ttk.Checkbutton | None = None
        self.sound_check: ttk.Checkbutton | None = None
        self.language_combo: ttk.Combobox | None = None
        self.form_grade_radios: list[ttk.Radiobutton] = []
        self.form_account_combo: ttk.Combobox | None = None
        self.device_tree: ttk.Treeview | None = None
        self.log_view: scrolledtext.ScrolledText | None = None
        self.auto_scan_worker: threading.Thread | None = None
        self.auto_scan_after_id: str | None = None

        self._build_layout()
        if self.form_entry_enabled:
            self._refresh_form_accounts()
        self._attach_logger_sink()
        self._refresh_action_states()
        self.root.after(100, self._drain_ui_queue)
        self.root.after(1000, self._refresh_elapsed_times)

    def _language_code(self) -> str:
        try:
            return "en" if self.language_var.get() == "English" else "zh"
        except Exception:
            return "zh"

    def _t(self, key: str, **kwargs) -> str:
        text = UI_TEXT.get(self._language_code(), UI_TEXT["zh"]).get(key, UI_TEXT["zh"].get(key, key))
        return text.format(**kwargs) if kwargs else text

    def _register_text(self, key: str, widget):
        self._text_widgets[key] = widget
        return widget

    def _status_text(self, status_code: str) -> str:
        source = self.STATUS_TEXT_EN if self._language_code() == "en" else self.STATUS_TEXT
        return source.get(status_code, status_code)

    def _timestamped(self, key: str, **kwargs) -> str:
        return f"{datetime.now().strftime('%H:%M:%S')}  {self._t(key, **kwargs)}"

    def _build_layout(self) -> None:
        self.root.title(self._t("app_title"))
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(1, weight=1)

        controls = self._register_text("test_params", ttk.LabelFrame(main, text=self._t("test_params"), padding=12))
        controls.grid(row=0, column=0, sticky=tk.EW)
        controls.columnconfigure(1, weight=1)

        self._register_text("sn_label", ttk.Label(controls, text=self._t("sn_label"))).grid(
            row=0, column=0, sticky=tk.W, pady=4
        )
        self.sn_entry = ttk.Entry(controls, textvariable=self.sn_var, width=40)
        self.sn_entry.grid(row=0, column=1, sticky=tk.EW, pady=4)
        self.sn_entry.bind("<Return>", self._on_sn_enter)
        self.sn_entry.focus()

        self._register_text("nas_ip_label", ttk.Label(controls, text=self._t("nas_ip_label"))).grid(
            row=1, column=0, sticky=tk.W, pady=4
        )
        ip_frame = ttk.Frame(controls)
        ip_frame.grid(row=1, column=1, sticky=tk.EW, pady=4)
        self._register_text(
            "auto_discovery",
            ttk.Radiobutton(ip_frame, text=self._t("auto_discovery"), variable=self.ip_mode_var, value="auto"),
        ).pack(side=tk.LEFT, padx=(0, 8))
        self._register_text(
            "manual_input",
            ttk.Radiobutton(ip_frame, text=self._t("manual_input"), variable=self.ip_mode_var, value="manual"),
        ).pack(side=tk.LEFT)
        ttk.Entry(ip_frame, textvariable=self.manual_ip_var, width=20).pack(side=tk.LEFT, padx=(8, 0))
        self.auto_scan_check = ttk.Checkbutton(
            ip_frame,
            text=self._t("auto_scan"),
            variable=self.auto_scan_var,
            command=self._on_auto_scan_toggle,
        )
        self._register_text("auto_scan", self.auto_scan_check)
        self.auto_scan_check.pack(side=tk.LEFT, padx=(12, 0))

        self._register_text("mode_label", ttk.Label(controls, text=self._t("mode_label"))).grid(
            row=2, column=0, sticky=tk.W, pady=4
        )
        mode_frame = ttk.Frame(controls)
        mode_frame.grid(row=2, column=1, sticky=tk.EW, pady=4)
        self._register_text(
            "setup_mode",
            ttk.Radiobutton(
                mode_frame,
                text=self._t("setup_mode"),
                variable=self.flow_mode_var,
                value="setup",
            ),
        ).pack(side=tk.LEFT, padx=(0, 12))
        self._register_text(
            "login_mode",
            ttk.Radiobutton(
                mode_frame,
                text=self._t("login_mode"),
                variable=self.flow_mode_var,
                value="login",
            ),
        ).pack(side=tk.LEFT)

        self._register_text("finish_actions", ttk.Label(controls, text=self._t("finish_actions"))).grid(
            row=3, column=0, sticky=tk.NW, pady=4
        )
        option_frame = ttk.Frame(controls)
        option_frame.grid(row=3, column=1, sticky=tk.EW, pady=4)
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
        )
        self._register_text("auto_form_entry", self.auto_form_entry_check)
        self.auto_form_entry_check.pack(side=tk.LEFT)

        form_config_label = self._register_text("form_settings", ttk.Label(controls, text=self._t("form_settings")))
        form_config_label.grid(row=4, column=0, sticky=tk.W, pady=4)
        form_frame = ttk.Frame(controls)
        form_frame.grid(row=4, column=1, sticky=tk.EW, pady=4)
        self._register_text("model_auto", ttk.Label(form_frame, text=self._t("model_auto"))).pack(
            side=tk.LEFT, padx=(0, 12)
        )
        self._register_text("grade", ttk.Label(form_frame, text=self._t("grade"))).pack(side=tk.LEFT, padx=(0, 4))
        self.form_grade_radios = []
        for grade in ("A", "B"):
            radio = ttk.Radiobutton(form_frame, text=grade, variable=self.form_grade_var, value=grade)
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
        self.auto_seed_previous_step_check = ttk.Checkbutton(
            form_frame,
            text=self._t("auto_seed_previous"),
            variable=self.auto_seed_previous_step_var,
        )
        self._register_text("auto_seed_previous", self.auto_seed_previous_step_check)
        self.auto_seed_previous_step_check.pack(side=tk.LEFT)

        button_row = 5
        if not self.form_entry_enabled:
            self.auto_form_entry_var.set(False)
            if self.auto_form_entry_check is not None:
                self.auto_form_entry_check.pack_forget()
            form_config_label.grid_remove()
            form_frame.grid_remove()
            button_row = 4

        btn_frame = ttk.Frame(controls)
        btn_frame.grid(row=button_row, column=0, columnspan=2, sticky=tk.EW, pady=(8, 0))
        self.start_btn = ttk.Button(btn_frame, text=self._t("add_to_queue"), command=self._on_start_test)
        self._register_text("add_to_queue", self.start_btn)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.smoke_btn = ttk.Button(btn_frame, text=self._t("smoke_check"), command=self._on_smoke)
        self._register_text("smoke_check", self.smoke_btn)
        self.smoke_btn.pack(side=tk.LEFT, padx=(0, 6))
        self._register_text(
            "open_screenshot_dir", ttk.Button(btn_frame, text=self._t("open_screenshot_dir"), command=self._open_output)
        ).pack(side=tk.LEFT, padx=(0, 6))
        self._register_text(
            "remove_finished", ttk.Button(btn_frame, text=self._t("remove_finished"), command=self._remove_finished_tasks)
        ).pack(side=tk.LEFT, padx=(0, 6))
        self.show_browser_btn = ttk.Button(btn_frame, text=self._t("show_browser"), command=self._on_show_browser)
        self._register_text("show_browser", self.show_browser_btn)
        self.show_browser_btn.pack(side=tk.LEFT, padx=(0, 6))
        self.cancel_btn = ttk.Button(btn_frame, text=self._t("cancel_task"), command=self._on_cancel_task)
        self._register_text("cancel_task", self.cancel_btn)
        self.cancel_btn.pack(side=tk.LEFT)

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
        self.device_tree = ttk.Treeview(queue_frame, columns=columns, show="headings", selectmode="browse")
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

        log_frame = self._register_text("log", ttk.LabelFrame(content, text=self._t("log"), padding=8))
        log_frame.columnconfigure(0, weight=1)
        log_frame.rowconfigure(1, weight=1)
        content.add(log_frame, weight=2)

        ttk.Label(log_frame, textvariable=self.log_title_var).grid(row=0, column=0, sticky=tk.W, pady=(0, 8))
        self.log_view = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, font=("Consolas", 9))
        self.log_view.grid(row=1, column=0, sticky=tk.NSEW)
        self.log_view.configure(state=tk.DISABLED)
        self._set_log_contents(self._t("select_device_log_sentence"))

        status_bar = ttk.Frame(main, relief=tk.SUNKEN, padding=(6, 2))
        status_bar.grid(row=2, column=0, sticky=tk.EW, pady=(8, 0))
        ttk.Label(status_bar, textvariable=self.status_var).pack(side=tk.LEFT)
        self.language_combo = ttk.Combobox(
            status_bar,
            textvariable=self.language_var,
            values=LANGUAGE_OPTIONS,
            width=10,
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
        for task in self.devices.values():
            task.status = self._status_text(task.state_code)
            self._refresh_device_row(task)
        if self.selected_task_id is None:
            self.log_title_var.set(self._t("select_device_log"))
        self._refresh_log_title()
        self._refresh_summary()

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
        elif event_type == "smoke_done":
            self._finish_smoke(event)
        elif event_type == "auto_scan_done":
            self._finish_auto_scan(event)
        elif event_type == "confirm_previous_step":
            self._handle_confirm_previous_step(event)
        elif event_type == "confirm_disk_shortage":
            self._handle_confirm_disk_shortage(event)
        elif event_type == "resolve_form_grade":
            self._handle_resolve_form_grade(event)

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
        fallback = str(event.get("grade") or (task.form_grade if task is not None else "A") or "A").strip().upper()
        grade = str(self.form_grade_var.get() or fallback).strip().upper()
        if grade not in {"A", "B"}:
            grade = fallback if fallback in {"A", "B"} else "A"
        if task is not None:
            task.form_grade = grade
        if isinstance(reply, dict):
            reply["grade"] = grade
        if done is not None:
            done.set()

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
        self._refresh_log_title()
        self._refresh_summary()
        self._refresh_action_states()

    def _handle_task_event(self, event: dict) -> None:
        task = self.devices.get(str(event.get("task_id")))
        if task is None:
            return

        status_code = str(event.get("status") or task.state_code)
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
        if event.get("type") == "finished" and status_code == "failed":
            task.finished_monotonic = time.monotonic()
            self._show_failure_alert_if_needed(task, str(event.get("error") or ""))
            self._play_completion_sound(success=False)
        if event.get("type") == "finished" and status_code == "cancelled":
            task.finished_monotonic = time.monotonic()
            self._play_completion_sound(success=False)

        self._refresh_device_row(task)
        self._refresh_log_title()
        self._refresh_summary()
        self._refresh_action_states()

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
            if self._sn_output_has_images(sn_root) or self._sn_output_has_success_report(sn_root, normalized):
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
        return bool(report_sn and same_sn_identity(report_sn, sn))

    def _read_report(self, report_path: Path) -> dict | None:
        try:
            return json.loads(report_path.read_text(encoding="utf-8"))
        except Exception:
            return None

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
        )

    def _refresh_elapsed_times(self) -> None:
        for task in self.devices.values():
            if task.state_code in self.ACTIVE_STATES:
                self._refresh_device_row(task)
        self.root.after(1000, self._refresh_elapsed_times)

    def _append_local_log(self, task_id: str, message: str) -> None:
        task = self.devices.get(task_id)
        if task is None:
            return
        line = f"{datetime.now().strftime('%H:%M:%S')} | INFO    | {message}\n"
        task.logs.append(line)
        if task.task_id == self.selected_task_id:
            self._append_log(line)

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

    def _nas_ip(self) -> str:
        if self.ip_mode_var.get() == "manual":
            ip = self.manual_ip_var.get().strip()
            if not ip:
                raise ValueError("手动模式下请输入 NAS IP")
            return ip
        return "auto"

    def _on_factory_reset_toggle(self) -> None:
        if self.factory_reset_before_finish_var.get():
            self.cleanup_before_finish_var.set(True)

    def _on_cleanup_toggle(self) -> None:
        if self.factory_reset_before_finish_var.get() and not self.cleanup_before_finish_var.get():
            self.cleanup_before_finish_var.set(True)

    def _on_auto_scan_toggle(self) -> None:
        if self.auto_scan_var.get():
            if not self._validate_form_settings():
                self.auto_scan_var.set(False)
                return
            self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  自动扫描已开启")
            self._schedule_auto_scan(0)
            return

        if self.auto_scan_after_id is not None:
            try:
                self.root.after_cancel(self.auto_scan_after_id)
            except Exception:
                pass
            self.auto_scan_after_id = None
        self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  自动扫描已关闭")

    def _schedule_auto_scan(self, delay_ms: int) -> None:
        if not self.auto_scan_var.get():
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
        if self.auto_scan_var.get():
            self._schedule_auto_scan(self.AUTO_SCAN_INTERVAL_MS if success else self.AUTO_SCAN_RETRY_MS)

    def _on_sn_enter(self, _event) -> None:
        self._on_start_test()

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

    def _refresh_form_grade_options(self) -> None:
        if not self.form_entry_enabled:
            return
        grades = ["A", "B"]
        if self.form_grade_var.get() not in grades and grades:
            self.form_grade_var.set(grades[0])

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

        account_var = tk.StringVar()
        password_var = tk.StringVar()
        captcha_var = tk.StringVar()
        client_var = tk.StringVar()
        captcha_path = self.project_root / "state" / "captcha_login.jpg"

        frame = ttk.Frame(dialog, padding=12)
        frame.grid(row=0, column=0, sticky=tk.NSEW)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="账号").grid(row=0, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=account_var, width=32).grid(row=0, column=1, sticky=tk.EW, pady=4)
        ttk.Label(frame, text="密码").grid(row=1, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=password_var, show="*", width=32).grid(row=1, column=1, sticky=tk.EW, pady=4)
        ttk.Label(frame, text="验证码").grid(row=2, column=0, sticky=tk.W, pady=4)
        ttk.Entry(frame, textvariable=captcha_var, width=18).grid(row=2, column=1, sticky=tk.W, pady=4)
        status_var = tk.StringVar(value="点击刷新验证码后，会打开验证码图片。")
        ttk.Label(frame, textvariable=status_var).grid(row=3, column=0, columnspan=2, sticky=tk.W, pady=(4, 8))

        def refresh_captcha() -> None:
            try:
                info = form_entry.get_login_captcha()
                client_var.set(info["client"])
                form_entry.save_captcha_image(info["captcha"], captcha_path)
                try:
                    os.startfile(captcha_path)
                except Exception:
                    pass
                status_var.set(f"验证码已刷新：{captcha_path}")
            except Exception as exc:
                messagebox.showerror("验证码失败", str(exc), parent=dialog)

        def save_account() -> None:
            account = account_var.get().strip()
            password = password_var.get()
            captcha = captcha_var.get().strip()
            client_id = client_var.get().strip()
            if not account or not password or not captcha or not client_id:
                messagebox.showwarning("缺少信息", "请填写账号、密码、验证码，并先刷新验证码。", parent=dialog)
                return
            try:
                entry = form_entry.add_or_update_account(
                    self.project_root,
                    account=account,
                    password=password,
                    captcha=captcha,
                    client_id=client_id,
                )
                self._refresh_form_accounts()
                self.form_account_var.set(str(entry.get("name") or account))
                messagebox.showinfo("账号已保存", f"当前录表账号：{self.form_account_var.get()}", parent=dialog)
                dialog.destroy()
            except Exception as exc:
                messagebox.showerror("登录失败", str(exc), parent=dialog)
                refresh_captcha()

        buttons = ttk.Frame(frame)
        buttons.grid(row=4, column=0, columnspan=2, sticky=tk.EW)
        ttk.Button(buttons, text="刷新验证码", command=refresh_captcha).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="保存并切换", command=save_account).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side=tk.LEFT)
        refresh_captcha()

    def _validate_form_settings(self) -> bool:
        if not self.form_entry_enabled:
            self.auto_form_entry_var.set(False)
            return True
        if not self.auto_form_entry_var.get():
            self.status_var.set(self._timestamped("auto_form_off"))
            return True
        account_name = self.form_account_var.get().strip()
        if not account_name:
            messagebox.showwarning("缺少录表账号", "请先添加或选择一个录表账号。")
            return False
        try:
            for model in form_entry.available_models(self.project_root):
                form_entry.build_report_form_data(
                    {"sn": "PREVIEW"},
                    self.project_root,
                    model=model,
                    grade=self.form_grade_var.get(),
                )
        except Exception as exc:
            messagebox.showwarning("录表配置不可用", str(exc))
            return False
        return True

    def _on_start_test(self) -> None:
        if not self._validate_form_settings():
            return
        sn = normalize_sn(self.sn_var.get().strip())
        if not sn:
            messagebox.showwarning("缺少 SN", "请先扫码或输入序列号")
            return
        if len(sn_tail(sn)) < 4:
            messagebox.showwarning("SN 过短", "请输入完整 SN 或最后 4 位 SN")
            return

        if self._has_active_sn(sn):
            messagebox.showwarning("设备已存在", f"SN {sn} 已在队列中运行，请勿重复添加")
            return

        try:
            nas_ip = self._nas_ip()
        except ValueError as exc:
            messagebox.showwarning("输入错误", str(exc))
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
            form_grade=self.form_grade_var.get() if self.form_entry_enabled else "",
            form_account_name=self.form_account_var.get().strip() if auto_form_entry else "",
            reserved_ips=set(reserved_ips or {nas_ip}),
            network_interface=network_interface,
        )
        task.status = self._status_text(task.state_code)
        self.devices[task.task_id] = task
        if self.device_tree is not None:
            self.device_tree.insert(
                "",
                tk.END,
                iid=task.task_id,
                values=(task.sn, task.display_ip, task.status, task.elapsed_display, "0%", task.current_step),
            )
            if select:
                self.device_tree.selection_set(task.task_id)
                self.device_tree.focus(task.task_id)

        if select:
            self.selected_task_id = task.task_id
            self._refresh_log_title()
            self._set_log_contents("")
        self._append_local_log(
            task.task_id,
            f"SN {task.sn} 已加入队列，来源={source}，模式={task.mode}，IP={task.requested_ip}，"
            f"{f'接口={task.network_interface}，' if task.network_interface else ''}"
            f"自动录表={task.auto_form_entry}，自动补第一步={task.auto_seed_previous_step}，"
            f"机型={task.form_model or 'SN自动识别'}，等级={task.form_grade}，账号={task.form_account_name}",
        )
        self._refresh_summary()
        self._refresh_action_states()

        worker = threading.Thread(target=self._run_test_task, args=(task,), daemon=True)
        self.workers[task.task_id] = worker
        worker.start()
        return task

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

    def _on_smoke(self) -> None:
        if self._active_task_count() > 0:
            messagebox.showwarning("任务进行中", "当前有设备正在测试，请先等待设备队列空闲")
            return

        if self.smoke_worker and self.smoke_worker.is_alive():
            messagebox.showwarning("任务进行中", "当前已有冒烟检查在运行")
            return

        if self.ip_mode_var.get() != "manual" or not self.manual_ip_var.get().strip():
            messagebox.showwarning("需要 IP", "冒烟检查需要手动填写 NAS IP")
            return

        nas_ip = self.manual_ip_var.get().strip()
        self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  冒烟检查中 {nas_ip}")

        def worker() -> None:
            try:
                result = run_smoke(nas_ip, setup_file_log=False)
                summary = f"选择器命中 {result['hits']}/{result['total']}，未填写 TODO {len(result['todos'])}"
                self.ui_queue.put({"type": "smoke_done", "success": True, "message": summary})
            except Exception as exc:
                self.ui_queue.put({"type": "smoke_done", "success": False, "message": str(exc)})

        self.smoke_worker = threading.Thread(target=worker, daemon=True)
        self.smoke_worker.start()
        self._refresh_action_states()

    def _finish_smoke(self, event: dict) -> None:
        success = bool(event.get("success"))
        message = str(event.get("message") or "")
        if success:
            self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  {message}")
            messagebox.showinfo("冒烟检查完成", message)
        else:
            self.status_var.set(f"{datetime.now().strftime('%H:%M:%S')}  冒烟检查失败")
            messagebox.showerror("冒烟检查失败", message)
        self._refresh_action_states()

    def _remove_finished_tasks(self) -> None:
        finished_ids = [
            task_id
            for task_id, task in self.devices.items()
            if task.state_code in {"success", "failed", "cancelled"}
        ]
        if not finished_ids:
            return

        selected_removed = self.selected_task_id in finished_ids
        for task_id in finished_ids:
            self.devices.pop(task_id, None)
            if self.device_tree is not None and self.device_tree.exists(task_id):
                self.device_tree.delete(task_id)

        if selected_removed:
            self.selected_task_id = None
            self.log_title_var.set(self._t("select_device_log"))
            self._set_log_contents(self._t("select_device_log_sentence"))

        self._refresh_summary()
        self._refresh_action_states()

    def _on_select_device(self, _event) -> None:
        if self.device_tree is None:
            return
        selection = self.device_tree.selection()
        self.selected_task_id = selection[0] if selection else None
        self._refresh_log_title()
        self._refresh_action_states()
        if self.selected_task_id is None:
            self._set_log_contents(self._t("select_device_log_sentence"))
            return

        task = self.devices.get(self.selected_task_id)
        if task is None:
            self._set_log_contents(self._t("select_device_log_sentence"))
            return

        self._set_log_contents("".join(task.logs) if task.logs else self._t("no_logs"))

    def _refresh_log_title(self) -> None:
        if self.selected_task_id is None:
            self.log_title_var.set(self._t("select_device_log"))
            return

        task = self.devices.get(self.selected_task_id)
        if task is None:
            self.log_title_var.set(self._t("select_device_log"))
            return

        self.log_title_var.set(
            f"SN {task.sn} | IP {task.display_ip} | {task.status} | {task.current_step}"
        )

    def _refresh_summary(self) -> None:
        queued = sum(1 for task in self.devices.values() if task.state_code == "queued")
        running = sum(1 for task in self.devices.values() if task.state_code == "running")
        transfer = sum(1 for task in self.devices.values() if task.state_code == "transfer")
        on_hold = sum(1 for task in self.devices.values() if task.state_code == "on_hold")
        retrying = sum(1 for task in self.devices.values() if task.state_code == "retrying")
        cancelling = sum(1 for task in self.devices.values() if task.state_code == "cancelling")
        success = sum(1 for task in self.devices.values() if task.state_code == "success")
        failed = sum(1 for task in self.devices.values() if task.state_code == "failed")
        cancelled = sum(1 for task in self.devices.values() if task.state_code == "cancelled")
        active = queued + running + transfer + on_hold + retrying + cancelling

        self.queue_summary_var.set(self._t("queue_summary", total=len(self.devices), active=active))
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

    def _refresh_action_states(self) -> None:
        smoke_busy = self._active_task_count() > 0 or bool(self.smoke_worker and self.smoke_worker.is_alive())
        if self.smoke_btn is not None:
            self.smoke_btn.configure(state=tk.DISABLED if smoke_busy else tk.NORMAL)

        selected = self._selected_task()
        show_enabled = selected is not None and selected.browser_pid is not None
        if self.show_browser_btn is not None:
            self.show_browser_btn.configure(state=tk.NORMAL if show_enabled else tk.DISABLED)
        cancel_enabled = (
            selected is not None
            and selected.state_code in self.ACTIVE_STATES
            and not selected.cancel_event.is_set()
        )
        if self.cancel_btn is not None:
            self.cancel_btn.configure(state=tk.NORMAL if cancel_enabled else tk.DISABLED)

    def _next_task_id(self, sn: str) -> str:
        self.task_counter += 1
        return f"{sn}-{self.task_counter:03d}"

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
