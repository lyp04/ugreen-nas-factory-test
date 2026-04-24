from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .logger import logger

if TYPE_CHECKING:
    from playwright.sync_api import Page


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def session_dirs(output_root: Path, sn: str) -> dict[str, Path]:
    sn_root = output_root / sn
    base = sn_root
    dirs = {
        "sn_root": sn_root,
        "base": base,
        "screenshots": base / "图片",
    }
    for p in dirs.values():
        p.mkdir(parents=True, exist_ok=True)
    trace_dir = base / "traces"
    if trace_dir.exists():
        shutil.rmtree(trace_dir, ignore_errors=True)
    return dirs


def relocate_session_dirs(output_root: Path, current_dirs: dict[str, Path], sn: str) -> dict[str, Path]:
    current_root = current_dirs["sn_root"]
    target_root = output_root / sn
    if current_root.resolve() == target_root.resolve():
        return current_dirs

    if current_root.exists():
        if not target_root.exists():
            shutil.move(str(current_root), str(target_root))
        else:
            _merge_session_dirs(current_root, target_root)
            shutil.rmtree(current_root, ignore_errors=True)

    return session_dirs(output_root, sn)


def _merge_session_dirs(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for item in source.iterdir():
        destination = target / item.name
        if item.is_dir():
            _merge_session_dirs(item, destination)
            shutil.rmtree(item, ignore_errors=True)
            continue
        if not destination.exists():
            shutil.move(str(item), str(destination))


def capture_page(page: "Page", sn: str, page_key: str, dest_dir: Path) -> Path:
    filename = f"{sn}_{page_key}_{_timestamp()}.png"
    target = dest_dir / filename
    page.screenshot(path=str(target), full_page=True)
    logger.info(f"Captured {page_key} -> {filename}")
    return target


def capture_failure(page: "Page", sn: str, step: str, dest_dir: Path) -> None:
    ts = _timestamp()
    shot = dest_dir / f"{sn}_FAIL_{step}_{ts}.png"
    html = dest_dir / f"{sn}_FAIL_{step}_{ts}.html"
    try:
        page.screenshot(path=str(shot), full_page=True, timeout=5_000)
        logger.error(f"Saved failure screenshot: {shot.name}")
    except Exception as exc:
        logger.error(f"Failed to save failure screenshot for step={step}: {exc}")
    try:
        html.write_text(page.content(), encoding="utf-8")
        logger.error(f"Saved failure HTML: {html.name}")
    except Exception as exc:
        logger.error(f"Failed to save failure HTML for step={step}: {exc}")
