from src.flows import system_update


class FakePage:
    def __init__(self, *, reload_fails: bool = False) -> None:
        self.reload_fails = reload_fails
        self.calls: list[tuple[str, dict]] = []

    def reload(self, **kwargs) -> None:
        self.calls.append(("reload", kwargs))
        if self.reload_fails:
            raise RuntimeError("reload failed")

    def goto(self, url: str, **kwargs) -> None:
        self.calls.append(("goto", {"url": url, **kwargs}))

    def wait_for_timeout(self, timeout_ms: int) -> None:
        self.calls.append(("wait_for_timeout", {"timeout_ms": timeout_ms}))


def test_refresh_update_page_prefers_browser_reload() -> None:
    page = FakePage()

    system_update._refresh_update_page(page, "http://nas.local:9999", "unit test")

    assert [name for name, _ in page.calls] == ["reload", "wait_for_timeout"]


def test_refresh_update_page_falls_back_to_goto_when_reload_fails() -> None:
    page = FakePage(reload_fails=True)

    system_update._refresh_update_page(page, "http://nas.local:9999", "unit test")

    assert [name for name, _ in page.calls] == ["reload", "goto", "wait_for_timeout"]
    assert page.calls[1][1]["url"] == "http://nas.local:9999"
