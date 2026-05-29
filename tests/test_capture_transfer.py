import os
from pathlib import Path

import pytest

from src.flows import capture
from src.utils.smb_transfer import SmbTransferConfig, build_download_script, build_upload_script


READ_MARKER = "读取速度（总）"
WRITE_MARKER = "写入速度（总）"


def test_existing_capture_path_uses_latest_non_failure_png(tmp_path) -> None:
    screenshots = tmp_path / "shots"
    screenshots.mkdir()
    old = screenshots / "SN123_system_update_20260428_100000.png"
    latest = screenshots / "SN123_system_update_20260428_110000.png"
    failure = screenshots / "SN123_FAIL_system_update_20260428_120000.png"
    unrelated = screenshots / "SN123_storage_pool_20260428_120000.png"
    for path in (old, latest, failure, unrelated):
        path.write_bytes(b"png")
    os.utime(old, (1, 1))
    os.utime(latest, (2, 2))
    os.utime(failure, (3, 3))
    os.utime(unrelated, (4, 4))

    assert capture._existing_capture_path(screenshots, "SN123", "system_update") == latest


def test_existing_capture_path_requires_reported_values_when_needed(tmp_path) -> None:
    screenshots = tmp_path / "SN123" / "图片"
    screenshots.mkdir(parents=True)
    shot = screenshots / "SN123_network_interface_20260506_120000.png"
    shot.write_bytes(b"png")
    report = tmp_path / "SN123" / "test_report.json"
    report.write_text('{"captured_values":{}}', encoding="utf-8")

    assert (
        capture._existing_capture_path(screenshots, "SN123", "network_interface", require_reported_values=True)
        is None
    )

    report.write_text('{"captured_values":{"network_interface":{"link_bps":"10"}}}', encoding="utf-8")

    assert capture._existing_capture_path(
        screenshots,
        "SN123",
        "network_interface",
        require_reported_values=True,
    ) == shot


def test_network_interface_extracts_highest_connected_link() -> None:
    text = "LAN1 网络状态: 10Gbps，全双工，MTU1500 LAN2 网络状态: 2.5Gbps，全双工，MTU1500"

    assert capture._network_link_bps_text(text) == "10"


def test_system_update_extracts_actual_version() -> None:
    text = "系统更新 已经是最新版本 当前版本：9.15.1.0127 上次检查更新：1分钟前"

    assert capture._system_update_version_text(text) == "9.15.1.0127"


def test_build_transfer_config_falls_back_to_existing_10g_rar(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "TEN_GIB", len(b"fixturedata"))
    monkeypatch.setattr(capture, "GIB", 1)
    fixture = tmp_path / "测试10G.rar"
    fixture.write_bytes(b"fixturedata")
    output_run_dir = tmp_path / "synced-output" / "SN123"
    work_dir = tmp_path / "local-work"

    cfg = capture._build_transfer_config(
        "http://192.168.0.168:9999",
        "hdd",
        output_run_dir,
        {"username": "admin", "password": "pw"},
        {
            "model": "4800",
            "source_file": str(tmp_path / "测试10G - 副本.rar"),
            "source_file_sizes_gib": {"4800": 10},
            "work_dir": str(work_dir),
        },
    )

    assert cfg.source_file == fixture
    assert cfg.file_name == fixture.name
    assert cfg.local_dir == work_dir / "SN123" / "hdd"
    assert not str(cfg.download_file).startswith(str(output_run_dir))


def test_build_transfer_config_selects_model_source_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "GIB", 1)
    source_5g = tmp_path / "测试5G.rar"
    source_10g = tmp_path / "测试10G.rar"
    source_20g = tmp_path / "测试20G.rar"
    source_5g.write_bytes(b"5" * 5)
    source_10g.write_bytes(b"10" * 5)
    source_20g.write_bytes(b"20" * 10)

    cfg = capture._build_transfer_config(
        "http://192.168.0.168:9999",
        "hdd",
        tmp_path / "output" / "SN123",
        {"username": "admin", "password": "pw"},
        {
            "model": "4800Plus",
            "source_files": {
                "2800": str(source_5g),
                "4800": str(source_10g),
                "4800Plus": str(source_20g),
            },
            "source_file_sizes_gib": {"2800": 5, "4800": 10, "4800Plus": 20},
        },
    )

    assert cfg.source_file == source_20g
    assert cfg.file_size_bytes == 20
    assert cfg.remote_file_name == "测试20G.rar"


