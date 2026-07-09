from __future__ import annotations

import time
from typing import TYPE_CHECKING

from .logger import logger

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page


SHORT_GUIDE_WAIT_MS = 700
SHORT_UI_WAIT_MS = 400
STORAGE_GUIDE_WAIT_MS = 4_000
STORAGE_GUIDE_MODAL_SELECTOR = (
    "section.storage-guide-modal, section.ivu-modal-default.storage-guide-modal, "
    ".arco-modal-container.storage-guide-modal"
)
STORAGE_GUIDE_MASK_SELECTOR = "#ugreen0, .basic-mask.ivu-modal-mask, .arco-modal-container.storage-guide-modal .arco-modal-mask"
STORAGE_GUIDE_CLOSE_SELECTORS = [
    "section.storage-guide-modal .action-bar .icon-close.pro-public.pro-icon-func-close",
    "section.ivu-modal-default.storage-guide-modal .action-bar .icon-close.pro-public.pro-icon-func-close",
    "section.storage-guide-modal .icon-close.pro-public.pro-icon-func-close",
    "section.ivu-modal-default.storage-guide-modal .icon-close.pro-public.pro-icon-func-close",
    ".storage-guide-modal .action-bar .icon-close",
]
STORAGE_GUIDE_EXIT_SELECTORS = [
    'section.ivu-modal-default .submit-btn.ivu-btn-primary:has-text("退出")',
    ".quit-modal .submit-btn.ivu-btn-primary",
    # Arco 版关闭引导后会追问「确定要退出吗？」，必须点「退出」才真正收起。
    '.arco-modal:has-text("确定要退出吗") button:has-text("退出")',
]


def dismiss_arco_app_guides(page: "Page", frame: "Frame", label: str) -> bool:
    """Dismiss first-run Arco guide modals and tutorial overlays inside an app iframe."""

    dismissed = False
    modal = frame.locator(".arco-modal-container .guide-modal-body").first
    try:
        if modal.is_visible(timeout=SHORT_GUIDE_WAIT_MS):
            start_button = frame.locator(".arco-modal-container button.play-start").first
            start_button.click(force=True, timeout=2_000)
            logger.info(f"Dismissed {label} welcome modal")
            page.wait_for_timeout(SHORT_UI_WAIT_MS)
            dismissed = True
    except Exception:
        pass

    if dismiss_storage_guide_modal(frame, label):
        dismissed = True

    if _click_skip_tutorial(page, frame, label):
        dismissed = True

    removed = _remove_tutorial_artifacts(frame)
    if removed:
        logger.info(f"Removed {label} tutorial artifacts")
        page.wait_for_timeout(SHORT_UI_WAIT_MS)
        dismissed = True

    return dismissed


def dismiss_storage_guide_modal(target: "Page | Frame", label: str) -> bool:
    acted = False
    last_action = ""
    deadline = time.monotonic() + (STORAGE_GUIDE_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        if _click_first_visible_locator(target, STORAGE_GUIDE_EXIT_SELECTORS, timeout_ms=250):
            acted = True
            last_action = "exit"
            target.wait_for_timeout(SHORT_UI_WAIT_MS)
            continue

        if _click_first_visible_locator(target, STORAGE_GUIDE_CLOSE_SELECTORS, timeout_ms=250):
            acted = True
            last_action = "close"
            target.wait_for_timeout(SHORT_UI_WAIT_MS)
            continue

        if acted:
            break

        try:
            result = target.evaluate(
                f"""() => {{
                    const modal = document.querySelector({STORAGE_GUIDE_MODAL_SELECTOR!r});
                    if (!modal) {{
                        return '';
                    }}

                    let hidden = 0;
                    for (const el of [modal, ...document.querySelectorAll({STORAGE_GUIDE_MASK_SELECTOR!r})]) {{
                        el.style.setProperty('display', 'none', 'important');
                        el.style.setProperty('visibility', 'hidden', 'important');
                        el.style.setProperty('opacity', '0', 'important');
                        el.style.setProperty('pointer-events', 'none', 'important');
                        hidden += 1;
                    }}
                    return hidden > 0 ? 'hidden' : '';
                }}"""
            )
        except Exception:
            return acted

        if not result:
            target.wait_for_timeout(250)
            continue

        acted = True
        last_action = str(result)
        target.wait_for_timeout(SHORT_UI_WAIT_MS)
        break

    if acted:
        logger.info(f"Dismissed {label} storage welcome modal via {last_action}")
    return acted


def _click_first_visible_locator(target: "Page | Frame", selectors: list[str], timeout_ms: int) -> bool:
    for selector in selectors:
        try:
            locator = target.locator(selector).first
            locator.wait_for(state="visible", timeout=timeout_ms)
            locator.click(timeout=max(timeout_ms, 1_000), force=True)
            return True
        except Exception:
            continue
    return False


def _click_skip_tutorial(page: "Page", frame: "Frame", label: str) -> bool:
    clicked_any = False
    for _ in range(4):
        clicked = frame.evaluate(
            """() => {
                const skipText = '\\u8df3\\u8fc7';
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0
                        && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && style.opacity !== '0';
                };
                const target = Array.from(document.querySelectorAll('button, a, span, div, p'))
                    .find((el) => isVisible(el) && (el.innerText || '').trim().startsWith(skipText));
                if (!target) {
                    return false;
                }
                target.click();
                return true;
            }"""
        )
        if not clicked:
            break
        clicked_any = True
        page.wait_for_timeout(SHORT_UI_WAIT_MS)

    if clicked_any:
        logger.info(f"Dismissed {label} tutorial")
    return clicked_any


def _remove_tutorial_artifacts(frame: "Frame") -> int:
    return int(
        frame.evaluate(
            """() => {
                let count = 0;
                for (const selector of ['div.mask', 'div.stepElem', '[data-v-d10fc649]', '#tourMain', '.tour-main']) {
                    for (const el of Array.from(document.querySelectorAll(selector))) {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        if (rect.width > 0
                            && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && style.opacity !== '0') {
                            el.remove();
                            count += 1;
                        }
                    }
                }
                return count;
            }"""
        )
    )
