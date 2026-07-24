from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Callable

from playwright.sync_api import expect

from ..utils.app_guides import dismiss_arco_app_guides
from ..utils.desktop import click_desktop_launcher, dismiss_desktop_overlays, find_visible_locator
from ..utils.logger import logger

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page


FRAME_WAIT_MS = 15_000
POOL_DELETE_WAIT_MS = 180_000
POOL_DELETE_MAX_OBSERVATION_ERRORS = 3
POOL_DELETE_STABLE_ABSENCE_SAMPLES = 3
SHORT_UI_WAIT_MS = 800


class CleanupError(RuntimeError):
    pass


def run(
    page: "Page",
    selectors: dict,
    admin: dict,
    *,
    cancel_check_cb: Callable[[], None] | None = None,
    on_pool_deleted: Callable[[str], None] | None = None,
) -> list[str]:
    _check_cancel(cancel_check_cb)
    dismiss_desktop_overlays(page, selectors, max_rounds=2, completion_wait_ms=0)
    desktop = selectors.get("desktop_launchers", {})
    nav = selectors.get("provision_nav", {})
    cleanup = selectors.get("cleanup", {})

    logger.info("Cleanup: delete storage pools before next reflash")
    frame = _open_app(page, desktop, "storagemgr")
    _navigate(frame, nav, ["storagemgr_storage_menu", "storagemgr_pool_tab"])

    deleted: list[str] = []
    for pool_id in reversed(_list_pool_ids(frame, cleanup)):
        _check_cancel(cancel_check_cb)
        _delete_pool(
            page,
            frame,
            cleanup,
            admin,
            pool_id,
            cancel_check_cb=cancel_check_cb,
        )
        deleted.append(pool_id)
        if on_pool_deleted is not None:
            on_pool_deleted(pool_id)
        # Once the final confirmation is submitted, _delete_pool deliberately
        # finishes its absence verification without interruption.  Honour a
        # cancellation only after the durable per-pool checkpoint above.
        _check_cancel(cancel_check_cb)

    if deleted:
        logger.info(f"Cleanup complete, deleted pools: {', '.join(deleted)}")
    else:
        logger.info("Cleanup complete, no storage pools found")
    return deleted


def _list_pool_ids(frame: "Frame", cleanup: dict) -> list[str]:
    selector = _require_selector(cleanup.get("pool_container"), "cleanup.pool_container")
    raw_ids = frame.locator(selector).evaluate_all("(els) => els.map((el) => el.id)")
    pool_ids = [pool_id for pool_id in raw_ids if isinstance(pool_id, str) and pool_id]
    pool_ids.sort(key=_pool_sort_key)
    if not pool_ids and _pools_visible_in_text(frame):
        # 页面上明明有「存储池N」却按容器 id 找不到 —— 多半是 UI 又改了池卡片
        # DOM。宁可报错留现场，也不能静默当"无池"跳过：跳过会让池带进下一台
        # 的流程（出厂还原保留硬盘数据，池元数据随盘存活）。
        raise CleanupError(
            "Storage pools are visible on the page but none matched "
            f"cleanup.pool_container ({selector!r}); the pool-card DOM likely changed"
        )
    return pool_ids


def _pools_visible_in_text(frame: "Frame") -> bool:
    try:
        text = " ".join(frame.locator("body").inner_text(timeout=2_000).split())
    except Exception as exc:
        # An empty pool-card query is only trustworthy when the surrounding page
        # can also be observed successfully. A detached/reloading frame is
        # "unknown", not proof that the NAS has no storage pools.
        raise CleanupError(
            "Could not verify whether storage pools exist because the storage "
            "manager page text could not be read"
        ) from exc
    return bool(re.search(r"存储池\d+", text))


def _delete_pool(
    page: "Page",
    frame: "Frame",
    cleanup: dict,
    admin: dict,
    pool_id: str,
    *,
    cancel_check_cb: Callable[[], None] | None = None,
) -> None:
    logger.info(f"  Deleting {pool_id}")
    menu_button_selector = _require_selector(cleanup.get("pool_menu_button"), "cleanup.pool_menu_button")
    delete_item_selector = _require_selector(cleanup.get("pool_delete_item"), "cleanup.pool_delete_item")
    delete_confirm_selector = _require_selector(cleanup.get("delete_confirm_button"), "cleanup.delete_confirm_button")
    password_input_selector = _require_selector(cleanup.get("modal_password_input"), "cleanup.modal_password_input")
    modal_confirm_selector = _require_selector(cleanup.get("modal_confirm_button"), "cleanup.modal_confirm_button")

    container = frame.locator(f"#{pool_id}").first
    expect(container).to_be_visible(timeout=FRAME_WAIT_MS)

    _check_cancel(cancel_check_cb)
    menu_button = container.locator(menu_button_selector).first
    expect(menu_button).to_be_visible(timeout=FRAME_WAIT_MS)
    menu_button.click()

    delete_item = frame.locator(delete_item_selector).first
    expect(delete_item).to_be_visible(timeout=FRAME_WAIT_MS)
    delete_item.click()

    delete_confirm = frame.locator(delete_confirm_selector).first
    expect(delete_confirm).to_be_visible(timeout=FRAME_WAIT_MS)
    delete_confirm.click()

    password_input = frame.locator(password_input_selector).first
    expect(password_input).to_be_visible(timeout=FRAME_WAIT_MS)
    password_input.fill(admin["password"])

    modal_confirm = frame.locator(modal_confirm_selector).first
    expect(modal_confirm).to_be_visible(timeout=FRAME_WAIT_MS)
    expect(modal_confirm).to_be_enabled(timeout=FRAME_WAIT_MS)
    # Last cancellable boundary: after this click the NAS owns a destructive
    # operation, so we must observe its outcome before unwinding the browser.
    _check_cancel(cancel_check_cb)
    modal_confirm.click()

    _wait_for_pool_removed(page, frame, pool_id)
    page.wait_for_timeout(SHORT_UI_WAIT_MS)


