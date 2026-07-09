from src.gui import TimingSlice, _compact_timing_slices, build_timing_slices


def test_build_timing_slices_splits_each_update_cycle() -> None:
    logs = [
        "10:00:00 | INFO    | SN ABC 已加入队列\n",
        "10:00:10 | INFO    | Setup wizard -> http://nas:9999\n",
        "10:01:00 | INFO    | Setup wizard complete; desktop reached\n",
        "10:01:10 | INFO    | System update: existing update/reboot screen detected; waiting before provisioning\n",
        "10:02:10 | INFO    | System update: verifying latest status after update/reboot\n",
        "10:02:20 | INFO    | System update: clicking 立即更新\n",
        "10:02:20 | INFO    | System update: 立即更新 clicked; waiting for desktop to return\n",
        "10:04:00 | INFO    | System update: verifying latest status after update/reboot\n",
        "10:04:10 | INFO    | System update: update page now reports latest version\n",
        "10:04:20 | INFO    | Provisioning: ensure SMB service is enabled\n",
        "10:05:00 | INFO    | Capturing system_update via desktop app 'ctlmgr'\n",
    ]

    slices = {item.label: item.seconds for item in build_timing_slices(logs)}

    assert slices["首次设置"] == 50
    assert slices["更新1"] == 60
    assert slices["更新2"] == 100
    assert slices["建池共享"] == 40


def test_build_timing_slices_tolerates_small_out_of_order_log_lines() -> None:
    logs = [
        "10:56:28 | INFO    | SN HB670EE022517E15 已加入队列\n",
        "10:56:30 | INFO    | 浏览器已在后台启动，可按“显示浏览器”查看\n",
        "10:56:29 | INFO    | Setup wizard -> http://192.0.2.239:9999\n",
        "10:57:10 | INFO    | [Page 0] welcome + agreements\n",
        "10:59:16 | INFO    | Setup wizard complete; desktop reached\n",
        "10:59:16 | INFO    | System update: existing update/reboot screen detected; waiting before provisioning\n",
        "11:00:16 | INFO    | System update: verifying latest status after update/reboot\n",
    ]

    slices = {item.label: item.seconds for item in build_timing_slices(logs)}

    assert slices["准备"] == 2
    assert slices["首次设置"] == 166
    assert slices["更新1"] == 60


def test_compact_timing_slices_moves_short_phases_to_other() -> None:
    slices = _compact_timing_slices(
        [
            TimingSlice("准备更新", 8),
            TimingSlice("更新1", 90),
            TimingSlice("准备建池", 5),
            TimingSlice("建池共享", 45),
        ]
    )

    assert [(item.label, item.seconds) for item in slices] == [
        ("更新1", 90),
        ("建池共享", 45),
        ("其他", 13),
    ]
