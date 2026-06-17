from __future__ import annotations

import time
from typing import TYPE_CHECKING

from playwright.sync_api import TimeoutError as PWTimeoutError

from ..flows import login as login_flow
from ..utils.desktop import dismiss_desktop_overlays, find_visible_locator
from ..utils.logger import logger

if TYPE_CHECKING:
    from playwright.sync_api import Page


UPDATE_WAIT_S = 1800
UPDATE_START_WAIT_S = 180
CONFIRM_PROMPT_WAIT_S = 60
MIN_POST_CONFIRM_WAIT_MS = 8_000
UPDATE_IDLE_REFRESH_S = 180
UPDATE_REFRESH_SETTLE_MS = 3_000
CHECK_UPDATE_SETTLE_MS = 3_000
UPDATE_INSTALL_HTTP_PROBE_S = 15
UPDATE_READY_HTTP_PROBE_SAMPLES = 2
UPDATE_DESKTOP_NAV_INTERVAL_S = 30
UPDATE_HTTP_PROBE_TIMEOUT_MS = 3_000
UPDATE_NOTICE_SELECTOR = '.ivu-notice:has-text("立即更新"), .ivu-notice:has-text("已经下载并准备好")'
UPDATE_NOTICE_LINK_SELECTOR = '.ivu-notice:has-text("立即更新") .notice-link'
LOGIN_PASSWORD_SELECTOR = 'input[type="password"]'
SETUP_WIZARD_START_SELECTOR = "button.start-btn"
# A reboot can briefly serve the first-run page before the config DB is restored; only treat
# the setup wizard as a real reset after it survives this long (≈3 of the 30s reloads).
SETUP_WIZARD_CONFIRM_S = 90
LATEST_TEXT = "\u5df2\u7ecf\u662f\u6700\u65b0\u7248\u672c"
CHECK_UPDATE_TEXTS = ("\u68c0\u6d4b\u66f4\u65b0", "\u68c0\u67e5\u66f4\u65b0")
INSTALL_UPDATE_TEXTS = (
    "\u7acb\u5373\u66f4\u65b0",
    "\u4e0b\u8f7d\u5e76\u5b89\u88c5",
    "\u4e0b\u8f7d\u5b89\u88c5",
    "\u5b89\u88c5\u66f4\u65b0",
    "\u7acb\u5373\u5b89\u88c5",
    "\u7acb\u5373\u5347\u7ea7",
    "\u5f00\u59cb\u66f4\u65b0",
)
DOWNLOAD_UPDATE_TEXTS = (
    "\u7acb\u5373\u4e0b\u8f7d",
    "\u4e0b\u8f7d\u66f4\u65b0",
    "\u4e0b\u8f7d",
)
UPDATE_ACTION_TEXTS = INSTALL_UPDATE_TEXTS + DOWNLOAD_UPDATE_TEXTS
CONFIRM_UPDATE_TEXTS = (
    "\u786e\u8ba4\u66f4\u65b0",
    "\u7acb\u5373\u66f4\u65b0",
    "\u7acb\u5373\u5b89\u88c5",
    "\u5b89\u88c5\u66f4\u65b0",
    "\u4e0b\u8f7d\u5e76\u5b89\u88c5",
    "\u4e0b\u8f7d\u5b89\u88c5",
    "\u5f00\u59cb\u66f4\u65b0",
    "\u7ee7\u7eed",
    "\u786e\u8ba4",
    "\u786e\u5b9a",
)
CONFIRM_MODAL_SELECTORS = (
    '.ivu-modal:visible',
    '.ivu-modal-wrap:visible',
    '.arco-modal:visible',
    '[role="dialog"]:visible',
)
UPDATE_PROMPT_KEYWORDS = (
    "\u66f4\u65b0",
    "\u5347\u7ea7",
    "\u5b89\u88c5",
    "\u91cd\u542f",
    "\u7cfb\u7edf",
)
UPDATE_INSTALLING_MARKERS = (
    "\u7cfb\u7edf\u5b89\u88c5\u4e2d",
    "\u7cfb\u7edf\u6b63\u5728\u66f4\u65b0\u81f3\u6700\u65b0\u7248\u672c",
    "\u6b63\u5728\u5b89\u88c5",
    "\u6b63\u5728\u66f4\u65b0",
    "\u66f4\u65b0\u4e2d",
    "\u8bbe\u5907\u6b63\u5728\u91cd\u542f",
    "\u8bf7\u52ff\u5173\u95ed\u8bbe\u5907",
    "\u8bf7\u52ff\u65ad\u7535",
    "\u9884\u8ba1\u9700\u898115-20\u5206\u949f",
    "\u5b89\u88c5\u5b8c\u6210\u540e\u8bbe\u5907\u5c06\u81ea\u52a8\u91cd\u542f",
    "\u7ee7\u7eed\u5b8c\u6210\u91cd\u542f\u6307\u4ee4",
    "\u670d\u52a1\u542f\u52a8\u4e2d",
)
UPDATE_PROGRESS_MARKERS = (
    "\u4e0b\u8f7d\u4e2d",
    "\u6b63\u5728\u4e0b\u8f7d",
    "\u8fdb\u5ea6",
    "\u68c0\u6d4b\u5230\u65b0\u7248\u672c",
    "\u6709\u53ef\u7528\u7684\u7cfb\u7edf\u66f4\u65b0",
)
AGREEMENT_CHECKBOX_TEXTS = (
    "\u6211\u5df2",
    "\u6211\u5df2\u9605\u8bfb",
    "\u540c\u610f",
    "\u77e5\u6653",
)