def test_transfer_source_file_rejects_all_zero_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "TEN_GIB", 16)
    zero_file = tmp_path / "zero.bin"
    data_file = tmp_path / "data.bin"
    zero_file.write_bytes(b"\0" * 32)
    data_file.write_bytes(b"\0" * 16 + b"x" + b"\0" * 16)

    assert capture._valid_transfer_source_file(zero_file) is False
    assert capture._valid_transfer_source_file(data_file) is True


def test_generate_transfer_source_file_writes_nonzero_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(capture, "TEN_GIB", 64)
    monkeypatch.setattr(capture, "TRANSFER_SOURCE_GENERATE_CHUNK_BYTES", 16)
    path = tmp_path / "fixture.bin"

    result = capture._generate_transfer_source_file(path)

    assert result == path
    assert path.stat().st_size == 64
    assert any(path.read_bytes())
    assert capture._valid_transfer_source_file(path) is True


def test_upload_min_seed_uses_model_mapping() -> None:
    assert capture._transfer_upload_min_seed_bytes(
        {"model": "2800", "upload_min_seed_mb": {"2800": 5, "4800": 10, "4800Plus": 20}}
    ) == 5 * 1024 * 1024
    assert capture._transfer_upload_min_seed_bytes(
        {"model": "4800Plus", "upload_min_seed_mb": {"2800": 5, "4800": 10, "4800Plus": 20}}
    ) == 20 * 1024 * 1024


def test_default_transfer_work_dir_is_local_to_runtime_root(tmp_path, monkeypatch) -> None:
    runtime_root = tmp_path / "runtime"
    monkeypatch.setattr(capture, "_runtime_root", lambda: runtime_root)

    cfg = capture._build_transfer_config(
        "http://192.168.0.168:9999",
        "ssd",
        tmp_path / "synced-output" / "SN456",
        {"username": "admin", "password": "pw"},
        {},
    )

    assert cfg.local_dir == runtime_root / "transfer_work" / "SN456" / "ssd"


def test_cleanup_transfer_run_dir_keeps_source_file_outside_workdir(tmp_path) -> None:
    source = tmp_path / "测试10G.rar"
    source.write_bytes(b"fixture")
    run_dir = tmp_path / "transfer_work" / "SN123"
    share_dir = run_dir / "hdd"
    share_dir.mkdir(parents=True)
    (share_dir / "download_测试10G.rar").write_bytes(b"download")
    (share_dir / "测试10G.rar.progress").write_text("1", encoding="utf-8")

    capture._cleanup_transfer_run_dir(run_dir)

    assert source.exists()
    assert not run_dir.exists()


def test_chunked_transfer_script_creates_progress_directory(tmp_path) -> None:
    cfg = SmbTransferConfig(
        unc_share=r"\\192.168.0.168\hdd",
        local_dir=tmp_path / "missing" / "smb",
        drive_letter="H",
        source_file=tmp_path / "source.bin",
    )

    script = build_upload_script(cfg)

    assert "New-Item -ItemType Directory -Force -Path $progressDir" in script
    assert "Split-Path -Parent $progress" in script
    assert "[System.IO.FileOptions]::WriteThrough" in script


def test_download_transfer_uses_chunked_copy_with_progress(tmp_path) -> None:
    cfg = SmbTransferConfig(
        unc_share=r"\\192.168.0.168\hdd",
        local_dir=tmp_path / "smb",
        drive_letter="H",
        source_file=tmp_path / "source.bin",
    )

    script = build_download_script(cfg)

    assert "while (($read = $inputStream.Read" in script
    assert "[System.IO.File]::WriteAllText($progress" in script


def test_transfer_speed_sample_parses_hdd_download_and_ssd_upload() -> None:
    text = (
        f"{READ_MARKER} 220 MB/s 硬盘1 215 MB/s "
        f"{WRITE_MARKER} 1.2 GB/s M.2硬盘1 860 MB/s"
    )

    hdd_read = capture._transfer_speed_sample(text, "hdd", "download")
    ssd_write = capture._transfer_speed_sample(text, "ssd", "upload")

    assert hdd_read is not None
    assert hdd_read.rate_mb_s == 220
    assert ssd_write is not None
    assert round(ssd_write.rate_mb_s, 1) == 1228.8


