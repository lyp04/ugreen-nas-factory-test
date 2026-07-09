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
        "http://192.0.2.168:9999",
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
        "http://192.0.2.168:9999",
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
        "http://192.0.2.168:9999",
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
        unc_share=r"\\192.0.2.168\hdd",
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
        unc_share=r"\\192.0.2.168\hdd",
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
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\hdd", local_dir=tmp_path)
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
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\hdd", local_dir=tmp_path)
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


def test_transfer_capture_records_bracketed_stable_rate(tmp_path, monkeypatch) -> None:
    # A capture is only accepted when a read immediately before AND immediately
    # after the screenshot are identical (the display held steady across the shot),
    # so the recorded value equals the number frozen in the image.
    shot = tmp_path / "speed.png"
    shot.write_bytes(b"png")
    texts = iter(
        [
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",  # iter1 loop-top sample
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",  # iter2 loop-top sample
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",  # iter2 pre-screenshot read
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",  # iter2 post-screenshot read (== pre -> accept)
        ]
    )
    process_checks = iter([False, True])
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\hdd", local_dir=tmp_path)

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


def test_transfer_capture_rejects_unstable_screenshot(tmp_path, monkeypatch) -> None:
    # If the displayed rate moved across the screenshot window (180 just before, 560
    # just after — the shot straddled a ~1s refresh edge), that screenshot is ambiguous
    # and is discarded. A later steady window is accepted, and the recorded value equals
    # the rate frozen in the kept screenshot.
    reject_shot = tmp_path / "reject.png"
    accept_shot = tmp_path / "accept.png"
    for path in (reject_shot, accept_shot):
        path.write_bytes(b"png")

    texts = iter(
        [
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",   # iter1 loop-top
            f"{READ_MARKER} 180 MB/s {WRITE_MARKER} 0 B/s",   # iter1 pre-screenshot
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",   # iter1 post (!= pre -> reject)
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",   # iter2 loop-top
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",   # iter2 pre-screenshot
            f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s",   # iter2 post (== pre -> accept)
        ]
    )
    shots = iter([reject_shot, accept_shot])
    process_checks = iter([False, True])
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\hdd", local_dir=tmp_path)

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
    assert result.values["rate_mbps"] == "560"
    assert result.shot_path == accept_shot
    assert not reject_shot.exists()  # the straddled screenshot was discarded


def test_below_threshold_logs_bound_value_not_running_max(tmp_path, monkeypatch) -> None:
    # The transfer peaks at 95 then tails to 60 (all < 100 threshold). The OLD code
    # logged the running max (95) paired with whatever screenshot it happened to hold —
    # that is exactly 录表里的速度 > 截图里的速度. Now the only recorded value comes from
    # the final bracket-bound capture (60), which equals the number frozen in its image;
    # the unbound 95 peak is never paired with a screenshot.
    final_shot = tmp_path / "final.png"
    final_shot.write_bytes(b"png")
    texts = iter(
        [
            f"{READ_MARKER} 95 MB/s {WRITE_MARKER} 0 B/s",   # iter1 loop-top: peak 95 (best_seen)
            f"{READ_MARKER} 80 MB/s {WRITE_MARKER} 0 B/s",   # iter2 loop-top: process ends after
            f"{READ_MARKER} 60 MB/s {WRITE_MARKER} 0 B/s",   # final bound capture: pre
            f"{READ_MARKER} 60 MB/s {WRITE_MARKER} 0 B/s",   # final bound capture: post (== pre)
        ]
    )
    process_checks = iter([False, True])
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\hdd", local_dir=tmp_path)

    class FakePage:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    monkeypatch.setattr(capture, "_frame_text", lambda _frame: next(texts))
    monkeypatch.setattr(capture, "_process_finished", lambda _proc: next(process_checks))
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "capture_page", lambda *_args, **_kwargs: final_shot)

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
        100,
        1,
        capture_on_low=True,
    )

    assert result.reached_threshold is False
    assert result.values["rate_mbps"] == "60"          # bound to the screenshot, NOT the 95 peak
    assert result.values["speed_status"] == "below_threshold"
    assert result.shot_path == final_shot              # 60 is the number frozen in final_shot


