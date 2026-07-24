import json
from types import SimpleNamespace

import pytest

from src import cli
from src.utils.screenshot import session_dirs


class _ProgressRecorder:
    def __init__(self) -> None:
        self.sn_updates: list[str] = []
        self.stages: list[tuple[str, str]] = []

    def update_sn(self, sn: str) -> None:
        self.sn_updates.append(sn)

    def set_stage(self, stage: str, status: str = "running", **kwargs) -> None:
        self.stages.append((stage, status))


class _FakeLocator:
    def __init__(self, text: str) -> None:
        self.text = text

    def inner_text(self, timeout: int) -> str:
        return self.text


class _FakePage:
    def __init__(self, body: str = "", content: str = "", storage: str = "") -> None:
        self.body = body
        self.html = content
        self.storage = storage

    def title(self) -> str:
        return "UGOS"

    def locator(self, selector: str) -> _FakeLocator:
        assert selector == "body"
        return _FakeLocator(self.body)

    def content(self) -> str:
        return self.html

    def evaluate(self, script: str) -> str:
        return self.storage


def test_pool_creation_timeout_uses_pool_failure_stage() -> None:
    error = RuntimeError("Storage pool summary did not appear in time after creation")
    missing_disks_error = RuntimeError(
        "Storage-pool creation aborted by user: 存储池1 has no configured disks available"
    )
    missing_specific = RuntimeError(
        "Storage-pool creation aborted by user: 存储池2 has missing disks ['M.2硬盘2']"
    )

    assert cli.is_pool_creation_timeout_error(error)
    assert cli.is_pool_creation_timeout_error(missing_disks_error)
    assert cli.failure_stage_for_error(error) == "建池失败：摘要超时"
    assert cli.failure_stage_for_error(missing_disks_error) == "建池失败：硬盘配置不匹配"
    assert cli.failure_stage_for_error(missing_specific) == "建池失败：缺M.2硬盘2"


def test_ugos_not_ready_stage() -> None:
    error = RuntimeError("UGOS at 192.0.2.151:9999 did not become ready within 90s")
    assert cli.failure_stage_for_error(error) == "设备上线超时（90s）"


def test_service_starting_stuck_stage() -> None:
    error = RuntimeError(
        "UGOS setup page stayed on 'service starting' screen for 300s; last page: <empty>"
    )
    assert cli.failure_stage_for_error(error) == "UGOS 卡在服务启动中（300s）"


def test_sn_not_unbound_stage() -> None:
    assert cli.failure_stage_for_error(RuntimeError("SN未解绑，请先解绑SN")) == "SN 未解绑"


def test_form_already_submitted_stage() -> None:
    error = RuntimeError(
        "SN EC752JJ3825157A4 already has a submitted record for this form."
    )
    assert cli.failure_stage_for_error(error) == "表单已有提交记录"


def test_factory_reset_unconfirmed_uses_dedicated_stage_and_operator_category() -> None:
    error = RuntimeError(
        "Factory reset was initiated but completion was not confirmed before timeout"
    )

    assert cli.is_factory_reset_unconfirmed_error(error)
    assert cli.failure_stage_for_error(error) == "恢复出厂设置待确认"
    assert cli.classify_failure_category(error) == "operator"


def test_factory_reset_initiation_failure_uses_dedicated_stage() -> None:
    assert cli.failure_stage_for_error(RuntimeError("Could not initiate factory reset")) == "恢复出厂设置失败"


def test_factory_reset_retry_blocked_uses_terminal_operator_stage() -> None:
    error = RuntimeError(
        "A factory reset from a previous run must not be repeated: already confirmed"
    )

    assert cli.is_factory_reset_retry_blocked_error(error)
    assert cli.failure_stage_for_error(error) == "恢复出厂设置已确认，禁止重复执行"
    assert cli.classify_failure_category(error) == "operator"


