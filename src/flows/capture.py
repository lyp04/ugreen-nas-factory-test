from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from playwright.sync_api import expect

from ..measurements import (
    DEFAULT_CPU_TEMP_MAX_C,
    fan_mode_page_is_failing,
    fan_mode_value_failures,
)
from ..utils.app_guides import dismiss_arco_app_guides, dismiss_storage_guide_modal
from ..utils.desktop import click_desktop_launcher, dismiss_desktop_overlays, find_visible_locator
from ..utils.logger import logger
from ..utils.screenshot import capture_page
from ..utils.sn import extract_full_sn
from ..utils.smb_transfer import (
    SmbTransferConfig,
    SmbTransferError,
    TEN_GIB,
    build_cleanup_script,
    build_download_script,
    build_mount_script,
    build_prepare_sparse_file_script,
    build_remote_cleanup_script,
    build_unmount_script,
    build_upload_script,
    run_powershell,
    start_powershell,
    wait_powershell,
)

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Locator, Page


FAN_CAPTURE_KEYS = {"fan_normal", "fan_silent", "fan_full_speed"}
# 风扇模式页读数不合格（温度超标 / 转速异常）时，每隔这么久重读+重抓一次，直到合格或用满预算。
FAN_RECHECK_INTERVAL_MS = 5_000
# 重抓预算默认值（秒）。CLI 会用 config validation.cpu_temp_recheck_seconds 覆盖。
DEFAULT_FAN_RECHECK_BUDGET_S = 30.0
TRANSFER_ACTIVITY_RE = re.compile(r"\b(?:[1-9]\d*(?:\.\d+)?)\s?(?:KB|MB|GB)/s\b")
RATE_RE = re.compile(r"([0-9]+(?:\.\d+)?)\s*([KMG]?B/s)")
POOL_DISK_TOKEN_RE = re.compile(r"M\.2硬盘\d+|硬盘\d+")
FRAME_WAIT_MS = 15_000
SHORT_UI_WAIT_MS = 1_000
FAN_POST_LAYOUT_WAIT_MS = 2_000
TRANSFER_ACTIVITY_WAIT_S = 20
TRANSFER_TIMEOUT_S = 3_600
TRANSFER_MIDPOINT_RATIO = 0.5
MIN_TRANSFER_RATE_BYTES = 5 * 1024 * 1024
TRANSFER_SPEED_DEFAULT_THRESHOLD_MB_S = 200.0
TRANSFER_SPEED_SAMPLE_INTERVAL_MS = 500
TRANSFER_SPEED_STABLE_SAMPLES = 1
TRANSFER_SPEED_MAX_RETRIES = 1
TRANSFER_SPEED_ATTEMPT_TIMEOUT_S = 45
# The resource-monitor display refreshes ~1.0s (measured min ~0.78s) and holds each value
# steady between repaints. A bracket (read -> screenshot -> read) whose total span stays
# under this bound can contain at most ONE repaint edge, so any value change makes the two
# reads differ and the ambiguous shot is rejected. 0.6s is comfortably below the 0.78s floor
# yet ~4x above the measured ~0.1s full-page screenshot, so steady windows are never rejected.
# Default only — override per deployment via transfer.bracket_max_span_s for slow hardware.
TRANSFER_BRACKET_MAX_SPAN_S = 0.6
TRANSFER_UPLOAD_MIN_SEED_MB = 10 * 1024
TRANSFER_UPLOAD_SEED_TIMEOUT_S = 180
TRANSFER_UPLOAD_FINISH_TIMEOUT_S = 30
TRANSFER_POST_UPLOAD_SETTLE_TIMEOUT_S = 30
TRANSFER_POST_UPLOAD_SETTLE_INTERVAL_MS = 1_000
TRANSFER_POST_UPLOAD_SETTLE_SAMPLES = 2
TRANSFER_SOURCE_SAMPLE_BYTES = 1024 * 1024
TRANSFER_SOURCE_GENERATE_CHUNK_BYTES = 16 * 1024 * 1024
GIB = 1024 * 1024 * 1024
TRANSFER_SOURCE_SIZE_GIB_BY_MODEL = {"2800": 5, "4800": 10, "4800Plus": 20}
FAN_CTL_WINDOW_LAYOUT = {"left": -260, "top": 120, "width": 780, "height": 760, "z_index": 20}
FAN_TASK_WINDOW_LAYOUT = {"left": 540, "top": 120, "width": 1340, "height": 760, "z_index": 21}
TRANSFER_STAGE_LOCK = threading.Lock()


class CaptureError(RuntimeError):
    pass


@dataclass(slots=True)
class TransferRateSample:
    amount: str
    unit: str
    rate_bytes: float
    source: str = ""

    @property
    def rate_mb_s(self) -> float:
        return self.rate_bytes / (1024 * 1024)

    @property
    def label(self) -> str:
        return f"{self.amount} {self.unit}"


@dataclass(slots=True)
class TransferAttemptResult:
    values: dict[str, str]
    shot_path: Path | None
    reached_threshold: bool


def extract_full_sn_from_settings(page: "Page", selectors: dict, expected_sn: str) -> str | None:
    logger.info("Trying to resolve full SN from Control Panel About page")
    dismiss_desktop_overlays(page, selectors, max_rounds=2, completion_wait_ms=0)
    desktop_selectors = selectors.get("desktop_launchers", {})
    frame = _open_app(page, desktop_selectors, "ctlmgr")
    nav_selectors = selectors.get("capture_nav", {})
    about_selectors = [
        nav_selectors.get("ctlmgr_about_menu"),
        'text=关于本机',
        '.menu-item-name:text-is("关于本机")',
        '.ivu-menu-item:has-text("关于本机")',
    ]

    clicked = False
    for selector in about_selectors:
        if not selector or selector == "TODO":
            continue
        try:
            loc = frame.locator(selector).first
            loc.wait_for(state="visible", timeout=3_000)
            _click_frame_locator(frame, loc, frame.name, "about page")
            clicked = True
            break
        except Exception:
            continue

    if not clicked:
        logger.info("Control Panel About menu was not visible; scraping current Control Panel page")

    deadline = time.monotonic() + 12
    last_preview = ""
    while time.monotonic() < deadline:
        text = _frame_identity_text(page, frame, include_browser_storage=bool(expected_sn))
        last_preview = " ".join(text.split())[:180]
        full_sn = extract_full_sn(text, expected_sn)
        if full_sn:
            logger.info(f"Resolved full SN from Control Panel: {full_sn}")
            return full_sn
        page.wait_for_timeout(800)

    logger.info(f"Control Panel did not expose a full SN yet: {last_preview or '<empty>'}")
    return None


def run(
    page: "Page",
    nas_url: str,
    sn: str,
    page_keys: list[str],
    selectors: dict,
    screenshots_dir: Path,
    admin: dict,
    transfer_cfg: dict | None = None,
    progress_cb: Callable[[dict], None] | None = None,
    capture_values: dict[str, dict[str, str]] | None = None,
    cpu_temp_max_c: float = DEFAULT_CPU_TEMP_MAX_C,
    fan_recheck_budget_s: float = DEFAULT_FAN_RECHECK_BUDGET_S,
) -> dict[str, str]:
    dismiss_desktop_overlays(page, selectors, max_rounds=2, completion_wait_ms=0)
    desktop_selectors = selectors.get("desktop_launchers", {})
    nav_selectors = selectors.get("capture_nav", {})
    page_specs = selectors.get("capture_pages", {})
    transfer_cfg = transfer_cfg or {}
    saved: dict[str, str] = {}
    transfer_slot_held = False
    transfer_slot_context: tuple[str, str, str] | None = None
    transfer_work_root = _transfer_work_root(transfer_cfg)
    _cleanup_all_transfer_workdirs_if_idle(transfer_work_root)
    _cleanup_legacy_transfer_workdirs_if_idle(screenshots_dir.parent.parent)

    try:
        for index, page_key in enumerate(page_keys):
            spec = page_specs.get(page_key)
            if not spec:
                logger.warning(f"Page '{page_key}' has no entry in selectors.yml, skipping")
                continue

            try:
                is_transfer_page = bool(spec.get("transfer"))
                existing_min_rate = (
                    _transfer_speed_threshold_mb_s(page_key, transfer_cfg) if is_transfer_page else None
                )
                require_reported_values = _page_requires_reported_values(page_key, spec)
                existing = _existing_capture_path(
                    screenshots_dir,
                    sn,
                    page_key,
                    existing_min_rate,
                    require_reported_values=require_reported_values,
                    fan_mode_max_c=cpu_temp_max_c if page_key in FAN_CAPTURE_KEYS else None,
                )
                if existing is not None:
                    logger.info(f"Skipping {page_key}; existing screenshot found -> {existing.name}")
                    saved[page_key] = str(existing)
                    if capture_values is not None:
                        _merge_reported_capture_values(capture_values, screenshots_dir.parent / "test_report.json", page_key)
                    _emit_capture_event(
                        progress_cb,
                        "capture_page_completed",
                        page_key=page_key,
                        screenshot=str(existing),
                        values=(capture_values or {}).get(page_key) or {},
                        skipped=True,
                    )
                    if is_transfer_page and transfer_slot_held and not _next_page_is_transfer(page_keys, page_specs, index):
                        if transfer_slot_context is not None:
                            release_page_key, release_share, release_direction = transfer_slot_context
                            _release_transfer_slot(progress_cb, release_page_key, release_share, release_direction)
                        transfer_slot_held = False
                        transfer_slot_context = None
                    continue

                if is_transfer_page and not transfer_slot_held:
                    share, direction = _transfer_share_direction(spec, page_key)
                    _acquire_transfer_slot(progress_cb, page_key, share, direction)
                    transfer_slot_held = True
                    transfer_slot_context = (page_key, share, direction)

                _emit_capture_event(
                    progress_cb,
                    "capture_page_started",
                    page_key=page_key,
                    transfer=is_transfer_page,
                )
                if page_key in FAN_CAPTURE_KEYS:
                    path = _capture_fan_mode(
                        page,
                        sn,
                        page_key,
                        spec,
                        page_specs,
                        desktop_selectors,
                        nav_selectors,
                        screenshots_dir,
                        capture_values,
                        cpu_temp_max_c=cpu_temp_max_c,
                        recheck_budget_s=fan_recheck_budget_s,
                    )
                elif is_transfer_page:
                    share, direction = _transfer_share_direction(spec, page_key)
                    transfer_slot_context = (page_key, share, direction)
                    path = _capture_transfer_page(
                        page,
                        nas_url,
                        sn,
                        page_key,
                        spec,
                        desktop_selectors,
                        nav_selectors,
                        screenshots_dir,
                        admin,
                        transfer_cfg,
                        progress_cb,
                        slot_already_acquired=transfer_slot_held,
                        capture_values=capture_values,
                    )
                else:
                    path = _capture_standard_page(
                        page,
                        sn,
                        page_key,
                        spec,
                        desktop_selectors,
                        nav_selectors,
                        screenshots_dir,
                        capture_values,
                    )
                saved[page_key] = str(path)
                _emit_capture_event(
                    progress_cb,
                    "capture_page_completed",
                    page_key=page_key,
                    screenshot=str(path),
                    values=(capture_values or {}).get(page_key) or {},
                )
                if is_transfer_page and not _next_page_is_transfer(page_keys, page_specs, index):
                    if transfer_slot_context is not None:
                        release_page_key, release_share, release_direction = transfer_slot_context
                        _release_transfer_slot(progress_cb, release_page_key, release_share, release_direction)
                    transfer_slot_held = False
                    transfer_slot_context = None
            except Exception as exc:
                _emit_capture_event(progress_cb, "capture_page_failed", page_key=page_key, error=str(exc))
                logger.error(f"Capture failed for '{page_key}': {exc}")
                raise CaptureError(f"Capture failed at page '{page_key}': {exc}") from exc
    finally:
        if transfer_slot_held and transfer_slot_context is not None:
            release_page_key, release_share, release_direction = transfer_slot_context
            _release_transfer_slot(progress_cb, release_page_key, release_share, release_direction)
        _cleanup_transfer_run_dir(_transfer_run_dir(screenshots_dir.parent, transfer_cfg))

    return saved