def test_transfer_capture_replaces_lower_bound_with_higher(tmp_path, monkeypatch) -> None:
    # A second bracket-bound capture at a higher steady rate replaces the first: the
    # recorded value and screenshot update together, and the superseded (lower) screenshot
    # is deleted so it can never be mistaken for the proof image.
    shot200 = tmp_path / "s200.png"
    shot300 = tmp_path / "s300.png"
    shot200.write_bytes(b"png")
    shot300.write_bytes(b"png")
    texts = iter(
        [
            f"{READ_MARKER} 200 MB/s {WRITE_MARKER} 0 B/s",  # iter1 loop-top
            f"{READ_MARKER} 200 MB/s {WRITE_MARKER} 0 B/s",  # bound1 pre
            f"{READ_MARKER} 200 MB/s {WRITE_MARKER} 0 B/s",  # bound1 post
            f"{READ_MARKER} 300 MB/s {WRITE_MARKER} 0 B/s",  # iter2 loop-top
            f"{READ_MARKER} 300 MB/s {WRITE_MARKER} 0 B/s",  # bound2 pre
            f"{READ_MARKER} 300 MB/s {WRITE_MARKER} 0 B/s",  # bound2 post
        ]
    )
    shots = iter([shot200, shot300])
    process_checks = iter([False, True])
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\hdd", local_dir=tmp_path)

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
        100,
        1,
    )

    assert result.reached_threshold is True
    assert result.values["rate_mbps"] == "300"
    assert result.shot_path == shot300
    assert not shot200.exists()  # superseded lower-rate screenshot dropped
    assert shot300.exists()


def test_capture_on_low_straddle_yields_no_shot(tmp_path, monkeypatch) -> None:
    # On the final attempt, if the failure capture straddles a refresh edge (pre != post),
    # _bound_transfer_capture returns None and control falls through to a text-only
    # diagnostic with NO screenshot — the unbound peak is never paired with an ambiguous image.
    straddle_shot = tmp_path / "straddle.png"
    straddle_shot.write_bytes(b"png")
    texts = iter(
        [
            f"{READ_MARKER} 80 MB/s {WRITE_MARKER} 0 B/s",   # iter1 loop-top (best_seen=80)
            f"{READ_MARKER} 80 MB/s {WRITE_MARKER} 0 B/s",   # iter2 loop-top, process ends
            f"{READ_MARKER} 70 MB/s {WRITE_MARKER} 0 B/s",   # final bound capture: pre
            f"{READ_MARKER} 90 MB/s {WRITE_MARKER} 0 B/s",   # final bound capture: post (!= pre -> None)
        ]
    )
    process_checks = iter([False, True])
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\hdd", local_dir=tmp_path)

    class FakePage:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    monkeypatch.setattr(capture, "_frame_text", lambda _frame: next(texts))
    monkeypatch.setattr(capture, "_process_finished", lambda _proc: next(process_checks))
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "capture_page", lambda *_args, **_kwargs: straddle_shot)

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
        100,
        1,
        capture_on_low=True,
    )

    assert result.reached_threshold is False
    assert result.shot_path is None
    assert result.values["rate_mbps"] == "80"
    assert result.values["speed_status"] == "below_threshold"
    assert not straddle_shot.exists()  # the ambiguous straddled screenshot was discarded


def test_bound_capture_rejects_unit_boundary_flip(tmp_path, monkeypatch) -> None:
    # "8192 KB/s" and "8 MB/s" are equal byte counts but DIFFERENT on-screen strings. The
    # bracket compares the displayed amount+unit (not the byte count), so this window is
    # rejected rather than recording a label the screenshot between the reads may not show.
    shot = tmp_path / "flip.png"
    shot.write_bytes(b"png")
    texts = iter(
        [
            f"{READ_MARKER} 8192 KB/s {WRITE_MARKER} 0 B/s",  # pre
            f"{READ_MARKER} 8 MB/s {WRITE_MARKER} 0 B/s",     # post (equal bytes, different text)
        ]
    )
    monkeypatch.setattr(capture, "_frame_text", lambda _frame: next(texts))
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "capture_page", lambda *_args, **_kwargs: shot)

    out = capture._bound_transfer_capture(
        object(),
        object(),
        "SN123",
        "hdd_read",
        "hdd",
        "download",
        tmp_path,
        min_rate_bytes=0,
    )

    assert out is None
    assert not shot.exists()  # ambiguous (unit-flip) shot discarded


