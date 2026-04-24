from __future__ import annotations

import time
from typing import TYPE_CHECKING

from playwright.sync_api import TimeoutError as PWTimeoutError

from .logger import logger

if TYPE_CHECKING:
    from playwright.sync_api import Locator, Page


USER_GUIDE_MODAL_SELECTOR = 'section.user-guide-main-modal.show'
USER_GUIDE_MODAL_BUTTON = 'section.user-guide-main-modal.show button.ivu-btn-primary'
USER_GUIDE_PATIENT_WAIT_MS = 8_000


def dismiss_desktop_overlays(
    page: "Page",
    selectors: dict | None = None,
    max_rounds: int = 6,
    completion_wait_ms: int = USER_GUIDE_PATIENT_WAIT_MS,
    idle_wait_ms: int = 0,
    close_notices: bool = True,
) -> bool:
    post = (selectors or {}).get("post_setup", {})
    actions = []
    if close_notices:
        actions.append(("top_right_notice_close", [post.get("top_right_notice_close"), "a.ivu-notice-close"], 200))
    actions.extend(
        [
        (
            "welcome_start_tutorial",
            [
                'section.user-guide-main-modal .ivu-btn-primary',
                post.get("welcome_start_tutorial_button"),
                'button.ivu-btn-primary:has-text("开始教学")',
            ],
            500,
        ),
        ("tutorial_skip", [post.get("tutorial_skip_button"), "button.cancel-btn"], 400),
        (
            "goto_storage",
            [
                post.get("goto_storage_button"),
                'button.ivu-btn-primary:has-text("前往存储管理")',
                'button.ivu-btn-primary:has-text("前往")',
            ],
            400,
        ),
        ]
    )

    any_clicked = False
    click_count = 0
    started_at = time.monotonic()
    idle_spent_ms = 0
    completion_spent_ms = 0
    idle_deadline = page.evaluate("Date.now()") + idle_wait_ms
    for _ in range(max_rounds):
        round_clicked = False
        for name, candidates, timeout_ms in actions:
            if _click_first_visible(page, candidates, timeout_ms):
                logger.info(f"Dismissed overlay: {name}")
                round_clicked = True
                any_clicked = True
                click_count += 1
                page.wait_for_timeout(500)
        if not round_clicked:
            if idle_wait_ms and page.evaluate("Date.now()") < idle_deadline:
                idle_started = time.monotonic()
                page.wait_for_timeout(500)
                idle_spent_ms += int((time.monotonic() - idle_started) * 1000)
                continue
            break

    # The "太棒了！你已了解UGREEN NAS的核心功能！" completion modal often
    # appears a few seconds AFTER tutorial_skip is clicked — too late for the
    # fast dismiss loop above. Its basic-mask (#ugreen0) blocks every
    # subsequent desktop click, so we patiently poll for it here and
    # dismiss via its 前往存储管理 primary button.
    # Only do the patient wait when: (a) we actually dismissed
    # something (setup flow — modal is expected to appear) OR (b) it's
    # already visible on the page. Login mode on a pre-initialized device
    # should short-circuit.
    if completion_wait_ms > 0 and (any_clicked or _modal_is_visible(page, USER_GUIDE_MODAL_SELECTOR, 200)):
        completion_started = time.monotonic()
        if _dismiss_user_guide_completion_modal(page, completion_wait_ms):
            any_clicked = True
            click_count += 1
            page.wait_for_timeout(800)
        completion_spent_ms = int((time.monotonic() - completion_started) * 1000)

    if _hide_new_user_guide_cards(page):
        logger.info("Dismissed overlay: new_user_guide_cards")
        any_clicked = True
        click_count += 1
        page.wait_for_timeout(300)

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    total_spent_ms = int((time.monotonic() - started_at) * 1000)
    if total_spent_ms >= 2_000:
        logger.info(
            "Desktop overlay sweep finished in "
            f"{total_spent_ms}ms (clicks={click_count}, idle_wait={idle_spent_ms}ms, completion_wait={completion_spent_ms}ms)"
        )
    return any_clicked


