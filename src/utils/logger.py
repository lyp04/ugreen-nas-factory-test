from __future__ import annotations

import sys
from pathlib import Path
from loguru import logger

_managed_sink_ids: list[int] = []


def setup_logger(output_dir: Path, sn: str | None = None, include_stderr: bool = True) -> None:
    global _managed_sink_ids
    for sid in _managed_sink_ids:
        try:
            logger.remove(sid)
        except Exception:
            pass
    _managed_sink_ids = []

    if include_stderr:
        _managed_sink_ids.append(
            logger.add(sys.stderr, level="INFO", format="<green>{time:HH:mm:ss}</green> <level>{level:<7}</level> {message}")
        )

    log_dir = output_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    _managed_sink_ids.append(
        logger.add(log_dir / "run.log", level="DEBUG", rotation="10 MB", encoding="utf-8")
    )


def remove_default_sinks() -> None:
    logger.remove()


__all__ = ["logger", "setup_logger", "remove_default_sinks"]
