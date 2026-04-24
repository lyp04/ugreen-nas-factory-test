"""For each target deliverable page, find which captured step_NN.html contains its marker text."""
from pathlib import Path

# Each entry: (description, set of marker strings that MUST all appear)
TARGETS = [
    ("fan_settings",      ["风扇", "转速"]),
    ("fan_mode_names",    ["普通", "静音", "全速"]),
    ("system_update",     ["系统更新", "检查更新"]),
    ("latest_version",    ["已是最新"]),
    ("nic_speed",         ["网卡", "协商速率"]),
    ("nic_10g",           ["10000", "10 Gbps"]),
    ("storage_pool_list", ["存储池", "RAID 0"]),
    ("cpu_temp_label",    ["CPU温度"]),
    ("disk_io_lane",      ["盘 1", "上传"]),
    ("shared_folder_mgmt",["共享文件夹"]),
]

root = Path("output/wizard")
files = sorted(root.glob("step_*.html"))

for desc, markers in TARGETS:
    found: list[str] = []
    for f in files:
        html = f.read_text(encoding="utf-8")
        if all(m in html for m in markers):
            found.append(f.stem)
    marker_str = " & ".join(markers)
    if found:
        print(f"  [{desc:20s}] {marker_str} -> {len(found)} hits: {found[:5]}{'...' if len(found)>5 else ''}")
    else:
        print(f"  [{desc:20s}] {marker_str} -> MISSING (need capture)")
