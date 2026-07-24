from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path
from loguru import logger

from ..report.redact import scrub_text

_managed_sink_ids: list[int] = []


def setup_logger(
    output_dir: Path,
    sn: str | None = None,
    include_stderr: bool = True,
    filename: str = "run.log",
    extra_secrets: Iterable[str] = (),
) -> None:
    global _managed_sink_ids
    # CLI mode owns the process-wide logger. Remove Loguru's default stderr
    # sink too; otherwise the redacted managed sinks would be accompanied by a
    # second, raw copy of every message.
    logger.remove()
    _managed_sink_ids = []
    secrets = tuple(str(value or "") for value in extra_secrets)

    def redact_record(record: dict) -> bool:
        record["message"] = scrub_text(
            record.get("message", ""),
            extra_secrets=secrets,
            mask_identifiers=False,
        )
        return True

    if include_stderr:
        _managed_sink_ids.append(
            logger.add(
                sys.stderr,
                level="INFO",
                format="<green>{time:HH:mm:ss}</green> <level>{level:<7}</level> {message}",
                filter=redact_record,
            )
        )

    log_dir = output_dir
    log_dir.mkdir(parents=True, exist_ok=True)
    _managed_sink_ids.append(
        logger.add(
            log_dir / filename,
            level="DEBUG",
            rotation="10 MB",
            encoding="utf-8",
            filter=redact_record,
        )
    )


def remove_default_sinks() -> None:
    logger.remove()


__all__ = ["logger", "setup_logger", "remove_default_sinks"]
