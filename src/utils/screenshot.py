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
    # Intentionally does NOT create the directories. The folder is materialized
    # lazily by whoever writes the first real artifact (test_report checkpoint
    # after the device connects, a screenshot, run.log, ...). That way a manual
    # SN / SN-tail entry whose device is never found leaves no empty folder.
    sn_root = output_root / sn
    base = sn_root
    dirs = {
        "sn_root": sn_root,
        "base": base,
        "screenshots": base / "图片",
    }
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
            continue
        try:
            source_mtime = item.stat().st_mtime
            target_mtime = destination.stat().st_mtime
        except OSError:
            continue
        if source_mtime <= target_mtime:
            continue
        try:
            destination.unlink()
        except OSError:
            continue
        shutil.move(str(item), str(destination))


def _unique_target(dest_dir: Path, stem: str, suffix: str) -> Path:
    """Return a path under ``dest_dir`` that does not yet exist. ``_timestamp()`` has
    1-second granularity, so two captures of the same page within the same wall-clock
    second would otherwise collide and the second screenshot would silently overwrite
    the first — fatal when a caller keeps a reference to the first file (it would point
    at an overwritten or deleted image). Append _2, _3, ... until the name is free."""
    target = dest_dir / f"{stem}{suffix}"
    counter = 2
    while target.exists():
        target = dest_dir / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def capture_page(page: "Page", sn: str, page_key: str, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = _unique_target(dest_dir, f"{sn}_{page_key}_{_timestamp()}", ".png")
    try:
        page.screenshot(path=str(target), full_page=True)
    except Exception:
        # Don't leave a partial / zero-byte PNG behind if the screenshot fails mid-write;
        # a stray file would later be picked up as a (bogus) existing capture.
        try:
            target.unlink()
        except OSError:
            pass
        raise
    logger.info(f"Captured {page_key} -> {target.name}")
    return target


def capture_failure(page: "Page", sn: str, step: str, dest_dir: Path) -> None:
    dest_dir.mkdir(parents=True, exist_ok=True)
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
