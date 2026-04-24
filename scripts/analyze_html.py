"""Search wizard HTML captures for specific text nodes and close-button classes.

Avoid passing Chinese strings via PowerShell command line (GBK mangling) —
put them in this file instead.
"""
from pathlib import Path

TARGETS = {
    "step_05": ["跳过", "jump-btn", "skip"],
    "step_09": ["跳过", "skip", "初始化", "重启", "完成"],
    "step_10": ["开始教学", "close-icon", "ivu-modal-close"],
    "step_11": ["跳过", "cancel-btn", "skip"],
    "step_12": ["前往存储管理"],
    "step_13": ["欢迎使用存储管理", "ivu-modal-close", "banner-close", "close-btn", "我知道了", "跳过"],
}

root = Path("output/wizard")

for step, keywords in TARGETS.items():
    html_path = root / f"{step}.html"
    if not html_path.exists():
        print(f"missing {html_path}")
        continue
    html = html_path.read_text(encoding="utf-8")
    print(f"\n=== {step} ({len(html)} chars) ===")
    for kw in keywords:
        idx = html.find(kw)
        if idx < 0:
            print(f"  [miss] {kw}")
            continue
        ctx = html[max(0, idx - 220):idx + 80]
        ctx = ctx.replace("\n", " ").replace("\r", "")
        print(f"  [hit] {kw} @ {idx}")
        print(f"    ...{ctx}...")
