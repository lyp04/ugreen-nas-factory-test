from __future__ import annotations

import re
import time
from pathlib import Path
from typing import TYPE_CHECKING, Callable
from urllib.parse import urlparse

from playwright.sync_api import expect

from ..utils.app_guides import dismiss_arco_app_guides, dismiss_storage_guide_modal
from ..utils.desktop import click_desktop_launcher, dismiss_desktop_overlays
from ..utils.logger import logger
from ..utils.smb_transfer import SmbTransferConfig, SmbTransferError, build_mount_script, build_unmount_script, run_powershell

if TYPE_CHECKING:
    from playwright.sync_api import Frame, Page


FRAME_WAIT_MS = 15_000
SHORT_UI_WAIT_MS = 800
POOL_READY_TIMEOUT_MS = 180_000
POOL_CREATE_RETRY_INTERVAL_MS = 5_000
POOL_CREATE_RETRY_POLL_MS = 500
SHARE_FOLDER_MENU_TEXT = "\u65b0\u5efa\u5171\u4eab\u6587\u4ef6\u5939"
SHARE_NAME_CONFLICT_TEXT = "\u5b58\u5728\u540c\u540d\u6587\u4ef6\u5939"
MODAL_CANCEL_TEXT = "\u53d6\u6d88"
POOL_CREATE_WARNING_TEXT = "\u4e0d\u6ee1\u8db3\u521b\u5efa\u8981\u6c42"
POOL_CREATE_WARNING_ACK_TEXT = "\u6211\u77e5\u9053\u4e86"
POOL_CREATE_WARNING_MODAL_SELECTOR = f'section.ivu-modal-default:has-text("{POOL_CREATE_WARNING_TEXT}")'
POOL_CREATE_WARNING_ACK_SELECTOR = (
    f'section.ivu-modal-default:has-text("{POOL_CREATE_WARNING_TEXT}") '
    f'.submit-btn.ivu-btn-primary:has-text("{POOL_CREATE_WARNING_ACK_TEXT}")'
)
POOL_SUMMARY_SELECTOR = ".storage-itemInfo"
SINGLE_DISK_RAID_LABEL = "Basic"
MODEL_HDD_DISK_COUNTS = {
    "2800": 2,
    "4800": 4,
    "4800plus": 4,
}
POOL_DISK_TOKEN_RE = re.compile(r"M\.2硬盘\d+|硬盘\d+")
APP_LIBRARY_ICON_SELECTOR = ".nav-home .nav-home-icon"
APP_LIBRARY_ITEM_SELECTOR = "div.app-item-wrapper"


class ProvisionError(RuntimeError):
    pass


class ProvisionAborted(ProvisionError):
    pass


def run(
    page: "Page",
    config: dict,
    selectors: dict,
    admin: dict,
    confirm_disk_shortage_cb: Callable[[dict], bool] | None = None,
    model: str | None = None,
) -> None:
    dismiss_desktop_overlays(page, selectors, max_rounds=2, completion_wait_ms=0)
    desktop = selectors.get("desktop_launchers", {})
    nav = selectors.get("provision_nav", {})
    provision_selectors = selectors.get("provision", {})
    pools = config.get("provision", {}).get("pools", [])

    _ensure_smb_enabled(page, desktop, nav, provision_selectors)

    for pool in pools:
        _ensure_pool(page, desktop, nav, provision_selectors, pool, admin, confirm_disk_shortage_cb, model)

    for pool in pools:
        _ensure_share(page, desktop, nav, provision_selectors, pool, admin)


def _ensure_smb_enabled(page: "Page", desktop: dict, nav: dict, provision: dict) -> None:
    logger.info("Provisioning: ensure SMB service is enabled")
    frame = _open_app(page, desktop, "ctlmgr")
    _navigate(page, frame, nav, ["ctlmgr_file_service_menu", "ctlmgr_smb_tab"])

    checkbox = frame.locator(_require_selector(provision.get("smb_enable_checkbox"), "provision.smb_enable_checkbox")).first
    expect(checkbox).to_be_attached(timeout=FRAME_WAIT_MS)

    if checkbox.is_checked():
        logger.info("  SMB service already enabled")
        return

    checkbox.check(force=True)
    apply_button = frame.locator(_require_selector(provision.get("smb_apply_button"), "provision.smb_apply_button")).first
    expect(apply_button).to_be_enabled(timeout=FRAME_WAIT_MS)
    apply_button.click()
    expect(checkbox).to_be_checked(timeout=FRAME_WAIT_MS)
    page.wait_for_timeout(SHORT_UI_WAIT_MS)
    logger.info("  SMB service enabled")


def _ensure_pool(
    page: "Page",
    desktop: dict,
    nav: dict,
    provision: dict,
    pool: dict,
    admin: dict,
    confirm_disk_shortage_cb: Callable[[dict], bool] | None,
    model: str | None,
) -> None:
    frame = _open_app(page, desktop, "storagemgr")
    _navigate(page, frame, nav, ["storagemgr_storage_menu", "storagemgr_pool_tab"])

    pool_name = _require_meta(pool.get("pool_name"), "provision.pools[].pool_name")
    summary_text = _find_matching_pool_summary_text(frame, pool)

    if summary_text:
        logger.info(f"Provisioning: storage pool already present for {pool_name} -> {summary_text}")
        return

    logger.info(f"Provisioning: create storage pool {pool_name}")
    _dismiss_pool_creation_warning(frame)
    _start_pool_creation(frame, provision)
    _select_pool_disks_and_raid(frame, provision, pool, confirm_disk_shortage_cb, model)
    _finish_pool_creation(page, frame, provision, admin)

    summary_text = _wait_for_matching_pool_summary_text(frame, pool, timeout_ms=POOL_READY_TIMEOUT_MS)
    page.wait_for_timeout(SHORT_UI_WAIT_MS)
    logger.info(f"  Storage pool ready: {pool_name} -> {summary_text}")