def test_device_identity_error_is_operator_failure() -> None:
    error = RuntimeError("设备身份校验失败: wrong SN")

    assert cli.is_device_identity_error(error)
    assert cli.failure_stage_for_error(error) == cli.DEVICE_IDENTITY_FAILURE_STAGE
    assert cli.classify_failure_category(error) == "operator"


@pytest.mark.parametrize(
    "admin",
    [
        {},
        {"username": "CHANGE_ME", "password": "secret"},
        {"username": "factory", "password": "changeme"},
    ],
)
def test_admin_placeholders_fail_closed_before_nas_use(admin: dict) -> None:
    with pytest.raises(ValueError, match="Unsafe administrator configuration"):
        cli._validate_admin_credentials({"admin": admin})


def test_admin_credentials_accept_non_placeholder_values() -> None:
    cli._validate_admin_credentials({"admin": {"username": "factory", "password": "secret"}})


def test_page_host_guard_accepts_exact_verified_ip() -> None:
    assert (
        cli._verify_page_host(
            SimpleNamespace(url="http://192.0.2.168:9999/desktop"),
            "192.0.2.168",
            "provision",
        )
        == "192.0.2.168"
    )


def test_page_host_guard_rejects_redirect_to_other_nas() -> None:
    with pytest.raises(RuntimeError, match="page address changed before cleanup"):
        cli._verify_page_host(
            SimpleNamespace(url="http://192.0.2.169:9999/desktop"),
            "192.0.2.168",
            "cleanup",
        )


def test_explicit_ip_reserves_all_broadcast_pair_aliases(monkeypatch) -> None:
    hit = SimpleNamespace(
        address="192.0.2.169",
        sn="EC752VV42251611A",
        mac="AA:BB:CC:DD:EE:13",
        data={
            "pair": {
                "192.0.2.168": "AA:BB:CC:DD:EE:12",
                "192.0.2.169": "AA:BB:CC:DD:EE:13",
            }
        },
    )
    monkeypatch.setattr(cli.ugreen_broadcast, "discover", lambda **_kwargs: [hit])
    cli.ACTIVE_DEVICE_IPS.clear()

    try:
        ip, reserved, full_sn = cli._resolve_ip_for_task(
            "EC752VV42251611A",
            "192.0.2.169",
            {},
            _ProgressRecorder(),
        )
        assert ip == "192.0.2.169"
        assert reserved == {"192.0.2.168", "192.0.2.169"}
        assert full_sn == "EC752VV42251611A"
        assert cli._canonical_device_lock_ip(ip, reserved) == "192.0.2.168"
        assert cli.ACTIVE_DEVICE_IPS == reserved
    finally:
        cli._release_reserved_ips(set(cli.ACTIVE_DEVICE_IPS))


def test_explicit_ip_rejects_mismatched_broadcast_identity(monkeypatch) -> None:
    hit = SimpleNamespace(
        address="192.0.2.168",
        sn="EC752VV42251699Z",
        mac="AA:BB:CC:DD:EE:12",
        data={},
    )
    monkeypatch.setattr(cli.ugreen_broadcast, "discover", lambda **_kwargs: [hit])

    with pytest.raises(RuntimeError, match=cli.DEVICE_IDENTITY_FAILURE_STAGE):
        cli._resolve_ip_for_task(
            "EC752VV42251611A",
            "192.0.2.168",
            {},
            _ProgressRecorder(),
        )


def test_capture_speed_failure_stage() -> None:
    error = RuntimeError("Capture failed at page 'hdd_read': 99 MB/s < 120 MB/s")
    assert cli.failure_stage_for_error(error) == "HDD 读取 失败：99 MB/s"


def test_capture_screenshot_only_stage() -> None:
    error = RuntimeError("Capture failed at page 'hdd_read'")
    assert cli.failure_stage_for_error(error) == "HDD 读取 截图失败"


def test_unknown_error_falls_back() -> None:
    assert cli.failure_stage_for_error(RuntimeError("something else")) == "测试失败"


