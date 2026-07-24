"""UGOS Pro first-time setup wizard driver."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from playwright.sync_api import TimeoutError as PWTimeoutError
from playwright.sync_api import expect

from . import login as login_flow
from ..utils.desktop import dismiss_desktop_overlays, find_visible_locator
from ..utils.logger import logger
from ..utils.sn import extract_full_sn, is_auto_sn_placeholder, sn_tail

if TYPE_CHECKING:
    from playwright.sync_api import Page


class SetupWizardError(RuntimeError):
    pass


class SetupAlreadyRegistered(SetupWizardError):
    pass


SN_UNBOUND_MESSAGE = "SN\u672a\u89e3\u7ed1\uff0c\u8bf7\u5148\u89e3\u7ed1SN"
INIT_MAX_WAIT_MS = 900_000  # first-time init + reboot runs long; 5 min was too short
OVERLAY_WAIT_MS = 30_000
SHORT_WAIT_MS = 3_000
INITIAL_STATE_WAIT_MS = 300_000
INITIAL_STATE_REFRESH_MS = 10_000
INTRO_START_BUTTONS = ("button.start-btn", 'button:has-text("开始")')
COMBINED_DEVICE_INPUT = 'input[placeholder*="设备名"]'
COMBINED_ADMIN_INPUT = 'input[placeholder*="管理员账号"]'
COMBINED_NEXT_BUTTONS = ('button:has-text("下一步")', "button.ivu-btn-primary")
LOADING_MARKERS = (
    "正在加载UGOS Pro系统文件",
    "正在配置UGOS Pro",
    "加载完成后设备将会重启",
    "完成后设备将会重启",
)
STARTING_MARKERS = (
    "服务启动中",
    "您可以尝试手动刷新页面",
    "设备启动中",
    "系统准备中",
)
MISSING_DEVICE_INFO_MARKERS = (
    "设备必要信息缺失",
    "型号或序列号缺失",
    "请与厂商售后客服取得联系",
    "无法激活",
)
SETUP_MARKERS = (
    "未初始化",
    "欢迎使用绿联云存储",
    "命名您的设备",
    "命名您的绿联云",
    "注册并绑定绿联云账号",
    "正在加载UGOS Pro系统文件",
    "正在配置UGOS Pro",
)


def _require(selector: str, name: str) -> str:
    if not selector or selector == "TODO":
        raise SetupWizardError(f"Selector '{name}' is not configured in selectors.yml")
    return selector


def _click_visible(page: "Page", selector: str, name: str, timeout_ms: int = OVERLAY_WAIT_MS) -> None:
    loc = page.locator(_require(selector, name))
    expect(loc).to_be_visible(timeout=timeout_ms)
    loc.click()


def _click_first_visible_selector(
    page: "Page",
    selectors: tuple[str | None, ...],
    name: str,
    timeout_ms: int = OVERLAY_WAIT_MS,
) -> None:
    last_error: Exception | None = None
    for selector in selectors:
        if not selector or selector == "TODO":
            continue
        try:
            loc = page.locator(selector).first
            loc.wait_for(state="visible", timeout=timeout_ms)
            loc.click()
            return
        except Exception as exc:
            last_error = exc
            continue
    raise SetupWizardError(f"Could not click {name}") from last_error


def _try_click_if_visible(page: "Page", selector: str | None, name: str, timeout_ms: int = SHORT_WAIT_MS) -> bool:
    if not selector or selector == "TODO":
        return False
    try:
        loc = page.locator(selector).first
        loc.wait_for(state="visible", timeout=timeout_ms)
        loc.click()
        logger.info(f"Dismissed overlay: {name}")
        return True
    except PWTimeoutError:
        return False


def _wizard_surface(page: "Page"):
    """UGOS 1.17 renders the first-time wizard inside a child iframe
    (name="device-wizard"); older firmware renders it on the top page. Return
    whichever surface holds the wizard so one flow drives both — element-level
    calls go through this; page-level nav / loading / desktop stay on ``page``.
    Falls back to ``page`` when the iframe isn't present (old firmware, or not
    created yet), so old-firmware behaviour is byte-for-byte unchanged."""
    try:
        frame = page.frame(name="device-wizard")
        if frame is not None:
            return frame
    except Exception:
        pass
    return page


def run(page: "Page", nas_url: str, admin: dict, selectors: dict, sn: str = "") -> str | None:
    logger.info(f"Setup wizard -> {nas_url}")
    page.goto(nas_url, wait_until="domcontentloaded")
    discovered_sn = _extract_full_sn_from_page(page, sn)

    sw = selectors.get("setup_wizard", {})
    login_selectors = selectors.get("login", {})
    post = selectors.get("post_setup", {})

    state = _detect_initial_state(page, sw, login_selectors, nas_url)
    if state == "login":
        raise SetupAlreadyRegistered("Device is already initialized")
    if state == "loading":
        _wait_for_desktop(page, nas_url, admin, selectors)
        _dismiss_post_setup_overlays_best_effort(page, post)
        logger.info("Setup wizard complete; desktop reached")
        return discovered_sn or _extract_full_sn_from_page(page, sn)

    _page0_intro_start_if_present(page, sw)
    discovered_sn = discovered_sn or _extract_full_sn_from_page(page, sn)
    if _is_combined_device_admin_page(page):
        _combined_device_admin_page(page, admin, sn)
    else:
        _page1_device_name(page, sw)
        _page2_admin_account(page, sw, admin)
    _page3_skip_phone(page, sw)
    _page4_update_mode_and_init(page, sw)
    _wait_for_desktop(page, nas_url, admin, selectors)
    _dismiss_post_setup_overlays_best_effort(page, post)
    logger.info("Setup wizard complete; desktop reached")
    return discovered_sn or _extract_full_sn_from_page(page, sn)


def _detect_initial_state(page: "Page", sw: dict, login_selectors: dict, nas_url: str) -> str:
    wizard_marker = _require(sw.get("page1_next_button"), "setup_wizard.page1_next_button")
    login_marker = _require(login_selectors.get("password_input"), "login.password_input")
    deadline = time.monotonic() + (INITIAL_STATE_WAIT_MS / 1000)
    next_refresh = time.monotonic() + (INITIAL_STATE_REFRESH_MS / 1000)
    wait_reason: str | None = None
    logged_wait_reasons: set[str] = set()

    while time.monotonic() < deadline:
        if _is_loading_page(page):
            return "loading"
        surface = _wizard_surface(page)
        if _is_visible(surface, wizard_marker):
            return "wizard"
        if _is_visible(surface, COMBINED_DEVICE_INPUT) or _is_visible(surface, COMBINED_ADMIN_INPUT):
            return "wizard"
        if any(_is_visible(surface, selector) for selector in INTRO_START_BUTTONS):
            return "wizard"
        if _is_visible(page, login_marker):
            logger.info("Setup mode detected a registered device; login page is already shown")
            return "login"

        current_wait_reason = _initial_wait_reason(page)
        if current_wait_reason:
            if current_wait_reason == "missing_device_info":
                logger.error(SN_UNBOUND_MESSAGE)
                raise SetupWizardError(SN_UNBOUND_MESSAGE)
            wait_reason = current_wait_reason
            if current_wait_reason not in logged_wait_reasons:
                logger.info("Setup page reports UGOS service is still starting; waiting and refreshing")
                logged_wait_reasons.add(current_wait_reason)
            if time.monotonic() >= next_refresh:
                _refresh_initial_wait_page(page, nas_url)
                next_refresh = time.monotonic() + (INITIAL_STATE_REFRESH_MS / 1000)
            page.wait_for_timeout(1_000)
            continue

        page.wait_for_timeout(250)

    if wait_reason == "service_starting":
        raise SetupWizardError(
            "UGOS setup page stayed on 'service starting' screen "
            f"for {INITIAL_STATE_WAIT_MS // 1000}s; last page: {_text_preview(_body_text(page))}"
        )
    if wait_reason == "missing_device_info":
        raise SetupWizardError(
            "UGOS setup page still reports missing device information "
            f"after {INITIAL_STATE_WAIT_MS // 1000}s; last page: {_text_preview(_body_text(page))}"
        )

    raise SetupWizardError(
        f"Could not determine whether setup wizard or login page was shown within {INITIAL_STATE_WAIT_MS // 1000}s"
    )


def _is_visible(page: "Page", selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible(timeout=500)
    except Exception:
        return False


def _page0_intro_start_if_present(page: "Page", sw: dict) -> None:
    surface = _wizard_surface(page)
    start_btn = _first_visible_locator(surface, INTRO_START_BUTTONS)
    if start_btn is None:
        return

    logger.info("[Page 0] welcome + agreements")
    _check_agreements(page, sw)
    expect(start_btn).to_be_enabled(timeout=OVERLAY_WAIT_MS)
    start_btn.click()

    deadline = time.monotonic() + (OVERLAY_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        if _is_combined_device_admin_page(page) or _is_visible(_wizard_surface(page), sw.get("device_name_input", "")) or _is_loading_page(page):
            return
        page.wait_for_timeout(250)
    raise SetupWizardError("Welcome page did not advance after clicking Start")


def _check_agreements(page: "Page", sw: dict) -> None:
    surface = _wizard_surface(page)
    # Both iView (input.ivu-checkbox-input) and Arco (input.arco-checkbox-target)
    # render a real <input type=checkbox>; Arco hides it off-screen and toggles it
    # from the visible label, so is_visible() is False and check(force=True) can't
    # act on it. A JS click on the input fires the change for both frameworks —
    # same trick as the SMB checkbox — so we don't gate on visibility.
    boxes = surface.locator('input[type="checkbox"]')
    checked = 0
    for i in range(boxes.count()):
        cb = boxes.nth(i)
        try:
            if cb.is_checked():
                continue
            cb.evaluate("el => { if (!el.checked) el.click(); }")
            checked += 1
        except Exception:
            continue
    if checked:
        logger.info(f"  checked {checked} agreement checkbox(es)")


def _is_combined_device_admin_page(page: "Page") -> bool:
    surface = _wizard_surface(page)
    return _is_visible(surface, COMBINED_DEVICE_INPUT) and _is_visible(surface, COMBINED_ADMIN_INPUT)


def _combined_device_admin_page(page: "Page", admin: dict, sn: str) -> None:
    surface = _wizard_surface(page)
    logger.info("[Page 1] device name + admin account")
    surface.locator(COMBINED_DEVICE_INPUT).first.fill(_device_name(sn))
    surface.locator(COMBINED_ADMIN_INPUT).first.fill(admin["username"])

    pw_inputs = surface.locator('input[type="password"]')
    expect(pw_inputs.first).to_be_visible(timeout=OVERLAY_WAIT_MS)
    if pw_inputs.count() < 2:
        raise SetupWizardError(f"Expected 2 password inputs on combined account page, got {pw_inputs.count()}")
    pw_inputs.nth(0).fill(admin["password"])
    pw_inputs.nth(1).fill(admin["password"])

    next_btn = _first_visible_locator(surface, COMBINED_NEXT_BUTTONS)
    if next_btn is None:
        raise SetupWizardError("Combined account page next button was not found")
    expect(next_btn).to_be_enabled(timeout=OVERLAY_WAIT_MS)
    next_btn.click()
    page.wait_for_timeout(SHORT_WAIT_MS)


def _device_name(sn: str) -> str:
    tail = sn_tail(sn)
    return f"UGREEN-{tail}" if tail else "UGREEN-NAS"


def _first_visible_locator(page: "Page", selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=500):
                return loc
        except Exception:
            continue
    return None


def _page1_device_name(page: "Page", sw: dict) -> None:
    surface = _wizard_surface(page)
    logger.info("[Page 1] device name + agreements")
    device_input = surface.locator(_require(sw.get("device_name_input"), "setup_wizard.device_name_input"))
    expect(device_input).to_be_visible(timeout=OVERLAY_WAIT_MS)

    _check_agreements(page, sw)

    next_btn = surface.locator(_require(sw.get("page1_next_button"), "setup_wizard.page1_next_button"))
    expect(next_btn).to_be_enabled(timeout=OVERLAY_WAIT_MS)
    next_btn.click()


def _page2_admin_account(page: "Page", sw: dict, admin: dict) -> None:
    surface = _wizard_surface(page)
    logger.info("[Page 2] admin account")
    user_input = surface.locator(_require(sw.get("admin_username_input"), "setup_wizard.admin_username_input")).first
    # Wait for page 2 to actually render before filling. Filling during the
    # page-1 -> page-2 transition lands on the wrong input (Arco drops it) and the
    # account-name box stays empty, leaving Next disabled. Fill, verify, retry.
    expect(user_input).to_be_visible(timeout=OVERLAY_WAIT_MS)
    username = str(admin["username"])
    for _ in range(4):
        user_input.fill(username)
        try:
            if (user_input.input_value() or "").strip() == username:
                break
        except Exception:
            pass
        page.wait_for_timeout(400)

    pw_sel = _require(sw.get("admin_password_inputs"), "setup_wizard.admin_password_inputs")
    pw_inputs = surface.locator(pw_sel)
    expect(pw_inputs.first).to_be_visible(timeout=OVERLAY_WAIT_MS)
    if pw_inputs.count() < 2:
        raise SetupWizardError(f"Expected 2 password inputs, got {pw_inputs.count()} via '{pw_sel}'")
    pw_inputs.nth(0).fill(admin["password"])
    pw_inputs.nth(1).fill(admin["password"])

    next_btn = surface.locator(_require(sw.get("page2_next_button"), "setup_wizard.page2_next_button"))
    expect(next_btn).to_be_enabled(timeout=OVERLAY_WAIT_MS)
    next_btn.click()


def _page3_skip_phone(page: "Page", sw: dict) -> None:
    surface = _wizard_surface(page)
    logger.info("[Page 3] skip phone binding")
    if _is_loading_page(page):
        logger.info("  system loading already started; phone binding step skipped")
        return

    def _click_skip_confirm() -> None:
        # The confirm dialog may live in the wizard iframe (Arco) or be portalled
        # to the top page — try both surfaces.
        sel = sw.get("skip_phone_confirm_button")
        if not _try_click_if_visible(surface, sel, "setup_wizard.skip_phone_confirm_button"):
            _try_click_if_visible(page, sel, "setup_wizard.skip_phone_confirm_button")

    if _try_click_if_visible(surface, sw.get("skip_phone_link"), "setup_wizard.skip_phone_link"):
        _click_skip_confirm()
        return

    skip_btn = _first_visible_locator(surface, ('button:has-text("跳过")', ".jump-btn"))
    if skip_btn is None:
        logger.info("  phone binding step not shown")
        return
    skip_btn.click()
    page.wait_for_timeout(SHORT_WAIT_MS)
    _click_skip_confirm()


def _page4_update_mode_and_init(page: "Page", sw: dict) -> None:
    surface = _wizard_surface(page)
    logger.info("[Page 4] update mode + start init")
    if _is_loading_page(page):
        logger.info("  system loading already started")
        return
    radio_sel = _require(sw.get("update_mode_radios"), "setup_wizard.update_mode_radios")
    init_selectors = tuple(
        s for s in (
            sw.get("start_system_init_button"),
            'button.complete-btn',
            'button.ivu-btn-primary:has-text("\u5f00\u59cb\u7cfb\u7edf\u521d\u59cb\u5316")',
            'button.ivu-btn-primary:has-text("\u5f00\u59cb\u521d\u59cb\u5316")',
            'button.arco-btn-primary:has-text("\u5f00\u59cb\u7cfb\u7edf\u521d\u59cb\u5316")',
            'button.arco-btn-primary:has-text("\u5f00\u59cb\u521d\u59cb\u5316")',
            'button:has-text("\u5f00\u59cb\u7cfb\u7edf\u521d\u59cb\u5316")',
        )
        if s
    )
    # Clicking through page 3 lands us here a frame early, so poll for page 4 to
    # render (its update-mode radios or the init button) before acting \u2014 do NOT
    # early-return on a not-yet-rendered radio count, or we skip init entirely.
    deadline = time.monotonic() + (OVERLAY_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        if _is_loading_page(page):
            logger.info("  system loading already started")
            return
        if surface.locator(radio_sel).count() > 0 or _first_visible_locator(surface, init_selectors) is not None:
            break
        page.wait_for_timeout(300)

    # A recommended update mode is pre-selected and the init button is enabled
    # without touching the radios; best-effort switch to "install all updates"
    # like the old flow, but never block on it.
    radios = surface.locator(radio_sel)
    if radios.count() > 1:
        try:
            surface.locator('label.arco-radio, label.ivu-radio-wrapper').nth(1).click(timeout=2_000)
            logger.info("  selected auto-update option")
        except Exception:
            logger.info("  update mode left at recommended default")

    _click_first_visible_selector(surface, init_selectors, "setup_wizard.start_system_init_button")
    logger.info("  system init started; waiting for reboot + desktop...")


def _wait_for_desktop(page: "Page", nas_url: str, admin: dict, selectors: dict) -> None:
    logger.info(f"  waiting up to {INIT_MAX_WAIT_MS // 1000}s for desktop")
    deadline = time.monotonic() + (INIT_MAX_WAIT_MS / 1000)
    next_reload = time.monotonic() + 15
    login_password_selector = (selectors.get("login", {}) or {}).get("password_input")

    while time.monotonic() < deadline:
        if _is_desktop_visible(page, selectors):
            logger.info("  desktop ready")
            return
        if login_password_selector and _is_visible(page, login_password_selector):
            # UGOS 1.17 invalidates the setup-wizard browser session after the
            # initialization reboot and returns to the login page. Waiting only
            # for a desktop launcher would otherwise stall for the full 15 min.
            logger.info("  login page shown after initialization; logging in with the new admin account")
            login_flow.run(page, nas_url, admin, selectors)
            logger.info("  desktop ready after post-initialization login")
            return
        if time.monotonic() >= next_reload and not _is_loading_page(page):
            try:
                page.goto(nas_url, wait_until="domcontentloaded", timeout=5_000)
            except Exception:
                pass
            next_reload = time.monotonic() + 15
        page.wait_for_timeout(2_000)

    raise SetupWizardError(
        f"Desktop not reached within {INIT_MAX_WAIT_MS // 1000}s - system init may have stalled"
    )


def _is_desktop_visible(page: "Page", selectors: dict) -> bool:
    for selector in (selectors.get("desktop_launchers", {}).get("apps", {}) or {}).values():
        if selector and selector != "TODO" and find_visible_locator(page, selector, 500) is not None:
            return True
    return False


def _is_loading_page(page: "Page") -> bool:
    text = _body_text(page)
    return any(marker in text for marker in LOADING_MARKERS)


def _initial_wait_reason(page: "Page") -> str | None:
    text = _body_text(page)
    if any(marker in text for marker in STARTING_MARKERS):
        return "service_starting"
    if any(marker in text for marker in MISSING_DEVICE_INFO_MARKERS):
        return "missing_device_info"
    return None


def _refresh_initial_wait_page(page: "Page", nas_url: str) -> None:
    for selector in ('button:has-text("刷新")', 'button:has-text("Refresh")', ".connect-find-page button"):
        try:
            button = page.locator(selector).first
            if button.is_visible(timeout=500):
                button.click(timeout=2_000)
                return
        except Exception:
            continue
    try:
        page.reload(wait_until="domcontentloaded", timeout=5_000)
    except Exception:
        try:
            page.goto(nas_url, wait_until="domcontentloaded", timeout=5_000)
        except Exception:
            pass


def _is_setup_or_loading_page(page: "Page") -> bool:
    text = _body_text(page)
    return any(marker in text for marker in SETUP_MARKERS)


def _body_text(page: "Page") -> str:
    try:
        return page.locator("body").inner_text(timeout=1_000)
    except Exception:
        return ""


def _text_preview(text: str, limit: int = 160) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit] if compact else "<empty>"


def _extract_full_sn_from_page(page: "Page", sn: str) -> str | None:
    expected_sn = "" if is_auto_sn_placeholder(sn) else sn
    if not expected_sn:
        return None
    parts: list[str] = []
    try:
        parts.append(page.title())
    except Exception:
        pass
    parts.append(_body_text(page))
    # New-firmware wizard renders in a child iframe; include its text so the full
    # SN (shown as 序列号: ...) is recoverable from a scanned tail.
    surface = _wizard_surface(page)
    if surface is not page:
        parts.append(_body_text(surface))
    # Browser storage can retain stale SN values from another device; only use it
    # when a scanned tail is available to validate the candidate.
    if expected_sn:
        try:
            parts.append(page.content())
        except Exception:
            pass
        try:
            parts.append(
                page.evaluate(
                    "() => JSON.stringify({localStorage: {...localStorage}, sessionStorage: {...sessionStorage}})"
                )
            )
        except Exception:
            pass
    return extract_full_sn("\n".join(parts), expected_sn)


def _dismiss_post_setup_overlays(page: "Page", post: dict) -> None:
    logger.info("Dismissing post-setup overlays (best-effort)")
    dismiss_desktop_overlays(
        page,
        {"post_setup": post},
        max_rounds=10,
        completion_wait_ms=6_000,
        idle_wait_ms=8_000,
    )


def _dismiss_post_setup_overlays_best_effort(page: "Page", post: dict) -> None:
    try:
        _dismiss_post_setup_overlays(page, post)
    except Exception as exc:
        logger.warning(f"Post-setup overlay cleanup was interrupted; continuing after setup: {exc}")