def _ensure_share(page: "Page", desktop: dict, nav: dict, provision: dict, pool: dict, admin: dict) -> None:
    frame = _open_app(page, desktop, "filemgr")
    _dismiss_filemgr_intro_with_wait(page, frame, provision, wait_ms=6_000)
    _dismiss_filemgr_self_folder_tip_with_wait(page, frame, provision, wait_ms=6_000)
    _dismiss_filemgr_intro_with_wait(page, frame, provision, wait_ms=6_000)
    _dismiss_filemgr_tutorial(page, frame, wait_ms=6_000)
    _dismiss_filemgr_self_folder_tip_with_wait(page, frame, provision, wait_ms=2_000)
    _dismiss_filemgr_intro_with_wait(page, frame, provision, wait_ms=2_000)
    _ensure_filemgr_shared_folder_view(page, frame, nav, provision)

    share_name = _require_meta(pool.get("share_name"), "provision.pools[].share_name")
    storage_space = _require_meta(pool.get("storage_space"), "provision.pools[].storage_space")
    share_row = frame.locator(f'.name-text:text-is("{share_name}")').first

    if share_row.count() and share_row.is_visible():
        logger.info(f"Provisioning: shared folder already present for {share_name}")
        return

    logger.info(f"Provisioning: create shared folder {share_name} on {storage_space}")
    _open_share_create_modal(frame, provision)

    modal = frame.locator(_require_selector(provision.get("filemgr_share_modal"), "provision.filemgr_share_modal")).first
    expect(modal).to_be_visible(timeout=FRAME_WAIT_MS)

    _fill_share_name(frame, provision, share_name)
    if _handle_share_name_conflict(page, frame, share_name, admin):
        return

    storage_select = frame.locator(
        _require_selector(provision.get("filemgr_share_storage_select"), "provision.filemgr_share_storage_select")
    ).first
    storage_select.click()

    option_selector = _require_selector(provision.get("filemgr_share_storage_option"), "provision.filemgr_share_storage_option")
    storage_option = frame.locator(option_selector).filter(has_text=storage_space).first
    expect(storage_option).to_be_visible(timeout=FRAME_WAIT_MS)
    storage_option.click()

    if _handle_share_name_conflict(page, frame, share_name, admin):
        return

    create_button = frame.locator(
        _require_selector(provision.get("filemgr_share_create_button"), "provision.filemgr_share_create_button")
    ).first
    expect(create_button).to_be_enabled(timeout=FRAME_WAIT_MS)
    create_button.click()

    acl_modal = frame.locator(_require_selector(provision.get("filemgr_acl_modal"), "provision.filemgr_acl_modal")).first
    expect(acl_modal).to_be_visible(timeout=FRAME_WAIT_MS)
    acl_confirm = frame.locator(
        _require_selector(provision.get("filemgr_acl_confirm_button"), "provision.filemgr_acl_confirm_button")
    ).first
    acl_confirm.click()

    if _wait_for_share_row(share_row, timeout_ms=5_000):
        page.wait_for_timeout(SHORT_UI_WAIT_MS)
        logger.info(f"  Shared folder ready: {share_name}")
        return

    if _share_exists_via_smb(page, share_name, admin):
        logger.info(f"  Shared folder ready: {share_name} (verified via SMB)")
        return

    expect(share_row).to_be_visible(timeout=FRAME_WAIT_MS)
    page.wait_for_timeout(SHORT_UI_WAIT_MS)
    logger.info(f"  Shared folder ready: {share_name}")