def test_select_candidate_reserves_visible_sn_aliases(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.168": "登录 DXP4800-7046",
            "192.0.2.169": "登录 DXP4800-7046",
            "192.0.2.170": "登录 DXP4800-DFFD",
        },
    )

    selection = cli._select_candidate_for_sn("7046", ["192.0.2.168", "192.0.2.169", "192.0.2.170"], 9999, {})

    assert selection is not None
    assert selection.ip == "192.0.2.168"
    assert selection.reserved_ips == frozenset({"192.0.2.168", "192.0.2.169"})


def test_select_candidate_prefers_4800plus_eth0_when_visible_sn_matches(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.107": (
                "UGREEN broadcast\n"
                "IP=192.0.2.107\n"
                "SN=EC752JJ16241A92E\n"
                "MAC=AA:BB:CC:0C:73:FE\n"
                "interface=eth1\n"
                "model=DXP4800 Plus"
            ),
            "192.0.2.108": (
                "UGREEN broadcast\n"
                "IP=192.0.2.108\n"
                "SN=EC752JJ16241A92E\n"
                "MAC=AA:BB:CC:0C:73:FD\n"
                "interface=eth0\n"
                "model=DXP4800 Plus"
            ),
        },
    )

    selection = cli._select_candidate_for_sn("A92E", ["192.0.2.107", "192.0.2.108"], 9999, {})

    assert selection is not None
    assert selection.ip == "192.0.2.108"
    assert selection.reserved_ips == frozenset({"192.0.2.107", "192.0.2.108"})


def test_auto_placeholder_can_upgrade_to_real_sn(tmp_path) -> None:
    output_root = tmp_path / "screenshot"
    dirs = session_dirs(output_root, "AUTOC0A800D6")
    # session_dirs is intentionally lazy; an artifact writer materializes it.
    dirs["base"].mkdir(parents=True, exist_ok=True)
    (dirs["base"] / "marker.txt").write_text("kept", encoding="utf-8")
    progress = _ProgressRecorder()
    report: dict = {"sn": "AUTOC0A800D6"}

    sn, new_dirs = cli._upgrade_task_sn(
        current_sn="AUTOC0A800D6",
        discovered_sn="HB670EE07251E54E",
        output_root=output_root,
        dirs=dirs,
        progress=progress,
        report=report,
        setup_file_log=False,
    )

    assert sn == "HB670EE07251E54E"
    assert report["sn"] == "HB670EE07251E54E"
    assert report["input_sn"] == "AUTOC0A800D6"
    assert progress.sn_updates == ["HB670EE07251E54E"]
    assert (new_dirs["base"] / "marker.txt").read_text(encoding="utf-8") == "kept"
    assert not (output_root / "AUTOC0A800D6").exists()


def test_full_sn_can_never_upgrade_to_a_different_full_sn_with_same_tail(tmp_path) -> None:
    current_sn = "EC752VV42251611A"
    wrong_sn = "EC752ZZ99251611A"

    with pytest.raises(RuntimeError, match=cli.DEVICE_IDENTITY_FAILURE_STAGE):
        cli._upgrade_task_sn(
            current_sn=current_sn,
            discovered_sn=wrong_sn,
            output_root=tmp_path,
            dirs=session_dirs(tmp_path, current_sn),
            progress=_ProgressRecorder(),
            report={"sn": current_sn},
            setup_file_log=False,
        )


@pytest.mark.parametrize("invalid", ["611A", "AUTOC0A800D6", "NOT-A-SERIAL"])
def test_sn_upgrade_rejects_non_full_discovery_values(tmp_path, invalid: str) -> None:
    with pytest.raises(RuntimeError, match=cli.DEVICE_IDENTITY_FAILURE_STAGE):
        cli._upgrade_task_sn(
            current_sn="AUTOC0A800D6",
            discovered_sn=invalid,
            output_root=tmp_path,
            dirs=session_dirs(tmp_path, "AUTOC0A800D6"),
            progress=_ProgressRecorder(),
            report={"sn": "AUTOC0A800D6"},
            setup_file_log=False,
        )


