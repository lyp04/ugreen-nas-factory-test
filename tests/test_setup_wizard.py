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