def test_transfer_speed_sample_uses_total_when_device_rows_are_stale() -> None:
    text = f"{READ_MARKER} 267.4 MB/s {WRITE_MARKER} 0 B/s M.2硬盘1 0 B/s"

    sample = capture._transfer_speed_sample(text, "ssd", "download")

    assert sample is not None
    assert sample.rate_mb_s == 267.4


def test_transfer_speed_sample_ignores_write_total_for_download() -> None:
    text = f"{READ_MARKER} 0 B/s {WRITE_MARKER} 308.5 MB/s"

    sample = capture._transfer_speed_sample(text, "hdd", "download")

    assert sample is None


def test_transfer_speed_thresholds_are_model_configurable() -> None:
    cfg = {
        "speed_thresholds_mb_s": {
            "default": 200,
            "models": {
                "2800": {
                    "default": 100,
                },
                "4800": {
                    "default": 100,
                },
                "4800Plus": {
                    "default": 200,
                }
            },
        }
    }

    assert capture._transfer_speed_threshold_mb_s("hdd_read", {**cfg, "model": "2800"}) == 100
    assert capture._transfer_speed_threshold_mb_s("hdd_write", {**cfg, "model": "2800"}) == 100
    assert capture._transfer_speed_threshold_mb_s("ssd_write", {**cfg, "model": "2800"}) == 100
    assert capture._transfer_speed_threshold_mb_s("hdd_read", {**cfg, "model": "4800"}) == 100
    assert capture._transfer_speed_threshold_mb_s("ssd_read", {**cfg, "model": "4800"}) == 100
    assert capture._transfer_speed_threshold_mb_s("hdd_read", {**cfg, "model": "4800Plus"}) == 200
    assert capture._transfer_speed_threshold_mb_s("ssd_write", {**cfg, "model": "4800Plus"}) == 200


def test_transfer_upload_seed_defaults_to_inferred_model_size() -> None:
    assert capture._transfer_upload_min_seed_bytes({}) == 5 * 1024 * 1024 * 1024
    assert capture._transfer_upload_min_seed_bytes({"model": "4800"}) == 10 * 1024 * 1024 * 1024
    assert capture._transfer_upload_min_seed_bytes({"model": "4800Plus"}) == 20 * 1024 * 1024 * 1024
    assert capture._transfer_upload_min_seed_bytes({"upload_min_seed_mb": 1024}) == 1024 * 1024 * 1024


def test_upload_capture_passes_when_full_seed_completes_after_speed_sample(tmp_path, monkeypatch) -> None:
    shot = tmp_path / "speed.png"
    shot.write_bytes(b"png")
    text = f"{READ_MARKER} 0 B/s {WRITE_MARKER} 560 MB/s"
    cfg = SmbTransferConfig(unc_share=r"\\192.168.0.168\hdd", local_dir=tmp_path)
    process_checks = iter([False, True, True])
    progress_values = iter([10 * 1024 * 1024 * 1024])

    class FakePage:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    monkeypatch.setattr(capture, "_frame_text", lambda _frame: text)
    monkeypatch.setattr(capture, "_process_finished", lambda _proc: next(process_checks))
    monkeypatch.setattr(capture, "_progress_size", lambda _path: next(progress_values))
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "capture_page", lambda *_args, **_kwargs: shot)

    result = capture._capture_transfer_attempt(
        FakePage(),
        object(),
        object(),
        cfg,
        "SN123",
        "hdd_write",
        "hdd",
        "upload",
        tmp_path,
        {
            "speed_stable_samples": 2,
            "speed_attempt_timeout_s": 5,
            "speed_sample_interval_ms": 100,
            "upload_min_seed_mb": 10240,
            "post_upload_settle_timeout_s": 0,
        },
        300,
        1,
    )

    assert result.reached_threshold is True
    assert result.shot_path == shot
    assert result.values["rate_mbps"] == "560"
    assert result.values["speed_status"] == "ok"