def test_sn_upgrade_blocks_unsafe_reset_report_before_relocation(tmp_path) -> None:
    full_sn = "EC752VV42251611A"
    target = tmp_path / full_sn
    target.mkdir(parents=True)
    (target / "test_report.json").write_text(
        json.dumps({"status": "failed", "factory_reset": "initiated"}),
        encoding="utf-8",
    )
    short_dirs = session_dirs(tmp_path, "611A")

    with pytest.raises(cli.reset_factory_flow.FactoryResetUnconfirmed):
        cli._upgrade_task_sn(
            current_sn="611A",
            discovered_sn=full_sn,
            output_root=tmp_path,
            dirs=short_dirs,
            progress=_ProgressRecorder(),
            report={"sn": "611A"},
            setup_file_log=False,
        )

    assert json.loads((target / "test_report.json").read_text(encoding="utf-8"))[
        "factory_reset"
    ] == "initiated"


@pytest.mark.parametrize("state", ["starting", "initiated", "uncertain"])
def test_any_unsafe_prior_reset_state_blocks_retest(state: str) -> None:
    with pytest.raises(cli.reset_factory_flow.FactoryResetUnconfirmed):
        cli._raise_for_unsafe_prior_factory_reset(
            {"status": "failed", "factory_reset": state},
            sn="EC752VV42251611A",
        )


def test_confirmed_reset_with_incomplete_report_blocks_repeat() -> None:
    with pytest.raises(cli.reset_factory_flow.FactoryResetRetryBlocked):
        cli._raise_for_unsafe_prior_factory_reset(
            {"status": "running", "factory_reset": "confirmed"},
            sn="EC752VV42251611A",
        )


def test_verify_nas_identity_requires_exact_full_sn_for_selected_ip() -> None:
    hits = [SimpleNamespace(address="192.0.2.10", sn="EC752VV42251611A")]

    assert (
        cli._verify_nas_identity_at_ip(
            "192.0.2.10",
            "EC752VV42251611A",
            discover_fn=lambda **_kwargs: hits,
            wait_fn=lambda _seconds: None,
        )
        == "EC752VV42251611A"
    )


def test_verify_nas_identity_accepts_selected_ip_advertised_by_other_interface() -> None:
    hits = [
        SimpleNamespace(
            address="192.0.2.107",
            sn="EC752VV42251611A",
            mac="AA:BB:CC:DD:EE:01",
            data={
                "pair": {
                    "192.0.2.107": "AA:BB:CC:DD:EE:01",
                    "192.0.2.108": "AA:BB:CC:DD:EE:02",
                }
            },
        )
    ]

    assert (
        cli._verify_nas_identity_at_ip(
            "192.0.2.108",
            "EC752VV42251611A",
            discover_fn=lambda **_kwargs: hits,
            wait_fn=lambda _seconds: None,
        )
        == "EC752VV42251611A"
    )


def test_verify_nas_identity_rejects_conflicting_macs_for_same_interface() -> None:
    hits = [
        SimpleNamespace(
            address="192.0.2.107",
            sn="EC752VV42251611A",
            mac="AA:BB:CC:DD:EE:01",
            data={"pair": {"192.0.2.108": "AA:BB:CC:DD:EE:02"}},
        ),
        SimpleNamespace(
            address="192.0.2.109",
            sn="EC752VV42251611A",
            mac="AA:BB:CC:DD:EE:03",
            data={"pair": {"192.0.2.108": "AA:BB:CC:DD:EE:99"}},
        ),
    ]

    with pytest.raises(RuntimeError, match="conflicting MACs"):
        cli._verify_nas_identity_at_ip(
            "192.0.2.108",
            "EC752VV42251611A",
            discover_fn=lambda **_kwargs: hits,
            wait_fn=lambda _seconds: None,
        )