def test_transfer_capture_records_bracketed_gbps(tmp_path, monkeypatch) -> None:
    # A steady GB/s plateau passes and is recorded as MB/s correctly (1.2 GB/s -> 1228.8),
    # with the 'rate' label preserving the on-screen GB/s string.
    shot = tmp_path / "gbps.png"
    shot.write_bytes(b"png")
    text = f"{READ_MARKER} 1.2 GB/s {WRITE_MARKER} 0 B/s"
    process_checks = iter([False, True])
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\hdd", local_dir=tmp_path)

    class FakePage:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    monkeypatch.setattr(capture, "_frame_text", lambda _frame: text)
    monkeypatch.setattr(capture, "_process_finished", lambda _proc: next(process_checks))
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(capture, "capture_page", lambda *_args, **_kwargs: shot)

    result = capture._capture_transfer_attempt(
        FakePage(),
        object(),
        object(),
        cfg,
        "SN123",
        "ssd_read",
        "ssd",
        "download",
        tmp_path,
        {"speed_stable_samples": 1, "speed_attempt_timeout_s": 5, "speed_sample_interval_ms": 100},
        300,
        1,
    )

    assert result.reached_threshold is True
    assert result.values["rate"] == "1.2 GB/s"
    assert result.values["rate_mbps"] == "1228.8"
    assert result.shot_path == shot


def test_bracket_max_span_config_override() -> None:
    assert capture._transfer_bracket_max_span_s({}) == capture.TRANSFER_BRACKET_MAX_SPAN_S
    assert capture._transfer_bracket_max_span_s({"bracket_max_span_s": 0.9}) == 0.9
    # 0 / non-positive / garbage fall back to the default rather than disabling the guard.
    assert capture._transfer_bracket_max_span_s({"bracket_max_span_s": 0}) == capture.TRANSFER_BRACKET_MAX_SPAN_S
    assert capture._transfer_bracket_max_span_s({"bracket_max_span_s": "x"}) == capture.TRANSFER_BRACKET_MAX_SPAN_S


def test_bound_capture_span_guard_rejects_slow_shot_and_is_configurable(tmp_path, monkeypatch) -> None:
    # A display read identically before AND after the shot is STILL rejected when the bracket
    # spanned too long (a slow screenshot could hide an A->B->A flip and show a number neither
    # read saw). The ceiling is tunable so a slow factory PC can widen it instead of
    # false-failing a steady page. perf_counter is mocked to force the span.
    text = f"{READ_MARKER} 560 MB/s {WRITE_MARKER} 0 B/s"
    # two _bound_transfer_capture calls, each reads perf_counter twice (start, end):
    # call#1 span 0.9 (rejected at default 0.6), call#2 span 0.4 (accepted at widened 1.0)
    perf = iter([0.0, 0.9, 0.0, 0.4])
    monkeypatch.setattr(capture.time, "perf_counter", lambda: next(perf))
    monkeypatch.setattr(capture, "_frame_text", lambda _frame: text)
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_args, **_kwargs: None)

    slow_shot = tmp_path / "slow.png"
    fast_shot = tmp_path / "fast.png"
    shots = iter([slow_shot, fast_shot])

    def fake_capture(*_args, **_kwargs):
        path = next(shots)
        path.write_bytes(b"png")
        return path

    monkeypatch.setattr(capture, "capture_page", fake_capture)

    out_slow = capture._bound_transfer_capture(
        object(), object(), "SN123", "hdd_read", "hdd", "download", tmp_path,
        min_rate_bytes=0, max_span_s=0.6,
    )
    assert out_slow is None
    assert not slow_shot.exists()  # slow (ambiguous) shot discarded

    out_fast = capture._bound_transfer_capture(
        object(), object(), "SN123", "hdd_read", "hdd", "download", tmp_path,
        min_rate_bytes=0, max_span_s=1.0,
    )
    assert out_fast is not None
    sample, shot = out_fast
    assert shot == fast_shot and fast_shot.exists()
    assert sample.rate_mb_s == 560


