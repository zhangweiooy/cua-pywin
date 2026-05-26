"""Shared UIA call timeout/gating helpers."""

from __future__ import annotations

import logging
import threading
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class UIACallGate:
    """Run UIA calls behind a single in-flight daemon-thread gate."""

    def __init__(self) -> None:
        self._semaphore = threading.Semaphore(1)

    def run_with_timeout(self, func: Callable[[], T], timeout: float = 0.5) -> T | None:
        """Return call result, or None if another call is stuck or timed out."""
        if not self._semaphore.acquire(blocking=False):
            logger.debug("UIA call skipped; previous call still in flight")
            return None

        result: T | None = None
        done = threading.Event()

        def _target() -> None:
            nonlocal result
            try:
                result = func()
            except Exception:
                pass
            finally:
                done.set()
                self._semaphore.release()

        thread = threading.Thread(target=_target, daemon=True)
        thread.start()
        if not done.wait(timeout):
            logger.debug("UIA operation timed out after %.1fs", timeout)
            return None
        return result