def test_verify_nas_identity_rejects_mac_change_between_stages() -> None:
    first_hits = [
        SimpleNamespace(
            address="192.0.2.108",
            sn="EC752VV42251611A",
            mac="AA:BB:CC:DD:EE:02",
            data={},
        )
    ]
    second_hits = [
        SimpleNamespace(
            address="192.0.2.108",
            sn="EC752VV42251611A",
            mac="AA:BB:CC:DD:EE:99",
            data={},
        )
    ]
    observed_macs: set[str] = set()

    assert cli._verify_nas_identity_at_ip(
        "192.0.2.108",
        "EC752VV42251611A",
        discover_fn=lambda **_kwargs: first_hits,
        wait_fn=lambda _seconds: None,
        observed_macs_out=observed_macs,
    ) == "EC752VV42251611A"

    with pytest.raises(RuntimeError, match="MAC fingerprint changed"):
        cli._verify_nas_identity_at_ip(
            "192.0.2.108",
            "EC752VV42251611A",
            discover_fn=lambda **_kwargs: second_hits,
            wait_fn=lambda _seconds: None,
            expected_macs=frozenset(observed_macs),
        )


def test_verify_nas_identity_rejects_wrong_full_sn_before_ui() -> None:
    hits = [SimpleNamespace(address="192.0.2.10", sn="EC752VV42251699Z")]

    with pytest.raises(RuntimeError, match=cli.DEVICE_IDENTITY_FAILURE_STAGE):
        cli._verify_nas_identity_at_ip(
            "192.0.2.10",
            "EC752VV42251611A",
            discover_fn=lambda **_kwargs: hits,
            wait_fn=lambda _seconds: None,
        )


def test_verify_nas_identity_rejects_missing_identity_after_retries() -> None:
    calls = 0

    def discover(**_kwargs):
        nonlocal calls
        calls += 1
        return []

    with pytest.raises(RuntimeError, match="could not bind IP"):
        cli._verify_nas_identity_at_ip(
            "192.0.2.10",
            "611A",
            discover_fn=discover,
            wait_fn=lambda _seconds: None,
        )

    assert calls == 3


def test_auto_placeholder_ignores_cached_page_sn_without_visible_sn() -> None:
    page = _FakePage(storage='{"localStorage":{"sn":"TT6PHVOC471JQ7C9YZTB2BSDZAV"}}')

    assert cli._extract_full_sn_from_page(page, "AUTOC0A800DF") is None


def test_auto_placeholder_ignores_generic_visible_page_sn() -> None:
    page = _FakePage(body="SN: EC752JJ172517046")

    assert cli._extract_full_sn_from_page(page, "AUTOC0A800DF") is None


def test_scanned_tail_can_use_cached_page_sn_when_tail_matches() -> None:
    page = _FakePage(storage='{"localStorage":{"sn":"HB670EE02251AF1F2"}}')

    assert cli._extract_full_sn_from_page(page, "F1F2") == "HB670EE02251AF1F2"



def test_select_candidate_learns_full_ec_sn(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.168": "序列号：EC4800ABCDEF7046",
        },
    )

    selection = cli._select_candidate_for_sn("7046", ["192.0.2.168"], 9999, {})

    assert selection is not None
    assert selection.full_sn == "EC4800ABCDEF7046"


def test_select_candidate_learns_full_sn_from_ugreen_broadcast(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.103": "UGREEN broadcast\nIP=192.0.2.103\nSN=HB670EE02251AF1F2\nMAC=AA:BB:CC:DD:EE:FF",
        },
    )

    selection = cli._select_candidate_for_sn("F1F2", ["192.0.2.103"], 9999, {})

    assert selection is not None
    assert selection.ip == "192.0.2.103"
    assert selection.full_sn == "HB670EE02251AF1F2"