class SystemUpdateError(RuntimeError):
    pass


def _refresh_update_page(page: "Page", nas_url: str, reason: str) -> None:
    logger.info(f"System update: no progress for {UPDATE_IDLE_REFRESH_S}s; refreshing browser ({reason})")
    try:
        page.reload(wait_until="domcontentloaded", timeout=10_000)
    except Exception:
        try:
            page.goto(nas_url, wait_until="domcontentloaded", timeout=10_000)
        except Exception:
            pass
    try:
        page.wait_for_timeout(UPDATE_REFRESH_SETTLE_MS)
    except Exception:
        time.sleep(UPDATE_REFRESH_SETTLE_MS / 1000)


def run_update_first(page: "Page", nas_url: str, admin: dict, selectors: dict) -> bool:
    if _wait_for_existing_update_if_visible(page, nas_url, admin, selectors):
        return True

    dismiss_desktop_overlays(
        page,
        selectors,
        max_rounds=8,
        completion_wait_ms=6_000,
        idle_wait_ms=2_000,
        close_notices=False,
    )
    if _run_notice_update_if_visible(page, nas_url, admin, selectors):
        return True

    logger.info("System update: opening update page before provisioning")
    frame = _open_system_update_frame(page, selectors)
    _click_check_update_if_available(frame)
    if _run_notice_update_if_visible(page, nas_url, admin, selectors):
        return True

    body_text = _frame_body_text(frame)
    if LATEST_TEXT in body_text:
        logger.info("System update: already latest")
        return False

    action_text = _click_update_action(frame)
    if not action_text:
        logger.info("System update: update page is not latest; waiting/upgrading before provisioning")
        _wait_for_update_completion(page, nas_url, admin, selectors, frame)
        logger.info("System update flow finished; desktop reached")
        return True

    if action_text in DOWNLOAD_UPDATE_TEXTS:
        logger.info(f"System update: download started via {action_text}; waiting for install action")
        page.wait_for_timeout(1_000)
        _require_update_confirmation_or_transition(page, frame)
        page.wait_for_timeout(MIN_POST_CONFIRM_WAIT_MS)
        _wait_for_update_completion(page, nas_url, admin, selectors, frame)
        logger.info("System update flow finished; desktop reached")
        return True

    logger.info(f"System update: {action_text} clicked; waiting for desktop to return")
    page.wait_for_timeout(1_000)
    _require_update_confirmation_or_transition(page, frame)
    page.wait_for_timeout(MIN_POST_CONFIRM_WAIT_MS)
    _wait_for_update_completion(page, nas_url, admin, selectors, frame)
    logger.info("System update flow finished; desktop reached")
    return True


def run_ready_update_if_prompted(page: "Page", nas_url: str, admin: dict, selectors: dict) -> bool:
    if _wait_for_existing_update_if_visible(page, nas_url, admin, selectors):
        return True

    dismiss_desktop_overlays(
        page,
        selectors,
        max_rounds=8,
        completion_wait_ms=6_000,
        idle_wait_ms=2_000,
        close_notices=False,
    )
    return _run_notice_update_if_visible(page, nas_url, admin, selectors)


