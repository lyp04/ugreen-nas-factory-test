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
