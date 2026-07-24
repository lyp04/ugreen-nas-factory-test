from src.flows import setup_wizard


class _FakePage:
    def __init__(self, body: str = "", content: str = "", storage: str = "") -> None:
        self.body = body
        self.html = content
        self.storage = storage

    def title(self) -> str:
        return "UGOS"

    def content(self) -> str:
        return self.html

    def evaluate(self, script: str) -> str:
        return self.storage


def test_initial_wait_reason_detects_service_starting(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard,
        "_body_text",
        lambda page: "服务启动中 您可以尝试手动刷新页面 刷新",
    )

    assert setup_wizard._initial_wait_reason(object()) == "service_starting"


def test_initial_wait_reason_detects_missing_device_info(monkeypatch) -> None:
    monkeypatch.setattr(
        setup_wizard,
        "_body_text",
        lambda page: "设备必要信息缺失 请与厂商售后客服取得联系",
    )

    assert setup_wizard._initial_wait_reason(object()) == "missing_device_info"


def test_sn_unbound_message_is_not_mojibake() -> None:
    assert setup_wizard.SN_UNBOUND_MESSAGE == "SN\u672a\u89e3\u7ed1\uff0c\u8bf7\u5148\u89e3\u7ed1SN"


def test_setup_auto_placeholder_ignores_cached_page_sn(monkeypatch) -> None:
    page = _FakePage(storage='{"localStorage":{"sn":"MR4N74WSTWGLF3Q4BJZ"}}')
    monkeypatch.setattr(setup_wizard, "_body_text", lambda page: "")

    assert setup_wizard._extract_full_sn_from_page(page, "AUTOC0A800DE") is None


def test_setup_auto_placeholder_ignores_visible_page_sn(monkeypatch) -> None:
    page = _FakePage()
    monkeypatch.setattr(setup_wizard, "_body_text", lambda page: "SN: EC752JJ172517046")

    assert setup_wizard._extract_full_sn_from_page(page, "AUTOC0A800DE") is None


def test_wait_for_desktop_logs_in_when_init_reboot_returns_login(monkeypatch) -> None:
    page = object()
    admin = {"username": "factory", "password": "secret"}
    selectors = {"login": {"password_input": "#password"}}
    login_calls: list[tuple] = []

    monkeypatch.setattr(setup_wizard.time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(setup_wizard, "_is_desktop_visible", lambda *_args: False)
    monkeypatch.setattr(
        setup_wizard,
        "_is_visible",
        lambda candidate_page, selector: candidate_page is page and selector == "#password",
    )
    monkeypatch.setattr(
        setup_wizard.login_flow,
        "run",
        lambda *args: login_calls.append(args),
    )

    setup_wizard._wait_for_desktop(page, "http://192.0.2.10:9999", admin, selectors)

    assert login_calls == [(page, "http://192.0.2.10:9999", admin, selectors)]


def test_combined_setup_page_does_not_repeat_legacy_admin_page(monkeypatch) -> None:
    class Page:
        def goto(self, *_args, **_kwargs) -> None:
            pass

    page = Page()
    admin = {"username": "factory", "password": "secret"}
    calls: list[str] = []

    monkeypatch.setattr(setup_wizard, "_extract_full_sn_from_page", lambda *_args: None)
    monkeypatch.setattr(setup_wizard, "_detect_initial_state", lambda *_args: "wizard")
    monkeypatch.setattr(setup_wizard, "_page0_intro_start_if_present", lambda *_args: None)
    monkeypatch.setattr(setup_wizard, "_is_combined_device_admin_page", lambda *_args: True)
    monkeypatch.setattr(
        setup_wizard,
        "_combined_device_admin_page",
        lambda *_args: calls.append("combined"),
    )
    monkeypatch.setattr(
        setup_wizard,
        "_page2_admin_account",
        lambda *_args: calls.append("legacy_admin"),
    )
    monkeypatch.setattr(setup_wizard, "_page3_skip_phone", lambda *_args: None)
    monkeypatch.setattr(setup_wizard, "_page4_update_mode_and_init", lambda *_args: None)
    monkeypatch.setattr(setup_wizard, "_wait_for_desktop", lambda *_args: None)
    monkeypatch.setattr(setup_wizard, "_dismiss_post_setup_overlays_best_effort", lambda *_args: None)

    setup_wizard.run(page, "http://192.0.2.10:9999", admin, {}, sn="EC752VV42251611A")

    assert calls == ["combined"]
