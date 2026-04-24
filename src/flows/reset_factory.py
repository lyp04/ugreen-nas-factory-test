from __future__ import annotations

import time
from typing import TYPE_CHECKING

from playwright.sync_api import expect

from ..utils.desktop import click_desktop_launcher, dismiss_desktop_overlays, find_visible_locator
from ..utils.logger import logger

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page


FRAME_WAIT_MS = 15_000
SHORT_UI_WAIT_MS = 800


class FactoryResetError(RuntimeError):
    pass


def run(page: "Page", selectors: dict, admin: dict) -> None:
    dismiss_desktop_overlays(page, selectors, max_rounds=2, completion_wait_ms=0)
    desktop = selectors.get("desktop_launchers", {})
    nav = selectors.get("capture_nav", {})
    reset = selectors.get("factory_reset", {})

    logger.info("Post action: restore factory settings")
    frame = _open_app(page, desktop, "ctlmgr")

    update_menu = _require_selector(nav.get("ctlmgr_update_menu"), "capture_nav.ctlmgr_update_menu")
    restore_tab = _require_selector(reset.get("restore_tab"), "factory_reset.restore_tab")
    restore_button = _require_selector(reset.get("restore_button"), "factory_reset.restore_button")
    first_modal = _require_selector(reset.get("first_modal_root"), "factory_reset.first_modal_root")
    acknowledge_checkbox = _require_selector(
        reset.get("first_modal_acknowledge_checkbox"),
        "factory_reset.first_modal_acknowledge_checkbox",
    )
    first_confirm = _require_selector(
        reset.get("first_modal_confirm_button"),
        "factory_reset.first_modal_confirm_button",
    )
    password_modal = _require_selector(reset.get("password_modal_root"), "factory_reset.password_modal_root")
    password_input = _require_selector(reset.get("password_input"), "factory_reset.password_input")
    password_submit = _require_selector(reset.get("password_submit_button"), "factory_reset.password_submit_button")

    frame.locator(update_menu).first.click()
    page.wait_for_timeout(SHORT_UI_WAIT_MS)

    tab = frame.locator(restore_tab).first
    expect(tab).to_be_visible(timeout=FRAME_WAIT_MS)
    tab.click()
    page.wait_for_timeout(SHORT_UI_WAIT_MS)

    restore = frame.locator(restore_button).first
    expect(restore).to_be_visible(timeout=FRAME_WAIT_MS)
    restore.click()

    first_modal_loc = frame.locator(first_modal).first
    expect(first_modal_loc).to_be_visible(timeout=FRAME_WAIT_MS)

    acknowledge = frame.locator(acknowledge_checkbox).first
    expect(acknowledge).to_be_attached(timeout=FRAME_WAIT_MS)
    if not acknowledge.is_checked():
        acknowledge.check(force=True)

    first_confirm_button = frame.locator(first_confirm).first
    expect(first_confirm_button).to_be_enabled(timeout=FRAME_WAIT_MS)
    first_confirm_button.click()

    password_modal_loc = frame.locator(password_modal).first
    expect(password_modal_loc).to_be_visible(timeout=FRAME_WAIT_MS)

    password_box = frame.locator(password_input).first
    expect(password_box).to_be_visible(timeout=FRAME_WAIT_MS)
    password_box.fill(admin["password"])

    submit_button = frame.locator(password_submit).first
    expect(submit_button).to_be_enabled(timeout=FRAME_WAIT_MS)
    submit_button.click()

    page.wait_for_timeout(2_000)
    logger.info("Factory reset initiated")


def _open_app(page: "Page", desktop_selectors: dict, app: str) -> "Frame":
    app_selector = _require_selector(desktop_selectors.get("apps", {}).get(app), f"desktop_launchers.apps.{app}")
    frame_selector = _require_selector(desktop_selectors.get("frames", {}).get(app), f"desktop_launchers.frames.{app}")

    iframe = page.locator(frame_selector).first
    if iframe.count() and iframe.is_visible():
        _activate_window(page, frame_selector)
    else:
        launcher = find_visible_locator(page, app_selector, FRAME_WAIT_MS)
        if launcher is None:
            raise FactoryResetError(f"Desktop launcher for '{app}' did not appear")
        click_desktop_launcher(page, launcher, app)
        expect(iframe).to_be_visible(timeout=FRAME_WAIT_MS)

    page.wait_for_timeout(SHORT_UI_WAIT_MS)
    frame = _wait_for_frame(page, iframe, app)
    frame.wait_for_load_state("domcontentloaded")
    return frame


def _wait_for_frame(page: "Page", iframe_locator, app: str) -> "Frame":
    deadline = time.monotonic() + (FRAME_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        handle = iframe_locator.element_handle()
        frame = handle.content_frame() if handle is not None else None
        if frame is not None:
            return frame
        page.wait_for_timeout(200)
    raise FactoryResetError(f"iframe '{app}' did not appear on the desktop")


def _activate_window(page: "Page", frame_selector: str) -> None:
    window = page.locator(f".cloud-window-main:has({frame_selector})").first
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


def _require_selector(selector: str | None, name: str) -> str:
    if not selector or selector == "TODO":
        raise FactoryResetError(f"Selector '{name}' is not configured")
    return selector
