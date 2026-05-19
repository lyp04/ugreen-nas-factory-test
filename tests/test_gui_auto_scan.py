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


class _Var:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class _Button:
    def __init__(self):
        self.options = {}

    def configure(self, **kwargs):
        self.options.update(kwargs)


class _Tree:
    def __init__(self):
        self.rows = {}
        self.next_id = 0

    def get_children(self):
        return list(self.rows)

    def delete(self, item):
        self.rows.pop(item, None)

    def insert(self, parent, index, text="", values=(), open=False, tags=()):
        self.next_id += 1
        item_id = f"row-{self.next_id}"
        self.rows[item_id] = {
            "parent": parent,
            "text": text,
            "values": values,
            "open": open,
            "tags": tags,
        }
        return item_id


class _Root:
    def __init__(self):
        self.after_calls = []
        self.cancelled = []

    def after(self, delay_ms, callback):
        after_id = f"after-{len(self.after_calls) + 1}"
        self.after_calls.append((after_id, delay_ms, callback))
        return after_id

    def after_cancel(self, after_id):
        self.cancelled.append(after_id)


def _gui_with_daily_stats(tmp_path):
    gui = _gui_for_output(tmp_path)
    gui.language_var = SimpleNamespace(get=lambda: "中文")
    gui.success_count_var = _Var()
    gui.failed_count_var = _Var()
    gui.daily_stats_date, gui.daily_stats_devices = gui._load_daily_stats()
    return gui


def _gui_with_queue_state(tmp_path):
    gui = _gui_for_output(tmp_path)
    gui.language_var = SimpleNamespace(get=lambda: "中文")
    gui.task_counter = 0
    gui.device_tree = None
    return gui


def _gui_with_form_settings(tmp_path, auto_form=True, grade=""):
    gui = _gui_for_output(tmp_path)
    gui.language_var = SimpleNamespace(get=lambda: "中文")
    gui.form_entry_enabled = True
    gui.auto_form_entry_var = _Var(auto_form)
    gui.form_grade_var = _Var(grade)
    gui.form_account_var = _Var("operator01")
    gui.status_var = _Var()
    return gui


def _gui_with_action_state(tmp_path):
    gui = _gui_with_daily_stats(tmp_path)
    gui.root = _Root()
    gui.task_counter = 0
    gui.selected_task_id = None
    gui.workers = {}
    gui.device_tree = None
    gui.log_view = None
    gui.materials_tree = None
    gui.timing_canvas = None
    gui.timing_chart_after_id = None
    gui.remove_current_btn = _Button()
    gui.show_browser_btn = _Button()
    gui.cancel_btn = _Button()
    gui.queue_summary_var = _Var()
    gui.status_var = _Var()
    return gui


