"""Show the unique visible interactive texts per capture, grouped by iframe.

Supports iframe-aware captures: each element has a `frame` field indicating
which same-origin iframe it was found in. We bucket elements by frame and show
the texts per frame so each app (ctlmgr, storagemgr, filemgr0, taskmgr) is visible.

Writes report directly to <dir>/_map.txt as UTF-8 to avoid Windows console
code page mangling when redirecting stdout.
"""
from pathlib import Path
import json
import sys

root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/apps")
files = sorted(root.glob("step_*.json"))

lines: list[str] = []
lines.append(f"Scanning {len(files)} captures in {root}/")
lines.append("")


def texts_of(el: dict) -> list[str]:
    out = []
    for k in ("text", "placeholder", "aria_label"):
        v = (el.get(k) or "").strip()
        if v and v not in ("on",):
            out.append(v[:50])
    return out


for f in files:
    data = json.loads(f.read_text(encoding="utf-8"))
    url = data.get("url", "")
    by_frame: dict[str, list[str]] = {}
    for el in data.get("elements", []):
        frame = el.get("frame") or "(main)"
        by_frame.setdefault(frame, []).extend(texts_of(el))

    iframes = data.get("iframes", [])
    visible = [fr for fr in iframes if fr.get("visible")]

    url_tail = url.split("#/", 1)[-1] if "#/" in url else url
    lines.append(f"{f.stem}  n={data['element_count']:3d}  url=#/{url_tail}  visible_iframes={len(visible)}")
    for frame, texts in sorted(by_frame.items()):
        unique = sorted(set(texts))
        if not unique:
            continue
        label = frame if frame else "(main)"
        lines.append(f"  [{label}] ({len(unique)}): {unique}")
    lines.append("")

out = root / "_map.txt"
out.write_text("\n".join(lines), encoding="utf-8")
print(f"Wrote {out} ({len(files)} captures)")