def _existing_capture_path(
    screenshots_dir: Path,
    sn: str,
    page_key: str,
    min_rate_mb_s: float | None = None,
    require_reported_values: bool = False,
    fan_mode_max_c: float | None = None,
) -> Path | None:
    report_path = screenshots_dir.parent / "test_report.json"
    if require_reported_values and not _reported_capture_values(report_path, page_key):
        logger.info(f"Skipping existing {page_key} screenshot; no reported key values found")
        return None

    # 重测护栏：上次留下的风扇/温度截图若记录的就是不合格读数（温度超标 / 转速异常），
    # 不要复用——否则会拿旧失败值重新判定、永远复现同一个失败。强制重抓让设备有机会
    # 在已冷却/风扇已稳态后给出合格读数。与传输速度「低于阈值不复用」同理。
    if fan_mode_max_c is not None:
        reported = _reported_capture_values(report_path, page_key)
        if fan_mode_page_is_failing(page_key, reported, fan_mode_max_c):
            logger.info(
                f"Skipping existing {page_key} screenshot; recorded fan/temp reading "
                f"would fail validation -> re-capturing"
            )
            return None

    if min_rate_mb_s is not None:
        reported_rate = _reported_transfer_rate_mb_s(report_path, page_key)
        if reported_rate is None or reported_rate < min_rate_mb_s:
            if reported_rate is None:
                logger.info(f"Skipping existing {page_key} screenshot; no reported transfer rate found")
            else:
                logger.info(
                    f"Skipping existing {page_key} screenshot; reported rate "
                    f"{reported_rate:.1f} MB/s < {min_rate_mb_s:.1f} MB/s"
                )
            return None

        reported_path = _reported_capture_path(report_path, page_key)
        if reported_path and _valid_existing_capture(reported_path) and _matches_capture_name(reported_path, sn, page_key):
            return reported_path

    pattern = f"{sn}_{page_key}_*.png"
    candidates = [
        path
        for path in screenshots_dir.glob(pattern)
        if path.is_file() and "_FAIL_" not in path.name and _valid_existing_capture(path)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def _page_requires_reported_values(page_key: str, spec: dict) -> bool:
    return page_key in FAN_CAPTURE_KEYS or page_key in {"system_update", "network_interface", "storage_pool"} or bool(
        spec.get("key_values")
    )


def _merge_reported_capture_values(
    capture_values: dict[str, dict[str, str]],
    report_path: Path,
    page_key: str,
) -> None:
    values = _reported_capture_values(report_path, page_key)
    if values:
        capture_values.setdefault(page_key, {}).update(values)


def _reported_capture_values(report_path: Path, page_key: str) -> dict[str, str]:
    report = _read_json_report(report_path)
    raw = (report.get("captured_values") or {}).get(page_key) or {}
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if value is not None}


def _reported_capture_path(report_path: Path, page_key: str) -> Path | None:
    report = _read_json_report(report_path)
    raw = (report.get("captured") or {}).get(page_key)
    if not raw:
        return None
    return Path(str(raw))


def _reported_transfer_rate_mb_s(report_path: Path, page_key: str) -> float | None:
    report = _read_json_report(report_path)
    values = (report.get("captured_values") or {}).get(page_key) or {}
    return _float_or_none(values.get("rate_mbps"))


def _read_json_report(report_path: Path) -> dict:
    try:
        return json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _matches_capture_name(path: Path, sn: str, page_key: str) -> bool:
    return path.name.startswith(f"{sn}_{page_key}_") and "_FAIL_" not in path.name


