"""Classify the first-time UGOS setup wizard's UI framework (iView vs Arco).

The 2026-07 UGOS 1.17 rewrite moved the control-panel / storage-manager
*in-app iframes* from iView (``ivu-*``) to Arco (``arco-*``); the login page and
desktop shell stayed iView. The first-time **setup wizard** is a separate
frontend that is only served while a unit is still uninitialized, so it can't be
observed on a registered/booting device — you need a freshly flashed unit.

Point this at such a unit to answer "did the wizard also go Arco?" and to check
whether the ``setup_wizard`` selectors in ``config/selectors.yml`` still resolve:

    python scripts/probe_setup_wizard.py --url http://192.0.2.152:9999

Read-only: it never advances the wizard (no clicks), only reads the DOM.
Exit code 0 = a wizard was found and classified; 2 = the device wasn't at the
setup wizard (login / desktop / still starting), so try a fresh unit.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import yaml
from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CENSUS_JS = r"""
() => {
    const q = (s) => document.querySelectorAll(s).length;
    const t = (document.body && document.body.innerText || '').replace(/\s+/g, ' ');
    return {
        ivu: q('[class*="ivu-"]'),
        arco: q('[class*="arco-"]'),
        pw: q('input[type=password]'),
        start_btn: q('button.start-btn'),
        next_step: q('button.next-step-btn'),
        login_marker: q('.login-wrapper, .login-account'),
        launchers: q('.click-region'),
        text: t.slice(0, 200),
    };
}
"""


def classify_state(c: dict) -> str:
    if c["start_btn"] or c["next_step"] or any(k in c["text"] for k in ("开始系统初始化", "开始初始化", "欢迎使用绿联")):
        return "setup_wizard"
    if c["launchers"] >= 3:
        return "desktop"
    if c["pw"] or c["login_marker"]:
        return "login"
    if not c["text"].strip() or "启动中" in c["text"]:
        return "starting"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True, help="Device URL, e.g. http://192.0.2.152:9999")
    ap.add_argument("--wait", type=int, default=60, help="Seconds to wait for the page to render (default 60)")
    ap.add_argument("--out-dir", default="output/wizard", help="Where to dump DOM + screenshot")
    args = ap.parse_args()

    selectors = yaml.safe_load((PROJECT_ROOT / "config" / "selectors.yml").read_text(encoding="utf-8"))
    wizard_selectors = selectors.get("setup_wizard", {})

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, channel="msedge")
        page = browser.new_context(viewport={"width": 1600, "height": 1000}, locale="zh-CN").new_page()

        state, census = "starting", {}
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            try:
                page.goto(args.url, wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(4000)
                census = page.evaluate(CENSUS_JS)
                state = classify_state(census)
                if state in ("setup_wizard", "login", "desktop"):
                    break
            except Exception as exc:  # noqa: BLE001
                print(f"[probe] load error: {exc}", file=sys.stderr)
            time.sleep(6)

        print(f"[probe] device state: {state}")
        print(f"[probe] framework census: ivu={census.get('ivu')} arco={census.get('arco')}")

        if state != "setup_wizard":
            print("[probe] NOT at the setup wizard — use a freshly flashed / uninitialized unit.")
            print(f"[probe]   page text: {census.get('text', '')!r}")
            browser.close()
            return 2

        # --- we are on the wizard: classify and check the configured selectors ---
        (out_dir / "wizard.html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(out_dir / "wizard.png"), full_page=True)

        framework = "iView" if census["ivu"] and not census["arco"] else \
                    "Arco" if census["arco"] and not census["ivu"] else \
                    "mixed / unknown"
        print(f"\n[probe] WIZARD FRAMEWORK: {framework}  (ivu={census['ivu']}, arco={census['arco']})")

        print("\n[probe] setup_wizard selectors from config/selectors.yml — do they resolve here?")
        broken = 0
        for name, sel in wizard_selectors.items():
            if not isinstance(sel, str):
                continue
            try:
                n = page.locator(sel).count()
            except Exception as exc:  # noqa: BLE001
                n = -1
                print(f"    {name}: BAD SELECTOR ({exc})")
                broken += 1
                continue
            mark = "ok" if n > 0 else "MISSING"
            if n <= 0:
                broken += 1
            print(f"    [{mark:>7}] {name}  ({n})  {sel}")

        print(f"\n[probe] {broken} selector(s) did not resolve. DOM + shot in {out_dir}/")
        print("[probe] If the wizard is now Arco, update the `setup_wizard` block in")
        print("[probe] config/selectors.yml to comma-unions (old iView, new Arco), same as ctlmgr/storagemgr.")
        browser.close()
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
