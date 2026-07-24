from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.flows import reset_factory


SN = "EC752VV42251611A"


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def wait(self, milliseconds: int) -> None:
        self.value += milliseconds / 1000


def test_wait_for_confirmation_requires_two_stable_samples() -> None:
    clock = _Clock()
    probes: list[str] = []

    result = reset_factory._wait_for_confirmation(
        expected_sn=SN,
        old_ip="192.0.2.10",
        deadline=10,
        discover_fn=lambda: [reset_factory._ResetCandidate("192.0.2.10", True)],
        probe_state_fn=lambda ip: (
            probes.append(ip) or reset_factory._ResetPageState("setup_wizard", SN)
        ),
        wait_fn=clock.wait,
        now_fn=clock.now,
        stable_interval_s=2,
        poll_ms=1_000,
    )

    assert result == reset_factory.FactoryResetResult("192.0.2.10", SN, False)
    assert probes == ["192.0.2.10", "192.0.2.10", "192.0.2.10"]


def test_wait_for_confirmation_follows_same_sn_to_new_ip() -> None:
    clock = _Clock()

    def probe(ip: str) -> reset_factory._ResetPageState:
        if ip == "192.0.2.10":
            raise TimeoutError("old address rebooting")
        return reset_factory._ResetPageState("setup_wizard", SN)

    result = reset_factory._wait_for_confirmation(
        expected_sn=SN,
        old_ip="192.0.2.10",
        deadline=10,
        discover_fn=lambda: [reset_factory._ResetCandidate("192.0.2.44", True)],
        probe_state_fn=probe,
        wait_fn=clock.wait,
        now_fn=clock.now,
        stable_interval_s=1,
        poll_ms=1_000,
    )

    assert result == reset_factory.FactoryResetResult("192.0.2.44", SN, True)


def test_wait_for_confirmation_rejects_probe_redirect_to_other_host() -> None:
    clock = _Clock()

    def probe(ip: str) -> reset_factory._ResetPageState:
        reset_factory._verify_probe_page_host(
            SimpleNamespace(url="http://192.0.2.99:9999/setup"),
            ip,
        )
        return reset_factory._ResetPageState("setup_wizard", SN)

    with pytest.raises(reset_factory.FactoryResetUnconfirmed, match="verification page address changed"):
        reset_factory._wait_for_confirmation(
            expected_sn=SN,
            old_ip="192.0.2.10",
            deadline=10,
            discover_fn=lambda: [reset_factory._ResetCandidate("192.0.2.10", True)],
            probe_state_fn=probe,
            wait_fn=clock.wait,
            now_fn=clock.now,
            stable_interval_s=1,
            poll_ms=1_000,
        )

    assert clock.value == 0.0


def test_wait_for_confirmation_allows_same_sn_to_alternate_between_interfaces() -> None:
    clock = _Clock()
    discoveries = iter(
        [
            [reset_factory._ResetCandidate("192.0.2.10", True)],
            [reset_factory._ResetCandidate("192.0.2.11", True)],
        ]
    )

    result = reset_factory._wait_for_confirmation(
        expected_sn=SN,
        old_ip="192.0.2.10",
        deadline=5,
        discover_fn=lambda: next(discoveries),
        probe_state_fn=lambda _ip: reset_factory._ResetPageState("setup_wizard"),
        wait_fn=clock.wait,
        now_fn=clock.now,
        stable_interval_s=1,
        poll_ms=1_000,
    )

    assert result.ip == "192.0.2.11"


def test_wait_for_confirmation_does_not_reuse_stale_broadcast_identity() -> None:
    clock = _Clock()
    discoveries = iter(
        [
            [reset_factory._ResetCandidate("192.0.2.10", True)],
            [],
            [],
        ]
    )

    with pytest.raises(reset_factory.FactoryResetUnconfirmed):
        reset_factory._wait_for_confirmation(
            expected_sn=SN,
            old_ip="192.0.2.10",
            deadline=3,
            discover_fn=lambda: next(discoveries, []),
            probe_state_fn=lambda _ip: reset_factory._ResetPageState("setup_wizard"),
            wait_fn=clock.wait,
            now_fn=clock.now,
            stable_interval_s=1,
            poll_ms=1_000,
        )