def _run_notice_update_if_visible(page: "Page", nas_url: str, admin: dict, selectors: dict) -> bool:
    if not _update_notice_visible(page):
        return False
    logger.info("System update is ready; updating before provisioning")
    if not _click_update_notice(page):
        raise SystemUpdateError("System update notice was visible, but the update link could not be clicked")

    page.wait_for_timeout(1_000)
    _require_update_confirmation_or_transition(page)
    page.wait_for_timeout(MIN_POST_CONFIRM_WAIT_MS)
    _wait_for_update_completion(page, nas_url, admin, selectors)
    logger.info("System update flow finished; desktop reached")
    return True


def _wait_for_existing_update_if_visible(page: "Page", nas_url: str, admin: dict, selectors: dict) -> bool:
    if not _update_installing_visible(page):
        return False
    logger.info("System update: existing update/reboot screen detected; waiting before provisioning")
    _wait_for_update_completion(page, nas_url, admin, selectors)
    logger.info("System update flow finished; desktop reached")
    return True


def _open_system_update_frame(page: "Page", selectors: dict):
    from . import capture as capture_flow

    frame = capture_flow._open_app(page, selectors.get("desktop_launchers", {}), "ctlmgr")
    capture_flow._navigate(
        frame,
        {"nav_path": ["ctlmgr_update_menu"]},
        selectors.get("capture_nav", {}),
    )
    frame.page.wait_for_timeout(1_000)
    return frame


def _click_check_update_if_available(frame) -> bool:
    for text in CHECK_UPDATE_TEXTS:
        selector = f'button:visible:has-text("{text}")'
        try:
            button = frame.locator(selector).first
            button.wait_for(state="visible", timeout=2_000)
            if button.is_enabled(timeout=500):
                logger.info(f"System update: clicking {text}")
                button.click(force=True)
                frame.page.wait_for_timeout(CHECK_UPDATE_SETTLE_MS)
                return True
        except Exception:
            continue
    return False


def _click_update_action(frame, *, include_download: bool = True) -> str | None:
    texts = UPDATE_ACTION_TEXTS if include_download else INSTALL_UPDATE_TEXTS
    selector_templates = (
        'button:visible:has-text("{text}")',
        '.ivu-btn:visible:has-text("{text}")',
        '[role="button"]:visible:has-text("{text}")',
        'a:visible:has-text("{text}")',
    )
    for text in texts:
        for selector_template in selector_templates:
            selector = selector_template.format(text=text)
            try:
                button = frame.locator(selector).first
                button.wait_for(state="visible", timeout=2_000)
                if button.is_enabled(timeout=500):
                    logger.info(f"System update: clicking {text}")
                    button.click(force=True)
                    return text
            except Exception:
                continue
    return None