def test_broadcast_pair_projection_allows_selecting_4800plus_fast_port(monkeypatch) -> None:
    monkeypatch.setattr(
        cli.ugreen_broadcast,
        "discover",
        lambda **_kwargs: [
            SimpleNamespace(
                address="192.0.2.213",
                sn="EC752VV42251611A",
                mac="AA:BB:CC:DD:EE:13",
                data={
                    "model": "DXP4800 Plus",
                    "interface": "eth1",
                    "pair": {
                        "192.0.2.212": "AA:BB:CC:DD:EE:12",
                        "192.0.2.213": "AA:BB:CC:DD:EE:13",
                    },
                },
            )
        ],
    )

    texts = cli._probe_ugreen_broadcast_identity_texts(
        ["192.0.2.212", "192.0.2.213"]
    )
    selection = cli._select_candidate_for_sn(
        "611A",
        ["192.0.2.212", "192.0.2.213"],
        9999,
        {},
    )

    assert "MAC=AA:BB:CC:DD:EE:12" in texts["192.0.2.212"]
    assert "interface=eth0" in texts["192.0.2.212"]
    assert "interface=eth1" in texts["192.0.2.213"]
    assert selection is not None
    assert selection.ip == "192.0.2.212"
    assert selection.reserved_ips == frozenset({"192.0.2.212", "192.0.2.213"})


def test_select_candidate_rejects_uninitialized_candidate_with_mismatched_broadcast_sn(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.211": (
                "UGREEN broadcast\n"
                "IP=192.0.2.211\n"
                "SN=EC752JJ1725110A3\n"
                "绿联云 未初始化 欢迎使用绿联云存储"
            ),
        },
    )

    assert cli._select_candidate_for_sn("E4D3", ["192.0.2.211"], 9999, {}) is None


def test_select_candidate_matches_truncated_4800_plus_label(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.168": "登录 DXP4800 Plus-704",
        },
    )

    selection = cli._select_candidate_for_sn("7046", ["192.0.2.168"], 9999, {})

    assert selection is not None
    assert selection.ip == "192.0.2.168"


def test_select_candidate_reserves_neighbor_setup_port_without_visible_sn(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.168": "绿联云 命名您的绿联云 序列号： 设备名 下一步",
            "192.0.2.169": "绿联云 命名您的绿联云 序列号：EC752JJ172517046 设备名 下一步",
        },
    )

    selection = cli._select_candidate_for_sn("7046", ["192.0.2.168", "192.0.2.169"], 9999, {})

    assert selection is not None
    assert selection.ip == "192.0.2.169"
    assert selection.reserved_ips == frozenset({"192.0.2.168", "192.0.2.169"})
    assert selection.full_sn == "EC752JJ172517046"


def test_select_candidate_groups_consecutive_uninitialized_ips(monkeypatch) -> None:
    uninitialized = "绿联云 未初始化 欢迎使用绿联云存储 我们将引导您完成设备的初始化过程"
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.168": uninitialized,
            "192.0.2.169": uninitialized,
        },
    )

    selection = cli._select_candidate_for_sn("7046", ["192.0.2.168", "192.0.2.169"], 9999, {})

    assert selection is not None
    assert selection.ip == "192.0.2.168"
    assert selection.reserved_ips == frozenset({"192.0.2.168", "192.0.2.169"})


def test_select_candidate_waits_while_service_is_starting(monkeypatch) -> None:
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.169": "绿联云 服务启动中 您可以尝试手动刷新页面 刷新",
        },
    )

    assert cli._select_candidate_for_sn("7046", ["192.0.2.169"], 9999, {}) is None