def _valid_existing_capture(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _capture_standard_page(
    page: "Page",
    sn: str,
    page_key: str,
    spec: dict,
    desktop_selectors: dict,
    nav_selectors: dict,
    screenshots_dir: Path,
    capture_values: dict[str, dict[str, str]] | None,
) -> Path:
    app = _require_meta(spec.get("app"), f"capture_pages.{page_key}.app")
    logger.info(f"Capturing {page_key} via desktop app '{app}'")
    frame = _open_app(page, desktop_selectors, app)
    _navigate(frame, spec, nav_selectors)
    _wait_for_landmark(frame, spec, page_key)
    dismiss_desktop_overlays(page, max_rounds=2, completion_wait_ms=0)
    page.wait_for_timeout(SHORT_UI_WAIT_MS)
    # 读数必须和截图背靠背：resource_monitor 在 4 个 SMB 传输刚结束时打开，CPU 正快速降温，
    # 若先读数、再关弹窗+等待+截图，上传的数值会比图里冻住的数值偏高（实测上传 70℃ vs 截图 52℃）。
    # 与风扇页 _capture_fan_mode 同理——读完立刻截，保证上传值 == 截图里的数字。
    values = _log_key_values(frame, spec, page_key)
    shot = capture_page(page, sn, page_key, screenshots_dir)
    if capture_values is not None and values:
        capture_values[page_key] = values
    return shot


def _capture_transfer_page(
    page: "Page",
    nas_url: str,
    sn: str,
    page_key: str,
    spec: dict,
    desktop_selectors: dict,
    nav_selectors: dict,
    screenshots_dir: Path,
    admin: dict,
    transfer_cfg: dict,
    progress_cb: Callable[[dict], None] | None,
    slot_already_acquired: bool = False,
    capture_values: dict[str, dict[str, str]] | None = None,
) -> Path:
    share, direction = _transfer_share_direction(spec, page_key)
    threshold_mb_s = _transfer_speed_threshold_mb_s(page_key, transfer_cfg)
    max_retries = _transfer_speed_max_retries(transfer_cfg)

    frame = _open_app(page, desktop_selectors, _require_meta(spec.get("app"), f"capture_pages.{page_key}.app"))
    _navigate(frame, spec, nav_selectors)
    _wait_for_landmark(frame, spec, page_key)

    cfg = _build_transfer_config(nas_url, share, screenshots_dir.parent, admin, transfer_cfg)
    slot_acquired = False
    shot_path: Path | None = None
    best_result: TransferAttemptResult | None = None
    seen_shots: list[Path] = []
    attempts_run = 0
    try:
        if slot_already_acquired:
            logger.info(f"{page_key}: transfer slot already held")
            _emit_capture_event(
                progress_cb,
                "transfer_started",
                page_key=page_key,
                share=share,
                direction=direction,
            )
        else:
            _acquire_transfer_slot(progress_cb, page_key, share, direction)
            slot_acquired = True
        _prepare_share(cfg)
        if direction == "download" and not _remote_file_exists(cfg):
            logger.info(f"Seeding remote SMB fixture for share '{share}'")
            run_powershell(build_upload_script(cfg), timeout_s=TRANSFER_TIMEOUT_S)

        for attempt in range(1, max_retries + 2):
            attempts_run = attempt
            final_attempt = attempt > max_retries
            logger.info(
                f"{page_key}: transfer speed attempt {attempt}/{max_retries + 1} "
                f"(threshold {threshold_mb_s:.1f} MB/s)"
            )
            proc = start_powershell(build_upload_script(cfg) if direction == "upload" else build_download_script(cfg))
            try:
                result = _capture_transfer_attempt(
                    page,
                    frame,
                    proc,
                    cfg,
                    sn,
                    page_key,
                    share,
                    direction,
                    screenshots_dir,
                    transfer_cfg,
                    threshold_mb_s,
                    attempt,
                    capture_on_low=final_attempt,
                )
            finally:
                _finish_transfer_process(proc, page_key, attempt)

            if result.shot_path is not None:
                seen_shots.append(result.shot_path)
            best_result = _better_transfer_result(best_result, result)
            if result.reached_threshold:
                best_result = result
                break

            logger.info(
                f"{page_key}: attempt {attempt} below threshold; best "
                f"{(result.values.get('rate_mbps') or '0')} MB/s"
            )

        # Drop screenshots from superseded attempts so a losing retry never leaves an
        # orphan proof image behind; only the kept attempt's screenshot survives. Done
        # after the loop so the final best_result is settled and its shot is never deleted.
        kept_shot = best_result.shot_path if best_result else None
        for stale in seen_shots:
            if stale != kept_shot:
                _unlink_quietly(stale)

        if best_result and best_result.values:
            values = dict(best_result.values)
            values["attempts"] = str(attempts_run)
            for value_key, value in values.items():
                logger.info(f"  {page_key}.{value_key}: {value}")
            # value and screenshot must come from the SAME attempt, never mixed across
            # retries — otherwise the logged rate could describe a different image.
            shot_path = best_result.shot_path
            if capture_values is not None:
                # On a below-threshold page this records a diagnostic rate (clearly marked
                # below_threshold / no_sample) and then raises just below, so the run fails
                # and the value never ships — it is only ever read back for the failure
                # report, never auto-filled into MES as a passing measurement.
                capture_values[page_key] = values
            if not best_result.reached_threshold:
                raise CaptureError(_transfer_threshold_error(page_key, values, threshold_mb_s, attempts_run))
    finally:
        if slot_acquired:
            try:
                _cleanup_transfer(cfg, direction)
            finally:
                _release_transfer_slot(progress_cb, page_key, share, direction)

    if shot_path is None:
        raise CaptureError(f"Transfer capture '{page_key}' did not produce a screenshot")
    return shot_path


def _transfer_threshold_error(
    page_key: str,
    values: dict[str, str],
    threshold_mb_s: float,
    attempts_run: int,
) -> str:
    rate = values.get("rate_mbps") or "0"
    status = values.get("speed_status") or "below_threshold"
    rate_value = _float_or_none(rate)
    if rate_value is not None and rate_value >= threshold_mb_s:
        return (
            f"{page_key} speed reached {_format_decimal(rate_value)} MB/s, but transfer capture did not satisfy "
            f"all completion checks after {attempts_run} attempt(s) ({status}). "
            "Please retest until the screenshot speed and transfer completeness checks pass."
        )
    return (
        f"{page_key} speed did not reach screenshot threshold after {attempts_run} attempt(s): "
        f"{rate} MB/s < {_format_decimal(threshold_mb_s)} MB/s ({status}). "
        "未自动录表，请重新测试直到截图速度达标。"
    )


def _transfer_share_direction(spec: dict, page_key: str) -> tuple[str, str]:
    transfer = spec.get("transfer") or {}
    share = _require_meta(transfer.get("share"), f"capture_pages.{page_key}.transfer.share")
    direction = _require_meta(transfer.get("direction"), f"capture_pages.{page_key}.transfer.direction")
    return share, direction


def _next_page_is_transfer(page_keys: list[str], page_specs: dict, index: int) -> bool:
    for next_page_key in page_keys[index + 1 :]:
        next_spec = page_specs.get(next_page_key)
        if not next_spec:
            continue
        return bool(next_spec.get("transfer"))
    return False


def _acquire_transfer_slot(
    progress_cb: Callable[[dict], None] | None,
    page_key: str,
    share: str,
    direction: str,
) -> None:
    if not TRANSFER_STAGE_LOCK.acquire(blocking=False):
        logger.info(f"{page_key}: transfer slot busy, waiting on hold")
        _emit_capture_event(
            progress_cb,
            "transfer_waiting",
            page_key=page_key,
            share=share,
            direction=direction,
        )
        TRANSFER_STAGE_LOCK.acquire()

    logger.info(f"{page_key}: transfer slot acquired")
    _emit_capture_event(
        progress_cb,
        "transfer_started",
        page_key=page_key,
        share=share,
        direction=direction,
    )


def _release_transfer_slot(
    progress_cb: Callable[[dict], None] | None,
    page_key: str,
    share: str,
    direction: str,
) -> None:
    logger.info(f"{page_key}: transfer slot released")
    _emit_capture_event(
        progress_cb,
        "transfer_finished",
        page_key=page_key,
        share=share,
        direction=direction,
    )
    TRANSFER_STAGE_LOCK.release()


def _capture_transfer_attempt(
    page: "Page",
    frame: "Frame",
    proc,
    cfg: SmbTransferConfig,
    sn: str,
    page_key: str,
    share: str,
    direction: str,
    screenshots_dir: Path,
    transfer_cfg: dict,
    threshold_mb_s: float,
    attempt: int,
    capture_on_low: bool = False,
) -> TransferAttemptResult:
    threshold_bytes = threshold_mb_s * 1024 * 1024
    interval_ms = _transfer_speed_sample_interval_ms(transfer_cfg)
    stable_target = _transfer_speed_stable_samples(transfer_cfg)
    attempt_timeout_s = _transfer_speed_attempt_timeout_s(transfer_cfg)
    max_span_s = _transfer_bracket_max_span_s(transfer_cfg)
    min_upload_seed_bytes = _transfer_upload_min_seed_bytes(transfer_cfg)
    deadline = time.monotonic() + attempt_timeout_s
    # Invariant: the only (value, screenshot) pair this function ever returns comes
    # from _bound_transfer_capture, so the logged rate always equals the number
    # frozen in the saved image. `best_seen` is a text-only read kept purely for the
    # failure diagnostic — it is never paired with a screenshot.
    bound_sample: TransferRateSample | None = None      # best >= threshold bound capture
    bound_shot: Path | None = None
    best_seen: TransferRateSample | None = None
    stable_samples = 0
    last_excerpt = ""

    while time.monotonic() < deadline:
        text = _frame_text(frame)
        last_excerpt = " ".join(text.split())[:300]
        sample = _transfer_speed_sample(text, share, direction)
        if sample is not None:
            if best_seen is None or sample.rate_bytes > best_seen.rate_bytes:
                best_seen = sample
            if sample.rate_bytes >= threshold_bytes:
                # Require the rate to sit >= threshold for `stable_target` polls (a real
                # sustained plateau, not one bursty refresh tick) before spending a
                # bracketed screenshot, and only when it would improve on what we hold.
                stable_samples += 1
                if stable_samples >= stable_target and (
                    bound_sample is None or sample.rate_bytes > bound_sample.rate_bytes
                ):
                    captured = _bound_transfer_capture(
                        page,
                        frame,
                        sn,
                        page_key,
                        share,
                        direction,
                        screenshots_dir,
                        min_rate_bytes=threshold_bytes,
                        max_span_s=max_span_s,
                    )
                    if captured is None:
                        stable_samples = 0  # screenshot straddled a refresh edge; resample
                    else:
                        new_sample, new_shot = captured
                        if bound_sample is None or new_sample.rate_bytes > bound_sample.rate_bytes:
                            if bound_shot is not None and bound_shot != new_shot:
                                _unlink_quietly(bound_shot)
                            bound_sample, bound_shot = new_sample, new_shot
                        elif new_shot != bound_shot:
                            _unlink_quietly(new_shot)
            else:
                stable_samples = 0

        if _process_finished(proc):
            break
        page.wait_for_timeout(interval_ms)

    upload_seed_complete = True
    if bound_sample is not None and direction == "upload":
        upload_seed_complete = _wait_for_upload_seed_complete(
            page,
            proc,
            cfg.progress_file,
            min_upload_seed_bytes,
            page_key,
            transfer_cfg,
        )

    if bound_sample is not None and (direction != "upload" or upload_seed_complete):
        if direction == "upload":
            _wait_for_transfer_process_exit(page, proc, page_key, transfer_cfg)
            _wait_for_post_upload_settle(page, frame, page_key, transfer_cfg)
        values = _transfer_values_from_sample(
            bound_sample,
            share,
            direction,
            threshold_mb_s,
            attempt,
            reached_threshold=True,
        )
        return TransferAttemptResult(values, bound_shot, True)

    # Reached >= threshold but the upload seed never completed: still a bound pair,
    # reported as not-passing so the wrapper retries / fails the page.
    if bound_sample is not None:
        values = _transfer_values_from_sample(
            bound_sample,
            share,
            direction,
            threshold_mb_s,
            attempt,
            reached_threshold=False,
        )
        values["speed_status"] = "seed_incomplete"
        return TransferAttemptResult(values, bound_shot, False)

    # Never reached threshold. On the final attempt, bind whatever steady rate is on
    # screen so even the failure artifact's number matches its screenshot.
    if capture_on_low:
        captured = _bound_transfer_capture(
            page,
            frame,
            sn,
            page_key,
            share,
            direction,
            screenshots_dir,
            min_rate_bytes=0,
            max_span_s=max_span_s,
        )
        if captured is not None:
            low_sample, low_shot = captured
            values = _transfer_values_from_sample(
                low_sample,
                share,
                direction,
                threshold_mb_s,
                attempt,
                reached_threshold=False,
            )
            return TransferAttemptResult(values, low_shot, False)

    # No screenshot can be bound to a number here, so ship the best text-only read as a
    # diagnostic with no image (the page fails and is retested; nothing is shipped).
    if best_seen is None:
        logger.info(f"{page_key}: no speed sample captured. Last body excerpt: {last_excerpt}")
        values = {
            "share": share,
            "direction": direction,
            "rate": "0 MB/s",
            "rate_mbps": "0",
            "threshold_mbps": _format_decimal(threshold_mb_s),
            "speed_status": "no_sample",
            "attempt": str(attempt),
        }
    else:
        values = _transfer_values_from_sample(
            best_seen,
            share,
            direction,
            threshold_mb_s,
            attempt,
            reached_threshold=False,
        )
        if best_seen.rate_bytes >= threshold_bytes:
            values["speed_status"] = "unstable_threshold"
    return TransferAttemptResult(values, None, False)


def _wait_for_upload_seed_complete(
    page: "Page",
    proc,
    progress_file: Path,
    min_seed_bytes: int,
    page_key: str,
    transfer_cfg: dict,
) -> bool:
    if min_seed_bytes <= 0:
        return True

    current = _progress_size(progress_file)
    if current >= min_seed_bytes:
        return True

    timeout_s = _transfer_upload_seed_timeout_s(transfer_cfg)
    interval_ms = _transfer_speed_sample_interval_ms(transfer_cfg)
    deadline = time.monotonic() + timeout_s
    logger.info(
        f"{page_key}: speed reached threshold; waiting for upload seed to complete "
        f"({_bytes_to_mib(current):.0f}/{_bytes_to_mib(min_seed_bytes):.0f} MB)"
    )

    last_progress = current
    while time.monotonic() < deadline:
        current = _progress_size(progress_file)
        last_progress = current
        if current >= min_seed_bytes:
            logger.info(
                f"{page_key}: upload seed complete "
                f"({_bytes_to_mib(current):.0f}/{_bytes_to_mib(min_seed_bytes):.0f} MB)"
            )
            return True
        if _process_finished(proc):
            logger.info(
                f"{page_key}: transfer process finished before upload seed completed "
                f"({_bytes_to_mib(current):.0f}/{_bytes_to_mib(min_seed_bytes):.0f} MB)"
            )
            return False
        page.wait_for_timeout(interval_ms)

    logger.info(
        f"{page_key}: upload seed incomplete after {timeout_s:.0f}s "
        f"({_bytes_to_mib(last_progress):.0f}/{_bytes_to_mib(min_seed_bytes):.0f} MB)"
    )
    return False


def _wait_for_transfer_process_exit(page: "Page", proc, page_key: str, transfer_cfg: dict) -> None:
    if _process_finished(proc):
        return

    timeout_s = _transfer_upload_finish_timeout_s(transfer_cfg)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _process_finished(proc):
            return
        page.wait_for_timeout(250)

    logger.info(f"{page_key}: transfer process still running after upload seed completion")


def _bytes_to_mib(value: int | float) -> float:
    return float(value) / (1024 * 1024)


def _wait_for_post_upload_settle(page: "Page", frame: "Frame", page_key: str, transfer_cfg: dict) -> None:
    timeout_s = _transfer_post_upload_settle_timeout_s(transfer_cfg)
    interval_ms = _transfer_post_upload_settle_interval_ms(transfer_cfg)
    stable_target = _transfer_post_upload_settle_samples(transfer_cfg)
    if timeout_s <= 0:
        return

    deadline = time.monotonic() + timeout_s
    quiet_samples = 0
    last_rate = "unknown"
    logger.info(f"{page_key}: waiting for NAS write activity to settle before read test")
    while time.monotonic() < deadline:
        _read_section, write_section = _disk_sections(_frame_text(frame))
        sample = _section_total_rate_sample(write_section, "write_total")
        if sample is None or sample.rate_bytes < MIN_TRANSFER_RATE_BYTES:
            quiet_samples += 1
            if quiet_samples >= stable_target:
                logger.info(f"{page_key}: NAS write activity settled")
                return
        else:
            quiet_samples = 0
            last_rate = sample.label
        page.wait_for_timeout(interval_ms)

    logger.info(f"{page_key}: NAS write activity did not fully settle within {timeout_s:.0f}s; last write rate {last_rate}")


def _finish_transfer_process(proc, page_key: str, attempt: int) -> None:
    if _process_finished(proc):
        wait_powershell(proc, timeout_s=30)
        return

    logger.info(f"{page_key}: stopping transfer process after speed attempt {attempt}")
    try:
        proc.kill()
    except Exception as exc:
        logger.warning(f"{page_key}: could not stop transfer process: {exc}")
        return
    try:
        proc.communicate(timeout=10)
    except Exception:
        pass


def _process_finished(proc) -> bool:
    try:
        return proc.poll() is not None
    except Exception:
        return False


def _unlink_quietly(path: Path | None) -> None:
    """Best-effort delete of a superseded transfer screenshot. When a higher
    bracket-bound capture replaces an earlier one, drop the stale file so only the
    screenshot whose number equals the logged rate survives."""
    if path is None:
        return
    try:
        path.unlink()
    except OSError:
        pass


def _bound_transfer_capture(
    page: "Page",
    frame: "Frame",
    sn: str,
    page_key: str,
    share: str,
    direction: str,
    screenshots_dir: Path,
    *,
    min_rate_bytes: float,
    max_span_s: float = TRANSFER_BRACKET_MAX_SPAN_S,
) -> tuple[TransferRateSample, Path] | None:
    """Screenshot the transfer rate so the saved image provably shows the logged number.

    The UGOS resource monitor is piecewise-constant: it repaints the rate roughly
    once a second and holds it steady in between (measured ~1.0s refresh on real
    hardware). We read the displayed rate immediately before AND after the
    screenshot; the shot lands inside a single refresh window only when both reads
    are identical. If they differ, the screenshot straddled a refresh edge and is
    ambiguous, so we discard it and signal the caller to resample. The returned
    sample therefore equals the number frozen in the returned image — that binding,
    not any OCR of the pixels, is what guarantees 录表数值 == 截图数值.

    ``min_rate_bytes`` gates acceptance: pass the screenshot threshold for a pass
    capture, or 0 to bind whatever steady non-idle rate is on screen (the failure
    artifact). Returns ``(sample, shot)`` or ``None`` when the window was not steady.

    The ``max_span_s`` ceiling is deliberately correctness-first: if the screenshot
    itself drags long enough that the bracket could hide an A->B->A flip (two refresh
    edges), we reject rather than risk logging a number the image does not show. On a
    pathologically slow PC this can resample/false-fail a steady page (recoverable —
    operator retests); widen ``transfer.bracket_max_span_s`` (kept < the ~0.78s refresh
    floor) if that ever shows up. Measured screenshots are ~0.1s, ~6x under the default.
    """
    dismiss_desktop_overlays(page, max_rounds=2, completion_wait_ms=0)
    span_start = time.perf_counter()
    pre = _transfer_speed_sample(_frame_text(frame), share, direction)
    shot = capture_page(page, sn, page_key, screenshots_dir)
    post = _transfer_speed_sample(_frame_text(frame), share, direction)
    span = time.perf_counter() - span_start
    # Accept only when the SAME displayed text was read just before and just after the shot
    # — compare amount+unit, not just the byte count, so a unit-boundary flip like
    # "1024 KB/s" -> "1 MB/s" (equal bytes, different on-screen string) is rejected — AND
    # the whole bracket spanned less than one refresh window, so at most one repaint edge
    # could fall inside it and any change would have made the two reads differ.
    steady = (
        pre is not None
        and post is not None
        and pre.amount == post.amount
        and pre.unit == post.unit
        and post.rate_bytes >= min_rate_bytes
    )
    if steady and span < max_span_s:
        return post, shot
    if steady:
        # Display held steady across the reads but the shot took too long to trust as a
        # single-window capture; log it so a slow-machine false-fail is diagnosable.
        logger.info(
            f"{page_key}: discarding steady capture, bracket span {span:.2f}s "
            f">= {max_span_s:.2f}s (screenshot too slow to rule out a refresh straddle)"
        )
    _unlink_quietly(shot)
    return None


def _better_transfer_result(
    current: TransferAttemptResult | None,
    candidate: TransferAttemptResult,
) -> TransferAttemptResult:
    if current is None:
        return candidate
    # A result whose value is bound to a screenshot outranks a higher-but-unbound
    # text-only read, so the kept value stays tied to an image whenever one exists
    # (e.g. a final bracketed failure artifact must not lose to an earlier attempt's
    # higher peak that has no screenshot). Among equals, the higher rate wins.
    current_has_shot = current.shot_path is not None
    candidate_has_shot = candidate.shot_path is not None
    if current_has_shot != candidate_has_shot:
        return candidate if candidate_has_shot else current
    current_rate = _float_or_none(current.values.get("rate_mbps")) or 0.0
    candidate_rate = _float_or_none(candidate.values.get("rate_mbps")) or 0.0
    return candidate if candidate_rate >= current_rate else current


def _transfer_values_from_sample(
    sample: TransferRateSample,
    share: str,
    direction: str,
    threshold_mb_s: float,
    attempt: int,
    reached_threshold: bool,
) -> dict[str, str]:
    values = {
        "share": share,
        "direction": direction,
        "rate": sample.label,
        "rate_mbps": _format_decimal(sample.rate_mb_s),
        "threshold_mbps": _format_decimal(threshold_mb_s),
        "speed_status": "ok" if reached_threshold else "below_threshold",
        "attempt": str(attempt),
    }
    if sample.source:
        values["sample_source"] = sample.source
    return values


def _capture_fan_mode(
    page: "Page",
    sn: str,
    page_key: str,
    spec: dict,
    page_specs: dict,
    desktop_selectors: dict,
    nav_selectors: dict,
    screenshots_dir: Path,
    capture_values: dict[str, dict[str, str]] | None,
    cpu_temp_max_c: float = DEFAULT_CPU_TEMP_MAX_C,
    recheck_budget_s: float = DEFAULT_FAN_RECHECK_BUDGET_S,
) -> Path:
    fan_spec = _require_page_spec(page_specs, "fan_control")
    monitor_spec = _require_page_spec(page_specs, "resource_monitor")
    fan_row_selector = _require_selector(
        fan_spec.get("key_values", {}).get("fan_row"),
        "capture_pages.fan_control.key_values.fan_row",
    )
    mode_selector = _require_selector(
        spec.get("key_values", {}).get("mode_selector"),
        f"capture_pages.{page_key}.key_values.mode_selector",
    )
    wait_after_mode_ms = int(spec.get("wait_after_mode_ms", 5000))

    logger.info(f"Switching fan mode for {page_key}")
    fan_frame = _open_app(page, desktop_selectors, _require_meta(fan_spec.get("app"), "capture_pages.fan_control.app"))
    _navigate(fan_frame, fan_spec, nav_selectors)
    _wait_for_landmark(fan_frame, fan_spec, "fan_control")
    _open_fan_modal(fan_frame, fan_spec)
    _select_fan_mode(fan_frame, mode_selector, page_key)
    _confirm_fan_modal(fan_frame, fan_spec)
    page.wait_for_timeout(wait_after_mode_ms)
    fan_row = fan_frame.locator(fan_row_selector).first
    expect(fan_row).to_be_visible(timeout=FRAME_WAIT_MS)
    fan_row.scroll_into_view_if_needed()
    fan_layout = _get_window_layout(page, "ctlmgr")

    monitor_frame = _open_app(
        page,
        desktop_selectors,
        _require_meta(monitor_spec.get("app"), "capture_pages.resource_monitor.app"),
    )
    _navigate(monitor_frame, monitor_spec, nav_selectors)
    _wait_for_landmark(monitor_frame, monitor_spec, "resource_monitor")
    monitor_layout = _get_window_layout(page, "taskmgr")

    try:
        _arrange_fan_capture_windows(page)
        page.wait_for_timeout(FAN_POST_LAYOUT_WAIT_MS)
        # 读数不合格（温度超标 / 转速异常）就重读+重抓，最多 recheck_budget_s 秒，等读数进入
        # 合格区间再定格；超时仍不合格才保留最后一张按失败记录。与传输速度重试同理——消化
        # 「满载尾巴瞬时高温」「风扇刚切档还在爬坡」这类瞬时假故障，免得操作员白白重测一台好机。
        deadline = time.monotonic() + max(0.0, recheck_budget_s)
        attempt = 0
        values: dict[str, str] = {}
        shot: Path | None = None
        while True:
            attempt += 1
            dismiss_desktop_overlays(page, max_rounds=2, completion_wait_ms=0)
            page.wait_for_timeout(SHORT_UI_WAIT_MS)
            # Read the RPM back-to-back with the screenshot. The fan is still
            # ramping after a mode switch, and dismissing overlays / grabbing the
            # screenshot spikes the CPU (and fan), so any gap between the read and
            # the shot makes the reported RPM diverge from the number frozen in the
            # screenshot (observed 508 reported vs 1044 on screen).
            values = _log_key_values(monitor_frame, monitor_spec, page_key)
            shot = capture_page(page, sn, page_key, screenshots_dir)
            failures = fan_mode_value_failures(page_key, values, cpu_temp_max_c)
            if not failures:
                break
            if time.monotonic() >= deadline:
                if attempt > 1:
                    logger.warning(
                        f"{page_key}: 读数仍不合格（{'；'.join(failures)}），已用满 "
                        f"{recheck_budget_s:.0f}s 重抓预算，按当前截图记录"
                    )
                break
            logger.info(
                f"{page_key}: 读数不合格（{'；'.join(failures)}），"
                f"{FAN_RECHECK_INTERVAL_MS // 1000}s 后重抓（第 {attempt} 次，预算 {recheck_budget_s:.0f}s）"
            )
            try:
                shot.unlink(missing_ok=True)  # 丢弃这张不合格截图，免得目录里堆叠中间产物
            except OSError:
                pass
            page.wait_for_timeout(FAN_RECHECK_INTERVAL_MS)
        if capture_values is not None and values:
            capture_values[page_key] = values
        return shot
    finally:
        _restore_window_layout(page, "ctlmgr", fan_layout)
        _restore_window_layout(page, "taskmgr", monitor_layout)
        page.wait_for_timeout(600)


def _open_app(page: "Page", desktop_selectors: dict, app: str) -> "Frame":
    app_selector = _require_selector(desktop_selectors.get("apps", {}).get(app), f"desktop_launchers.apps.{app}")
    frame_selector = _require_selector(desktop_selectors.get("frames", {}).get(app), f"desktop_launchers.frames.{app}")

    iframe = page.locator(frame_selector).first
    if iframe.count() and iframe.is_visible():
        _activate_window(page, app)
    else:
        launcher = find_visible_locator(page, app_selector, FRAME_WAIT_MS)
        if launcher is None:
            if app == "taskmgr" and _open_taskmgr_fallback(page):
                logger.info("Task manager desktop launcher was not pinned; opened taskmgr via fallback")
            else:
                raise CaptureError(f"Desktop launcher for '{app}' did not appear")
        else:
            click_desktop_launcher(page, launcher, app)
        expect(iframe).to_be_visible(timeout=FRAME_WAIT_MS)

    page.wait_for_timeout(SHORT_UI_WAIT_MS)

    frame = _wait_for_frame(page, app)
    frame.wait_for_load_state("domcontentloaded")
    if app == "storagemgr":
        dismiss_arco_app_guides(page, frame, "storage manager")
    _recover_app_focus(page, frame, app)
    return frame


def _open_taskmgr_fallback(page: "Page") -> bool:
    _remove_direct_taskmgr_window(page)
    if _open_taskmgr_from_status_widget(page):
        return True
    return _open_taskmgr_direct(page)


def _open_taskmgr_from_status_widget(page: "Page") -> bool:
    candidates = [
        '.nav-operate .dashboard-tooltip-box .ivu-tooltip-rel:has-text("CPU")',
        '.nav-operate .dashboard-tooltip-box .ivu-tooltip-rel:has-text("RAM")',
        '.nav-operate .dashboard-tooltip-box .tool-use-operate',
        '.nav-operate .dashboard-tooltip-box',
    ]
    for selector in candidates:
        locator = page.locator(selector)
        try:
            count = min(locator.count(), 20)
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible(timeout=200):
                    continue
                box = candidate.bounding_box(timeout=500)
                if not box or box["y"] > 80 or box["x"] < 1400:
                    continue
                candidate.click(force=True, timeout=2_000)
                if _taskmgr_frame_visible(page, timeout_ms=8_000):
                    return True
            except Exception:
                continue

    try:
        viewport = page.viewport_size or {"width": 1920, "height": 1080}
        for offset in (230, 260, 300, 190):
            page.mouse.click(viewport["width"] - offset, 24)
            if _taskmgr_frame_visible(page, timeout_ms=8_000):
                return True
    except Exception:
        pass
    return False


def _taskmgr_frame_visible(page: "Page", timeout_ms: int) -> bool:
    try:
        page.locator('iframe[name="taskmgr"]').first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _remove_direct_taskmgr_window(page: "Page") -> None:
    try:
        page.evaluate(
            """() => {
                for (const section of Array.from(document.querySelectorAll('.cloud-window-main[data-name="taskmgr"][ver="direct"]'))) {
                    section.remove();
                }
            }"""
        )
    except Exception:
        pass


def _open_taskmgr_direct(page: "Page") -> bool:
    parsed = urlparse(page.url)
    if not parsed.scheme or not parsed.netloc:
        return False

    src = f"{parsed.scheme}://{parsed.netloc}/taskmgr/?_taskmgr=direct{int(time.time() * 1000)}#/"
    try:
        page.evaluate(
            """({src}) => {
                for (const iframe of Array.from(document.querySelectorAll('iframe[name="taskmgr"]'))) {
                    const windowEl = iframe.closest('.cloud-window-main');
                    if (windowEl) {
                        windowEl.remove();
                    } else {
                        iframe.remove();
                    }
                }

                const section = document.createElement('section');
                section.setAttribute('data-name', 'taskmgr');
                section.className = 'cloud-window-main anim focus';
                section.setAttribute('ver', 'direct');
                Object.assign(section.style, {
                    width: '1200px',
                    height: '771px',
                    left: '440px',
                    top: '211.5px',
                    zIndex: '999',
                    borderRadius: '12px',
                    background: 'rgb(245, 245, 245)',
                });

                const container = document.createElement('div');
                container.className = 'container full';
                container.style.height = '100%';

                const iframe = document.createElement('iframe');
                iframe.name = 'taskmgr';
                iframe.src = src;
                iframe.tabIndex = -1;
                iframe.style.width = '100%';
                iframe.style.height = '100%';
                iframe.style.border = '0';
                iframe.style.minWidth = '900px';
                iframe.style.minHeight = '600px';
                iframe.setAttribute('allowfullscreen', 'allowfullscreen');

                container.appendChild(iframe);
                section.appendChild(container);
                document.body.appendChild(section);
            }""",
            {"src": src},
        )
        expect(page.locator('iframe[name="taskmgr"]').first).to_be_visible(timeout=FRAME_WAIT_MS)
        return True
    except Exception as exc:
        logger.warning(f"Direct taskmgr iframe fallback failed: {exc}")
        return False


def _wait_for_frame(page: "Page", app: str) -> "Frame":
    deadline = time.monotonic() + (FRAME_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        frame = page.frame(name=app)
        if frame is not None:
            return frame
        page.wait_for_timeout(200)
    raise CaptureError(f"iframe '{app}' did not appear on the desktop")


def _frame_identity_text(page: "Page", frame: "Frame", include_browser_storage: bool = True) -> str:
    parts: list[str] = []
    getters = [
        lambda: page.title(),
        lambda: frame.title(),
        lambda: frame.locator("body").inner_text(timeout=1_000),
    ]
    if include_browser_storage:
        getters.extend(
            [
                lambda: frame.content(),
                lambda: frame.evaluate(
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
                ),
            ]
        )
    for getter in getters:
        try:
            parts.append(getter())
        except Exception:
            pass
    return "\n".join(parts)


def _navigate(frame: "Frame", spec: dict, nav_selectors: dict) -> None:
    app = frame.name
    for nav_key in spec.get("nav_path", []):
        selector = _require_selector(nav_selectors.get(nav_key), f"capture_nav.{nav_key}")
        loc = frame.locator(selector).first
        expect(loc).to_be_visible(timeout=FRAME_WAIT_MS)
        _click_frame_locator(frame, loc, app, f"navigation '{nav_key}'")
        frame.page.wait_for_timeout(800)


def _wait_for_landmark(frame: "Frame", spec: dict, page_key: str) -> None:
    if page_key == "system_update" and _wait_for_system_update_status(frame):
        return
    landmark = _require_selector(spec.get("landmark"), f"capture_pages.{page_key}.landmark")
    expect(frame.locator(landmark).first).to_be_visible(timeout=FRAME_WAIT_MS)


def _log_key_values(frame: "Frame", spec: dict, page_key: str) -> dict[str, str]:
    values = _collect_key_values(frame, spec, page_key)
    for value_key, value in values.items():
        logger.info(f"  {page_key}.{value_key}: {value}")
    return values


def _collect_key_values(frame: "Frame", spec: dict, page_key: str) -> dict[str, str]:
    if page_key == "system_update":
        values = _system_update_key_values(frame)
        if values:
            return values
    if page_key == "network_interface":
        values = _network_interface_key_values(frame)
        if values:
            return values
    if page_key == "storage_pool":
        values = _storage_pool_key_values(frame)
        if values:
            return values

    values: dict[str, str] = {}
    for value_key, selector in spec.get("key_values", {}).items():
        loc = frame.locator(_require_selector(selector, f"capture_pages.{page_key}.key_values.{value_key}")).first
        expect(loc).to_be_visible(timeout=5_000)
        values[value_key] = _locator_text(loc)
    return values


def _network_interface_key_values(frame: "Frame") -> dict[str, str]:
    text = _frame_text(frame)
    link_bps = _network_link_bps_text(text)
    if not link_bps:
        return {}
    return {"link_bps": link_bps}


def _network_link_bps_text(text: str) -> str | None:
    normalized = " ".join((text or "").split())
    if not normalized:
        return None

    candidates: list[float] = []
    for match in re.finditer(r"(?i)\b(10|2\.5)\s*G(?:bps|b/s|bit/s)?\b", normalized):
        candidates.append(float(match.group(1)))
    for match in re.finditer(r"(?i)\b(10000|2500)\s*M(?:bps|b/s|bit/s)?\b", normalized):
        value = float(match.group(1))
        if value >= 10000:
            candidates.append(10.0)
        elif value >= 2500:
            candidates.append(2.5)

    if not candidates:
        return None
    return "10" if max(candidates) >= 10 else "2.5"


def _system_update_key_values(frame: "Frame") -> dict[str, str]:
    text = _frame_text(frame)
    values: dict[str, str] = {}
    status = _system_update_status_text(text)
    version = _system_update_version_text(text)
    if status:
        values["latest_status"] = status
    if version:
        values["ugos_version"] = version
    return values


def _storage_pool_key_values(frame: "Frame") -> dict[str, str]:
    items = frame.locator(".storage-itemInfo")
    count = items.count()
    if count == 0:
        return {}

    values: dict[str, str] = {}
    for index in range(count):
        locator = items.nth(index)
        try:
            if not locator.is_visible(timeout=250):
                continue
            summary = " ".join(locator.inner_text(timeout=1_000).split())
        except Exception:
            continue

        disks = set(POOL_DISK_TOKEN_RE.findall(summary))
        if not disks:
            continue
        if any(disk.startswith("M.2硬盘") for disk in disks):
            values.setdefault("ssd_pool_raid", summary)
        elif any(disk.startswith("硬盘") for disk in disks):
            values.setdefault("hdd_pool_raid", summary)
    return values


def _wait_for_system_update_status(frame: "Frame") -> bool:
    deadline = time.monotonic() + (FRAME_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        text = _frame_text(frame)
        if _system_update_status_text(text):
            return True
        frame.page.wait_for_timeout(500)
    return False


def _log_system_update_key_values(frame: "Frame") -> bool:
    text = _frame_text(frame)
    status = _system_update_status_text(text)
    version = _system_update_version_text(text)
    if not status and not version:
        return False
    if status:
        logger.info(f"  system_update.latest_status: {status}")
    if version:
        logger.info(f"  system_update.ugos_version: {version}")
    return True


def _frame_text(frame: "Frame") -> str:
    try:
        return frame.locator("body").inner_text(timeout=2_000)
    except Exception:
        return ""


def _system_update_status_text(text: str) -> str | None:
    normalized = " ".join(text.split())
    if not normalized:
        return None
    marker = "\u5df2\u7ecf\u662f\u6700\u65b0\u7248\u672c"
    if marker in normalized:
        return _excerpt_around(normalized, marker)
    return None


def _system_update_version_text(text: str) -> str | None:
    normalized = " ".join(text.split())
    if not normalized:
        return None
    for marker in ("\u5f53\u524d\u7248\u672c", "\u68c0\u6d4b\u5230\u65b0\u7248\u672c"):
        if marker in normalized:
            excerpt = _excerpt_around(normalized, marker)
            version = _version_from_text(excerpt)
            return version or excerpt
    return _version_from_text(normalized)


def _version_from_text(text: str) -> str | None:
    match = re.search(r"\d+(?:\.\d+)+(?:[-._A-Za-z0-9]*)?", text or "")
    if not match:
        return None
    return match.group(0)


def _excerpt_around(text: str, marker: str, radius: int = 80) -> str:
    index = text.find(marker)
    if index < 0:
        return text[: radius * 2].strip()
    start = max(0, index - radius)
    end = min(len(text), index + len(marker) + radius)
    return text[start:end].strip()


def _log_storage_pool_key_values(frame: "Frame") -> bool:
    items = frame.locator(".storage-itemInfo")
    count = items.count()
    if count == 0:
        return False

    hdd_summary: str | None = None
    ssd_summary: str | None = None
    for index in range(count):
        locator = items.nth(index)
        try:
            if not locator.is_visible(timeout=250):
                continue
            summary = " ".join(locator.inner_text(timeout=1_000).split())
        except Exception:
            continue

        disks = set(POOL_DISK_TOKEN_RE.findall(summary))
        if not disks:
            continue
        if any(disk.startswith("M.2硬盘") for disk in disks):
            ssd_summary = ssd_summary or summary
        elif any(disk.startswith("硬盘") for disk in disks):
            hdd_summary = hdd_summary or summary

    if hdd_summary:
        logger.info(f"  storage_pool.hdd_pool_raid: {hdd_summary}")
    if ssd_summary:
        logger.info(f"  storage_pool.ssd_pool_raid: {ssd_summary}")
    return bool(hdd_summary or ssd_summary)


def _open_fan_modal(frame: "Frame", fan_spec: dict) -> None:
    key_values = fan_spec.get("key_values", {})
    fan_row_selector = _require_selector(key_values.get("fan_row"), "capture_pages.fan_control.key_values.fan_row")
    settings_selector = _require_selector(
        key_values.get("settings_button"),
        "capture_pages.fan_control.key_values.settings_button",
    )
    modal_selector = _require_selector(key_values.get("modal_root"), "capture_pages.fan_control.key_values.modal_root")
    title_selector = _require_selector(key_values.get("modal_title"), "capture_pages.fan_control.key_values.modal_title")

    fan_row = frame.locator(fan_row_selector).first
    expect(fan_row).to_be_visible(timeout=FRAME_WAIT_MS)
    fan_row.scroll_into_view_if_needed()
    frame.page.wait_for_timeout(500)

    settings_button = frame.locator(settings_selector).first
    expect(settings_button).to_be_visible(timeout=FRAME_WAIT_MS)
    _click_frame_locator(frame, settings_button, frame.name, "fan settings button")

    expect(frame.locator(modal_selector).first).to_be_visible(timeout=FRAME_WAIT_MS)
    expect(frame.locator(title_selector).first).to_be_visible(timeout=FRAME_WAIT_MS)


def _select_fan_mode(frame: "Frame", mode_selector: str, page_key: str) -> None:
    target = frame.locator(mode_selector).first
    expect(target).to_be_visible(timeout=FRAME_WAIT_MS)

    classes = target.get_attribute("class") or ""
    if "checked" in classes:
        logger.info(f"  {page_key} target mode already selected")
        return

    _click_frame_locator(frame, target, frame.name, f"{page_key} mode selector")
    logger.info(f"  {page_key} mode selected")


def _confirm_fan_modal(frame: "Frame", fan_spec: dict) -> None:
    key_values = fan_spec.get("key_values", {})
    modal_selector = _require_selector(key_values.get("modal_root"), "capture_pages.fan_control.key_values.modal_root")
    confirm_selector = _require_selector(
        key_values.get("confirm_button"),
        "capture_pages.fan_control.key_values.confirm_button",
    )

    confirm_button = frame.locator(confirm_selector).first
    expect(confirm_button).to_be_visible(timeout=FRAME_WAIT_MS)
    _click_frame_locator(frame, confirm_button, frame.name, "fan modal confirm button")
    frame.locator(modal_selector).first.wait_for(state="hidden", timeout=FRAME_WAIT_MS)


def _arrange_fan_capture_windows(page: "Page") -> None:
    _set_window_layout(page, "ctlmgr", FAN_CTL_WINDOW_LAYOUT)
    _set_window_layout(page, "taskmgr", FAN_TASK_WINDOW_LAYOUT)
    page.wait_for_timeout(1200)


def _set_window_layout(page: "Page", app: str, layout: dict[str, int]) -> None:
    window = page.locator(f'.cloud-window-main:has(iframe[name="{app}"])').first
    expect(window).to_be_visible(timeout=FRAME_WAIT_MS)
    window.evaluate(
        """(el, layout) => {
            el.style.left = `${layout.left}px`;
            el.style.top = `${layout.top}px`;
            el.style.width = `${layout.width}px`;
            el.style.height = `${layout.height}px`;
            el.style.zIndex = String(layout.z_index);
        }""",
        layout,
    )


def _restore_window_layout(page: "Page", app: str, layout: dict[str, int]) -> None:
    try:
        _set_window_layout(page, app, layout)
    except Exception as exc:
        logger.warning(f"Best-effort restore of {app} window layout failed: {exc}")


def _get_window_layout(page: "Page", app: str) -> dict[str, int]:
    window = page.locator(f'.cloud-window-main:has(iframe[name="{app}"])').first
    expect(window).to_be_visible(timeout=FRAME_WAIT_MS)
    return window.evaluate(
        """(el) => {
            const rect = el.getBoundingClientRect();
            const styles = getComputedStyle(el);
            const asInt = (value, fallback) => {
                const parsed = parseInt(value, 10);
                return Number.isFinite(parsed) ? parsed : fallback;
            };
            return {
                left: asInt(el.style.left || styles.left, Math.round(rect.left)),
                top: asInt(el.style.top || styles.top, Math.round(rect.top)),
                width: asInt(el.style.width || styles.width, Math.round(rect.width)),
                height: asInt(el.style.height || styles.height, Math.round(rect.height)),
                z_index: asInt(el.style.zIndex || styles.zIndex, 1),
            };
        }"""
    )


def _activate_window(page: "Page", app: str) -> None:
    window = page.locator(f'.cloud-window-main:has(iframe[name="{app}"])').first
    expect(window).to_be_visible(timeout=FRAME_WAIT_MS)
    window.evaluate(
        """(el) => {
            const windows = Array.from(document.querySelectorAll('.cloud-window-main'));
            const maxZ = windows.reduce((acc, node) => {
                const value = parseInt(getComputedStyle(node).zIndex || '0', 10);
                return Number.isFinite(value) ? Math.max(acc, value) : acc;
            }, 0);
            el.style.zIndex = String(maxZ + 1);
            el.classList.add('focus');
            el.classList.remove('blur');
        }"""
    )
    page.wait_for_timeout(200)
    window.click(position={"x": 120, "y": 24}, force=True)


def _click_frame_locator(frame: "Frame", locator, app: str, label: str) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        if attempt > 0:
            logger.info(f"Recovering {app} focus for {label}")
            _recover_app_focus(frame.page, frame, app)
        if app == "storagemgr":
            dismiss_storage_guide_modal(frame, "storage manager")
        try:
            locator.click(timeout=5_000, force=attempt > 0)
            return
        except Exception as exc:
            last_error = exc
            if app == "storagemgr":
                logger.info(f"{label} in {app} was blocked; dismissing storage welcome modal and retrying")
                dismiss_storage_guide_modal(frame, "storage manager")
    if last_error is not None:
        raise last_error
    raise CaptureError(f"Could not click {label} in {app}")


def _recover_app_focus(page: "Page", frame: "Frame", app: str) -> None:
    _activate_window(page, app)
    removed = _remove_frame_blur(frame)
    if removed:
        logger.info(f"Recovered blurred state for {app} window")
    page.wait_for_timeout(200)


def _remove_frame_blur(frame: "Frame") -> int:
    return int(
        frame.evaluate(
            """() => {
                let count = 0;
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0
                        && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && style.opacity !== '0';
                };
                for (const el of Array.from(document.querySelectorAll('.container.blur'))) {
                    if (!isVisible(el)) {
                        continue;
                    }
                    el.classList.remove('blur');
                    el.classList.add('focus');
                    count += 1;
                }
                return count;
            }"""
        )
    )


def _build_transfer_config(nas_url: str, share: str, run_dir: Path, admin: dict, transfer_cfg: dict) -> SmbTransferConfig:
    host = urlparse(nas_url).hostname
    if not host:
        raise CaptureError(f"Could not extract NAS host from '{nas_url}'")

    local_dir = _transfer_run_dir(run_dir, transfer_cfg) / share
    local_dir.mkdir(parents=True, exist_ok=True)
    drive_letter = "H" if share == "hdd" else "S"

    source_file: Path | None = None
    model = _normalize_transfer_model_key(transfer_cfg.get("model") or transfer_cfg.get("form_model") or "2800")
    source_size_bytes = _transfer_source_size_bytes(transfer_cfg, model)
    source_path_str = _transfer_source_path_for_model(transfer_cfg, model, source_size_bytes)
    if source_path_str:
        source_file = _resolve_transfer_source_file(str(source_path_str), source_size_bytes)

    return SmbTransferConfig(
        unc_share=fr"\\{host}\{share}",
        local_dir=local_dir,
        drive_letter=drive_letter,
        source_file=source_file,
        file_name=source_file.name if source_file else f"factory_{share}_{source_size_bytes // GIB}gb.bin",
        file_size_bytes=source_size_bytes,
        username=admin.get("username"),
        password=admin.get("password"),
    )


def _transfer_speed_threshold_mb_s(page_key: str, transfer_cfg: dict) -> float:
    thresholds = transfer_cfg.get("speed_thresholds_mb_s") or transfer_cfg.get("speed_thresholds") or {}
    model = _normalize_transfer_model_key(transfer_cfg.get("model") or transfer_cfg.get("form_model") or "2800")
    configured_default = _float_or_none((thresholds or {}).get("default")) if isinstance(thresholds, dict) else None
    default = configured_default or _default_transfer_speed_threshold_mb_s(model, page_key)

    if not isinstance(thresholds, dict):
        return default

    models = thresholds.get("models") or {}
    model_thresholds = _lookup_mapping_key(models, model)
    if isinstance(model_thresholds, dict):
        model_default = _float_or_none(_lookup_mapping_key(model_thresholds, "default"))
        value = _float_or_none(_lookup_mapping_key(model_thresholds, page_key))
        if value is not None:
            return value

        transfer_type = "ssd" if page_key.startswith("ssd_") else "hdd" if page_key.startswith("hdd_") else ""
        direction = "read" if page_key.endswith("_read") else "write" if page_key.endswith("_write") else ""
        for key in (transfer_type, direction, f"{transfer_type}_{direction}"):
            value = _float_or_none(_lookup_mapping_key(model_thresholds, key))
            if value is not None:
                return value
        if model_default is not None:
            return model_default

    value = _float_or_none(_lookup_mapping_key(thresholds, page_key))
    return value if value is not None else default


def _default_transfer_speed_threshold_mb_s(model: str, page_key: str) -> float:
    if model == "4800Plus":
        return 700.0 if page_key.startswith("ssd_") else 300.0
    return TRANSFER_SPEED_DEFAULT_THRESHOLD_MB_S


def _normalize_transfer_model_key(model: object) -> str:
    compact = "".join(str(model or "").split()).lower()
    if compact in {"4800plus", "dxp4800plus"}:
        return "4800Plus"
    if compact in {"4800", "dxp4800"}:
        return "4800"
    return "2800"


def _lookup_mapping_key(mapping: object, key: str) -> object | None:
    if not isinstance(mapping, dict):
        return None
    if key in mapping:
        return mapping[key]
    normalized = key.lower()
    for candidate_key, value in mapping.items():
        if str(candidate_key).lower() == normalized:
            return value
    return None


def _transfer_speed_max_retries(transfer_cfg: dict) -> int:
    raw = transfer_cfg.get("speed_max_retries", transfer_cfg.get("max_speed_retries", TRANSFER_SPEED_MAX_RETRIES))
    return max(0, int(_float_or_none(raw) if _float_or_none(raw) is not None else TRANSFER_SPEED_MAX_RETRIES))


def _transfer_speed_sample_interval_ms(transfer_cfg: dict) -> int:
    raw = transfer_cfg.get("speed_sample_interval_ms", TRANSFER_SPEED_SAMPLE_INTERVAL_MS)
    value = int(_float_or_none(raw) if _float_or_none(raw) is not None else TRANSFER_SPEED_SAMPLE_INTERVAL_MS)
    return max(100, value)


def _transfer_speed_stable_samples(transfer_cfg: dict) -> int:
    raw = transfer_cfg.get("speed_stable_samples", TRANSFER_SPEED_STABLE_SAMPLES)
    value = int(_float_or_none(raw) if _float_or_none(raw) is not None else TRANSFER_SPEED_STABLE_SAMPLES)
    return max(1, value)


def _transfer_post_upload_settle_timeout_s(transfer_cfg: dict) -> float:
    raw = transfer_cfg.get("post_upload_settle_timeout_s", TRANSFER_POST_UPLOAD_SETTLE_TIMEOUT_S)
    value = _float_or_none(raw)
    return max(0.0, value if value is not None else TRANSFER_POST_UPLOAD_SETTLE_TIMEOUT_S)


def _transfer_post_upload_settle_interval_ms(transfer_cfg: dict) -> int:
    raw = transfer_cfg.get("post_upload_settle_interval_ms", TRANSFER_POST_UPLOAD_SETTLE_INTERVAL_MS)
    value = int(_float_or_none(raw) if _float_or_none(raw) is not None else TRANSFER_POST_UPLOAD_SETTLE_INTERVAL_MS)
    return max(100, value)


def _transfer_post_upload_settle_samples(transfer_cfg: dict) -> int:
    raw = transfer_cfg.get("post_upload_settle_samples", TRANSFER_POST_UPLOAD_SETTLE_SAMPLES)
    value = int(_float_or_none(raw) if _float_or_none(raw) is not None else TRANSFER_POST_UPLOAD_SETTLE_SAMPLES)
    return max(1, value)


def _transfer_speed_attempt_timeout_s(transfer_cfg: dict) -> float:
    raw = transfer_cfg.get("speed_attempt_timeout_s", TRANSFER_SPEED_ATTEMPT_TIMEOUT_S)
    value = _float_or_none(raw)
    return max(5.0, value if value is not None else TRANSFER_SPEED_ATTEMPT_TIMEOUT_S)


def _transfer_bracket_max_span_s(transfer_cfg: dict) -> float:
    # Tunable so an unusually slow factory PC (full-page screenshots dragging toward the
    # ~0.78s display-refresh floor) can widen the bracket window instead of false-failing
    # a steady page. Keep it below the min refresh period to preserve the binding guarantee.
    raw = transfer_cfg.get("bracket_max_span_s", TRANSFER_BRACKET_MAX_SPAN_S)
    value = _float_or_none(raw)
    return value if value is not None and value > 0 else TRANSFER_BRACKET_MAX_SPAN_S


def _transfer_upload_seed_timeout_s(transfer_cfg: dict) -> float:
    raw = _model_transfer_cfg_value(transfer_cfg, "upload_seed_timeout_s")
    value = _float_or_none(raw)
    return max(5.0, value if value is not None else TRANSFER_UPLOAD_SEED_TIMEOUT_S)


def _transfer_upload_finish_timeout_s(transfer_cfg: dict) -> float:
    raw = transfer_cfg.get("upload_finish_timeout_s", TRANSFER_UPLOAD_FINISH_TIMEOUT_S)
    value = _float_or_none(raw)
    return max(0.0, value if value is not None else TRANSFER_UPLOAD_FINISH_TIMEOUT_S)


def _transfer_upload_min_seed_bytes(transfer_cfg: dict) -> int:
    raw = _model_transfer_cfg_value(transfer_cfg, "upload_min_seed_mb")
    value = _float_or_none(raw)
    if value is None:
        model = _normalize_transfer_model_key(transfer_cfg.get("model") or transfer_cfg.get("form_model") or "2800")
        value = TRANSFER_SOURCE_SIZE_GIB_BY_MODEL.get(model, 10) * 1024
    mb = value if value is not None else TRANSFER_UPLOAD_MIN_SEED_MB
    return int(max(0.0, mb) * 1024 * 1024)


def _transfer_run_dir(run_dir: Path, transfer_cfg: dict) -> Path:
    session_name = run_dir.name or "session"
    return _transfer_work_root(transfer_cfg) / session_name


def _transfer_work_root(transfer_cfg: dict) -> Path:
    raw = transfer_cfg.get("work_dir") or transfer_cfg.get("local_work_dir")
    if raw:
        path = Path(str(raw)).expanduser()
        return (path if path.is_absolute() else _runtime_root() / path).resolve()
    return (_runtime_root() / "transfer_work").resolve()


def _transfer_source_path_for_model(transfer_cfg: dict, model: str, source_size_bytes: int) -> str:
    source_files = (
        transfer_cfg.get("source_files")
        or transfer_cfg.get("source_files_by_model")
        or transfer_cfg.get("model_source_files")
    )
    if isinstance(source_files, dict):
        value = _lookup_mapping_key(source_files, model)
        if value:
            return str(value)

    value = transfer_cfg.get("source_file")
    if value:
        return str(value)

    size_gib = max(1, source_size_bytes // GIB)
    return f"./测试{size_gib}G.rar"


def _transfer_source_size_bytes(transfer_cfg: dict, model: str) -> int:
    raw = _model_transfer_cfg_value(transfer_cfg, "source_file_sizes_gib")
    value = _float_or_none(raw)
    if value is None:
        raw = _model_transfer_cfg_value(transfer_cfg, "source_size_gib")
        value = _float_or_none(raw)
    if value is None:
        value = TRANSFER_SOURCE_SIZE_GIB_BY_MODEL.get(model, 10)
    return int(max(1.0, value) * GIB)


def _model_transfer_cfg_value(transfer_cfg: dict, key: str) -> object | None:
    raw = transfer_cfg.get(key)
    if isinstance(raw, dict):
        model = _normalize_transfer_model_key(transfer_cfg.get("model") or transfer_cfg.get("form_model") or "2800")
        value = _lookup_mapping_key(raw, model)
        if value is not None:
            return value
        return _lookup_mapping_key(raw, "default")
    return raw


def _resolve_transfer_source_file(source_path_str: str, min_size_bytes: int | None = None) -> Path:
    min_size_bytes = min_size_bytes if min_size_bytes is not None else TEN_GIB
    runtime_root = _runtime_root()
    raw_path = Path(source_path_str).expanduser()
    configured = (raw_path if raw_path.is_absolute() else runtime_root / raw_path).resolve()
    if _valid_transfer_source_file(configured, min_size_bytes):
        return configured

    search_dirs = [configured.parent, runtime_root, Path.cwd(), Path(__file__).resolve().parents[2]]
    size_gib = max(1, min_size_bytes // GIB)
    for directory in _unique_paths(search_dirs):
        candidates: list[Path] = []
        try:
            if directory.is_dir():
                for pattern in (f"*{size_gib}G*.rar", f"*{size_gib}G*"):
                    candidates.extend(
                        path.resolve()
                        for path in directory.glob(pattern)
                        if _valid_transfer_source_file(path, min_size_bytes)
                    )
        except Exception:
            continue

        unique_candidates = sorted(set(candidates), key=lambda path: path.stat().st_size, reverse=True)
        if unique_candidates:
            selected = unique_candidates[0]
            logger.warning(f"Configured transfer.source_file does not exist: {configured}; using {selected}")
            return selected

    generated = configured if not raw_path.is_absolute() else runtime_root / configured.name
    return _generate_transfer_source_file(generated, min_size_bytes)


def _runtime_root() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def _unique_paths(paths: list[Path]) -> list[Path]:
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        try:
            resolved = path.resolve()
        except Exception:
            continue
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def _valid_transfer_source_file(path: Path, min_size_bytes: int | None = None) -> bool:
    min_size_bytes = min_size_bytes if min_size_bytes is not None else TEN_GIB
    try:
        size = path.stat().st_size
        return path.is_file() and size >= min_size_bytes and _transfer_source_has_nonzero_sample(path, size)
    except OSError:
        return False


def _transfer_source_has_nonzero_sample(path: Path, size: int) -> bool:
    if size <= 0:
        return False
    sample_size = min(TRANSFER_SOURCE_SAMPLE_BYTES, size)
    offsets = _unique_sample_offsets(size, sample_size)
    with path.open("rb") as handle:
        for offset in offsets:
            handle.seek(offset)
            if any(handle.read(sample_size)):
                return True
    return False


def _unique_sample_offsets(size: int, sample_size: int) -> list[int]:
    starts = [
        0,
        size // 4,
        size // 2,
        (size * 3) // 4,
        max(0, size - sample_size),
    ]
    offsets: list[int] = []
    for start in starts:
        offset = min(max(0, start), max(0, size - sample_size))
        if offset not in offsets:
            offsets.append(offset)
    return offsets


def _generate_transfer_source_file(path: Path, size_bytes: int | None = None) -> Path:
    size_bytes = size_bytes if size_bytes is not None else TEN_GIB
    path.parent.mkdir(parents=True, exist_ok=True)
    logger.warning(
        f"Transfer source file missing, undersized, or all-zero; "
        f"generating non-compressible {size_bytes / GIB:.0f}GiB file: {path}"
    )
    tmp = path.with_name(f"{path.name}.partial")
    try:
        if tmp.exists():
            tmp.unlink()
        _write_nonzero_transfer_source(tmp, size_bytes)
        tmp.replace(path)
    except OSError as exc:
        raise CaptureError(f"Could not generate transfer source file at {path}: {exc}") from exc

    if not _valid_transfer_source_file(path, size_bytes):
        raise CaptureError(f"Generated transfer source file is invalid: {path}")
    return path


def _write_nonzero_transfer_source(path: Path, size: int) -> None:
    chunk_size = max(1, min(TRANSFER_SOURCE_GENERATE_CHUNK_BYTES, size))
    chunk = bytearray(os.urandom(chunk_size))
    if not any(chunk):
        chunk[0] = 1

    remaining = size
    chunk_index = 0
    with path.open("wb") as handle:
        while remaining > 0:
            write_size = min(chunk_size, remaining)
            counter = (chunk_index + 1).to_bytes(8, "little", signed=False)
            chunk[: min(8, write_size)] = counter[: min(8, write_size)]
            handle.write(memoryview(chunk)[:write_size])
            remaining -= write_size
            chunk_index += 1


def _ps_quote(value: str) -> str:
    return value.replace("'", "''")


def _prepare_share(cfg: SmbTransferConfig) -> None:
    _run_powershell_best_effort(build_unmount_script(cfg), timeout_s=30)
    run_powershell(build_mount_script(cfg), timeout_s=60)
    # When source_file is supplied, we upload an existing real file instead of
    # creating a 10GB sparse file in the output directory.
    if cfg.source_file is None:
        run_powershell(build_prepare_sparse_file_script(cfg), timeout_s=300)


def _remote_file_exists(cfg: SmbTransferConfig) -> bool:
    remote_file = cfg.remote_file.replace("'", "''")
    script = f"$remote = '{remote_file}'; if (Test-Path -LiteralPath $remote) {{ Write-Output 1 }} else {{ Write-Output 0 }}"
    result = run_powershell(script, timeout_s=30)
    return result.stdout.strip().endswith("1")


def _cleanup_transfer(cfg: SmbTransferConfig, direction: str) -> None:
    _run_powershell_best_effort(build_cleanup_script(cfg), timeout_s=60)
    if direction == "download":
        _run_powershell_best_effort(build_remote_cleanup_script(cfg), timeout_s=60)
    _run_powershell_best_effort(build_unmount_script(cfg), timeout_s=30)


def _wait_for_transfer_activity(page: "Page", frame: "Frame", page_key: str, share: str, direction: str) -> None:
    deadline = time.monotonic() + TRANSFER_ACTIVITY_WAIT_S
    last_excerpt = ""

    while time.monotonic() < deadline:
        text = frame.locator("body").inner_text(timeout=5_000)
        last_excerpt = " ".join(text.split())[:300]
        if _has_expected_transfer_activity(text, share, direction):
            logger.info(f"  {page_key} transfer activity detected")
            return
        page.wait_for_timeout(1_000)

    raise CaptureError(f"No disk transfer activity detected for '{page_key}'. Last body excerpt: {last_excerpt}")


def _wait_for_transfer_midpoint(
    page: "Page",
    cfg: SmbTransferConfig,
    direction: str,
    page_key: str,
    midpoint_ratio: float,
) -> None:
    total_bytes = _transfer_total_bytes(cfg)
    ratio = min(max(midpoint_ratio, 0.05), 0.95)
    target_bytes = int(total_bytes * ratio)
    deadline = time.monotonic() + TRANSFER_TIMEOUT_S
    last_size = 0

    while time.monotonic() < deadline:
        current_size = _progress_size(cfg.progress_file)
        last_size = max(last_size, current_size)
        if current_size >= target_bytes:
            logger.info(
                f"  {page_key} transfer reached {ratio:.0%} "
                f"({_format_bytes(current_size)} / {_format_bytes(total_bytes)})"
            )
            page.wait_for_timeout(400)
            return
        page.wait_for_timeout(500)

    raise CaptureError(
        f"Transfer '{page_key}' did not reach {ratio:.0%}; "
        f"last size {_format_bytes(last_size)} / {_format_bytes(total_bytes)}"
    )


def _transfer_total_bytes(cfg: SmbTransferConfig) -> int:
    try:
        return cfg.local_file.stat().st_size
    except OSError:
        return cfg.file_size_bytes


def _progress_size(path: Path) -> int:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").strip()
        return int(raw)
    except (OSError, ValueError):
        return 0


def _format_bytes(value: int) -> str:
    gib = value / (1024 * 1024 * 1024)
    return f"{gib:.2f}GiB"


def _run_powershell_best_effort(script: str, timeout_s: int) -> None:
    try:
        run_powershell(script, timeout_s=timeout_s)
    except SmbTransferError as exc:
        logger.warning(f"Best-effort PowerShell step failed: {exc}")


def _cleanup_transfer_run_dir(run_dir: Path) -> None:
    """Remove local SMB transfer artefacts for one SN/session."""
    if not run_dir.exists():
        return
    try:
        for entry in run_dir.iterdir():
            if entry.is_dir():
                _cleanup_transfer_file_dir(entry)
            elif entry.is_file():
                _unlink_transfer_file(entry)
        try:
            run_dir.rmdir()
        except OSError:
            pass
    except Exception as exc:
        logger.warning(f"Could not clean up transfer run dir {run_dir}: {exc}")


def _cleanup_transfer_file_dir(file_dir: Path) -> None:
    try:
        for entry in file_dir.iterdir():
            if entry.is_file():
                _unlink_transfer_file(entry)
        try:
            file_dir.rmdir()
        except OSError:
            pass
    except Exception as exc:
        logger.warning(f"Could not clean up transfer workdir {file_dir}: {exc}")


def _unlink_transfer_file(path: Path) -> None:
    try:
        path.unlink()
    except Exception as exc:
        logger.warning(f"Could not remove stale transfer file {path}: {exc}")


def _cleanup_transfer_workdir(run_dir: Path) -> None:
    """Remove stray SMB transfer artefacts and the empty smb/ dir."""
    smb_dir = run_dir / "smb"
    if not smb_dir.exists():
        return
    try:
        for entry in smb_dir.iterdir():
            if entry.is_file():
                try:
                    entry.unlink()
                except Exception as exc:
                    logger.warning(f"Could not remove stale transfer file {entry}: {exc}")
        try:
            smb_dir.rmdir()
        except OSError:
            pass  # non-empty (e.g. unexpected subdir) — leave it be
    except Exception as exc:
        logger.warning(f"Could not clean up transfer workdir {smb_dir}: {exc}")


def _cleanup_all_transfer_workdirs_if_idle(output_root: Path) -> None:
    if not TRANSFER_STAGE_LOCK.acquire(blocking=False):
        logger.info("Skipping old SMB workdir cleanup while another transfer is active")
        return
    try:
        _cleanup_all_transfer_workdirs(output_root)
    finally:
        TRANSFER_STAGE_LOCK.release()


def _cleanup_all_transfer_workdirs(output_root: Path) -> None:
    if not output_root.exists():
        return
    try:
        for run_dir in output_root.iterdir():
            if run_dir.is_dir():
                _cleanup_transfer_run_dir(run_dir)
    except Exception as exc:
        logger.warning(f"Could not clean old transfer workdirs under {output_root}: {exc}")


def _cleanup_legacy_transfer_workdirs_if_idle(output_root: Path) -> None:
    if not TRANSFER_STAGE_LOCK.acquire(blocking=False):
        logger.info("Skipping legacy SMB workdir cleanup while another transfer is active")
        return
    try:
        _cleanup_legacy_transfer_workdirs(output_root)
    finally:
        TRANSFER_STAGE_LOCK.release()


def _cleanup_legacy_transfer_workdirs(output_root: Path) -> None:
    if not output_root.exists():
        return
    try:
        for smb_dir in output_root.glob("*/smb"):
            if smb_dir.is_dir():
                _cleanup_transfer_file_dir(smb_dir)
    except Exception as exc:
        logger.warning(f"Could not clean legacy transfer workdirs under {output_root}: {exc}")


def _has_expected_transfer_activity(text: str, share: str, direction: str) -> bool:
    read_section, write_section = _disk_sections(text)
    target_section = write_section if direction == "upload" else read_section
    devices = [r"硬盘1", r"硬盘2", r"硬盘3", r"硬盘4"] if share == "hdd" else [r"M\.2硬盘1", r"M\.2硬盘2"]
    devices = _transfer_device_patterns(share)
    return _section_total_is_active(target_section) and _section_has_active_device(target_section, devices)


def _transfer_speed_sample(text: str, share: str, direction: str) -> TransferRateSample | None:
    read_section, write_section = _disk_sections(text)
    target_section = write_section if direction == "upload" else read_section
    target_source = "write_total" if direction == "upload" else "read_total"
    sample = _section_total_rate_sample(target_section, target_source)
    if sample is not None and sample.rate_bytes >= MIN_TRANSFER_RATE_BYTES:
        return sample

    return None


def _transfer_device_patterns(share: str) -> list[str]:
    return [r"硬盘1", r"硬盘2", r"硬盘3", r"硬盘4"] if share == "hdd" else [r"M\.2硬盘1", r"M\.2硬盘2"]


def _collect_transfer_values(frame: "Frame", share: str, direction: str) -> dict[str, str]:
    sample = _transfer_speed_sample(_frame_text(frame), share, direction)
    if sample is None:
        return {}
    values = {
        "share": share,
        "direction": direction,
        "rate": sample.label,
        "rate_mbps": _format_decimal(sample.rate_mb_s),
    }
    if sample.source:
        values["sample_source"] = sample.source
    return values


def _disk_sections(text: str) -> tuple[str, str]:
    read_marker = "读取速度（总）"
    write_marker = "写入速度（总）"

    read_section = ""
    write_section = ""

    if read_marker in text:
        read_section = text.split(read_marker, 1)[1]
        if write_marker in read_section:
            read_section = read_section.split(write_marker, 1)[0]

    if write_marker in text:
        write_section = text.split(write_marker, 1)[1]

    return read_section, write_section


def _section_total_is_active(section: str) -> bool:
    sample = _section_total_rate_sample(section)
    return bool(sample and sample.rate_bytes >= MIN_TRANSFER_RATE_BYTES)


def _section_total_rate_sample(section: str, source: str = "") -> TransferRateSample | None:
    match = RATE_RE.search(section)
    if not match:
        return None
    amount, unit = match.group(1), match.group(2)
    return TransferRateSample(amount=amount, unit=unit, rate_bytes=_rate_to_bytes(amount, unit), source=source)


def _section_has_active_device(section: str, device_patterns: list[str]) -> bool:
    for device in device_patterns:
        match = re.search(rf"{device}\s+([0-9]+(?:\.\d+)?)\s*([KMG]?B/s)", section)
        if match and _rate_to_bytes(match.group(1), match.group(2)) >= MIN_TRANSFER_RATE_BYTES:
            return True
    return False


def _rate_to_bytes(amount: str, unit: str) -> float:
    value = float(amount)
    factor = {
        "B/s": 1,
        "KB/s": 1024,
        "MB/s": 1024 * 1024,
        "GB/s": 1024 * 1024 * 1024,
    }.get(unit, 1)
    return value * factor


def _format_decimal(value: float) -> str:
    text = f"{value:.1f}"
    return text[:-2] if text.endswith(".0") else text


def _float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _locator_text(locator: "Locator") -> str:
    text = locator.inner_text().strip()
    return " ".join(text.split())


def _require_page_spec(page_specs: dict, page_key: str) -> dict:
    spec = page_specs.get(page_key)
    if not spec:
        raise CaptureError(f"Page '{page_key}' has no entry in selectors.yml")
    return spec


def _require_selector(selector: str | None, name: str) -> str:
    if not selector or selector == "TODO":
        raise CaptureError(f"Selector '{name}' is not configured")
    return selector


def _require_meta(value: str | None, name: str) -> str:
    if not value:
        raise CaptureError(f"Metadata '{name}' is not configured")
    return value


def _emit_capture_event(progress_cb: Callable[[dict], None] | None, event_type: str, **payload: object) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb({"type": event_type, **payload})
    except Exception:
        pass