def test_existing_image_directory_does_not_block_auto_scan_by_sn(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    image_dir = tmp_path / "HB670EE07251E54E" / "图片"
    image_dir.mkdir(parents=True)
    (image_dir / "HB670EE07251E54E_system_update_20260430_120004.png").write_bytes(b"png")

    assert not gui._has_completed_output_for_sn("E54E")


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


def test_auto_form_success_report_requires_successful_upload(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    sn_root = tmp_path / "HB670EE07251E54E"
    sn_root.mkdir()
    (sn_root / "test_report.json").write_text(
        json.dumps(
            {
                "status": "success",
                "sn": "HB670EE07251E54E",
                "auto_form_entry": True,
            }
        ),
        encoding="utf-8",
    )

    assert not gui._has_completed_output_for_sn("E54E")

    (sn_root / "test_report.json").write_text(
        json.dumps(
            {
                "status": "success",
                "sn": "HB670EE07251E54E",
                "auto_form_entry": True,
                "form_result": {"status": "already_submitted"},
            }
        ),
        encoding="utf-8",
    )

    assert gui._has_completed_output_for_sn("E54E")


def test_materials_tab_shows_fresh_full_retry_mode(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    gui.language_var = SimpleNamespace(get=lambda: "中文")
    gui.materials_tree = _Tree()
    gui.materials_status_var = _Var()
    task = DeviceTask(
        task_id="task-1",
        sn="HB670EE07251E54E",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        auto_form_entry=True,
    )
    sn_root = tmp_path / task.sn
    sn_root.mkdir()
    (sn_root / "test_report.json").write_text(
        json.dumps(
            {
                "form_data": {
                    "model_label": "DXP2800",
                    "grade": "A",
                    "material_groups": [
                        {
                            "title": "补充包材",
                            "items": [
                                {"code": "MR_A", "name": "包材 A", "default_qty": 1},
                                {"code": "MR_B", "name": "包材 B", "default_qty": 1},
                            ],
                        }
                    ],
                },
                "form_result": {
                    "status": "success",
                    "material_selection_mode": "fresh_full_select_then_retry_remove_missing",
                    "removed_material_codes": ["MR_B"],
                },
            }
        ),
        encoding="utf-8",
    )

    gui._refresh_materials_tab(task)

    assert "每次重取物料，全选后反选缺料" in gui.materials_status_var.get()
    assert "缺料 1 项" in gui.materials_status_var.get()
    assert any(row["text"] == "包材 B" and "item_missing" in row["tags"] for row in gui.materials_tree.rows.values())


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


def test_success_and_latest_failure_rows_are_tagged(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    success = DeviceTask(
        task_id="task-1",
        sn="HB670EE072512F12",
        requested_ip="192.168.0.191",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="success",
    )
    failed = DeviceTask(
        task_id="task-2",
        sn="HB670EE07251E54E",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="failed",
    )
    gui.devices = {"task-1": success, "task-2": failed}

    assert gui._row_tags_for_task(success) == (gui.ROW_SUCCESS_TAG,)
    assert gui._row_tags_for_task(failed) == (gui.ROW_FAILED_TAG,)


def test_failed_row_with_later_same_sn_retry_is_not_red(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    failed = DeviceTask(
        task_id="task-1",
        sn="E54E",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="failed",
    )
    retry = DeviceTask(
        task_id="task-2",
        sn="HB670EE07251E54E",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="running",
    )
    gui.devices = {"task-1": failed, "task-2": retry}

    assert gui._row_tags_for_task(failed) == ()
    assert gui._row_tags_for_task(retry) == ()


def test_cancel_button_becomes_retry_for_failed_task(tmp_path) -> None:
    gui = _gui_with_action_state(tmp_path)
    failed = DeviceTask(
        task_id="task-1",
        sn="HB670EE07251E54E",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="failed",
    )
    gui.devices = {failed.task_id: failed}
    gui.selected_task_id = failed.task_id

    gui._refresh_action_states()

    assert gui.cancel_btn.options["text"] == "重试任务"
    assert gui.cancel_btn.options["state"] == "normal"


def test_retry_failed_task_adds_later_retry_and_clears_failed_count(monkeypatch, tmp_path) -> None:
    gui = _gui_with_action_state(tmp_path)
    monkeypatch.setattr(gui, "_start_task_worker", lambda _task: None)
    failed = DeviceTask(
        task_id="task-1",
        sn="HB670EE07251E54E",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        auto_form_entry=True,
        form_model="2800",
        form_grade="B",
        form_account_name="operator01",
        state_code="failed",
        reserved_ips={"192.168.0.214"},
    )
    gui.devices = {failed.task_id: failed}
    gui.selected_task_id = failed.task_id
    gui._record_daily_task_result(failed, "failed")

    gui._on_cancel_task()

    retry = gui._selected_task()
    assert retry is not None
    assert retry.task_id != failed.task_id
    assert retry.state_code == "queued"
    assert retry.sn == failed.sn
    assert retry.requested_ip == failed.requested_ip
    assert gui._row_tags_for_task(failed) == ()
    assert gui.failed_count_var.get() == "今日失败 0 台"


def test_daily_stats_survive_removed_rows_and_migrate_auto_sn_retry(tmp_path) -> None:
    gui = _gui_with_daily_stats(tmp_path)
    failed = DeviceTask(
        task_id="task-1",
        sn="AUTOC0A800D6",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="failed",
    )
    retry = DeviceTask(
        task_id="task-2",
        sn="HB670EE07251E54E",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="running",
    )

    gui._record_daily_task_result(failed, "failed")
    gui._refresh_status_counts()
    assert gui.success_count_var.get() == "今日成功 0 台"
    assert gui.failed_count_var.get() == "今日失败 1 台"

    gui._mark_daily_task_retry_pending(retry)
    assert gui.failed_count_var.get() == "今日失败 0 台"

    gui._record_daily_task_result(retry, "success")
    gui._refresh_status_counts()
    assert gui.success_count_var.get() == "今日成功 1 台"
    assert gui.failed_count_var.get() == "今日失败 0 台"

    reloaded = _gui_with_daily_stats(tmp_path)
    assert reloaded._daily_status_counts() == (1, 0)
    assert "sn:E54E" in reloaded.daily_stats_devices
    assert "ip:192.168.0.214" not in reloaded.daily_stats_devices


def test_daily_rollover_clears_disconnected_records_but_keeps_active_and_present_ips(tmp_path) -> None:
    gui = _gui_with_action_state(tmp_path)
    disconnected = DeviceTask(
        task_id="disconnected-001",
        sn="HB670EE0725123DD",
        requested_ip="192.168.0.229",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="success",
    )
    connected = DeviceTask(
        task_id="connected-002",
        sn="HB670EE022513CDF",
        requested_ip="192.168.0.143",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="success",
    )
    active = DeviceTask(
        task_id="active-003",
        sn="HB670EE022517E15",
        requested_ip="192.168.0.239",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="running",
    )
    gui.devices = {
        disconnected.task_id: disconnected,
        connected.task_id: connected,
        active.task_id: active,
    }
    gui.selected_task_id = disconnected.task_id
    gui.daily_stats_devices = {
        "sn:23DD": {"sn": disconnected.sn, "status": "success"},
        "sn:3CDF": {"sn": connected.sn, "status": "success"},
        "sn:7E15": {"sn": active.sn, "status": "failed"},
    }
    gui._detect_present_task_ips = lambda _tasks: {"192.168.0.143"}

    gui._handle_daily_rollover()

    assert list(gui.devices) == [connected.task_id, active.task_id]
    assert gui.selected_task_id is None
    assert gui._daily_status_counts() == (0, 0)
    records = gui._load_queue_state_records()
    assert [record["task_id"] for record in records] == [connected.task_id, active.task_id]
    assert {record["state_code"] for record in records} == {"success", "running"}
    assert gui.daily_rollover_after_id is not None


def test_queue_state_restores_same_day_and_marks_active_cancelled(tmp_path) -> None:
    gui = _gui_with_queue_state(tmp_path)
    task = DeviceTask(
        task_id="HB670EE07251E54E-001",
        sn="HB670EE07251E54E",
        requested_ip="192.168.0.214",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="running",
        progress=42,
        current_step="截图传输中",
    )
    gui.devices = {task.task_id: task}

    gui._save_queue_state()

    reloaded = _gui_with_queue_state(tmp_path)
    records = reloaded._load_queue_state_records()
    restored = reloaded._task_from_queue_record(records[0])

    assert len(records) == 1
    assert restored is not None
    assert restored.state_code == "cancelled"
    assert restored.status == "已中断"
    assert restored.progress == 42
    assert restored.current_step == "上次退出时未完成：截图传输中"


def test_queue_state_ignores_previous_day(tmp_path) -> None:
    gui = _gui_with_queue_state(tmp_path)
    path = gui._queue_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"date": "2000-01-01", "devices": [{"task_id": "old", "sn": "HB670EE07251E54E"}]}),
        encoding="utf-8",
    )

    assert gui._load_queue_state_records() == []


def test_queue_state_loads_utf8_bom_file(tmp_path) -> None:
    gui = _gui_with_queue_state(tmp_path)
    path = gui._queue_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "date": gui._today_key(),
                "devices": [{"task_id": "old-001", "sn": "HB670EE0725123DD"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )

    assert gui._load_queue_state_records()[0]["task_id"] == "old-001"


def test_queue_state_bootstraps_today_records_from_output_folders(monkeypatch, tmp_path) -> None:
    gui = _gui_with_queue_state(tmp_path)
    today = gui._today_key()
    success_sn = "HB670EE0725123DD"
    failed_sn = "HB670EE07251E54E"
    stale_sn = "HB670EE07251ABCD"

    state_path = gui._queue_state_path()
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "date": today,
                "devices": [
                    {
                        "task_id": "kept-042",
                        "sn": success_sn,
                        "requested_ip": "auto",
                        "state_code": "failed",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    for sn, status, ip in (
        (success_sn, "success", "192.168.0.123"),
        (failed_sn, "failed", "192.168.0.124"),
        (stale_sn, "success", "192.168.0.125"),
    ):
        sn_root = tmp_path / sn
        sn_root.mkdir()
        (sn_root / "test_report.json").write_text(
            json.dumps(
                {
                    "sn": sn,
                    "mode": "setup",
                    "status": status,
                    "nas_ip": ip,
                    "started_at": f"{today}T08:00:00",
                    "finished_at": f"{today}T08:30:00",
                    "current_stage": "done",
                    "auto_form_entry": True,
                    "form_model": "2800",
                    "form_grade": "B",
                    "form_account_name": "operator01",
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        gui,
        "_path_created_date",
        lambda path: "2000-01-01" if path.name == stale_sn else today,
    )

    records = gui._load_queue_state_records()
    records_by_sn = {record["sn"]: record for record in records}

    assert len(records) == 2
    assert records_by_sn[success_sn]["task_id"] == "kept-042"
    assert records_by_sn[success_sn]["state_code"] == "success"
    assert records_by_sn[success_sn]["requested_ip"] == "192.168.0.123"
    assert records_by_sn[success_sn]["elapsed_seconds"] == 1800
    assert records_by_sn[failed_sn]["state_code"] == "failed"
    assert stale_sn not in records_by_sn


def test_queue_state_empty_memory_does_not_overwrite_existing_file(tmp_path) -> None:
    gui = _gui_with_queue_state(tmp_path)
    path = gui._queue_state_path()
    path.parent.mkdir(parents=True)
    original = {
        "date": gui._today_key(),
        "devices": [{"task_id": "old-001", "sn": "HB670EE0725123DD"}],
    }
    path.write_text(json.dumps(original), encoding="utf-8-sig")
    gui.devices = {}

    gui._save_queue_state()

    records = gui._load_queue_state_records()
    assert len(records) == 1
    assert records[0]["task_id"] == "old-001"


def test_daily_stats_loads_utf8_bom_file(tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    path = gui._daily_stats_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "date": gui._today_key(),
                "devices": {"sn:23DD": {"sn": "HB670EE0725123DD", "status": "failed"}},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8-sig",
    )

    _, devices = gui._load_daily_stats()

    assert devices["sn:23DD"]["status"] == "failed"


def test_daily_stats_bootstraps_today_counts_from_output_folder_creation_date(monkeypatch, tmp_path) -> None:
    gui = _gui_for_output(tmp_path)
    today = gui._today_key()
    success_sn = "HB670EE0725123DD"
    failed_sn = "HB670EE07251E54E"
    stale_sn = "HB670EE07251ABCD"

    state_path = gui._daily_stats_path()
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        json.dumps(
            {
                "date": today,
                "devices": {
                    "sn:23DD": {"sn": success_sn, "status": "failed"},
                },
            }
        ),
        encoding="utf-8",
    )

    for sn, status in ((success_sn, "success"), (failed_sn, "failed"), (stale_sn, "success")):
        sn_root = tmp_path / sn
        sn_root.mkdir()
        (sn_root / "test_report.json").write_text(
            json.dumps(
                {
                    "sn": sn,
                    "status": status,
                    "started_at": f"{today}T08:00:00",
                    "finished_at": f"{today}T08:30:00",
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        gui,
        "_path_created_date",
        lambda path: "2000-01-01" if path.name == stale_sn else today,
    )

    date, devices = gui._load_daily_stats()

    assert date == today
    assert devices["sn:23DD"]["status"] == "success"
    assert devices["sn:E54E"]["status"] == "failed"
    assert "sn:ABCD" not in devices

    gui.daily_stats_date = date
    gui.daily_stats_devices = devices
    assert gui._daily_status_counts() == (1, 1)


def test_queue_state_save_merges_existing_same_day_rows(tmp_path) -> None:
    gui = _gui_with_queue_state(tmp_path)
    path = gui._queue_state_path()
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "date": gui._today_key(),
                "devices": [
                    {
                        "task_id": "old-001",
                        "sn": "HB670EE0725123DD",
                        "requested_ip": "192.168.0.229",
                        "mode": "setup",
                        "state_code": "success",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    task = DeviceTask(
        task_id="new-001",
        sn="HB670EE022513CDF",
        requested_ip="192.168.0.143",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="success",
    )
    gui.devices = {task.task_id: task}

    gui._save_queue_state()

    task_ids = {record["task_id"] for record in gui._load_queue_state_records()}
    assert task_ids == {"old-001", "new-001"}


def test_queue_state_remove_replaces_instead_of_merging(tmp_path) -> None:
    gui = _gui_with_queue_state(tmp_path)
    task = DeviceTask(
        task_id="old-001",
        sn="HB670EE0725123DD",
        requested_ip="192.168.0.229",
        mode="setup",
        cleanup_before_finish=True,
        factory_reset_before_finish=True,
        state_code="success",
    )
    gui.devices = {task.task_id: task}
    gui._save_queue_state()
    gui.devices = {}

    gui._save_queue_state(merge_existing=False)

    assert gui._load_queue_state_records() == []


def test_auto_scan_waits_for_grade_when_auto_form_is_enabled(monkeypatch, tmp_path) -> None:
    gui = _gui_with_form_settings(tmp_path, auto_form=True, grade="")
    monkeypatch.setattr(gui_module.form_entry, "available_models", lambda _root: ["2800"])

    assert not gui._auto_scan_ready(show_warning=False)
    assert "请选择 A/B" in gui.status_var.get()


def test_auto_scan_ready_without_grade_when_auto_form_is_off(monkeypatch, tmp_path) -> None:
    gui = _gui_with_form_settings(tmp_path, auto_form=False, grade="")
    monkeypatch.setattr(gui_module.form_entry, "available_models", lambda _root: ["2800"])

    assert gui._auto_scan_ready(show_warning=False)
    assert gui.status_var.get().endswith("自动录表未开启，本次不会提交表单。")


def test_auto_scan_ready_after_grade_selected(monkeypatch, tmp_path) -> None:
    gui = _gui_with_form_settings(tmp_path, auto_form=True, grade="B")
    monkeypatch.setattr(gui_module.form_entry, "available_models", lambda _root: [])

    assert gui._auto_scan_ready(show_warning=False)


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
