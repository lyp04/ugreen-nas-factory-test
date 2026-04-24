import json
import queue
import threading
from types import SimpleNamespace

from src import gui as gui_module
from src.gui import FactoryTestGUI
from src.gui import DeviceTask


def _gui_for_output(tmp_path):
    gui = FactoryTestGUI.__new__(FactoryTestGUI)
    gui.config = {"output_dir": str(tmp_path)}
    gui.project_root = tmp_path
    gui.devices = {}
    return gui


def test_existing_image_directory_blocks_auto_scan_by_sn(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    image_dir = tmp_path / "HB670EE07251E54E" / "图片"
    image_dir.mkdir(parents=True)
    (image_dir / "HB670EE07251E54E_system_update_20260430_120004.png").write_bytes(b"png")

    assert gui._has_completed_output_for_sn("E54E")


def test_success_report_blocks_auto_scan_by_sn_not_reused_ip(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    sn_root = tmp_path / "HB670EE07251E54E"
    sn_root.mkdir()
    (sn_root / "test_report.json").write_text(
        json.dumps(
            {
                "status": "success",
                "sn": "HB670EE07251E54E",
                "nas_ip": "192.168.0.214",
                "nas_reserved_ips": ["192.168.0.215"],
            }
        ),
        encoding="utf-8",
    )

    assert gui._has_completed_output_for_sn("E54E")
    assert not gui._has_known_ip("192.168.0.214")
    assert not gui._has_known_ip("192.168.0.215")


def test_failed_report_does_not_block_auto_scan(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    sn_root = tmp_path / "HB670EE07251E54E"
    sn_root.mkdir()
    (sn_root / "test_report.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "sn": "HB670EE07251E54E",
                "nas_ip": "192.168.0.214",
            }
        ),
        encoding="utf-8",
    )

    assert not gui._has_completed_output_for_sn("E54E")
    assert not gui._has_known_ip("192.168.0.214")


def test_current_queue_ip_still_blocks_auto_scan_duplicate(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    gui.devices = {
        "task-1": DeviceTask(
            task_id="task-1",
            sn="HB670EE072512F12",
            requested_ip="192.168.0.191",
            mode="setup",
            cleanup_before_finish=True,
            factory_reset_before_finish=True,
        )
    }

    assert gui._has_known_ip("192.168.0.191")


def test_cancelling_task_is_still_active(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    task = DeviceTask(
        task_id="task-1",
        sn="HB670EE072512F12",
        requested_ip="192.168.0.191",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="cancelling",
    )
    task.cancel_event.set()
    gui.devices = {"task-1": task}

    assert gui._active_task_count() == 1
    assert task.cancel_event.is_set()


def test_gui_recognizes_manual_abort_errors(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)

    assert gui._is_user_abort_error(RuntimeError("Test cancelled by user"))
    assert gui._is_user_abort_error(RuntimeError("Storage-pool creation aborted by user"))
    assert gui._is_user_abort_error(RuntimeError("Previous-step auto seed cancelled by user"))



def test_pool_creation_timeout_fails_without_auto_retry(monkeypatch, tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    gui.ui_queue = queue.Queue()
    task = DeviceTask(
        task_id="task-1",
        sn="EC752JJ21251E4D3",
        requested_ip="192.168.0.232",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        max_attempts=2,
    )
    calls = 0
    error = "Storage pool summary did not appear in time after creation"

    def fake_run_test(*_args, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["progress_cb"](
            {
                "type": "finished",
                "status": "failed",
                "stage": "建池失败",
                "error": error,
            }
        )
        raise RuntimeError(error)

    monkeypatch.setattr(gui_module, "run_test", fake_run_test)

    gui._run_test_task(task)

    events = []
    while not gui.ui_queue.empty():
        events.append(gui.ui_queue.get())

    finished = [event for event in events if event.get("type") == "finished"]
    assert calls == 1
    assert finished == [
        {
            "type": "finished",
            "status": "failed",
            "stage": "建池失败",
            "error": error,
            "task_id": "task-1",
            "sn": "EC752JJ21251E4D3",
            "attempt": 1,
        }
    ]
    assert not any(event.get("status") == "retrying" for event in events)


def test_unflashed_password_error_fails_without_auto_retry(monkeypatch, tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    gui.ui_queue = queue.Queue()
    task = DeviceTask(
        task_id="task-1",
        sn="EC752JJ21251E4D3",
        requested_ip="192.168.0.232",
        mode="login",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        max_attempts=2,
    )
    calls = 0
    error = gui_module.UNFLASHED_MESSAGE

    def fake_run_test(*_args, **kwargs):
        nonlocal calls
        calls += 1
        kwargs["progress_cb"](
            {
                "type": "finished",
                "status": "failed",
                "stage": gui_module.UNFLASHED_TITLE,
                "error": error,
            }
        )
        raise RuntimeError(error)

    monkeypatch.setattr(gui_module, "run_test", fake_run_test)

    gui._run_test_task(task)

    events = []
    while not gui.ui_queue.empty():
        events.append(gui.ui_queue.get())

    finished = [event for event in events if event.get("type") == "finished"]
    assert calls == 1
    assert finished == [
        {
            "type": "finished",
            "status": "failed",
            "stage": gui_module.UNFLASHED_TITLE,
            "error": error,
            "task_id": "task-1",
            "sn": "EC752JJ21251E4D3",
            "attempt": 1,
        }
    ]
    assert not any(event.get("status") == "retrying" for event in events)


def test_unflashed_password_error_shows_popup_once(monkeypatch, tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    task = DeviceTask(
        task_id="task-1",
        sn="EC752JJ21251E4D3",
        requested_ip="192.168.0.232",
        mode="login",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
    )
    shown = []
    monkeypatch.setattr(gui_module.messagebox, "showerror", lambda title, message: shown.append((title, message)))

    gui._show_failure_alert_if_needed(task, gui_module.UNFLASHED_MESSAGE)
    gui._show_failure_alert_if_needed(task, gui_module.UNFLASHED_MESSAGE)

    assert shown == [(gui_module.UNFLASHED_TITLE, gui_module.UNFLASHED_MESSAGE)]


def test_auto_scan_sn_uses_only_valid_broadcast_sn(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)

    assert gui._auto_scan_sn("192.168.0.244", "HB670EE00000001A", "") == "HB670EE00000001A"
    assert gui._auto_scan_sn("192.168.0.244", "ECLGGEDQ8TB", "") == "AUTOC0A800F4"


def test_auto_scan_groups_consecutive_auto_placeholder_ports(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)

    devices = gui._auto_scan_devices_from_candidates(["192.168.0.222", "192.168.0.223"], {}, set())

    assert devices == []


def test_auto_scan_groups_broadcast_pair_ports(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    hits = {
        "192.168.0.222": SimpleNamespace(
            sn="",
            mac="AA:BB:CC:DD:EE:01",
            data={"pair": {"192.168.0.222": "AA:BB:CC:DD:EE:01", "192.168.0.223": "AA:BB:CC:DD:EE:02"}},
        ),
        "192.168.0.223": SimpleNamespace(sn="", mac="AA:BB:CC:DD:EE:02", data={}),
    }

    devices = gui._auto_scan_devices_from_candidates(["192.168.0.222", "192.168.0.223"], hits, set())

    assert devices == []


def test_auto_scan_groups_same_broadcast_sn_and_prefers_4800plus_10g_port(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    hits = {
        "192.168.0.104": SimpleNamespace(
            address="192.168.0.104",
            sn="EC752JJ232517D76",
            mac="AA:BB:CC:76:CB:6D",
            data={
                "interface": "eth0",
                "model": "DXP4800 Plus",
                "pair": {
                    "192.168.0.104": "AA:BB:CC:76:CB:6D",
                    "192.168.0.105": "AA:BB:CC:76:CB:6E",
                },
            },
        ),
        "192.168.0.105": SimpleNamespace(
            address="192.168.0.105",
            sn="EC752JJ232517D76",
            mac="AA:BB:CC:76:CB:6E",
            data={
                "interface": "eth1",
                "model": "DXP4800 Plus",
                "pair": {
                    "192.168.0.104": "AA:BB:CC:76:CB:6D",
                    "192.168.0.105": "AA:BB:CC:76:CB:6E",
                },
            },
        ),
    }

    devices = gui._auto_scan_devices_from_candidates(["192.168.0.104", "192.168.0.105"], hits, set())

    assert devices == [
        {
            "ip": "192.168.0.104",
            "sn": "EC752JJ232517D76",
            "mac": "AA:BB:CC:76:CB:6D",
            "reserved_ips": ["192.168.0.104", "192.168.0.105"],
            "interface": "eth0",
        }
    ]


def test_auto_scan_can_choose_4800plus_eth0_from_pair_when_only_eth1_replied(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    hits = {
        "192.168.0.213": SimpleNamespace(
            address="192.168.0.213",
            sn="EC752JJ38251DFD8",
            mac="AA:BB:CC:A6:34:B1",
            data={
                "interface": "eth1",
                "model": "DXP4800 Plus",
                "pair": {
                    "192.168.0.212": "AA:BB:CC:A6:34:B0",
                    "192.168.0.213": "AA:BB:CC:A6:34:B1",
                },
            },
        )
    }

    devices = gui._auto_scan_devices_from_candidates(["192.168.0.213"], hits, set())

    assert devices == [
        {
            "ip": "192.168.0.212",
            "sn": "EC752JJ38251DFD8",
            "mac": "AA:BB:CC:A6:34:B0",
            "reserved_ips": ["192.168.0.212", "192.168.0.213"],
            "interface": "eth0",
        }
    ]


def test_auto_scan_prefers_4800plus_eth0_over_explicit_10g_marker(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    hits = {
        "192.168.0.104": SimpleNamespace(
            address="192.168.0.104",
            sn="EC752JJ232517D76",
            mac="AA:BB:CC:76:CB:6D",
            data={"interface": "eth0", "model": "DXP4800 Plus"},
        ),
        "192.168.0.105": SimpleNamespace(
            address="192.168.0.105",
            sn="EC752JJ232517D76",
            mac="AA:BB:CC:76:CB:6E",
            data={"interface": "eth1", "model": "DXP4800 Plus", "link": "10000 Mbps"},
        ),
    }

    devices = gui._auto_scan_devices_from_candidates(["192.168.0.104", "192.168.0.105"], hits, set())

    assert devices[0]["ip"] == "192.168.0.104"


def test_auto_scan_pair_blocks_duplicate_when_known_alias_not_candidate(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    hits = {
        "192.168.0.105": SimpleNamespace(
            address="192.168.0.105",
            sn="EC752JJ232517D76",
            mac="AA:BB:CC:76:CB:6E",
            data={
                "interface": "eth1",
                "model": "DXP4800 Plus",
                "pair": {
                    "192.168.0.104": "AA:BB:CC:76:CB:6D",
                    "192.168.0.105": "AA:BB:CC:76:CB:6E",
                },
            },
        )
    }

    devices = gui._auto_scan_devices_from_candidates(["192.168.0.105"], hits, {"192.168.0.104"})

    assert devices == []


def test_auto_scan_skips_auto_placeholder_without_visible_sn(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)

    devices = gui._auto_scan_devices_from_candidates(["192.168.0.104"], {}, set())

    assert devices == []


def test_auto_scan_skips_link_local_candidate_even_if_worker_returns_it(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)

    devices = gui._auto_scan_devices_from_candidates(["169.254.250.115"], {}, set())

    assert devices == []


def test_auto_scan_defers_4800plus_eth1_without_eth0_alias(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    hits = {
        "192.168.0.118": SimpleNamespace(
            address="192.168.0.118",
            sn="EC752JJ3825155FE",
            mac="AA:BB:CC:A6:83:B7",
            data={"interface": "eth1", "model": "DXP4800 Plus"},
        )
    }

    devices = gui._auto_scan_devices_from_candidates(["192.168.0.117", "192.168.0.118"], hits, set())

    assert devices == []


def test_auto_scan_rejects_link_local_ip_even_before_queue(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    network = gui._auto_scan_network("192.168.0.0/24")

    assert gui._auto_scan_ip_allowed("192.168.0.213", network)
    assert not gui._auto_scan_ip_allowed("169.254.249.211", network)


def test_reserved_alias_ip_blocks_future_auto_scan_duplicate(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    gui.devices = {
        "task-1": DeviceTask(
            task_id="task-1",
            sn="AUTOC0A800DE",
            requested_ip="192.168.0.222",
            reserved_ips={"192.168.0.222", "192.168.0.223"},
            mode="setup",
            cleanup_before_finish=True,
            factory_reset_before_finish=True,
        )
    }

    assert gui._has_known_ip("192.168.0.223")
