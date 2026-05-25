"""Keystroke and scroll coalescing — pure logic, zero platform dependency.

Mirrors the coalescing behavior in Ghost OS Swift LearningRecorder:
- KeystrokeBuncher merges consecutive character keys into a single typeText action
- ScrollBuncher accumulates scroll deltas into a single scroll action
"""

from __future__ import annotations

import threading

# Flush timeouts (seconds) — matches Ghost OS Swift defaults.
KEYSTROKE_FLUSH_TIMEOUT = 0.5
SCROLL_FLUSH_TIMEOUT = 0.3


class KeystrokeBuncher:
    """Coalesces consecutive character keystrokes into typeText actions.

    When the user types "Hello" quickly, we capture 5 separate keyDown events.
    Rather than recording 5 individual keyPress actions, we buffer the characters
    and flush them as a single typeText action after inactivity.
    """

    def __init__(self, flush_callback):
        """
        Args:
            flush_callback: callable(text, app_name, bundle_id, window_title,
                           url, element_context) called when keystrokes are flushed.
        """
        self._flush_callback = flush_callback
        self._buffer: str = ""
        self._app_name: str = ""
        self._bundle_id: str = ""
        self._window_title: str | None = None
        self._url: str | None = None
        self._element_context = None
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def append(self, char: str, app_name: str, bundle_id: str,
               window_title: str | None, url: str | None,
               element_context=None) -> None:
        """Append a character to the buffer and reschedule the flush timer."""
        with self._lock:
            if not self._buffer:
                self._app_name = app_name
                self._bundle_id = bundle_id
                self._window_title = window_title
                self._url = url
                self._element_context = element_context
            self._buffer += char
            self._schedule_flush()

    def flush(self) -> str | None:
        """Flush the buffer immediately. Returns the text or None if empty."""
        with self._lock:
            if not self._buffer:
                return None
            text = self._buffer
            self._buffer = ""
            self._cancel_timer()
            ctx = (text, self._app_name, self._bundle_id,
                   self._window_title, self._url, self._element_context)
        self._flush_callback(*ctx)
        return text

    def is_empty(self) -> bool:
        with self._lock:
            return len(self._buffer) == 0

    def _schedule_flush(self):
        self._cancel_timer()
        self._timer = threading.Timer(KEYSTROKE_FLUSH_TIMEOUT, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def cancel(self):
        """Cancel any pending timer without flushing."""
        self._cancel_timer()


class ScrollBuncher:
    """Accumulates scroll wheel delta values into a single scroll action.

    Continuous scrolling produces many small scrollWheel events.
    We accumulate deltas and flush after inactivity to record a single
    meaningful scroll action.
    """

    def __init__(self, flush_callback):
        """
        Args:
            flush_callback: callable(delta_x, delta_y, x, y, app_name, bundle_id)
                           called when scroll deltas are flushed.
        """
        self._flush_callback = flush_callback
        self._delta_x: int = 0
        self._delta_y: int = 0
        self._x: float = 0
        self._y: float = 0
        self._app_name: str = ""
        self._bundle_id: str = ""
        self._timer: threading.Timer | None = None
        self._lock = threading.Lock()

    def append(self, dx: int, dy: int, x: float, y: float,
               app_name: str, bundle_id: str) -> None:
        """Accumulate scroll deltas and reschedule the flush timer."""
        with self._lock:
            if self._delta_x == 0 and self._delta_y == 0:
                self._x = x
                self._y = y
                self._app_name = app_name
                self._bundle_id = bundle_id
            self._delta_x += dx
            self._delta_y += dy
            self._schedule_flush()

    def flush(self) -> tuple | None:
        """Flush accumulated deltas. Returns (dx, dy, x, y, app, bid) or None."""
        with self._lock:
            if self._delta_x == 0 and self._delta_y == 0:
                return None
            result = (self._delta_x, self._delta_y, self._x, self._y,
                       self._app_name, self._bundle_id)
            self._delta_x = 0
            self._delta_y = 0
            self._cancel_timer()
        self._flush_callback(*result)
        return result

    def is_empty(self) -> bool:
        with self._lock:
            return self._delta_x == 0 and self._delta_y == 0

    def _schedule_flush(self):
        self._cancel_timer()
        self._timer = threading.Timer(SCROLL_FLUSH_TIMEOUT, self.flush)
        self._timer.daemon = True
        self._timer.start()

    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def cancel(self):
        """Cancel any pending timer without flushing."""
        self._cancel_timer()