def test_wait_for_confirmation_never_accepts_probe_completed_after_deadline() -> None:
    clock = _Clock()

    def slow_probe(_ip: str) -> reset_factory._ResetPageState:
        clock.wait(2_000)
        return reset_factory._ResetPageState("setup_wizard", SN)

    with pytest.raises(reset_factory.FactoryResetUnconfirmed):
        reset_factory._wait_for_confirmation(
            expected_sn=SN,
            old_ip="192.0.2.10",
            deadline=1,
            discover_fn=lambda: [reset_factory._ResetCandidate("192.0.2.10", True)],
            probe_state_fn=slow_probe,
            wait_fn=clock.wait,
            now_fn=clock.now,
            stable_interval_s=0,
            poll_ms=1_000,
        )


def test_wait_for_confirmation_caps_poll_to_remaining_deadline() -> None:
    clock = _Clock()
    waits: list[int] = []

    def wait(milliseconds: int) -> None:
        waits.append(milliseconds)
        clock.wait(milliseconds)

    with pytest.raises(reset_factory.FactoryResetUnconfirmed):
        reset_factory._wait_for_confirmation(
            expected_sn=SN,
            old_ip="192.0.2.10",
            deadline=0.1,
            discover_fn=lambda: [],
            probe_state_fn=lambda _ip: reset_factory._ResetPageState("unknown"),
            wait_fn=wait,
            now_fn=clock.now,
            poll_ms=2_000,
        )

    assert len(waits) == 1
    assert 1 <= waits[0] <= 100


def test_wait_for_confirmation_rejects_wrong_visible_sn() -> None:
    clock = _Clock()

    with pytest.raises(reset_factory.FactoryResetUnconfirmed, match="completion was not confirmed"):
        reset_factory._wait_for_confirmation(
            expected_sn=SN,
            old_ip="192.0.2.10",
            deadline=3,
            discover_fn=lambda: [reset_factory._ResetCandidate("192.0.2.10", False)],
            probe_state_fn=lambda _ip: reset_factory._ResetPageState(
                "setup_wizard", "EC752VV42251699Z"
            ),
            wait_fn=clock.wait,
            now_fn=clock.now,
            stable_interval_s=1,
            poll_ms=1_000,
        )


def test_wait_for_confirmation_resets_stability_after_login() -> None:
    clock = _Clock()
    states = iter(
        [
            reset_factory._ResetPageState("setup_wizard", SN),
            reset_factory._ResetPageState("login", SN),
            reset_factory._ResetPageState("setup_wizard", SN),
            reset_factory._ResetPageState("setup_wizard", SN),
        ]
    )

    result = reset_factory._wait_for_confirmation(
        expected_sn=SN,
        old_ip="192.0.2.10",
        deadline=10,
        discover_fn=lambda: [reset_factory._ResetCandidate("192.0.2.10", True)],
        probe_state_fn=lambda _ip: next(states),
        wait_fn=clock.wait,
        now_fn=clock.now,
        stable_interval_s=0,
        poll_ms=1_000,
    )

    assert result.ip == "192.0.2.10"
    assert clock.value == 3


def test_wait_for_confirmation_honors_cancellation() -> None:
    clock = _Clock()

    with pytest.raises(reset_factory.FactoryResetUnconfirmed, match="cancelled"):
        reset_factory._wait_for_confirmation(
            expected_sn=SN,
            old_ip="192.0.2.10",
            deadline=10,
            discover_fn=lambda: [],
            probe_state_fn=lambda _ip: pytest.fail("probe should not run"),
            wait_fn=clock.wait,
            cancel_requested_cb=lambda: True,
            now_fn=clock.now,
        )


class _Locator:
    def __init__(self, *, visible: bool = False, text: str = "") -> None:
        self.first = self
        self.visible = visible
        self.text = text

    def is_visible(self, timeout: int) -> bool:
        assert timeout == 500
        return self.visible

    def inner_text(self, timeout: int) -> str:
        assert timeout == 1_000
        return self.text