def test_run_reuse_existing_false_forces_fresh_capture(tmp_path, monkeypatch) -> None:
    # Resuming an interrupted run (reuse_existing=True) reuses a valid on-disk capture;
    # a retest (reuse_existing=False) re-shoots the page even though one exists, so the
    # operator never sees the previous run's cached screenshots presented as a new run.
    selectors = {
        "desktop_launchers": {},
        "capture_nav": {},
        "capture_pages": {"network_interface": {"app": "ctlmgr"}},
    }
    old = tmp_path / "old.png"
    fresh = tmp_path / "fresh.png"

    for name in (
        "dismiss_desktop_overlays",
        "_cleanup_all_transfer_workdirs_if_idle",
        "_cleanup_legacy_transfer_workdirs_if_idle",
        "_emit_capture_event",
        "_cleanup_transfer_run_dir",
    ):
        monkeypatch.setattr(capture, name, lambda *_a, **_k: None)
    monkeypatch.setattr(capture, "_existing_capture_path", lambda *_a, **_k: old)
    monkeypatch.setattr(capture, "_capture_standard_page", lambda *_a, **_k: fresh)

    def run_once(reuse: bool) -> dict:
        return capture.run(
            object(),
            "http://nas",
            "SN123",
            ["network_interface"],
            selectors,
            tmp_path / "图片",
            {"username": "a", "password": "b"},
            reuse_existing=reuse,
        )

    assert run_once(True)["network_interface"] == str(old)      # resume -> reuse cached
    assert run_once(False)["network_interface"] == str(fresh)   # retest -> fresh capture


def test_purge_stale_page_captures_keeps_fail_and_other_pages(tmp_path) -> None:
    # Force-fresh retest prunes a page's prior non-FAIL screenshots but keeps FAIL_
    # diagnostics and never touches other pages' captures.
    images = tmp_path / "图片"
    images.mkdir()
    old1 = images / "SN123_hdd_read_20260101_120000.png"
    old2 = images / "SN123_hdd_read_20260101_130000.png"
    fail = images / "SN123_FAIL_hdd_read_20260101_140000.png"
    other = images / "SN123_ssd_read_20260101_120000.png"
    for p in (old1, old2, fail, other):
        p.write_bytes(b"png")

    capture._purge_stale_page_captures(images, "SN123", "hdd_read")

    assert not old1.exists() and not old2.exists()  # stale captures for this page pruned
    assert fail.exists()   # FAIL diagnostic kept
    assert other.exists()  # other page untouched


def test_purge_stale_page_captures_tolerates_missing_dir(tmp_path) -> None:
    # No 图片 folder yet (first run) -> no-op, no error.
    capture._purge_stale_page_captures(tmp_path / "missing", "SN123", "hdd_read")


def test_cpu_temp_c_parses_value() -> None:
    assert capture._cpu_temp_c({"cpu_temp": "80 ℃"}) == 80.0
    assert capture._cpu_temp_c({"cpu_temp": "57.5℃"}) == 57.5
    assert capture._cpu_temp_c({"cpu_temp": ""}) is None
    assert capture._cpu_temp_c({}) is None


def _temp_page(monkeypatch, reads, shot_factory):
    monkeypatch.setattr(capture, "_log_key_values", lambda *_a, **_k: next(reads))
    monkeypatch.setattr(capture, "dismiss_desktop_overlays", lambda *_a, **_k: None)
    monkeypatch.setattr(capture, "capture_page", shot_factory)

    class FakePage:
        def wait_for_timeout(self, _ms: int) -> None:
            return None

    return FakePage()


def test_bound_value_capture_accepts_stable_temp_and_rpm(tmp_path, monkeypatch) -> None:
    # temp + RPM identical just before and just after the shot -> the recorded reading
    # provably equals the numbers frozen in the image.
    shot = tmp_path / "rm.png"
    shot.write_bytes(b"png")
    reads = iter(
        [
            {"cpu_temp": "65 ℃", "device_fan_rpm": "1200 转/分"},  # pre
            {"cpu_temp": "65 ℃", "device_fan_rpm": "1200 转/分"},  # post (== pre -> accept)
        ]
    )
    page = _temp_page(monkeypatch, reads, lambda *_a, **_k: shot)

    out = capture._bound_value_capture(
        page, object(), {}, "SN123", "resource_monitor", tmp_path,
        stable_keys=capture.RESOURCE_MONITOR_STABLE_KEYS,
    )
    assert out is not None
    values, kept = out
    assert values["cpu_temp"] == "65 ℃" and kept == shot and shot.exists()