def _wait_for_update_completion(
    page: "Page",
    nas_url: str,
    admin: dict,
    selectors: dict,
    frame=None,
) -> None:
    deadline = time.monotonic() + UPDATE_WAIT_S
    current_frame = frame
    logged_progress = False
    logged_settle = False
    logged_verify = False
    last_retry_log = 0.0
    last_progress_log = time.monotonic()

    def note_progress(message: str) -> None:
        nonlocal last_progress_log
        logger.info(message)
        last_progress_log = time.monotonic()

    def refresh_if_idle(reason: str) -> bool:
        nonlocal current_frame, last_progress_log
        if time.monotonic() - last_progress_log < UPDATE_IDLE_REFRESH_S:
            return False
        _refresh_update_page(page, nas_url, reason)
        current_frame = None
        last_progress_log = time.monotonic()
        return True

    while time.monotonic() < deadline:
        if _update_installing_visible(page):
            _wait_for_updated_desktop(page, nas_url, admin, selectors)
            deadline = time.monotonic() + UPDATE_WAIT_S
            current_frame = None
            last_progress_log = time.monotonic()
            continue

        try:
            if _login_page_visible(page):
                login_flow.run(page, nas_url, admin, selectors)
                dismiss_desktop_overlays(
                    page,
                    selectors,
                    max_rounds=8,
                    completion_wait_ms=6_000,
                    idle_wait_ms=2_000,
                    close_notices=False,
                )
                current_frame = None

            if current_frame is None:
                if not logged_verify:
                    note_progress("System update: verifying latest status after update/reboot")
                    logged_verify = True
                dismiss_desktop_overlays(
                    page,
                    selectors,
                    max_rounds=4,
                    completion_wait_ms=2_000,
                    idle_wait_ms=1_000,
                    close_notices=False,
                )
                current_frame = _open_system_update_frame(page, selectors)
                _click_check_update_if_available(current_frame)

            body_text = _frame_body_text(current_frame)
            if LATEST_TEXT in body_text:
                note_progress("System update: update page now reports latest version")
                return

            action_text = _click_update_action(current_frame)
            if action_text:
                note_progress(f"System update: continuing update via {action_text}")
                page.wait_for_timeout(1_000)
                _require_update_confirmation_or_transition(page, current_frame)
                page.wait_for_timeout(MIN_POST_CONFIRM_WAIT_MS)
                deadline = time.monotonic() + UPDATE_WAIT_S
                current_frame = None
                logged_progress = False
                logged_settle = False
                continue

            if _frame_has_update_progress(current_frame):
                if not logged_progress:
                    note_progress("System update: download/update is still in progress; waiting")
                    logged_progress = True
                if refresh_if_idle("update progress page idle"):
                    continue
                page.wait_for_timeout(10_000)
                continue

            if _update_notice_visible(page):
                note_progress("System update: update notice is visible; continuing update")
                if not _click_update_notice(page):
                    raise SystemUpdateError("System update notice was visible, but the update link could not be clicked")
                page.wait_for_timeout(1_000)
                _require_update_confirmation_or_transition(page)
                page.wait_for_timeout(MIN_POST_CONFIRM_WAIT_MS)
                deadline = time.monotonic() + UPDATE_WAIT_S
                current_frame = None
                logged_progress = False
                logged_settle = False
                last_progress_log = time.monotonic()
                continue

            if not logged_settle:
                note_progress("System update: waiting for update status to settle")
                logged_settle = True
            if refresh_if_idle("update status idle"):
                continue
            page.wait_for_timeout(5_000)
            current_frame = None
        except SystemUpdateError:
            raise
        except Exception as exc:
            now = time.monotonic()
            if now - last_retry_log >= 30:
                note_progress(f"System update: latest-status verification retrying after UI issue: {exc}")
                last_retry_log = now
            current_frame = None
            try:
                page.goto(nas_url, wait_until="domcontentloaded", timeout=10_000)
            except Exception:
                pass
            page.wait_for_timeout(5_000)

    raise SystemUpdateError(f"System update did not finish within {UPDATE_WAIT_S}s")


def _frame_has_update_progress(frame) -> bool:
    body_text = _frame_body_text(frame)
    return any(marker in body_text for marker in UPDATE_PROGRESS_MARKERS)


def _wait_for_download_then_install(frame, page: "Page", nas_url: str, admin: dict, selectors: dict) -> bool:
    deadline = time.monotonic() + UPDATE_WAIT_S
    while time.monotonic() < deadline:
        if _run_notice_update_if_visible(page, nas_url, admin, selectors):
            return True

        body_text = _frame_body_text(frame)
        if LATEST_TEXT in body_text:
            logger.info("System update: update page now reports latest version")
            return True

        action_text = _click_update_action(frame, include_download=False)
        if action_text:
            logger.info(f"System update: install action {action_text} clicked after download")
            page.wait_for_timeout(1_000)
            _require_update_confirmation_or_transition(page, frame)
            page.wait_for_timeout(MIN_POST_CONFIRM_WAIT_MS)
            _wait_for_updated_desktop(page, nas_url, admin, selectors)
            logger.info("System update flow finished; desktop reached")
            return True

        page.wait_for_timeout(5_000)

    raise SystemUpdateError(f"System update download did not expose an install action within {UPDATE_WAIT_S}s")


def _frame_body_text(frame) -> str:
    try:
        return frame.locator("body").inner_text(timeout=2_000)
    except Exception:
        return ""


def _page_body_text(page: "Page") -> str:
    try:
        return page.locator("body").inner_text(timeout=2_000)
    except Exception:
        return ""


def _update_installing_visible(page: "Page") -> bool:
    text = _page_body_text(page)
    return any(marker in text for marker in UPDATE_INSTALLING_MARKERS)