def test_upload_capture_waits_for_seed_after_speed_sample_window(tmp_path, monkeypatch) -> None:
    shot = tmp_path / "speed.png"
    shot.write_bytes(b"png")
    text = f"{READ_MARKER} 0 B/s {WRITE_MARKER} 560 MB/s"
    cfg = SmbTransferConfig(unc_share=r"\\192.168.0.168\hdd", local_dir=tmp_path)
    seed_bytes = 20 * 1024 * 1024
    process_checks = [False, False, True]
    progress_values = [0, seed_bytes]
    monotonic_values = iter([0.0, 0.0, 6.0, 6.0, 6.0, 6.0, 6.0])

    class FakePage:
        waits = 0

        def wait_for_timeout(self, _ms: int) -> None:
            self.waits += 1

    page = FakePage()

    def fake_process_finished(_proc) -> bool:
        return process_checks.pop(0) if process_checks else True

    def fake_progress_size(_path) -> int:
        return progress_values.pop(0) if progress_values else seed_bytes

    monkeypatch.setattr(capture.time, "monotonic", lambda: next(monotonic_values, 6.0))
    monkeypatch.setattr(capture, "_frame_text", lambda _frame: text)
    monkeypatch.setattr(capture, "_process_finished", fake_process_finished)
    monkeypatch.setattr(capture, "_progress_size", fake_progress_size)
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "capture_page", lambda *_args, **_kwargs: shot)

    result = capture._capture_transfer_attempt(
        page,
        object(),
        object(),
        cfg,
        "SN123",
        "hdd_write",
        "hdd",
        "upload",
        tmp_path,
        {
            "speed_stable_samples": 1,
            "speed_attempt_timeout_s": 5,
            "speed_sample_interval_ms": 100,
            "upload_min_seed_mb": 20,
            "upload_seed_timeout_s": 5,
            "post_upload_settle_timeout_s": 0,
        },
        300,
        1,
    )

    assert page.waits >= 1
    assert result.reached_threshold is True
    assert result.values["rate_mbps"] == "560"
    assert result.values["speed_status"] == "ok"


def test_transfer_capture_reports_highest_stable_threshold_sample(tmp_path, monkeypatch) -> None:
    shot = tmp_path / "speed.png"
    shot.write_bytes(b"png")
    # Each accepted threshold capture re-reads the rate right after the screenshot
    # (the "screenshot-moment" read), so two extra readings are needed vs. the
    # number of loop iterations.
    texts = iter(
        [
            f"{READ_MARKER} 320 MB/s {WRITE_MARKER} 0 B/s",
            f"{READ_MARKER} 320 MB/s {WRITE_MARKER} 0 B/s",
            f"{READ_MARKER} 320 MB/s {WRITE_MARKER} 0 B/s",
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",
        ]
    )
    process_checks = iter([False, False, False, True])
    cfg = SmbTransferConfig(unc_share=r"\\192.168.0.168\hdd", local_dir=tmp_path)

    class FakePage:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    monkeypatch.setattr(capture, "_frame_text", lambda _frame: next(texts))
    monkeypatch.setattr(capture, "_process_finished", lambda _proc: next(process_checks))
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "capture_page", lambda *_args, **_kwargs: shot)

    result = capture._capture_transfer_attempt(
        FakePage(),
        object(),
        object(),
        cfg,
        "SN123",
        "hdd_read",
        "hdd",
        "download",
        tmp_path,
        {"speed_stable_samples": 2, "speed_attempt_timeout_s": 5, "speed_sample_interval_ms": 100},
        300,
        1,
    )

    assert result.reached_threshold is True
    assert result.values["rate_mbps"] == "560"
    assert result.values["speed_status"] == "ok"


