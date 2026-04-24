from __future__ import annotations

import time
from functools import wraps
from typing import Callable, TypeVar

from .logger import logger

T = TypeVar("T")


def with_retry(max_attempts: int, backoff_base: float = 2.0, exceptions: tuple[type[BaseException], ...] = (Exception,)) -> Callable[[Callable[..., T]], Callable[..., T]]:
    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            last_exc: BaseException | None = None
            for attempt in range(1, max_attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    if attempt == max_attempts:
                        break
                    wait = backoff_base ** (attempt - 1)
                    logger.warning(f"{fn.__name__} failed (attempt {attempt}/{max_attempts}): {exc}. Retrying in {wait}s")
                    time.sleep(wait)
            assert last_exc is not None
            raise last_exc
        return wrapper
    return decorator