class _Surface:
    def __init__(self, text: str, visible: set[str] | None = None) -> None:
        self.text = text
        self.visible = visible or set()

    def locator(self, selector: str) -> _Locator:
        if selector == "body":
            return _Locator(text=self.text)
        return _Locator(visible=selector in self.visible)


class _Page:
    def __init__(self, *surfaces: _Surface) -> None:
        self.frames = list(surfaces)


def _selectors() -> dict:
    return {
        "setup_wizard": {
            "page1_next_button": "button.next-step-btn",
            "device_name_input": 'input[type="text"]',
        },
        "login": {"page_marker": ".login-wrapper", "password_input": 'input[type="password"]'},
        "desktop_launchers": {
            "apps": {"ctlmgr": ".desktop-launcher"},
            "frames": {"ctlmgr": 'iframe[name="ctlmgr"]'},
        },
    }


def test_classify_probe_page_finds_new_iframe_wizard_and_sn() -> None:
    page = _Page(
        _Surface(""),
        _Surface(
            f"命名您的绿联云 序列号：{SN}",
            {"button.next-step-btn", 'input[type="text"]'},
        ),
    )

    assert reset_factory._classify_probe_page(page, _selectors()) == reset_factory._ResetPageState(
        "setup_wizard", SN
    )


def test_classify_probe_page_accepts_combined_wizard_with_password_inputs() -> None:
    page = _Page(
        _Surface(
            f"命名您的绿联云 序列号：{SN}",
            {
                "button.next-step-btn",
                'input[type="text"]',
                'input[type="password"]',
            },
        )
    )

    assert reset_factory._classify_probe_page(page, _selectors()).state == "setup_wizard"


def test_classify_probe_page_never_accepts_login_as_wizard() -> None:
    page = _Page(
        _Surface(
            f"命名您的绿联云 序列号：{SN}",
            {"button.next-step-btn", 'input[type="password"]', ".login-wrapper"},
        )
    )

    assert reset_factory._classify_probe_page(page, _selectors()).state == "login"


def test_classify_probe_page_stops_querying_when_deadline_is_exhausted() -> None:
    class NoQuerySurface:
        def locator(self, _selector: str):
            pytest.fail("expired classification must not start another locator query")

    page = _Page(NoQuerySurface())

    assert reset_factory._classify_probe_page(
        page,
        _selectors(),
        deadline=0,
        now_fn=lambda: 0,
    ).state == "starting"


def test_run_calls_initiated_once_then_returns_confirmed_result(monkeypatch) -> None:
    callbacks: list[str] = []
    expected = reset_factory.FactoryResetResult("192.0.2.10", SN, True)

    def initiate(_page, _selectors, _admin, **kwargs) -> None:
        kwargs["on_initiated"]()

    monkeypatch.setattr(reset_factory, "_initiate", initiate)
    monkeypatch.setattr(reset_factory, "_verify_completion", lambda *_args, **_kwargs: expected)

    result = reset_factory.run(
        object(),
        {},
        {},
        nas_url="http://192.0.2.10:9999",
        expected_sn=SN,
        on_initiated=lambda: callbacks.append("initiated"),
    )

    assert result == expected
    assert callbacks == ["initiated"]


def test_verify_reset_target_requires_exact_sn_at_exact_ip(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory.ugreen_broadcast,
        "discover",
        lambda **_kwargs: [
            SimpleNamespace(
                address="192.0.2.10",
                sn=SN,
                mac="AA:BB:CC:DD:EE:01",
                data={"pair": {"192.0.2.11": "AA:BB:CC:DD:EE:02"}},
            ),
            SimpleNamespace(address="192.0.2.11", sn=SN),
        ],
    )

    assert reset_factory._verify_reset_target(
        SimpleNamespace(url="http://192.0.2.10:9999/desktop"),
        expected_sn=SN,
        expected_ip="192.0.2.10",
    ) == frozenset(
        {
            "AABBCCDDEE01",
            "AABBCCDDEE02",
        }
    )