def test_transfer_capture_rejects_trough_screenshot_and_matches_value(tmp_path, monkeypatch) -> None:
    # The screen rate is bursty: the peak (560) that triggers a capture has fallen
    # to a trough (26.4) by the time the screenshot is grabbed. That screenshot must
    # be rejected, and the reported value must equal the rate frozen in the screenshot
    # that is finally kept (never the pre-screenshot peak).
    best_shot = tmp_path / "best.png"
    trough_shot = tmp_path / "trough.png"
    peak_shot = tmp_path / "peak.png"
    for path in (best_shot, trough_shot, peak_shot):
        path.write_bytes(b"png")

    texts = iter(
        [
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",   # iter1 top: peak triggers capture
            f"{READ_MARKER} 26.4 MB/s {WRITE_MARKER} 0 B/s",  # iter1 screenshot-moment: trough -> reject
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",   # iter2 top: peak triggers capture
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",   # iter2 screenshot-moment: peak -> accept
        ]
    )
    shots = iter([best_shot, trough_shot, peak_shot])
    process_checks = iter([False, True])
    cfg = SmbTransferConfig(unc_share=r"\\192.168.0.168\hdd", local_dir=tmp_path)

    class FakePage:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    monkeypatch.setattr(capture, "_frame_text", lambda _frame: next(texts))
    monkeypatch.setattr(capture, "_process_finished", lambda _proc: next(process_checks))
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "capture_page", lambda *_args, **_kwargs: next(shots))

    result = capture._capture_transfer_attempt(
        FakePage(),
        object(),
        object(),
        cfg,
        "SN123",
        "hdd_read",
        "hdd",
        "download",
        tmp_path,
        {"speed_stable_samples": 1, "speed_attempt_timeout_s": 5, "speed_sample_interval_ms": 100},
        300,
        1,
    )

    assert result.reached_threshold is True
    assert result.values["rate_mbps"] == "560"   # screenshot-moment value, not the discarded peak/trough
    assert result.shot_path == peak_shot          # trough screenshot was rejected


def test_existing_transfer_capture_requires_reported_rate_above_threshold(tmp_path) -> None:
    screenshots = tmp_path / "shots"
    screenshots.mkdir()
    shot = screenshots / "SN123_ssd_write_20260504_120000.png"
    shot.write_bytes(b"png")
    report = tmp_path / "test_report.json"
    report.write_text(
        '{"captured":{"ssd_write":"%s"},"captured_values":{"ssd_write":{"rate_mbps":"38.7"}}}'
        % str(shot).replace("\\", "\\\\"),
        encoding="utf-8",
    )

    assert capture._existing_capture_path(screenshots, "SN123", "ssd_write", 200) is None

    report.write_text(
        '{"captured":{"ssd_write":"%s"},"captured_values":{"ssd_write":{"rate_mbps":"267.2"}}}'
        % str(shot).replace("\\", "\\\\"),
        encoding="utf-8",
    )

    assert capture._existing_capture_path(screenshots, "SN123", "ssd_write", 200) == shot


def test_transfer_capture_does_not_retry_when_first_attempt_passes(tmp_path, monkeypatch) -> None:
    shot = tmp_path / "pass.png"
    shot.write_bytes(b"png")
    attempts: list[int] = []

    _patch_transfer_page_dependencies(
        monkeypatch,
        attempts,
        [
            capture.TransferAttemptResult(
                {"rate_mbps": "220", "rate": "220 MB/s", "speed_status": "ok", "attempt": "1"},
                shot,
                True,
            )
        ],
    )

    captured: dict[str, dict[str, str]] = {}
    result = capture._capture_transfer_page(
        object(),
        "http://192.168.0.168:9999",
        "SN123",
        "ssd_write",
        {"app": "taskmgr", "transfer": {"share": "ssd", "direction": "upload"}},
        {},
        {},
        tmp_path,
        {},
        {"model": "2800", "speed_max_retries": 1},
        None,
        slot_already_acquired=True,
        capture_values=captured,
    )

    assert result == shot
    assert attempts == [1]
    assert captured["ssd_write"]["attempts"] == "1"


def test_transfer_capture_retries_low_attempt_and_uses_passing_attempt(tmp_path, monkeypatch) -> None:
    pass_shot = tmp_path / "pass.png"
    pass_shot.write_bytes(b"png")
    attempts: list[int] = []

    _patch_transfer_page_dependencies(
        monkeypatch,
        attempts,
        [
            capture.TransferAttemptResult(
                {"rate_mbps": "60", "rate": "60 MB/s", "speed_status": "below_threshold", "attempt": "1"},
                None,
                False,
            ),
            capture.TransferAttemptResult(
                {"rate_mbps": "230", "rate": "230 MB/s", "speed_status": "ok", "attempt": "2"},
                pass_shot,
                True,
            ),
        ],
    )

    captured: dict[str, dict[str, str]] = {}
    result = capture._capture_transfer_page(
        object(),
        "http://192.168.0.168:9999",
        "SN123",
        "ssd_write",
        {"app": "taskmgr", "transfer": {"share": "ssd", "direction": "upload"}},
        {},
        {},
        tmp_path,
        {},
        {"model": "2800", "speed_max_retries": 1},
        None,
        slot_already_acquired=True,
        capture_values=captured,
    )

    assert result == pass_shot
    assert attempts == [1, 2]
    assert captured["ssd_write"]["rate_mbps"] == "230"
    assert captured["ssd_write"]["attempts"] == "2"