def dismiss_blocking_desktop_overlays_now(page: "Page") -> bool:
    actions = [
        ["a.ivu-notice-close"],
        [USER_GUIDE_MODAL_BUTTON, 'section.user-guide-main-modal button.ivu-btn-primary'],
        ["button.cancel-btn"],
        ['button.ivu-btn-primary:has-text("\u524d\u5f80\u5b58\u50a8\u7ba1\u7406")', 'button.ivu-btn-primary:has-text("\u524d\u5f80")'],
    ]

    clicked = False
    for candidates in actions:
        if _click_first_visible(page, candidates, 250):
            clicked = True
            page.wait_for_timeout(250)
    if _hide_new_user_guide_cards(page):
        clicked = True
    return clicked


def click_desktop_launcher(page: "Page", launcher: "Locator", app_name: str) -> None:
    last_error: Exception | None = None
    for attempt in range(3):
        dismiss_blocking_desktop_overlays_now(page)
        try:
            launcher.click(timeout=5_000)
            return
        except Exception as exc:
            last_error = exc
            logger.info(f"Desktop click for {app_name} was blocked; dismissing overlays and retrying")
            if _dismiss_user_guide_completion_modal(page, 8_000):
                continue
            dismiss_desktop_overlays(page, max_rounds=4, completion_wait_ms=2_000, idle_wait_ms=2_000)
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            page.wait_for_timeout(500)

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Could not click desktop launcher for {app_name}")


def find_visible_locator(page: "Page", selector: str, timeout_ms: int) -> "Locator | None":
    deadline = time.monotonic() + (timeout_ms / 1000)
    locator = page.locator(selector)
    while True:
        try:
            count = min(locator.count(), 80)
            for index in range(count):
                candidate = locator.nth(index)
                if candidate.is_visible(timeout=100):
                    return candidate
        except Exception:
            pass
        if time.monotonic() >= deadline:
            return None
        page.wait_for_timeout(250)


def _dismiss_user_guide_completion_modal(page: "Page", timeout_ms: int) -> bool:
    """Wait up to timeout_ms for the user-guide completion modal to appear
    (it has a delayed render) and click its primary button if so."""
    try:
        modal = page.locator(USER_GUIDE_MODAL_SELECTOR).first
        modal.wait_for(state="visible", timeout=timeout_ms)
    except PWTimeoutError:
        return False
    except Exception:
        return False

    button = page.locator(USER_GUIDE_MODAL_BUTTON).first
    try:
        button.wait_for(state="visible", timeout=3_000)
        button.click(force=True)
        logger.info("Dismissed overlay: user_guide_completion_modal")
        return True
    except Exception as exc:
        logger.warning(f"Could not click user-guide completion modal button: {exc}")
        return False


def _modal_is_visible(page: "Page", selector: str, timeout_ms: int) -> bool:
    try:
        return page.locator(selector).first.is_visible(timeout=timeout_ms)
    except Exception:
        return False


def _click_first_visible(page: "Page", selectors: list[str | None], timeout_ms: int) -> bool:
    for selector in selectors:
        if not selector or selector == "TODO":
            continue
        try:
            locator = page.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(force=True)
            return True
        except PWTimeoutError:
            continue
        except Exception:
            continue
    return False


def _hide_new_user_guide_cards(page: "Page") -> bool:
    try:
        hidden = int(
            page.evaluate(
                """() => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0
                            && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && style.opacity !== '0';
                    };

                    let count = 0;
                    const seen = new Set();
                    const selectors = [
                        'section.user-guide-main-modal.show',
                        '#ugreen0',
                        '.user-guide-contain',
                        '.newuser-guide',
                        '.card.user-guide-card',
                        '.user-guide-contain .ivu-lottie',
                    ];

                    for (const selector of selectors) {
                        for (const el of Array.from(document.querySelectorAll(selector))) {
                            if (seen.has(el) || !isVisible(el)) {
                                continue;
                            }
                            seen.add(el);
                            el.style.setProperty('display', 'none', 'important');
                            el.style.setProperty('visibility', 'hidden', 'important');
                            el.style.setProperty('opacity', '0', 'important');
                            el.style.setProperty('pointer-events', 'none', 'important');
                            count += 1;
                        }
                    }

                    return count;
                }"""
            )
        )
        return hidden > 0
    except Exception:
        return False