def test_select_candidate_waits_when_multiple_uninitialized_groups(monkeypatch) -> None:
    uninitialized = "缁胯仈浜?鏈垵濮嬪寲 娆㈣繋浣跨敤缁胯仈浜戝瓨鍌?鎴戜滑灏嗗紩瀵兼偍瀹屾垚璁惧鐨勫垵濮嬪寲杩囩▼"
    monkeypatch.setattr(
        cli,
        "_probe_candidate_identity_texts",
        lambda candidates, port, browser_cfg: {
            "192.0.2.168": uninitialized,
            "192.0.2.169": uninitialized,
            "192.0.2.180": uninitialized,
        },
    )

    assert cli._select_candidate_for_sn("7046", ["192.0.2.168", "192.0.2.169", "192.0.2.180"], 9999, {}) is None


def test_device_process_lease_key_unifies_full_and_short_serials() -> None:
    assert cli._device_process_lease_key("EC752VV42251611A") == "device-sn-tail:611A"
    assert cli._device_process_lease_key("611a") == "device-sn-tail:611A"
    assert cli._device_process_lease_key("AUTO") is None


def test_run_test_holds_process_lease_before_loading_report_or_config(monkeypatch) -> None:
    events: list[str] = []
    lock = object()
    token = object()

    def acquire(key, _cancel, *, description):
        events.append(f"acquire:{key}")
        return lock, token

    def release(observed_lock, observed_token, *, description):
        assert observed_lock is lock
        assert observed_token is token
        events.append("release")

    def fail_load(_root):
        events.append("load-config")
        raise RuntimeError("bad config")

    monkeypatch.setattr(cli, "_acquire_process_lease_until_cancelled", acquire)
    monkeypatch.setattr(cli, "_release_process_lease", release)
    monkeypatch.setattr(cli, "load_configs", fail_load)

    with pytest.raises(RuntimeError, match="bad config"):
        cli.run_test("EC752VV42251611A", setup_file_log=False)

    assert events == ["acquire:device-sn-tail:611A", "load-config", "release"]


def test_auto_run_holds_global_claim_before_loading_report_or_config(monkeypatch) -> None:
    events: list[str] = []
    lock = object()
    token = object()

    def acquire(key, *, description):
        events.append(f"acquire:{key}")
        return lock, token

    def release(observed_lock, observed_token, *, description):
        assert observed_lock is lock
        assert observed_token is token
        events.append("release")

    def fail_load(_root):
        events.append("load-config")
        raise RuntimeError("bad config")

    monkeypatch.setattr(cli, "_acquire_process_lease_or_fail", acquire)
    monkeypatch.setattr(cli, "_release_process_lease", release)
    monkeypatch.setattr(cli, "load_configs", fail_load)

    with pytest.raises(RuntimeError, match="bad config"):
        cli.run_test("AUTOC0A800D6", setup_file_log=False)

    assert events == [f"acquire:{cli.AUTO_DEVICE_PROCESS_LEASE_KEY}", "load-config", "release"]


def test_busy_auto_claim_fails_before_loading_report_or_config(monkeypatch) -> None:
    def fail_claim(key, *, description):
        assert key == cli.AUTO_DEVICE_PROCESS_LEASE_KEY
        raise RuntimeError("duplicate AUTO run")

    monkeypatch.setattr(cli, "_acquire_process_lease_or_fail", fail_claim)
    monkeypatch.setattr(
        cli,
        "load_configs",
        lambda _root: pytest.fail("busy AUTO claim must fail before reading configuration or reports"),
    )

    with pytest.raises(RuntimeError, match="duplicate AUTO run"):
        cli.run_test("AUTOC0A800D6", setup_file_log=False)


def test_auto_real_sn_claim_is_non_waiting() -> None:
    key = cli._device_process_lease_key("EC752VV42251611A")
    assert key is not None
    owner = cli.InterProcessLock(key)
    owner_token = owner.try_acquire()
    assert owner_token is not None

    try:
        with pytest.raises(RuntimeError, match="refusing a duplicate AUTO run"):
            cli._acquire_process_lease_or_fail(key, description="NAS EC752VV42251611A")
    finally:
        assert owner.release(owner_token) is True
