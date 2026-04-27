from __future__ import annotations

import threading
from typing import Any


class ConcurrencyLimiter:
    """Explicit, injectable concurrency limiter for VM-bound operations.

    Replaces the module-level global semaphore to improve testability
    and eliminate hidden state coupling.
    """

    def __init__(self, limit: int) -> None:
        if limit < 1:
            limit = 1
        self._limit = limit
        self._semaphore = threading.Semaphore(limit)

    def acquire(self, blocking: bool = True, timeout: float | None = None) -> bool:
        return self._semaphore.acquire(blocking=blocking, timeout=timeout)

    def release(self) -> None:
        self._semaphore.release()

    def __enter__(self) -> ConcurrencyLimiter:
        self._semaphore.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self._semaphore.release()

    @property
    def limit(self) -> int:
        return self._limit
