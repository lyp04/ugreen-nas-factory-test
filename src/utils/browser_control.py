from __future__ import annotations

import ctypes
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from .logger import logger


SW_HIDE = 0
SW_RESTORE = 9
SW_SHOW = 5
SWP_NOZORDER = 0x0004
SWP_SHOWWINDOW = 0x0040
TOP_LEVEL_WINDOW_CLASS = "Chrome_WidgetWin_1"
if sys.platform.startswith("win"):
    _USER32 = ctypes.WinDLL("user32", use_last_error=True)
    _ENUM_WINDOWS_PROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
else:
    # Keep the module importable on non-Windows hosts so platform-neutral CLI/
    # GUI logic can still be unit-tested.  Window management remains a no-op.
    _USER32 = None
    _ENUM_WINDOWS_PROC = None


@dataclass(slots=True)
class ManagedBrowserContext:
    context: object
    page: object
    browser_pid: int | None
    user_data_dir: Path


def launch_managed_context(pw, browser_cfg: dict, task_id: str | None = None) -> ManagedBrowserContext:
    hidden = bool(browser_cfg.get("hidden", True)) and not browser_cfg.get("headless", False)
    viewport = browser_cfg["viewport"]
    user_data_dir = _make_profile_dir(task_id)
    launch_args = list(browser_cfg.get("launch_args") or [])

    if hidden:
        launch_args.extend(
            [
                "--window-position=-32000,-32000",
                f"--window-size={viewport[0]},{viewport[1]}",
            ]
        )

    launch_kwargs = {
        "user_data_dir": str(user_data_dir),
        "headless": browser_cfg.get("headless", False),
        "viewport": {"width": viewport[0], "height": viewport[1]},
        "locale": browser_cfg["language"],
        "args": launch_args,
    }
    channel = browser_cfg.get("channel") or None
    if channel and channel != "chromium":
        launch_kwargs["channel"] = channel

    context = pw.chromium.launch_persistent_context(**launch_kwargs)
    page = context.pages[0] if context.pages else context.new_page()
    browser_pid = wait_for_browser_pid(channel, user_data_dir.name)

    if hidden and browser_pid:
        hide_browser_windows(browser_pid)

    return ManagedBrowserContext(
        context=context,
        page=page,
        browser_pid=browser_pid,
        user_data_dir=user_data_dir,
    )


def close_managed_context(session: ManagedBrowserContext) -> None:
    try:
        session.context.close()
    finally:
        shutil.rmtree(session.user_data_dir, ignore_errors=True)


def wait_for_browser_pid(channel: str | None, profile_marker: str, timeout_s: int = 15) -> int | None:
    process_name = _browser_process_name(channel)
    if not process_name:
        return None

    escaped_marker = profile_marker.replace("'", "''")
    script = (
        f"Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.Name -eq '{process_name}' -and $_.CommandLine -like '*{escaped_marker}*' "
        f"-and $_.CommandLine -notlike '*--type=*' }} | "
        "Select-Object -First 1 -ExpandProperty ProcessId"
    )

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            out = subprocess.check_output(
                _powershell_command(script),
                text=True,
                encoding="utf-8",
                errors="replace",
                **_hidden_process_kwargs(),
            ).strip()
        except Exception as exc:
            logger.warning(f"Could not query browser pid: {exc}")
            return None

        if out.isdigit():
            return int(out)
        time.sleep(0.2)

    logger.warning(f"Timed out waiting for browser pid for profile marker '{profile_marker}'")
    return None


def show_browser_windows(browser_pid: int, left: int = 60, top: int = 60, width: int = 1440, height: int = 900) -> bool:
    hwnds = _top_level_windows_for_pid(browser_pid)
    if not hwnds:
        return False

    for hwnd in hwnds:
        _USER32.ShowWindow(hwnd, SW_RESTORE)
        _USER32.ShowWindow(hwnd, SW_SHOW)
        _USER32.SetWindowPos(hwnd, 0, left, top, width, height, SWP_NOZORDER | SWP_SHOWWINDOW)
        _USER32.SetForegroundWindow(hwnd)
    return True


def hide_browser_windows(browser_pid: int) -> bool:
    hwnds = _top_level_windows_for_pid(browser_pid)
    if not hwnds:
        return False

    for hwnd in hwnds:
        _USER32.ShowWindow(hwnd, SW_HIDE)
    return True


def terminate_browser_process(browser_pid: int) -> bool:
    if browser_pid <= 0:
        return False

    try:
        if sys.platform.startswith("win"):
            result = subprocess.run(
                ["taskkill", "/PID", str(browser_pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **_hidden_process_kwargs(),
            )
            return result.returncode == 0

        os.kill(browser_pid, signal.SIGTERM)
        return True
    except Exception as exc:
        logger.warning(f"Could not terminate browser process {browser_pid}: {exc}")
        return False


def _make_profile_dir(task_id: str | None) -> Path:
    safe_task_id = "".join(ch for ch in (task_id or "task") if ch.isalnum() or ch in {"-", "_"}).strip() or "task"
    suffix = int(time.time() * 1000)
    path = Path(tempfile.gettempdir()) / f"ugreen_browser_{safe_task_id}_{suffix}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _browser_process_name(channel: str | None) -> str | None:
    if channel == "msedge":
        return "msedge.exe"
    if channel == "chrome":
        return "chrome.exe"
    return "msedge.exe"


def _top_level_windows_for_pid(browser_pid: int) -> list[int]:
    if _USER32 is None or _ENUM_WINDOWS_PROC is None:
        return []

    hwnds: list[int] = []

    def callback(hwnd, _lparam):
        pid = wintypes.DWORD()
        _USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value != browser_pid:
            return True
        if _window_class(hwnd) == TOP_LEVEL_WINDOW_CLASS:
            hwnds.append(hwnd)
        return True

    enum_callback = _ENUM_WINDOWS_PROC(callback)
    _USER32.EnumWindows(enum_callback, 0)
    return hwnds


def _window_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    _USER32.GetClassNameW(hwnd, buf, len(buf))
    return buf.value


def _powershell_command(script: str) -> list[str]:
    return [
        "powershell",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-WindowStyle",
        "Hidden",
        "-Command",
        script,
    ]


def _hidden_process_kwargs() -> dict:
    if not sys.platform.startswith("win"):
        return {}

    kwargs: dict = {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }
    startupinfo_factory = getattr(subprocess, "STARTUPINFO", None)
    startf_use_show_window = getattr(subprocess, "STARTF_USESHOWWINDOW", 0)
    sw_hide = getattr(subprocess, "SW_HIDE", 0)
    if startupinfo_factory is not None:
        startupinfo = startupinfo_factory()
        startupinfo.dwFlags |= startf_use_show_window
        startupinfo.wShowWindow = sw_hide
        kwargs["startupinfo"] = startupinfo
    return kwargs