def _wait_for_update_to_start(page: "Page") -> bool:
    deadline = time.monotonic() + UPDATE_START_WAIT_S
    logged_wait = False
    while time.monotonic() < deadline:
        if _update_installing_visible(page):
            logger.info("System update: installation page is visible; waiting for completion")
            return True
        try:
            page.evaluate("() => 1")
        except Exception:
            logger.info("System update: page disconnected/reloading after confirmation")
            return True
        if not logged_wait:
            logger.info("System update: waiting for installation/reboot to start")
            logged_wait = True
        time.sleep(5)

    logger.warning(
        f"System update did not show installation/reboot within {UPDATE_START_WAIT_S}s; "
        "continuing to wait for a stable desktop"
    )
    return False


def _update_notice_visible(page: "Page") -> bool:
    try:
        return page.locator(UPDATE_NOTICE_SELECTOR).first.is_visible(timeout=1_000)
    except Exception:
        return False


def _click_update_notice(page: "Page") -> bool:
    candidates = [
        UPDATE_NOTICE_LINK_SELECTOR,
        '.ivu-notice:has-text("立即更新") span.notice-link',
        '.ivu-notice:has-text("已经下载并准备好") .notice-link',
        '.ivu-notice:has-text("已经下载并准备好") a',
        '.ivu-notice:has-text("已经下载并准备好") button',
        'text="立即更新"',
    ]
    for selector in candidates:
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=3_000)
            locator.click(force=True)
            return True
        except Exception:
            continue
    return False


def _require_update_confirmation_or_transition(page: "Page", preferred_frame=None) -> None:
    if _confirm_update_if_prompted(page, preferred_frame):
        return
    if _update_prompt_visible(page, preferred_frame):
        raise SystemUpdateError("System update confirmation prompt was visible, but no confirm button was clicked")
    if preferred_frame is not None and _update_action_visible(preferred_frame):
        raise SystemUpdateError("System update action is still visible after clicking; confirmation was not accepted")
    logger.info("No extra system update confirmation prompt was shown")


def _confirm_update_if_prompted(page: "Page", preferred_frame=None) -> bool:
    deadline = time.monotonic() + CONFIRM_PROMPT_WAIT_S
    while time.monotonic() < deadline:
        for target in _confirmation_targets(page, preferred_frame):
            if _click_confirm_in_target(target):
                logger.info(f"Confirmed system update prompt in {_target_name(target)}")
                return True
        page.wait_for_timeout(1_000)
    return False


def _confirmation_targets(page: "Page", preferred_frame=None) -> list:
    targets: list = []
    if preferred_frame is not None:
        targets.append(preferred_frame)
    targets.append(page)
    for frame in page.frames:
        if frame not in targets:
            targets.append(frame)
    return targets


def _click_confirm_in_target(target) -> bool:
    for modal in _visible_update_modals(target):
        _click_modal_checkboxes(modal)
        if _click_modal_confirm_button(modal):
            return True
    return False


def _visible_update_modals(target) -> list:
    modals: list = []
    for selector in CONFIRM_MODAL_SELECTORS:
        try:
            locator = target.locator(selector)
            count = min(locator.count(), 20)
        except Exception:
            continue
        for index in range(count):
            modal = locator.nth(index)
            try:
                if not modal.is_visible(timeout=200):
                    continue
                text = modal.inner_text(timeout=500)
            except Exception:
                text = ""
            if not text or any(keyword in text for keyword in UPDATE_PROMPT_KEYWORDS):
                modals.append(modal)
    return modals


def _click_modal_checkboxes(modal) -> None:
    clicked = False
    try:
        inputs = modal.locator('input[type="checkbox"]')
        for index in range(min(inputs.count(), 10)):
            checkbox = inputs.nth(index)
            try:
                if checkbox.is_checked(timeout=200):
                    continue
            except Exception:
                pass
            try:
                checkbox.click(force=True, timeout=1_000)
                clicked = True
            except Exception:
                continue
    except Exception:
        pass

    if clicked:
        time.sleep(0.5)
        return

    for text in AGREEMENT_CHECKBOX_TEXTS:
        for selector in (
            f'label:visible:has-text("{text}")',
            f'.ivu-checkbox-wrapper:visible:has-text("{text}")',
            f'.arco-checkbox:visible:has-text("{text}")',
        ):
            try:
                checkbox = modal.locator(selector).first
                checkbox.wait_for(state="visible", timeout=500)
                checkbox.click(force=True)
                time.sleep(0.5)
                return
            except Exception:
                continue


