"""Dump interactive DOM elements of the NAS UI to JSON for selector analysis.

Interactive loop mode:
    python scripts/inspect_dom.py --url http://192.0.2.152:9999 --out-dir output/wizard

    A browser opens. Each time you press Enter in the terminal, the current DOM
    is captured as <out-dir>/step_NN.json + .html + .png.
    Type 'q' then Enter to quit.

Single-shot mode (legacy, back-compat):
    python scripts/inspect_dom.py --url <url> --out output/dom.json --shot output/dom.png
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

JS_COLLECT = r"""
() => {
    const SELECTOR = 'button, a[role="button"], a, input, textarea, select, [role="button"], [role="checkbox"], [role="radio"], [role="tab"], [role="link"], [contenteditable="true"]';

    function collectInDoc(doc, framePath) {
        const out = [];
        const seen = new Set();
        let nodes;
        try { nodes = doc.querySelectorAll(SELECTOR); } catch (e) { return out; }
        nodes.forEach((el) => {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return;
            const win = doc.defaultView || window;
            const style = win.getComputedStyle(el);
            if (style.visibility === 'hidden' || style.display === 'none') return;

            const entry = {
                frame: framePath,
                tag: el.tagName.toLowerCase(),
                type: el.getAttribute('type'),
                id: el.id || null,
                name: el.getAttribute('name'),
                class: el.className && typeof el.className === 'string' ? el.className : null,
                text: (el.innerText || el.value || '').trim().slice(0, 80),
                placeholder: el.getAttribute('placeholder'),
                role: el.getAttribute('role'),
                aria_label: el.getAttribute('aria-label'),
                data_testid: el.getAttribute('data-testid') || el.getAttribute('data-qa') || el.getAttribute('data-test'),
                rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
            };
            const data_attrs = {};
            for (const a of el.attributes) {
                if (a.name.startsWith('data-') && !a.name.startsWith('data-v-')) {
                    data_attrs[a.name] = a.value;
                }
            }
            if (Object.keys(data_attrs).length) entry.data_attrs = data_attrs;
            const key = JSON.stringify([framePath, entry.tag, entry.id, entry.class, entry.text, entry.rect]);
            if (seen.has(key)) return;
            seen.add(key);
            out.push(entry);
        });

        // Recurse into same-origin iframes
        const iframes = doc.querySelectorAll('iframe');
        iframes.forEach((ifr) => {
            const rect = ifr.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return;  // hidden iframes
            const name = ifr.getAttribute('name') || ifr.id || 'anon';
            let childDoc = null;
            try { childDoc = ifr.contentDocument; } catch (e) {}
            if (!childDoc) return;  // cross-origin or not ready
            const childPath = framePath ? `${framePath}>${name}` : name;
            out.push(...collectInDoc(childDoc, childPath));
        });
        return out;
    }

    const all = collectInDoc(document, '');
    // Also collect iframe URLs for debugging
    const frameIndex = [];
    document.querySelectorAll('iframe').forEach((ifr) => {
        const rect = ifr.getBoundingClientRect();
        frameIndex.push({
            name: ifr.getAttribute('name') || ifr.id || null,
            src: ifr.getAttribute('src'),
            visible: rect.width > 0 && rect.height > 0,
            rect: { x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height) },
        });
    });
    return {
        url: location.href,
        title: document.title,
        lang: document.documentElement.lang,
        body_class: document.body.className,
        iframes: frameIndex,
        element_count: all.length,
        elements: all,
    };
}
"""


def capture(page, base: Path, label: str) -> None:
    base.parent.mkdir(parents=True, exist_ok=True)
    data = page.evaluate(JS_COLLECT)
    html = page.content()
    json_path = base.with_name(f"{base.name}.json")
    html_path = base.with_name(f"{base.name}.html")
    png_path = base.with_name(f"{base.name}.png")
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    html_path.write_text(html, encoding="utf-8")

    # Save per-iframe HTML so we have the full content of each app
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        name = frame.name
        if not name:
            parts = [p for p in (frame.url or "").split("/") if p]
            if len(parts) >= 2:
                name = parts[-2]
            elif parts:
                name = parts[-1]
            else:
                name = "anon"
        # Sanitize name for filename
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)[:40]
        try:
            frame_html = frame.content()
            frame_path = base.with_name(f"{base.name}.frame_{safe}.html")
            frame_path.write_text(frame_html, encoding="utf-8")
        except Exception:
            pass  # frame may be detaching / cross-origin

    page.screenshot(path=str(png_path), full_page=True)
    visible_frames = sum(1 for f in data.get("iframes", []) if f.get("visible"))
    print(f"[{label}] {data['element_count']} elements ({visible_frames} visible iframes) -> {json_path.name} + .html + .png")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--out-dir", default=None, help="Loop mode: capture each step into this directory")
    p.add_argument("--out", default=None, help="Single-shot mode: JSON path")
    p.add_argument("--shot", default=None, help="Single-shot mode: screenshot path")
    p.add_argument("--wait-ms", type=int, default=5000, help="Wait after navigation before first capture")
    p.add_argument("--channel", default="msedge")
    args = p.parse_args()

    if not args.out_dir and not args.out:
        print("ERROR: specify --out-dir (loop mode) or --out (single-shot)", file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        launch_kwargs = {"headless": False}
        if args.channel and args.channel != "chromium":
            launch_kwargs["channel"] = args.channel
        browser = pw.chromium.launch(**launch_kwargs)
        context = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="zh-CN")
        page = context.new_page()
        page.set_default_timeout(30000)
        page.goto(args.url, wait_until="networkidle")
        page.wait_for_timeout(args.wait_ms)

        if args.out_dir:
            out_dir = Path(args.out_dir)
            out_dir.mkdir(parents=True, exist_ok=True)
            print(f"Loop mode. Capturing into {out_dir}/")
            print("Drive the wizard in the browser. Press Enter here to capture each step. Type 'q' then Enter to quit.")
            step = 0
            while True:
                step += 1
                label = f"step_{step:02d}"
                base = out_dir / label
                try:
                    resp = input(f"[step {step}] Enter=capture, q=quit: ").strip().lower()
                except EOFError:
                    break
                if resp == "q":
                    print("Quit.")
                    break
                capture(page, base, label)
        else:
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            data = page.evaluate(JS_COLLECT)
            html = page.content()
            out_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            out_path.with_suffix(".html").write_text(html, encoding="utf-8")
            if args.shot:
                shot_path = Path(args.shot)
                shot_path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(shot_path), full_page=True)
            print(f"Wrote {out_path} ({data['element_count']} interactive elements)")
            print(f"Wrote {out_path.with_suffix('.html')} (full HTML)")
            if args.shot:
                print(f"Wrote {args.shot}")
            input("Press Enter to close browser...")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
