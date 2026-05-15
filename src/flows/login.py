from __future__ import annotations

import time
from typing import TYPE_CHECKING

from playwright.sync_api import expect

from ..utils.logger import logger

if TYPE_CHECKING:
    from playwright.sync_api import Page


class LoginError(RuntimeError):
    pass


UNFLASHED_TITLE = "\u672a\u5237\u673a"
UNFLASHED_MESSAGE = "\u672a\u5237\u673a\uff0c\u8bf7\u5148\u5237\u673a"
PASSWORD_ERROR_MARKERS = (
    "\u5bc6\u7801\u9519\u8bef",
    "\u5bc6\u7801\u4e0d\u6b63\u786e",
    "\u5bc6\u7801\u6709\u8bef",
    "\u7528\u6237\u540d\u6216\u5bc6\u7801\u9519\u8bef",
    "\u8d26\u53f7\u6216\u5bc6\u7801\u9519\u8bef",
)
LOGIN_RESULT_WAIT_MS = 15_000


def _require(selector: str, name: str) -> str:
    if not selector or selector == "TODO":
        raise LoginError(f"Selector '{name}' is not configured in selectors.yml")
    return selector


def run(page: "Page", nas_url: str, admin: dict, selectors: dict) -> None:
    logger.info(f"Logging in at {nas_url}")
    page.goto(nas_url, wait_until="domcontentloaded")

    lg = selectors.get("login", {})
    username = page.locator(_require(lg.get("username_input"), "login.username_input")).first
    password = page.locator(_require(lg.get("password_input"), "login.password_input")).first
    submit = page.locator(_require(lg.get("submit_button"), "login.submit_button")).first

    expect(username).to_be_visible(timeout=15_000)
    expect(password).to_be_visible(timeout=15_000)
    username.fill(admin["username"])
    password.fill(admin["password"])
    submit.click()

    marker = _require(lg.get("post_login_marker"), "login.post_login_marker")
    _wait_for_login_result(page, _post_login_success_selectors(selectors, marker))
    logger.info("Login succeeded")


def _wait_for_login_result(page: "Page", success_selectors: tuple[str, ...]) -> None:
    deadline = time.monotonic() + (LOGIN_RESULT_WAIT_MS / 1000)
    last_body = ""
    while time.monotonic() < deadline:
        if _page_has_password_error(page):
            raise LoginError(UNFLASHED_MESSAGE)
        if _login_success_visible(page, success_selectors):
            return
        page.wait_for_timeout(250)
        last_body = _body_text(page)
    if _body_has_password_error(last_body):
        raise LoginError(UNFLASHED_MESSAGE)
    raise LoginError(f"Login did not complete within {LOGIN_RESULT_WAIT_MS // 1000}s")


def _post_login_success_selectors(selectors: dict, marker: str) -> tuple[str, ...]:
    desktop = selectors.get("desktop_launchers", {}) or {}
    candidates = [
        *(desktop.get("apps", {}) or {}).values(),
        *(desktop.get("frames", {}) or {}).values(),
    ]
    if not _is_generic_page_marker(marker):
        candidates.append(marker)

    seen: set[str] = set()
    result: list[str] = []
    for selector in candidates:
        if not selector or selector == "TODO" or selector in seen:
            continue
        seen.add(selector)
        result.append(selector)

    if not result:
        result.append(marker)
    return tuple(result)


def _is_generic_page_marker(selector: str) -> bool:
    compact = "".join(str(selector or "").split()).lower()
    return compact in {"body", "html", "*", "body.lang-zh-cn"}


def _login_success_visible(page: "Page", success_selectors: tuple[str, ...]) -> bool:
    return any(_is_visible(page, selector) for selector in success_selectors)


def _page_has_password_error(page: "Page") -> bool:
    return _body_has_password_error(_body_text(page))


def _body_has_password_error(text: str) -> bool:
    return any(marker in text for marker in PASSWORD_ERROR_MARKERS)


def _body_text(page: "Page") -> str:
    try:
        return page.locator("body").inner_text(timeout=500)
    except Exception:
        return ""


def _is_visible(page: "Page", selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible(timeout=500)
    except Exception:
        return False