def _click_modal_confirm_button(modal) -> bool:
    text_templates = (
        '.ivu-btn-primary:visible:has-text("{text}")',
        '.arco-btn-primary:visible:has-text("{text}")',
        'button:visible:has-text("{text}")',
        '[role="button"]:visible:has-text("{text}")',
    )
    for text in CONFIRM_UPDATE_TEXTS:
        for template in text_templates:
            if _click_enabled_locator(modal.locator(template.format(text=text)).first):
                return True

    for selector in (
        '.ivu-btn-primary:visible',
        '.arco-btn-primary:visible',
        'button[type="submit"]:visible',
    ):
        locator = modal.locator(selector)
        try:
            count = min(locator.count(), 10)
        except Exception:
            continue
        for index in range(count):
            if _click_enabled_locator(locator.nth(index)):
                return True
    return False


def _click_enabled_locator(locator) -> bool:
    try:
        locator.wait_for(state="visible", timeout=500)
        if not locator.is_enabled(timeout=500):
            return False
        locator.click(force=True, timeout=2_000)
        return True
    except Exception:
        return False


def _update_prompt_visible(page: "Page", preferred_frame=None) -> bool:
    for target in _confirmation_targets(page, preferred_frame):
        if _visible_update_modals(target):
            return True
    return False


def _update_action_visible(frame) -> bool:
    for text in UPDATE_ACTION_TEXTS:
        for selector in (
            f'button:visible:has-text("{text}")',
            f'.ivu-btn:visible:has-text("{text}")',
            f'[role="button"]:visible:has-text("{text}")',
            f'a:visible:has-text("{text}")',
        ):
            try:
                button = frame.locator(selector).first
                button.wait_for(state="visible", timeout=500)
                if button.is_enabled(timeout=200):
                    return True
            except Exception:
                continue
    return False


def _target_name(target) -> str:
    try:
        name = getattr(target, "name", None)
        if name:
            return f"iframe '{name}'"
    except Exception:
        pass
    return "top page"