def test_verify_reset_target_accepts_ip_advertised_by_other_interface(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory.ugreen_broadcast,
        "discover",
        lambda **_kwargs: [
            SimpleNamespace(
                address="192.0.2.107",
                sn=SN,
                mac="AA:BB:CC:DD:EE:01",
                data={
                    "pair": {
                        "192.0.2.107": "AA:BB:CC:DD:EE:01",
                        "192.0.2.108": "AA:BB:CC:DD:EE:02",
                    }
                },
            )
        ],
    )

    assert reset_factory._verify_reset_target(
        SimpleNamespace(url="http://192.0.2.108:9999/desktop"),
        expected_sn=SN,
        expected_ip="192.0.2.108",
    ) == frozenset({"AABBCCDDEE01", "AABBCCDDEE02"})


def test_verify_reset_target_rejects_conflicting_macs_for_same_interface(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory.ugreen_broadcast,
        "discover",
        lambda **_kwargs: [
            SimpleNamespace(
                address="192.0.2.107",
                sn=SN,
                mac="AA:BB:CC:DD:EE:01",
                data={"pair": {"192.0.2.108": "AA:BB:CC:DD:EE:02"}},
            ),
            SimpleNamespace(
                address="192.0.2.109",
                sn=SN,
                mac="AA:BB:CC:DD:EE:03",
                data={"pair": {"192.0.2.108": "AA:BB:CC:DD:EE:99"}},
            ),
        ],
    )

    with pytest.raises(reset_factory.FactoryResetError, match="MAC conflict"):
        reset_factory._verify_reset_target(
            SimpleNamespace(url="http://192.0.2.108:9999/desktop"),
            expected_sn=SN,
            expected_ip="192.0.2.108",
        )


def test_verify_reset_target_rejects_wrong_sn_before_submit(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory.ugreen_broadcast,
        "discover",
        lambda **_kwargs: [
            SimpleNamespace(address="192.0.2.10", sn="EC752VV42251699Z"),
        ],
    )

    with pytest.raises(reset_factory.FactoryResetError, match="identity mismatch"):
        reset_factory._verify_reset_target(
            SimpleNamespace(url="http://192.0.2.10:9999/desktop"),
            expected_sn=SN,
            expected_ip="192.0.2.10",
        )


def test_verify_reset_target_rejects_page_host_change(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory.ugreen_broadcast,
        "discover",
        lambda **_kwargs: pytest.fail("broadcast should not run"),
    )

    with pytest.raises(reset_factory.FactoryResetError, match="address changed"):
        reset_factory._verify_reset_target(
            SimpleNamespace(url="http://192.0.2.99:9999/desktop"),
            expected_sn=SN,
            expected_ip="192.0.2.10",
        )


def test_cancel_check_raises_before_destructive_submit() -> None:
    with pytest.raises(reset_factory.FactoryResetCancelled, match="before final submit"):
        reset_factory._raise_if_cancelled_before_submit(lambda: True)