def _dismiss_filemgr_intro(page: "Page", frame: "Frame", provision: dict) -> bool:
    modal_selector = _require_selector(provision.get("filemgr_intro_modal"), "provision.filemgr_intro_modal")
    start_selector = _require_selector(
        provision.get("filemgr_intro_start_button"),
        "provision.filemgr_intro_start_button",
    )

    modal = frame.locator(modal_selector).first
    modal_visible = False
    try:
        modal_visible = modal.is_visible(timeout=1_000)
    except Exception:
        modal_visible = False

    if modal_visible:
        logger.info("Provisioning: dismiss file manager welcome modal")
        start_button = frame.locator(start_selector).first
        expect(start_button).to_be_visible(timeout=FRAME_WAIT_MS)
        start_button.click(force=True)
        page.wait_for_timeout(SHORT_UI_WAIT_MS)
        return True

    try:
        clicked = bool(
            frame.evaluate(
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
                    const welcomeTexts = [
                        '\\u6b22\\u8fce\\u4f7f\\u7528\\u6587\\u4ef6\\u7ba1\\u7406',
                        '\\u5f00\\u59cb\\u4f7f\\u7528'
                    ];
                    const modals = Array.from(document.querySelectorAll(
                        '.arco-modal, .arco-modal-container, .custom-modal, [role="dialog"], section'
                    )).filter(isVisible);
                    for (const modal of modals) {
                        const text = (modal.innerText || '').replace(/\\s+/g, '');
                        if (!welcomeTexts.some((item) => text.includes(item))) {
                            continue;
                        }
                        const buttons = Array.from(modal.querySelectorAll(
                            'button, [role="button"], .play-start, .footer-primary-btn, .fs-button-wrap, .icon'
                        )).filter(isVisible);
                        const start = buttons.find((button) => {
                            const buttonText = (button.innerText || button.textContent || '').replace(/\\s+/g, '');
                            const cls = button.className || '';
                            return buttonText.includes('\\u5f00\\u59cb\\u4f7f\\u7528')
                                || String(cls).includes('play-start');
                        });
                        const target = start || buttons.find((button) => {
                            const label = [
                                button.getAttribute('aria-label') || '',
                                button.getAttribute('title') || '',
                                button.className || ''
                            ].join(' ');
                            return label.includes('close') || label.includes('Close') || label.includes('icon');
                        });
                        if (target) {
                            target.click();
                            return true;
                        }
                    }
                    return false;
                }"""
            )
        )
    except Exception:
        clicked = False

    if clicked:
        logger.info("Provisioning: dismiss file manager welcome modal")
        page.wait_for_timeout(SHORT_UI_WAIT_MS)
    return clicked


def _dismiss_filemgr_intro_with_wait(page: "Page", frame: "Frame", provision: dict, wait_ms: int) -> bool:
    deadline = time.monotonic() + (wait_ms / 1000)
    while True:
        if _dismiss_filemgr_intro(page, frame, provision):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(400)


def _fill_share_name(frame: "Frame", provision: dict, share_name: str) -> None:
    configured_selector = _require_selector(
        provision.get("filemgr_share_name_input"),
        "provision.filemgr_share_name_input",
    )
    selectors = [
        configured_selector,
        '.folder-share-create input[placeholder="\u6587\u4ef6\u5939\u540d\u79f0"]',
        '.folder-share-create input[placeholder*="\u6587\u4ef6\u5939"]',
        '.folder-share-create input[type="text"]:visible',
        'input[placeholder="\u6587\u4ef6\u5939\u540d\u79f0"]',
    ]

    for selector in dict.fromkeys(selectors):
        try:
            locator = frame.locator(selector)
            count = min(locator.count(), 10)
        except Exception:
            continue
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible(timeout=300):
                    continue
                candidate.fill(share_name, timeout=3_000)
                frame.page.wait_for_timeout(300)
                if _visible_share_name_is(frame, share_name):
                    return
            except Exception:
                continue

    try:
        filled = bool(
            frame.evaluate(
                """(shareName) => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0
                            && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && style.opacity !== '0';
                    };
                    const inputs = Array.from(document.querySelectorAll(
                        '.folder-share-create input, input[placeholder*="\\u6587\\u4ef6\\u5939"]'
                    )).filter(isVisible);
                    const target = inputs.find((input) => {
                        const placeholder = input.getAttribute('placeholder') || '';
                        return placeholder.includes('\\u6587\\u4ef6\\u5939');
                    }) || inputs.find((input) => input.type === 'text') || inputs[0];
                    if (!target) {
                        return false;
                    }
                    target.focus();
                    target.value = shareName;
                    target.dispatchEvent(new Event('input', { bubbles: true }));
                    target.dispatchEvent(new Event('change', { bubbles: true }));
                    target.blur();
                    return true;
                }""",
                share_name,
            )
        )
    except Exception:
        filled = False

    frame.page.wait_for_timeout(300)
    if filled and _visible_share_name_is(frame, share_name):
        return

    raise ProvisionError(f"Could not fill shared-folder name '{share_name}' in file manager modal")


def _visible_share_name_is(frame: "Frame", share_name: str) -> bool:
    try:
        return bool(
            frame.evaluate(
                """(shareName) => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0
                            && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && style.opacity !== '0';
                    };
                    return Array.from(document.querySelectorAll(
                        '.folder-share-create input, input[placeholder*="\\u6587\\u4ef6\\u5939"]'
                    )).some((input) => isVisible(input) && input.value === shareName);
                }""",
                share_name,
            )
        )
    except Exception:
        return False


def _dismiss_filemgr_self_folder_tip(page: "Page", frame: "Frame", provision: dict) -> None:
    section_selector = _require_selector(
        provision.get("filemgr_self_folder_section"),
        "provision.filemgr_self_folder_section",
    )
    ignore_selector = _require_selector(
        provision.get("filemgr_self_folder_ignore"),
        "provision.filemgr_self_folder_ignore",
    )

    section = frame.locator(section_selector).first
    try:
        if not section.is_visible(timeout=2_000):
            return
    except Exception:
        return

    logger.info("Provisioning: dismiss file manager '开启个人文件夹' tip")
    ignore_button = frame.locator(ignore_selector).first
    expect(ignore_button).to_be_visible(timeout=FRAME_WAIT_MS)
    ignore_button.click(force=True)
    page.wait_for_timeout(SHORT_UI_WAIT_MS)


def _dismiss_filemgr_self_folder_tip_with_wait(page: "Page", frame: "Frame", provision: dict, wait_ms: int) -> bool:
    section_selector = _require_selector(
        provision.get("filemgr_self_folder_section"),
        "provision.filemgr_self_folder_section",
    )
    deadline = time.monotonic() + (wait_ms / 1000)
    while True:
        try:
            section = frame.locator(section_selector).first
            if not section.is_visible(timeout=400):
                if time.monotonic() >= deadline:
                    return False
                page.wait_for_timeout(400)
                continue
            _dismiss_filemgr_self_folder_tip(page, frame, provision)
            return True
        except Exception:
            if time.monotonic() >= deadline:
                return False
            page.wait_for_timeout(400)


def _dismiss_filemgr_tutorial(page: "Page", frame: "Frame", wait_ms: int = 0) -> bool:
    """Close the first-run guided tour that blocks the file-manager tree."""

    deadline = time.monotonic() + (wait_ms / 1000)
    while True:
        if _dismiss_filemgr_tutorial_once(page, frame):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(400)


def _dismiss_filemgr_tutorial_once(page: "Page", frame: "Frame") -> bool:
    closed_by_ui = False
    for _ in range(4):
        clicked = frame.evaluate(
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
                const candidates = Array.from(document.querySelectorAll('button, a, span, div, p'))
                    .filter((el) => isVisible(el) && (el.innerText || '').includes('跳过'));
                const target = candidates.find((el) => {
                    const text = (el.innerText || '').trim();
                    return text === '跳过' || text.startsWith('跳过');
                }) || candidates[0];
                if (!target) {
                    return false;
                }
                target.click();
                return true;
            }"""
        )
        if not clicked:
            break
        closed_by_ui = True
        page.wait_for_timeout(SHORT_UI_WAIT_MS)

        if not _has_visible_filemgr_tutorial_artifacts(frame):
            logger.info("Provisioning: dismissed file manager tutorial")
            return True

    if closed_by_ui and not _has_visible_filemgr_tutorial_artifacts(frame):
        logger.info("Provisioning: dismissed file manager tutorial")
        return True

    removed = _remove_filemgr_tutorial_artifacts(frame)
    if removed:
        logger.info("Provisioning: removed file manager tutorial artifacts")
        page.wait_for_timeout(300)
        return True

    return False


def _has_visible_filemgr_tutorial_artifacts(frame: "Frame") -> bool:
    return bool(
        frame.evaluate(
            """() => Array.from(document.querySelectorAll('div.mask, div.stepElem, [data-v-d10fc649]')).some((el) => {
                const rect = el.getBoundingClientRect();
                const style = window.getComputedStyle(el);
                return rect.width > 0
                    && rect.height > 0
                    && style.visibility !== 'hidden'
                    && style.display !== 'none'
                    && style.opacity !== '0';
            })"""
        )
    )