def _wait_for_updated_desktop(page: "Page", nas_url: str, admin: dict, selectors: dict) -> None:
    update_started = _wait_for_update_to_start(page)
    saw_installing_page = update_started and _update_installing_visible(page)
    stable_desktop_seen_at: float | None = None
    setup_wizard_seen_at: float | None = None
    deadline = time.monotonic() + UPDATE_WAIT_S
    last_retry_log = 0.0
    last_progress_log = time.monotonic()
    next_install_http_probe_at = time.monotonic() + UPDATE_INSTALL_HTTP_PROBE_S
    install_ready_probe_count = 0
    next_desktop_nav_at = 0.0

    def note_progress(message: str) -> None:
        nonlocal last_progress_log
        logger.info(message)
        last_progress_log = time.monotonic()

    def refresh_if_idle(reason: str) -> bool:
        nonlocal stable_desktop_seen_at, last_progress_log, install_ready_probe_count
        if time.monotonic() - last_progress_log < UPDATE_IDLE_REFRESH_S:
            return False
        stable_desktop_seen_at = None
        install_ready_probe_count = 0
        _refresh_update_page(page, nas_url, reason)
        last_progress_log = time.monotonic()
        return True

    while time.monotonic() < deadline:
        try:
            if _update_installing_visible(page):
                now = time.monotonic()
                if not saw_installing_page:
                    note_progress("System update: installation page is visible; waiting for completion")
                    saw_installing_page = True
                stable_desktop_seen_at = None
                if now >= next_install_http_probe_at:
                    if _nas_home_looks_ready(page, nas_url):
                        install_ready_probe_count += 1
                        if install_ready_probe_count >= UPDATE_READY_HTTP_PROBE_SAMPLES:
                            note_progress("System update: NAS web service responded; refreshing once to verify desktop")
                            if _goto_nas_home(page, nas_url):
                                next_desktop_nav_at = time.monotonic() + UPDATE_DESKTOP_NAV_INTERVAL_S
                                install_ready_probe_count = 0
                                continue
                    else:
                        install_ready_probe_count = 0
                    next_install_http_probe_at = now + UPDATE_INSTALL_HTTP_PROBE_S
                if refresh_if_idle("installation page idle"):
                    continue
                time.sleep(5)
                continue

            now = time.monotonic()
            if now >= next_desktop_nav_at:
                _goto_nas_home(page, nas_url)
                next_desktop_nav_at = time.monotonic() + UPDATE_DESKTOP_NAV_INTERVAL_S
                continue

            if _update_installing_visible(page):
                if not saw_installing_page:
                    note_progress("System update: installation page is visible; waiting for completion")
                    saw_installing_page = True
                stable_desktop_seen_at = None
                if refresh_if_idle("installation page idle"):
                    continue
                time.sleep(10)
                continue

            if _setup_wizard_visible(page):
                # An initialized unit returns to login/desktop after an update; the first-run
                # setup wizard means the update re-initialized/reset the unit, so the desktop
                # will never come back. Confirm it persists (across the periodic reloads) before
                # failing, so a boot transient doesn't false-fail a good unit — then fail fast
                # instead of spinning until UPDATE_WAIT_S.
                if setup_wizard_seen_at is None:
                    setup_wizard_seen_at = time.monotonic()
                    note_progress("System update: setup wizard seen post-update; confirming it persists")
                elif time.monotonic() - setup_wizard_seen_at >= SETUP_WIZARD_CONFIRM_S:
                    raise SystemUpdateError(
                        "Device returned to the setup wizard after the update "
                        "(likely re-initialized/reset); re-run setup for this unit"
                    )
                time.sleep(5)
                continue
            setup_wizard_seen_at = None

            if _login_page_visible(page):
                login_flow.run(page, nas_url, admin, selectors)
                dismiss_desktop_overlays(
                    page,
                    selectors,
                    max_rounds=8,
                    completion_wait_ms=6_000,
                    idle_wait_ms=2_000,
                    close_notices=False,
                )
            if _desktop_visible(page, selectors):
                if _update_notice_visible(page):
                    raise SystemUpdateError("System update notice is still visible after update confirmation")
                if stable_desktop_seen_at is None:
                    stable_desktop_seen_at = time.monotonic()
                    note_progress("System update: desktop returned; verifying it stays stable")
                    time.sleep(10)
                    continue
                if time.monotonic() - stable_desktop_seen_at >= 20:
                    return
        except SystemUpdateError:
            raise
        except Exception as exc:
            update_started = True
            stable_desktop_seen_at = None
            setup_wizard_seen_at = None
            now = time.monotonic()
            if now - last_retry_log >= 30:
                note_progress(f"System update: desktop wait retrying after UI issue: {exc}")
                last_retry_log = now
            refresh_if_idle("desktop wait idle")
        time.sleep(5)

    raise SystemUpdateError(f"Desktop did not return within {UPDATE_WAIT_S}s after system update")


def _goto_nas_home(page: "Page", nas_url: str) -> bool:
    try:
        page.goto(nas_url, wait_until="domcontentloaded", timeout=10_000)
        page.wait_for_timeout(2_000)
        return True
    except Exception:
        return False


def _nas_home_looks_ready(page: "Page", nas_url: str) -> bool:
    try:
        response = page.request.get(nas_url, timeout=UPDATE_HTTP_PROBE_TIMEOUT_MS)
    except Exception:
        return False
    try:
        status = int(getattr(response, "status", 0) or 0)
        if status < 200 or status >= 400:
            return False
    except Exception:
        pass
    try:
        text = response.text()
    except Exception:
        text = ""
    return not any(marker in text for marker in UPDATE_INSTALLING_MARKERS)


def _login_page_visible(page: "Page") -> bool:
    try:
        return page.locator(LOGIN_PASSWORD_SELECTOR).first.is_visible(timeout=1_000)
    except Exception:
        return False


def _setup_wizard_visible(page: "Page") -> bool:
    # The welcome page's start button ("开始" / 欢迎使用绿联云存储) is unique to an uninitialized
    # unit, so a top-level match unambiguously means the device fell back to first-run setup.
    try:
        return page.locator(SETUP_WIZARD_START_SELECTOR).first.is_visible(timeout=1_000)
    except Exception:
        return False


def _desktop_visible(page: "Page", selectors: dict) -> bool:
    for selector in (selectors.get("desktop_launchers", {}).get("apps", {}) or {}).values():
        if not selector or selector == "TODO":
            continue
        if find_visible_locator(page, selector, 500) is not None:
            return True
    return False
