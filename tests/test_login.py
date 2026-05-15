from src.flows import login


class _FakeLocator:
    def __init__(self, visible: bool) -> None:
        self._visible = visible

    @property
    def first(self):
        return self

    def is_visible(self, timeout: int = 0) -> bool:
        return self._visible


class _FakePage:
    def __init__(self, visible_selectors: set[str]) -> None:
        self.visible_selectors = visible_selectors

    def locator(self, selector: str) -> _FakeLocator:
        return _FakeLocator(selector in self.visible_selectors)


def test_password_error_maps_to_unflashed_message() -> None:
    assert login._body_has_password_error("用户名或密码错误")
    assert login.UNFLASHED_MESSAGE == "未刷机，请先刷机"


def test_non_password_error_does_not_map_to_unflashed_message() -> None:
    assert not login._body_has_password_error("服务启动中，请稍后刷新")


def test_generic_body_marker_is_not_login_success_without_desktop() -> None:
    desktop_selector = 'div[moduleid="com.ugreen.ctlmgr"] .click-region'
    success_selectors = login._post_login_success_selectors(
        {"desktop_launchers": {"apps": {"ctlmgr": desktop_selector}}},
        "body.lang-zh-CN",
    )

    assert "body.lang-zh-CN" not in success_selectors
    assert not login._login_success_visible(_FakePage({"body.lang-zh-CN"}), success_selectors)
    assert login._login_success_visible(_FakePage({desktop_selector}), success_selectors)