def _remove_filemgr_tutorial_artifacts(frame: "Frame") -> int:
    return int(
        frame.evaluate(
            """() => {
                let count = 0;
                for (const selector of ['div.mask', 'div.stepElem', '[data-v-d10fc649]']) {
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


def _ensure_filemgr_shared_folder_view(page: "Page", frame: "Frame", nav_selectors: dict, provision: dict) -> None:
    if _is_filemgr_shared_folder_view(frame, nav_selectors, provision):
        return

    if _navigate_filemgr_shared_folder(page, frame, nav_selectors, provision):
        return

    if _is_filemgr_shared_folder_view(frame, nav_selectors, provision):
        return

    raise ProvisionError("File manager shared-folder view did not become available")


def _navigate_filemgr_shared_folder(page: "Page", frame: "Frame", nav_selectors: dict, provision: dict) -> bool:
    selector = _require_selector(nav_selectors.get("filemgr_shared_folder_tree"), "provision_nav.filemgr_shared_folder_tree")
    deadline = time.monotonic() + (FRAME_WAIT_MS / 1000)
    loc = frame.locator(selector).first

    while time.monotonic() < deadline:
        if _is_filemgr_shared_folder_view(frame, nav_selectors, provision):
            return True
        _dismiss_filemgr_intro(page, frame, provision)
        _dismiss_filemgr_self_folder_tip_with_wait(page, frame, provision, wait_ms=500)
        _dismiss_filemgr_tutorial(page, frame)
        try:
            if loc.is_visible(timeout=500):
                loc.click()
                page.wait_for_timeout(SHORT_UI_WAIT_MS)
                if _is_filemgr_shared_folder_view(frame, nav_selectors, provision):
                    return True
        except Exception:
            pass
        page.wait_for_timeout(500)

    return _is_filemgr_shared_folder_view(frame, nav_selectors, provision)


def _is_filemgr_shared_folder_view(frame: "Frame", nav_selectors: dict, provision: dict) -> bool:
    shared_tree_selector = _require_selector(
        nav_selectors.get("filemgr_shared_folder_tree"),
        "provision_nav.filemgr_shared_folder_tree",
    )
    share_item_selector = _require_selector(
        provision.get("filemgr_quick_add_share_item"),
        "provision.filemgr_quick_add_share_item",
    )
    quick_add_selector = _require_selector(
        provision.get("filemgr_quick_add_button"),
        "provision.filemgr_quick_add_button",
    )
    shared_tree = frame.locator(shared_tree_selector).first

    try:
        if shared_tree.is_visible(timeout=300):
            class_name = (shared_tree.get_attribute("class") or "").lower()
            return "active" in class_name or "selected" in class_name
    except Exception:
        pass

    share_item = frame.locator(share_item_selector).first
    try:
        if share_item.is_visible(timeout=300):
            return True
    except Exception:
        pass

    quick_add = frame.locator(quick_add_selector).first
    try:
        if not quick_add.is_visible(timeout=300):
            return False
    except Exception:
        return False

    try:
        quick_add.click(force=True, timeout=1_000)
        frame.page.wait_for_timeout(200)
        if share_item.is_visible(timeout=500):
            try:
                frame.page.keyboard.press("Escape")
            except Exception:
                pass
            frame.page.wait_for_timeout(200)
            return True
        try:
            frame.page.keyboard.press("Escape")
        except Exception:
            pass
    except Exception:
        return False

    return False


def _start_pool_creation(frame: "Frame", provision: dict) -> None:
    _dismiss_pool_creation_warning(frame)
    empty_button = frame.locator(
        _require_selector(provision.get("storagemgr_empty_create_button"), "provision.storagemgr_empty_create_button")
    ).first

    if empty_button.count() and empty_button.is_visible():
        _click_pool_creation_entry(frame, empty_button)
        return

    create_button = frame.locator(
        _require_selector(provision.get("storagemgr_header_create_button"), "provision.storagemgr_header_create_button")
    ).first
    expect(create_button).to_be_visible(timeout=FRAME_WAIT_MS)
    _click_pool_creation_entry(frame, create_button)

    pool_item = frame.locator(
        _require_selector(provision.get("storagemgr_header_create_pool_item"), "provision.storagemgr_header_create_pool_item")
    ).first
    expect(pool_item).to_be_visible(timeout=FRAME_WAIT_MS)
    pool_item.click()


def _click_pool_creation_entry(frame: "Frame", locator) -> None:
    try:
        locator.click(timeout=5_000)
        return
    except Exception:
        if not _dismiss_pool_creation_warning(frame):
            raise
        logger.info("Provisioning: dismissed stale pool creation warning before retrying create entry")
        locator.click(timeout=FRAME_WAIT_MS)


def _select_pool_disks_and_raid(
    frame: "Frame",
    provision: dict,
    pool: dict,
    confirm_disk_shortage_cb: Callable[[dict], bool] | None,
    model: str | None,
) -> None:
    disks = pool.get("disks") or []
    raid = _require_meta(pool.get("raid"), "provision.pools[].raid")
    if not disks:
        raise ProvisionError("Metadata 'provision.pools[].disks' is not configured")

    expected_disks = _expected_pool_disks(pool, disks, model)
    _advance_pool_creation_to_disk_selection(frame, provision, expected_disks)
    _wait_for_any_pool_disk(frame, provision, expected_disks)
    visible_disks = _visible_pool_disk_tokens(frame)
    selected_disks = _resolve_available_pool_disks(frame, expected_disks)
    _confirm_disk_shortage_if_needed(
        pool,
        expected_disks,
        selected_disks,
        raid,
        confirm_disk_shortage_cb,
        model,
        visible_disks=visible_disks,
    )

    for disk in selected_disks:
        logger.info(f"Provisioning: select pool disk {disk}")
        disk_option = frame.get_by_text(disk, exact=True).first
        expect(disk_option).to_be_visible(timeout=FRAME_WAIT_MS)
        disk_option.click()

    _select_pool_raid(frame, provision, raid, selected_disk_count=len(selected_disks))


def _expected_pool_disks(pool: dict, configured_disks: list[str], model: str | None) -> list[str]:
    pool_key = str(pool.get("key") or "").lower()
    model_key = str(model or "").strip().lower()
    if pool_key == "hdd" and model_key in MODEL_HDD_DISK_COUNTS:
        return configured_disks[: MODEL_HDD_DISK_COUNTS[model_key]]
    return configured_disks


def _confirm_disk_shortage_if_needed(
    pool: dict,
    expected_disks: list[str],
    selected_disks: list[str],
    raid: str,
    confirm_disk_shortage_cb: Callable[[dict], bool] | None,
    model: str | None,
    visible_disks: list[str] | None = None,
) -> None:
    missing_disks = [disk for disk in expected_disks if disk not in selected_disks]
    if not missing_disks:
        return

    pool_name = str(pool.get("pool_name") or "")
    visible_disks = visible_disks or selected_disks
    can_continue = bool(selected_disks)
    payload = {
        "pool_key": pool.get("key"),
        "pool_name": pool_name,
        "raid": raid,
        "model": model or "",
        "expected_disks": expected_disks,
        "available_disks": selected_disks,
        "visible_disks": visible_disks,
        "missing_disks": missing_disks,
        "fallback_raid": SINGLE_DISK_RAID_LABEL if len(selected_disks) == 1 else (raid if selected_disks else ""),
        "can_continue": can_continue,
    }
    logger.info(
        f"Provisioning: disk shortage for {pool_name}; expected={expected_disks}, "
        f"available={selected_disks}, visible={visible_disks}, missing={missing_disks}"
    )
    if not can_continue:
        if confirm_disk_shortage_cb is not None:
            confirm_disk_shortage_cb(payload)
        raise ProvisionAborted(
            f"Storage-pool creation aborted by user: {pool_name} has no configured disks available; "
            f"expected={expected_disks}, visible={visible_disks}"
        )

    confirmed = (
        bool(confirm_disk_shortage_cb(payload))
        if confirm_disk_shortage_cb is not None
        else _confirm_disk_shortage_from_console(payload)
    )
    if confirmed:
        if len(selected_disks) == 1:
            logger.info(f"Provisioning: continuing {pool_name} with single disk as {SINGLE_DISK_RAID_LABEL}")
        else:
            logger.info(f"Provisioning: continuing {pool_name} with available disks {selected_disks}")
        return

    raise ProvisionAborted(f"Storage-pool creation aborted by user: {pool_name} has missing disks {missing_disks}")


def _confirm_disk_shortage_from_console(payload: dict) -> bool:
    pool_name = str(payload.get("pool_name") or "")
    expected = ", ".join(str(disk) for disk in payload.get("expected_disks") or [])
    available = ", ".join(str(disk) for disk in payload.get("available_disks") or [])
    missing = ", ".join(str(disk) for disk in payload.get("missing_disks") or [])
    fallback = str(payload.get("fallback_raid") or "")
    while True:
        answer = input(
            f"Storage pool {pool_name} has missing disks.\n"
            f"Expected: {expected}\n"
            f"Available: {available}\n"
            f"Missing: {missing}\n"
            f"Continue with {fallback}? [y/n]: "
        ).strip().lower()
        if answer in {"y", "yes"}:
            return True
        if answer in {"n", "no"}:
            return False


def _resolve_available_pool_disks(frame: "Frame", configured_disks: list[str]) -> list[str]:
    selected: list[str] = []
    missing: list[str] = []

    for disk in configured_disks:
        disk_option = frame.get_by_text(disk, exact=True).first
        try:
            if disk_option.is_visible(timeout=800):
                selected.append(disk)
                continue
        except Exception:
            pass
        missing.append(disk)

    if missing:
        logger.info(f"Provisioning: skip unavailable pool disks {missing}")
    if selected:
        logger.info(f"Provisioning: using pool disks {selected}")
    return selected


def _advance_pool_creation_to_disk_selection(frame: "Frame", provision: dict, configured_disks: list[str]) -> None:
    if _any_pool_disk_visible(frame, configured_disks):
        return

    next_button_selector = _require_selector(provision.get("storagemgr_next_button"), "provision.storagemgr_next_button")
    next_button = frame.locator(next_button_selector).first
    try:
        if not next_button.is_visible(timeout=1_000):
            return
    except Exception:
        return

    logger.info("Provisioning: advance pool wizard to disk selection")
    next_button.click()
    frame.page.wait_for_timeout(SHORT_UI_WAIT_MS)


def _wait_for_any_pool_disk(frame: "Frame", provision: dict, configured_disks: list[str]) -> None:
    deadline = time.monotonic() + (POOL_READY_TIMEOUT_MS / 1000)
    last_create_attempt = time.monotonic()
    saw_requirements_warning = False

    while time.monotonic() < deadline:
        if _any_pool_disk_visible(frame, configured_disks):
            if saw_requirements_warning:
                logger.info("Provisioning: storage pool disks are now ready")
            return
        visible_disks = _visible_pool_disk_tokens(frame)
        if visible_disks:
            logger.info(f"Provisioning: storage pool dialog visible disks {visible_disks}")
            return

        if _dismiss_pool_creation_warning(frame):
            saw_requirements_warning = True
            logger.info(
                "Provisioning: storage manager reports no unused healthy disks yet; "
                "waiting and retrying pool creation"
            )
            frame.page.wait_for_timeout(POOL_CREATE_RETRY_INTERVAL_MS)
            if _pool_creation_entry_is_visible(frame, provision):
                _start_pool_creation(frame, provision)
                last_create_attempt = time.monotonic()
            continue

        if (
            time.monotonic() - last_create_attempt >= (POOL_CREATE_RETRY_INTERVAL_MS / 1000)
            and _pool_creation_entry_is_visible(frame, provision)
        ):
            _start_pool_creation(frame, provision)
            last_create_attempt = time.monotonic()

        frame.page.wait_for_timeout(POOL_CREATE_RETRY_POLL_MS)

    if saw_requirements_warning:
        raise ProvisionError("Storage manager did not expose any unused healthy disks for pool creation before timeout")
    raise ProvisionError("Storage-pool creation dialog did not render any configured disk options in time")


def _any_pool_disk_visible(frame: "Frame", configured_disks: list[str]) -> bool:
    for disk in configured_disks:
        disk_option = frame.get_by_text(disk, exact=True).first
        try:
            if disk_option.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


def _visible_pool_disk_tokens(frame: "Frame") -> list[str]:
    try:
        text = frame.evaluate(
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
                const roots = Array.from(document.querySelectorAll(
                    'section[data-transfer="true"], .ivu-modal, .arco-modal, [role="dialog"]'
                )).filter(isVisible);
                const createPoolText = '\\u521b\\u5efa\\u5b58\\u50a8\\u6c60';
                const root = roots.find((el) => (el.innerText || '').includes(createPoolText))
                    || roots[roots.length - 1]
                    || document.body;
                return root ? (root.innerText || '') : '';
            }"""
        )
    except Exception:
        return []
    return _pool_disk_tokens_from_text(str(text or ""))


def _pool_disk_tokens_from_text(text: str) -> list[str]:
    tokens: list[str] = []
    for match in POOL_DISK_TOKEN_RE.finditer(text or ""):
        token = match.group(0)
        if token not in tokens:
            tokens.append(token)
    return tokens


def _select_pool_raid(frame: "Frame", provision: dict, raid: str, selected_disk_count: int | None = None) -> None:
    logger.info(f"Provisioning: select pool RAID {raid}")
    next_button_selector = _require_selector(provision.get("storagemgr_next_button"), "provision.storagemgr_next_button")

    for candidate in _pool_raid_candidates(raid, selected_disk_count):
        for locator in (
            frame.get_by_text(candidate, exact=True).first,
            frame.get_by_text(candidate).first,
        ):
            try:
                if locator.is_visible(timeout=1_000):
                    if candidate != raid:
                        logger.info(f"Provisioning: using RAID option {candidate} for configured {raid}")
                    locator.click()
                    frame.page.wait_for_timeout(SHORT_UI_WAIT_MS)
                    return
            except Exception:
                continue

    next_button = frame.locator(next_button_selector).first
    try:
        if next_button.is_visible(timeout=1_000) and next_button.is_enabled(timeout=1_000):
            logger.info(f"Provisioning: pool RAID {raid} does not require an explicit click")
            return
    except Exception:
        pass

    raise ProvisionError(f"Storage-pool RAID option '{raid}' is not visible")


def _pool_raid_candidates(raid: str, selected_disk_count: int | None = None) -> list[str]:
    candidates: list[str] = []
    if selected_disk_count == 1 and raid != SINGLE_DISK_RAID_LABEL:
        candidates.append(SINGLE_DISK_RAID_LABEL)
    candidates.append(raid)
    return candidates


def _pool_creation_entry_is_visible(frame: "Frame", provision: dict) -> bool:
    entry_selectors = [
        _require_selector(provision.get("storagemgr_empty_create_button"), "provision.storagemgr_empty_create_button"),
        _require_selector(provision.get("storagemgr_header_create_button"), "provision.storagemgr_header_create_button"),
    ]
    for selector in entry_selectors:
        locator = frame.locator(selector).first
        try:
            if locator.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


def _dismiss_pool_creation_warning(frame: "Frame") -> bool:
    try:
        has_warning = bool(
            frame.evaluate(
                """(warningText) => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0
                            && rect.height > 0
                            && style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && style.opacity !== '0';
                    };
                    return Array.from(document.querySelectorAll('section, .ivu-modal, .ivu-modal-wrap'))
                        .some((el) => isVisible(el) && (el.innerText || '').includes(warningText));
                }""",
                POOL_CREATE_WARNING_TEXT,
            )
        )
        if not has_warning:
            return False
    except Exception:
        return False

    clicked = False
    try:
        clicked = bool(
            frame.evaluate(
                """([warningText, ackText]) => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0
                            && rect.height > 0
                            && style.display !== 'none'
                            && style.visibility !== 'hidden'
                            && style.opacity !== '0';
                    };
                    const scopes = Array.from(document.querySelectorAll('section, .ivu-modal, .ivu-modal-wrap'))
                        .filter((el) => isVisible(el) && (el.innerText || '').includes(warningText));
                    for (const scope of scopes) {
                        const target = Array.from(scope.querySelectorAll('button, .ivu-btn, span, div'))
                            .find((el) => isVisible(el) && (el.innerText || '').trim() === ackText);
                        if (target) {
                            target.click();
                            return true;
                        }
                    }
                    return false;
                }""",
                [POOL_CREATE_WARNING_TEXT, POOL_CREATE_WARNING_ACK_TEXT],
            )
        )
    except Exception:
        clicked = False

    if not clicked:
        frame.evaluate(
            """(warningText) => {
                const scopes = Array.from(document.querySelectorAll('section, .ivu-modal, .ivu-modal-wrap'))
                    .filter((el) => (el.innerText || '').includes(warningText));
                for (const scope of scopes) {
                    const root = scope.closest('section') || scope;
                    root.style.setProperty('display', 'none', 'important');
                }
                for (const mask of document.querySelectorAll('.ivu-modal-mask, .basic-mask')) {
                    mask.style.setProperty('display', 'none', 'important');
                }
            }""",
            POOL_CREATE_WARNING_TEXT,
        )

    frame.page.wait_for_timeout(SHORT_UI_WAIT_MS)
    return True


def _finish_pool_creation(page: "Page", frame: "Frame", provision: dict, admin: dict) -> None:
    next_button_selector = _require_selector(provision.get("storagemgr_next_button"), "provision.storagemgr_next_button")
    create_button_selector = _require_selector(provision.get("storagemgr_create_button"), "provision.storagemgr_create_button")
    delete_all_selector = _require_selector(provision.get("storagemgr_delete_all_button"), "provision.storagemgr_delete_all_button")
    password_input_selector = _require_selector(provision.get("modal_password_input"), "provision.modal_password_input")
    confirm_selector = _require_selector(provision.get("modal_confirm_button"), "provision.modal_confirm_button")

    logger.info("Provisioning: advance pool wizard review step")
    next_button = frame.locator(next_button_selector).first
    expect(next_button).to_be_visible(timeout=FRAME_WAIT_MS)
    next_button.click()
    page.wait_for_timeout(SHORT_UI_WAIT_MS)

    logger.info("Provisioning: advance pool wizard confirmation step")
    next_button = frame.locator(next_button_selector).first
    expect(next_button).to_be_visible(timeout=FRAME_WAIT_MS)
    next_button.click()
    page.wait_for_timeout(SHORT_UI_WAIT_MS)

    logger.info("Provisioning: submit pool creation")
    create_button = frame.locator(create_button_selector).first
    expect(create_button).to_be_visible(timeout=FRAME_WAIT_MS)
    create_button.click()

    logger.info("Provisioning: confirm delete-all-data acknowledgement")
    delete_all_button = frame.locator(delete_all_selector).first
    expect(delete_all_button).to_be_visible(timeout=FRAME_WAIT_MS)
    delete_all_button.click()

    logger.info("Provisioning: enter pool creation password")
    password_input = frame.locator(password_input_selector).first
    expect(password_input).to_be_visible(timeout=FRAME_WAIT_MS)
    password_input.fill(admin["password"])

    logger.info("Provisioning: confirm pool creation")
    confirm_button = frame.locator(confirm_selector).first
    expect(confirm_button).to_be_visible(timeout=FRAME_WAIT_MS)
    expect(confirm_button).to_be_enabled(timeout=FRAME_WAIT_MS)
    confirm_button.click()


def _find_matching_pool_summary_text(frame: "Frame", pool: dict) -> str | None:
    for summary_text in _list_pool_summary_texts(frame):
        if _pool_summary_matches(summary_text, pool):
            return summary_text
    return None


def _wait_for_matching_pool_summary_text(frame: "Frame", pool: dict, timeout_ms: int) -> str:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        summary_text = _find_matching_pool_summary_text(frame, pool)
        if summary_text:
            return summary_text
        frame.page.wait_for_timeout(500)
    raise ProvisionError("Storage pool summary did not appear in time after creation")


def _list_pool_summary_texts(frame: "Frame") -> list[str]:
    summaries: list[str] = []
    items = frame.locator(POOL_SUMMARY_SELECTOR)
    count = items.count()
    for index in range(count):
        locator = items.nth(index)
        try:
            if not locator.is_visible(timeout=250):
                continue
            text = _normalize_pool_summary(locator.inner_text(timeout=1_000))
            if text:
                summaries.append(text)
        except Exception:
            continue
    return summaries


def _pool_summary_matches(summary_text: str, pool: dict) -> bool:
    configured_disks = set(pool.get("disks") or [])
    if not configured_disks:
        return False
    summary_disks = set(POOL_DISK_TOKEN_RE.findall(summary_text))
    matching_disks = summary_disks & configured_disks
    if not matching_disks:
        return False

    raid = pool.get("raid")
    if raid and not _pool_summary_raid_matches(summary_text, str(raid), len(matching_disks)):
        return False
    return True


def _pool_summary_raid_matches(summary_text: str, raid: str, matched_disk_count: int) -> bool:
    return any(candidate in summary_text for candidate in _pool_raid_candidates(raid, matched_disk_count))


def _normalize_pool_summary(text: str) -> str:
    return " ".join(text.split())


def _open_share_create_modal(frame: "Frame", provision: dict) -> None:
    last_error: Exception | None = None
    modal_selector = _require_selector(provision.get("filemgr_share_modal"), "provision.filemgr_share_modal")

    for _ in range(3):
        _dismiss_filemgr_tutorial(frame.page, frame)

        empty_button = frame.locator(
            _require_selector(provision.get("filemgr_empty_create_button"), "provision.filemgr_empty_create_button")
        ).first

        if empty_button.count() and empty_button.is_visible():
            empty_button.click(force=True)
            if _share_modal_is_visible(frame, modal_selector, timeout_ms=4_000):
                return
            if _click_share_folder_menu_item(frame, provision, modal_selector):
                return
            try:
                frame.page.keyboard.press("Escape")
            except Exception:
                pass
            frame.page.wait_for_timeout(500)

        quick_add = frame.locator(
            _require_selector(provision.get("filemgr_quick_add_button"), "provision.filemgr_quick_add_button")
        ).first
        expect(quick_add).to_be_visible(timeout=FRAME_WAIT_MS)
        quick_add.click(force=True)
        frame.page.wait_for_timeout(300)

        share_item = frame.locator(
            _require_selector(provision.get("filemgr_quick_add_share_item"), "provision.filemgr_quick_add_share_item")
        ).first
        try:
            expect(share_item).to_be_visible(timeout=3_000)
            share_item.click(force=True, timeout=3_000)
            if _share_modal_is_visible(frame, modal_selector, timeout_ms=4_000):
                return
            if _click_share_folder_menu_item(frame, provision, modal_selector):
                return
            frame.page.keyboard.press("Escape")
            frame.page.wait_for_timeout(500)
        except Exception as exc:
            last_error = exc
            if _click_visible_text(frame, SHARE_FOLDER_MENU_TEXT):
                if _share_modal_is_visible(frame, modal_selector, timeout_ms=4_000):
                    return
            frame.page.keyboard.press("Escape")
            frame.page.wait_for_timeout(500)

    if last_error is not None:
        raise last_error
    raise ProvisionError("Could not open shared-folder create modal")


def _click_share_folder_menu_item(frame: "Frame", provision: dict, modal_selector: str) -> bool:
    share_item_selector = _require_selector(
        provision.get("filemgr_quick_add_share_item"),
        "provision.filemgr_quick_add_share_item",
    )
    share_item = frame.locator(share_item_selector).first

    try:
        if share_item.is_visible(timeout=1_000):
            share_item.click(force=True, timeout=3_000)
            return _share_modal_is_visible(frame, modal_selector, timeout_ms=4_000)
    except Exception:
        pass

    try:
        if _click_visible_text(frame, SHARE_FOLDER_MENU_TEXT):
            return _share_modal_is_visible(frame, modal_selector, timeout_ms=4_000)
    except Exception:
        pass

    return False


def _share_modal_is_visible(frame: "Frame", modal_selector: str, timeout_ms: int) -> bool:
    try:
        frame.locator(modal_selector).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _click_visible_text(frame: "Frame", text: str) -> bool:
    return bool(
        frame.evaluate(
            """(text) => {
                const isVisible = (el) => {
                    const rect = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return rect.width > 0
                        && rect.height > 0
                        && style.visibility !== 'hidden'
                        && style.display !== 'none'
                        && style.opacity !== '0';
                };
                const target = Array.from(document.querySelectorAll('button, a, li, span, div, p'))
                    .find((el) => isVisible(el) && (el.innerText || '').includes(text));
                if (!target) {
                    return false;
                }
                target.click();
                return true;
            }""",
            text,
        )
    )


def _wait_for_share_row(share_row, timeout_ms: int) -> bool:
    try:
        share_row.wait_for(state="visible", timeout=timeout_ms)
        return True
    except Exception:
        return False


def _handle_share_name_conflict(page: "Page", frame: "Frame", share_name: str, admin: dict) -> bool:
    if not _share_name_conflict_is_visible(frame, timeout_ms=1_000):
        return False

    if _share_exists_via_smb(page, share_name, admin):
        _dismiss_share_create_modal(frame)
        logger.info(f"  Shared folder ready: {share_name} (duplicate name verified via SMB)")
        return True

    raise ProvisionError(
        f"File manager reports duplicate shared-folder name '{share_name}', but SMB verification failed"
    )


def _share_name_conflict_is_visible(frame: "Frame", timeout_ms: int = 0) -> bool:
    deadline = time.monotonic() + (timeout_ms / 1000)
    while True:
        if _visible_text_present(frame, SHARE_NAME_CONFLICT_TEXT):
            return True
        if time.monotonic() >= deadline:
            return False
        frame.page.wait_for_timeout(200)


def _visible_text_present(frame: "Frame", text: str) -> bool:
    try:
        return bool(
            frame.evaluate(
                """(text) => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0
                            && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && style.opacity !== '0';
                    };
                    return Array.from(document.querySelectorAll('.arco-modal, .arco-form-message, span, div, p'))
                        .some((el) => isVisible(el) && (el.innerText || '').includes(text));
                }""",
                text,
            )
        )
    except Exception:
        return False


def _dismiss_share_create_modal(frame: "Frame") -> None:
    try:
        clicked = bool(
            frame.evaluate(
                """(cancelText) => {
                    const isVisible = (el) => {
                        const rect = el.getBoundingClientRect();
                        const style = window.getComputedStyle(el);
                        return rect.width > 0
                            && rect.height > 0
                            && style.visibility !== 'hidden'
                            && style.display !== 'none'
                            && style.opacity !== '0';
                    };
                    const modal = Array.from(document.querySelectorAll('.arco-modal'))
                        .find((el) => isVisible(el) && (el.innerText || '').includes(cancelText));
                    if (!modal) {
                        return false;
                    }
                    const cancel = Array.from(modal.querySelectorAll('button, span, div'))
                        .find((el) => isVisible(el) && (el.innerText || '').trim() === cancelText);
                    const close = modal.querySelector('.arco-modal-close-btn, .arco-modal-close-icon');
                    const target = cancel || close;
                    if (!target) {
                        return false;
                    }
                    target.click();
                    return true;
                }""",
                MODAL_CANCEL_TEXT,
            )
        )
    except Exception:
        clicked = False

    if not clicked:
        try:
            frame.page.keyboard.press("Escape")
        except Exception:
            pass
    frame.page.wait_for_timeout(SHORT_UI_WAIT_MS)


def _share_exists_via_smb(page: "Page", share_name: str, admin: dict) -> bool:
    host = urlparse(page.url).hostname
    if not host:
        return False

    cfg = SmbTransferConfig(
        unc_share=fr"\\{host}\{share_name}",
        local_dir=Path.cwd(),
        drive_letter="T",
        username=admin.get("username"),
        password=admin.get("password"),
    )
    try:
        try:
            run_powershell(build_unmount_script(cfg), timeout_s=15)
        except SmbTransferError:
            pass
        run_powershell(build_mount_script(cfg), timeout_s=20)
        return True
    except SmbTransferError:
        return False
    finally:
        try:
            run_powershell(build_unmount_script(cfg), timeout_s=15)
        except SmbTransferError:
            pass


def _open_app(page: "Page", desktop_selectors: dict, app: str) -> "Frame":
    app_selector = _require_selector(desktop_selectors.get("apps", {}).get(app), f"desktop_launchers.apps.{app}")
    frame_selector = _require_selector(desktop_selectors.get("frames", {}).get(app), f"desktop_launchers.frames.{app}")
    frame_selectors = _frame_selector_candidates(frame_selector, app)

    frame_selector, iframe = _find_visible_frame_locator(page, frame_selectors, timeout_ms=500)
    if iframe is not None and frame_selector is not None:
        _activate_window(page, frame_selector)
    else:
        launcher = _find_visible_locator(page, app_selector, timeout_ms=1_500)
        if launcher is None:
            logger.info(f"Desktop launcher for {app} is not visible; opening app library")
            _open_app_library(page)
            launcher = _find_visible_locator(page, app_selector, timeout_ms=FRAME_WAIT_MS)
        if launcher is None:
            raise ProvisionError(f"Desktop/app-library launcher for '{app}' did not appear")
        click_desktop_launcher(page, launcher, app)
        frame_selector, iframe = _wait_for_visible_frame_locator(page, frame_selectors, timeout_ms=FRAME_WAIT_MS)

    page.wait_for_timeout(SHORT_UI_WAIT_MS)
    frame = _wait_for_frame(page, iframe, app)
    frame.wait_for_load_state("domcontentloaded")
    if app == "storagemgr":
        dismiss_arco_app_guides(page, frame, "storage manager")
    return frame


def _frame_selector_candidates(frame_selector: str, app: str) -> list[str]:
    selectors = [frame_selector]
    if app == "storagemgr":
        for fallback in ('iframe[name^="storagemgr"]', 'iframe[name*="storagemgr"]', 'iframe[src*="/storagemgr/"]'):
            if fallback not in selectors:
                selectors.append(fallback)
    if app == "filemgr":
        for fallback in ('iframe[name^="filemgr"]', 'iframe[name*="filemgr"]', 'iframe[src*="/filemgr/"]'):
            if fallback not in selectors:
                selectors.append(fallback)
    return selectors


def _find_visible_frame_locator(page: "Page", frame_selectors: list[str], timeout_ms: int):
    deadline = time.monotonic() + (timeout_ms / 1000)
    while True:
        for selector in frame_selectors:
            locator = page.locator(selector).first
            try:
                if locator.count() and locator.is_visible(timeout=200):
                    return selector, locator
            except Exception:
                continue
        if time.monotonic() >= deadline:
            return None, None
        page.wait_for_timeout(200)


def _wait_for_visible_frame_locator(page: "Page", frame_selectors: list[str], timeout_ms: int):
    selector, locator = _find_visible_frame_locator(page, frame_selectors, timeout_ms)
    if selector is None or locator is None:
        raise ProvisionError(f"iframe did not appear for selectors: {', '.join(frame_selectors)}")
    return selector, locator


def _find_visible_locator(page: "Page", selector: str, timeout_ms: int):
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


def _open_app_library(page: "Page") -> None:
    dismiss_desktop_overlays(page, max_rounds=2, completion_wait_ms=1_000, idle_wait_ms=0)
    icons = page.locator(APP_LIBRARY_ICON_SELECTOR)
    try:
        if icons.count() >= 2:
            icons.nth(1).click(force=True, timeout=3_000)
        else:
            icons.first.click(force=True, timeout=3_000)
    except Exception:
        clicked = bool(
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
                    const icons = Array.from(document.querySelectorAll('.nav-home .nav-home-icon'))
                        .filter(isVisible);
                    const target = icons[1] || icons[0];
                    if (!target) {
                        return false;
                    }
                    target.click();
                    return true;
                }"""
            )
        )
        if not clicked:
            raise ProvisionError("Could not open the app library from the desktop toolbar")

    try:
        page.locator(APP_LIBRARY_ITEM_SELECTOR).first.wait_for(state="visible", timeout=5_000)
    except Exception:
        page.wait_for_timeout(SHORT_UI_WAIT_MS)


def _wait_for_frame(page: "Page", iframe_locator, app: str) -> "Frame":
    deadline = time.monotonic() + (FRAME_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        frame = page.frame(name=app)
        if frame is not None:
            return frame
        try:
            handle = iframe_locator.element_handle(timeout=500)
            frame = handle.content_frame() if handle is not None else None
        except Exception:
            frame = None
        if frame is not None:
            return frame
        page.wait_for_timeout(200)
    raise ProvisionError(f"iframe '{app}' did not appear on the desktop")


def _navigate(page: "Page", frame: "Frame", nav_selectors: dict, nav_keys: list[str]) -> None:
    app = frame.name or ""
    for nav_key in nav_keys:
        selector = _require_selector(nav_selectors.get(nav_key), f"provision_nav.{nav_key}")
        loc = frame.locator(selector).first
        expect(loc).to_be_visible(timeout=FRAME_WAIT_MS)
        if app == "storagemgr":
            dismiss_storage_guide_modal(frame, "storage manager")
        try:
            loc.click(timeout=5_000)
        except Exception:
            if app != "storagemgr":
                raise
            logger.info(f"Navigation '{nav_key}' in {app} was blocked; dismissing storage welcome modal and retrying")
            if not dismiss_storage_guide_modal(frame, "storage manager"):
                raise
            expect(loc).to_be_visible(timeout=FRAME_WAIT_MS)
            loc.click(timeout=5_000, force=True)
        page.wait_for_timeout(SHORT_UI_WAIT_MS)


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
        raise ProvisionError(f"Selector '{name}' is not configured")
    return selector


def _require_meta(value: str | None, name: str) -> str:
    if not value:
        raise ProvisionError(f"Metadata '{name}' is not configured")
    return value