def test_transfer_capture_fails_when_all_attempts_are_below_threshold(tmp_path, monkeypatch) -> None:
    first_shot = tmp_path / "first-low.png"
    first_shot.write_bytes(b"png")
    final_shot = tmp_path / "final.png"
    final_shot.write_bytes(b"png")
    attempts: list[int] = []

    _patch_transfer_page_dependencies(
        monkeypatch,
        attempts,
        [
            capture.TransferAttemptResult(
                {"rate_mbps": "180", "rate": "180 MB/s", "speed_status": "below_threshold", "attempt": "1"},
                first_shot,
                False,
            ),
            capture.TransferAttemptResult(
                {"rate_mbps": "150", "rate": "150 MB/s", "speed_status": "below_threshold", "attempt": "2"},
                final_shot,
                False,
            ),
        ],
    )

    captured: dict[str, dict[str, str]] = {}
    with pytest.raises(capture.CaptureError, match="speed did not reach screenshot threshold"):
        capture._capture_transfer_page(
            object(),
            "http://192.168.0.168:9999",
            "SN123",
            "ssd_write",
            {"app": "taskmgr", "transfer": {"share": "ssd", "direction": "upload"}},
            {},
            {},
            tmp_path,
            {},
            {"model": "2800", "speed_max_retries": 1},
            None,
            slot_already_acquired=True,
            capture_values=captured,
        )

    assert attempts == [1, 2]
    assert captured["ssd_write"]["rate_mbps"] == "180"
    assert captured["ssd_write"]["speed_status"] == "below_threshold"


def test_transfer_capture_fails_on_no_sample_instead_of_uploading_zero(tmp_path, monkeypatch) -> None:
    final_shot = tmp_path / "final.png"
    final_shot.write_bytes(b"png")
    attempts: list[int] = []

    _patch_transfer_page_dependencies(
        monkeypatch,
        attempts,
        [
            capture.TransferAttemptResult(
                {"rate_mbps": "0", "rate": "0 MB/s", "speed_status": "no_sample", "attempt": "1"},
                None,
                False,
            ),
            capture.TransferAttemptResult(
                {"rate_mbps": "0", "rate": "0 MB/s", "speed_status": "no_sample", "attempt": "2"},
                final_shot,
                False,
            ),
        ],
    )

    captured: dict[str, dict[str, str]] = {}
    with pytest.raises(capture.CaptureError, match="no_sample"):
        capture._capture_transfer_page(
            object(),
            "http://192.168.0.168:9999",
            "SN123",
            "hdd_read",
            {"app": "taskmgr", "transfer": {"share": "hdd", "direction": "download"}},
            {},
            {},
            tmp_path,
            {},
            {"model": "4800Plus", "speed_max_retries": 1},
            None,
            slot_already_acquired=True,
            capture_values=captured,
        )

    assert attempts == [1, 2]
    assert captured["hdd_read"]["speed_status"] == "no_sample"
    assert captured["hdd_read"]["rate_mbps"] == "0"


def _patch_transfer_page_dependencies(monkeypatch, attempts: list[int], results: list[capture.TransferAttemptResult]) -> None:
    cfg = SmbTransferConfig(unc_share=r"\\192.168.0.168\ssd", local_dir=Path("."))
    monkeypatch.setattr(capture, "_open_app", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(capture, "_navigate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "_wait_for_landmark", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "_build_transfer_config", lambda *_args, **_kwargs: cfg)
    monkeypatch.setattr(capture, "_prepare_share", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "_remote_file_exists", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(capture, "start_powershell", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(capture, "_finish_transfer_process", lambda *_args, **_kwargs: None)

    def fake_attempt(*args, **_kwargs):
        attempts.append(args[11])
        return results.pop(0)

    monkeypatch.setattr(capture, "_capture_transfer_attempt", fake_attempt)