def test_bound_value_capture_rejects_when_reading_straddles_shot(tmp_path, monkeypatch) -> None:
    # CPU cooling fast + fan ramping: temp 80->74 and RPM 954->2177 across the shot. The
    # image would show a number neither read matches, so the capture is rejected (this is
    # exactly the 954-reported / 2177-on-screen divergence seen on a real unit).
    shot = tmp_path / "rm.png"
    shot.write_bytes(b"png")
    reads = iter(
        [
            {"cpu_temp": "80 ℃", "device_fan_rpm": "954 转/分"},   # pre
            {"cpu_temp": "74 ℃", "device_fan_rpm": "2177 转/分"},  # post (!= pre -> reject)
        ]
    )
    page = _temp_page(monkeypatch, reads, lambda *_a, **_k: shot)

    out = capture._bound_value_capture(
        page, object(), {}, "SN123", "resource_monitor", tmp_path,
        stable_keys=capture.RESOURCE_MONITOR_STABLE_KEYS,
    )
    assert out is None
    assert not shot.exists()  # ambiguous straddled shot discarded


def test_capture_bound_value_page_falls_back_after_budget(tmp_path, monkeypatch) -> None:
    # If every bracket straddles (display keeps changing) until the budget runs out, fall
    # back to a plain read+shot so the page still yields an artifact.
    reads = iter(
        [
            {"cpu_temp": "80 ℃", "device_fan_rpm": "1"},  # iter1 pre
            {"cpu_temp": "74 ℃", "device_fan_rpm": "1"},  # iter1 post (straddle)
            {"cpu_temp": "73 ℃", "device_fan_rpm": "1"},  # iter2 pre
            {"cpu_temp": "70 ℃", "device_fan_rpm": "1"},  # iter2 post (straddle)
            {"cpu_temp": "70 ℃", "device_fan_rpm": "1"},  # fallback plain read
        ]
    )
    shots = iter([tmp_path / "s1.png", tmp_path / "s2.png", tmp_path / "s3.png"])

    def shot_factory(*_a, **_k):
        p = next(shots)
        p.write_bytes(b"png")
        return p

    page = _temp_page(monkeypatch, reads, shot_factory)
    monotonic = iter([0.0, 0.0, 7.0])
    monkeypatch.setattr(capture.time, "monotonic", lambda: next(monotonic, 99.0))

    values, shot = capture._capture_bound_value_page(
        page, object(), {}, "SN123", "resource_monitor", tmp_path,
        stable_keys=capture.RESOURCE_MONITOR_STABLE_KEYS, settle_budget_s=6.0,
    )
    assert values["cpu_temp"] == "70 ℃"
    assert shot == tmp_path / "s3.png" and shot.exists()
    assert not (tmp_path / "s1.png").exists()  # straddled shots cleaned up
    assert not (tmp_path / "s2.png").exists()


def test_wait_for_cpu_temp_returns_once_cooled(monkeypatch) -> None:
    # resource_monitor: poll until CPU settles <= limit before capturing (drops the load-tail
    # spike). 80 -> 78 -> 65: waits twice, returns when <= 70.
    reads = iter([{"cpu_temp": "80 ℃"}, {"cpu_temp": "78 ℃"}, {"cpu_temp": "65 ℃"}])
    monkeypatch.setattr(capture, "_collect_key_values", lambda *_a, **_k: next(reads))
    monotonic = iter([0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(capture.time, "monotonic", lambda: next(monotonic, 999.0))
    waits: list[int] = []

    class FakePage:
        def wait_for_timeout(self, ms: int) -> None:
            waits.append(ms)

    capture._wait_for_cpu_temp_within_limit(FakePage(), object(), {}, "resource_monitor", 70.0, 30.0)
    assert len(waits) == 2  # waited through 80 and 78, returned at 65


def test_wait_for_cpu_temp_no_wait_when_already_within_limit(monkeypatch) -> None:
    monkeypatch.setattr(capture, "_collect_key_values", lambda *_a, **_k: {"cpu_temp": "60 ℃"})
    monkeypatch.setattr(capture.time, "monotonic", lambda: 0.0)
    waits: list[int] = []

    class FakePage:
        def wait_for_timeout(self, ms: int) -> None:
            waits.append(ms)

    capture._wait_for_cpu_temp_within_limit(FakePage(), object(), {}, "resource_monitor", 70.0, 30.0)
    assert waits == []  # already cool -> capture immediately


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
        "http://192.0.2.168:9999",
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
        "http://192.0.2.168:9999",
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
            "http://192.0.2.168:9999",
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
            "http://192.0.2.168:9999",
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
    cfg = SmbTransferConfig(unc_share=r"\\192.0.2.168\ssd", local_dir=Path("."))
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