def _wait_for_pool_removed(page: "Page", frame: "Frame", pool_id: str) -> None:
    deadline = time.monotonic() + (POOL_DELETE_WAIT_MS / 1000)
    selector = f"#{pool_id}"
    last_preview = ""
    last_error = ""
    consecutive_errors = 0
    absence_samples = 0

    while time.monotonic() < deadline:
        try:
            locator = frame.locator(selector).first
            missing = locator.count() == 0
            if missing:
                absence_samples += 1
                logger.debug(
                    f"Storage pool {pool_id} absent "
                    f"({absence_samples}/{POOL_DELETE_STABLE_ABSENCE_SAMPLES})"
                )
                if absence_samples >= POOL_DELETE_STABLE_ABSENCE_SAMPLES:
                    logger.info(f"  Storage pool removed: {pool_id}")
                    return
            else:
                # Visibility is not existence. Storage Manager can temporarily
                # hide a pool card while a backend deletion is still pending;
                # only DOM detachment is positive evidence of removal.
                absence_samples = 0
                if locator.is_visible(timeout=500):
                    last_preview = " ".join(locator.inner_text(timeout=1_000).split())[:200]
                else:
                    last_preview = "pool card is still present in the DOM but hidden"
            # Only a complete locator observation breaks a run of read errors.
            # Resetting immediately after frame.locator() would prevent repeated
            # count/is_visible/inner_text failures from ever reaching the
            # fail-closed threshold below.
            consecutive_errors = 0
        except Exception as exc:
            # A detached iframe, closed browser, or transient Playwright failure is
            # not proof that destructive cleanup succeeded. Permit a few transient
            # read failures, then fail closed and leave the unit for inspection.
            consecutive_errors += 1
            absence_samples = 0
            last_error = " ".join(str(exc).split())[:200]
            logger.debug(
                f"Could not verify removal of {pool_id} "
                f"({consecutive_errors}/{POOL_DELETE_MAX_OBSERVATION_ERRORS}): {last_error}"
            )
            if consecutive_errors >= POOL_DELETE_MAX_OBSERVATION_ERRORS:
                raise CleanupError(
                    f"Could not verify that storage pool {pool_id} was removed after "
                    f"{consecutive_errors} observation errors"
                    f"{f': {last_error}' if last_error else ''}"
                ) from exc
        page.wait_for_timeout(2_000)

    raise CleanupError(
        f"Storage pool {pool_id} was not confirmed absent after {POOL_DELETE_WAIT_MS // 1000}s"
        f"{f': {last_preview}' if last_preview else ''}"
        f"{f'; last observation error: {last_error}' if last_error else ''}"
    )


def _pool_sort_key(pool_id: str) -> int:
    match = re.search(r"(\d+)$", pool_id)
    return int(match.group(1)) if match else 0


def _check_cancel(cancel_check_cb: Callable[[], None] | None) -> None:
    if cancel_check_cb is not None:
        cancel_check_cb()


def _open_app(page: "Page", desktop_selectors: dict, app: str) -> "Frame":
    app_selector = _require_selector(desktop_selectors.get("apps", {}).get(app), f"desktop_launchers.apps.{app}")
    frame_selector = _require_selector(desktop_selectors.get("frames", {}).get(app), f"desktop_launchers.frames.{app}")

    iframe = page.locator(frame_selector).first
    if iframe.count() and iframe.is_visible():
        _activate_window(page, frame_selector)
    else:
        launcher = find_visible_locator(page, app_selector, FRAME_WAIT_MS)
        if launcher is None:
            raise CleanupError(f"Desktop launcher for '{app}' did not appear")
        click_desktop_launcher(page, launcher, app)
        expect(iframe).to_be_visible(timeout=FRAME_WAIT_MS)

    page.wait_for_timeout(SHORT_UI_WAIT_MS)
    frame = _wait_for_frame(page, iframe, app)
    frame.wait_for_load_state("domcontentloaded")
    if app == "storagemgr":
        dismiss_arco_app_guides(page, frame, "storage manager")
    return frame


def _wait_for_frame(page: "Page", iframe_locator, app: str) -> "Frame":
    deadline = time.monotonic() + (FRAME_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        handle = iframe_locator.element_handle()
        frame = handle.content_frame() if handle is not None else None
        if frame is not None:
            return frame
        page.wait_for_timeout(200)
    raise CleanupError(f"iframe '{app}' did not appear on the desktop")


def _navigate(frame: "Frame", nav_selectors: dict, nav_keys: list[str]) -> None:
    for nav_key in nav_keys:
        selector = _require_selector(nav_selectors.get(nav_key), f"provision_nav.{nav_key}")
        loc = frame.locator(selector).first
        expect(loc).to_be_visible(timeout=FRAME_WAIT_MS)
        loc.click()
        frame.page.wait_for_timeout(SHORT_UI_WAIT_MS)


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
        raise CleanupError(f"Selector '{name}' is not configured")
    return selector