def test_initiate_checks_cancel_immediately_before_submit(monkeypatch) -> None:
    class ActionLocator:
        def __init__(self) -> None:
            self.first = self
            self.clicks = 0
            self.checked = False

        def click(self) -> None:
            self.clicks += 1

        def is_checked(self) -> bool:
            return self.checked

        def check(self, **_kwargs) -> None:
            self.checked = True

        def evaluate(self, _script: str) -> None:
            self.checked = True

        def fill(self, _value: str) -> None:
            pass

    class Frame:
        def __init__(self) -> None:
            self.locators: dict[str, ActionLocator] = {}

        def locator(self, selector: str) -> ActionLocator:
            return self.locators.setdefault(selector, ActionLocator())

    class Expectation:
        def __getattr__(self, _name):
            return lambda **_kwargs: None

    frame = Frame()
    page = SimpleNamespace(wait_for_timeout=lambda _milliseconds: None)
    selectors = {
        "capture_nav": {"ctlmgr_update_menu": "update"},
        "factory_reset": {
            "restore_tab": "tab",
            "restore_button": "restore",
            "first_modal_root": "first-modal",
            "first_modal_acknowledge_checkbox": "acknowledge",
            "first_modal_confirm_button": "first-confirm",
            "password_modal_root": "password-modal",
            "password_input": "password",
            "password_submit_button": "submit",
        },
    }
    monkeypatch.setattr(reset_factory, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(reset_factory, "_open_app", lambda *_args, **_kwargs: frame)
    monkeypatch.setattr(reset_factory, "expect", lambda _locator: Expectation())
    monkeypatch.setattr(
        reset_factory,
        "_verify_reset_target",
        lambda *_args, **_kwargs: pytest.fail("identity check must not run after cancel"),
    )

    with pytest.raises(reset_factory.FactoryResetCancelled):
        reset_factory._initiate(
            page,
            selectors,
            {"password": "secret"},
            expected_sn=SN,
            expected_ip="192.0.2.10",
            cancel_requested_cb=lambda: True,
            on_initiated=lambda: pytest.fail("submit callback must not run"),
        )

    assert frame.locator("submit").clicks == 0


def test_verify_completion_uses_and_closes_isolated_context(monkeypatch) -> None:
    expected = reset_factory.FactoryResetResult("192.0.2.10", SN, False)

    class ProbePage:
        def wait_for_timeout(self, _milliseconds: int) -> None:
            pass

    class ProbeContext:
        def __init__(self) -> None:
            self.page = ProbePage()
            self.closed = False

        def new_page(self):
            return self.page

        def close(self) -> None:
            self.closed = True

    class Browser:
        def __init__(self) -> None:
            self.context = ProbeContext()
            self.kwargs = None

        def new_context(self, **kwargs):
            self.kwargs = kwargs
            return self.context

    browser = Browser()
    page = SimpleNamespace(context=SimpleNamespace(browser=browser))
    monkeypatch.setattr(
        reset_factory,
        "_wait_for_confirmation",
        lambda **_kwargs: expected,
    )

    result = reset_factory._verify_completion(
        page,
        {},
        expected_sn=SN,
        old_ip="192.0.2.10",
        scheme="http",
        port=9999,
        timeout_s=10,
        cancel_requested_cb=None,
    )

    assert result == expected
    assert browser.kwargs == {"locale": "zh-CN", "service_workers": "block"}
    assert browser.context.closed


def test_verify_completion_closes_isolated_context_when_new_page_fails() -> None:
    class ProbeContext:
        def __init__(self) -> None:
            self.closed = False

        def new_page(self):
            raise RuntimeError("new page failed")

        def close(self) -> None:
            self.closed = True

    probe_context = ProbeContext()
    browser = SimpleNamespace(new_context=lambda **_kwargs: probe_context)
    page = SimpleNamespace(context=SimpleNamespace(browser=browser))

    with pytest.raises(reset_factory.FactoryResetUnconfirmed, match="fresh verification page"):
        reset_factory._verify_completion(
            page,
            {},
            expected_sn=SN,
            old_ip="192.0.2.10",
            scheme="http",
            port=9999,
            timeout_s=10,
        )

    assert probe_context.closed


def test_wait_for_confirmation_requires_mac_when_pre_reset_fingerprint_exists() -> None:
    clock = _Clock()

    with pytest.raises(reset_factory.FactoryResetUnconfirmed):
        reset_factory._wait_for_confirmation(
            expected_sn=SN,
            old_ip="192.0.2.10",
            deadline=2,
            discover_fn=lambda: [
                reset_factory._ResetCandidate(
                    "192.0.2.10",
                    False,
                    "AABBCCDDEE99",
                )
            ],
            probe_state_fn=lambda _ip: reset_factory._ResetPageState(
                "setup_wizard",
                SN,
            ),
            wait_fn=clock.wait,
            now_fn=clock.now,
            stable_interval_s=0,
            poll_ms=1_000,
            expected_macs=frozenset({"AABBCCDDEE01"}),
        )


def test_discover_candidates_matches_exact_sn_and_pre_reset_mac(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory.ugreen_broadcast,
        "discover",
        lambda **_kwargs: [
            SimpleNamespace(
                address="192.0.2.10",
                sn=SN,
                mac="AA:BB:CC:DD:EE:01",
            ),
            SimpleNamespace(
                address="192.0.2.11",
                sn=SN,
                mac="AA:BB:CC:DD:EE:99",
            ),
        ],
    )

    candidates = reset_factory._discover_candidates(
        SN,
        "192.0.2.10",
        expected_macs=frozenset({"AABBCCDDEE01"}),
    )

    assert candidates == [
        reset_factory._ResetCandidate("192.0.2.10", True, "AABBCCDDEE01"),
        reset_factory._ResetCandidate("192.0.2.11", False, "AABBCCDDEE99"),
    ]


def test_discover_candidates_uses_remaining_timeout_budget(monkeypatch) -> None:
    timeouts: list[float] = []

    def discover(**kwargs):
        timeouts.append(kwargs["timeout"])
        return []

    monkeypatch.setattr(reset_factory.ugreen_broadcast, "discover", discover)

    reset_factory._discover_candidates(
        SN,
        "192.0.2.10",
        timeout_s=0.25,
    )

    assert timeouts == [0.25]


def test_discover_candidates_probes_paired_interface_that_did_not_reply(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory.ugreen_broadcast,
        "discover",
        lambda **_kwargs: [
            SimpleNamespace(
                address="192.0.2.107",
                sn=SN,
                mac="AA:BB:CC:DD:EE:01",
                data={
                    "pair": {
                        "192.0.2.107": "AA:BB:CC:DD:EE:01",
                        "192.0.2.108": "AA:BB:CC:DD:EE:02",
                    }
                },
            )
        ],
    )

    assert reset_factory._discover_candidates(
        SN,
        "192.0.2.108",
        expected_macs=frozenset({"AABBCCDDEE01", "AABBCCDDEE02"}),
    ) == [
        reset_factory._ResetCandidate("192.0.2.108", True, "AABBCCDDEE02"),
        reset_factory._ResetCandidate("192.0.2.107", True, "AABBCCDDEE01"),
    ]


def test_discover_candidates_rejects_conflicting_macs_for_same_interface(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory.ugreen_broadcast,
        "discover",
        lambda **_kwargs: [
            SimpleNamespace(
                address="192.0.2.107",
                sn=SN,
                mac="AA:BB:CC:DD:EE:01",
                data={"pair": {"192.0.2.108": "AA:BB:CC:DD:EE:02"}},
            ),
            SimpleNamespace(
                address="192.0.2.109",
                sn=SN,
                mac="AA:BB:CC:DD:EE:03",
                data={"pair": {"192.0.2.108": "AA:BB:CC:DD:EE:99"}},
            ),
        ],
    )

    with pytest.raises(reset_factory.FactoryResetUnconfirmed, match="MAC conflict"):
        reset_factory._discover_candidates(
            SN,
            "192.0.2.108",
            expected_macs=frozenset({"AABBCCDDEE02"}),
        )


def test_wait_for_confirmation_does_not_downgrade_mac_conflict_to_visible_sn() -> None:
    clock = _Clock()
    probes: list[str] = []

    def discover():
        raise reset_factory._ResetIdentityConflict(
            f"{reset_factory.FACTORY_RESET_UNCONFIRMED_MARKER}: MAC conflict"
        )

    with pytest.raises(reset_factory.FactoryResetUnconfirmed, match="MAC conflict"):
        reset_factory._wait_for_confirmation(
            expected_sn=SN,
            old_ip="192.0.2.108",
            deadline=10,
            discover_fn=discover,
            probe_state_fn=lambda ip: (
                probes.append(ip)
                or reset_factory._ResetPageState("setup_wizard", SN)
            ),
            wait_fn=clock.wait,
            now_fn=clock.now,
        )

    assert probes == []


def test_run_rejects_partial_sn_before_initiating(monkeypatch) -> None:
    monkeypatch.setattr(
        reset_factory,
        "_initiate",
        lambda *_args, **_kwargs: pytest.fail("reset must not be initiated"),
    )

    with pytest.raises(reset_factory.FactoryResetError, match="full NAS serial number"):
        reset_factory.run(
            object(),
            {},
            {},
            nas_url="http://192.0.2.10:9999",
            expected_sn="611A",
        )
