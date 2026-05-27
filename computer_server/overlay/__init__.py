"""
Agent cursor overlay for Windows.

Optional module — import failure degrades to invisible mode without
affecting core MCP functionality.

Usage::

    from computer_server.overlay import init, animate_to, shutdown

    init()                        # once at server start
    animate_to(x, y)              # fire-and-forget fly animation (non-blocking)
"""

import logging
import math
import threading

from .cursor import AgentCursor

logger = logging.getLogger(__name__)

# Module-level singleton
_agent_cursor: AgentCursor | None = None
_last_target_x: float | None = None
_last_target_y: float | None = None
_last_target_lock = threading.Lock()


def init(enabled: bool = True) -> AgentCursor | None:
    """Start the overlay animation thread. Returns the singleton or None."""
    global _agent_cursor
    if _agent_cursor is not None:
        return _agent_cursor
    if not enabled:
        return None
    try:
        _agent_cursor = AgentCursor(enabled=True)
        if not _agent_cursor.enabled:
            _agent_cursor = None
        else:
            # Start at the actual cursor position so the first flight
            # doesn't arc from (0, 0) in the top-left corner.
            import ctypes

            class _POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            _pt = _POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(_pt))
            set_initial_position(_pt.x, _pt.y)
    except Exception:
        logger.debug("Agent cursor overlay not available", exc_info=True)
        _agent_cursor = None
    return _agent_cursor


def _compute_heading(x: int, y: int) -> float:
    """Visual heading from last-target → new-target, tip trailing (+π)."""
    with _last_target_lock:
        return _compute_heading_unlocked(x, y)


def _compute_heading_and_update(x: int, y: int) -> float:
    """Compute heading and atomically mark (x, y) as the next target."""
    global _last_target_x, _last_target_y
    with _last_target_lock:
        heading = _compute_heading_unlocked(x, y)
        _last_target_x = float(x)
        _last_target_y = float(y)
        return heading


def _compute_heading_unlocked(x: int, y: int) -> float:
    if _last_target_x is None or _last_target_y is None:
        return math.pi
    dx = x - _last_target_x
    dy = y - _last_target_y
    if math.hypot(dx, dy) <= 0.5:
        return _agent_cursor._state.heading if _agent_cursor is not None else math.pi
    return math.atan2(dy, dx) + math.pi


def _update_last_target(x: int, y: int) -> None:
    global _last_target_x, _last_target_y
    with _last_target_lock:
        _last_target_x = float(x)
        _last_target_y = float(y)


def animate_to(x: int, y: int) -> None:
    """Fly the cursor to (x, y) on a background daemon thread."""
    if _agent_cursor is None:
        return
    heading = _compute_heading_and_update(x, y)
    threading.Thread(
        target=_agent_cursor.move_to,
        args=(x, y, heading),
        daemon=True,
        name="agent-cursor-fly",
    ).start()


def fly_and_wait(x: int, y: int) -> None:
    """Fly the cursor to (x, y) and block until the spring overshoot begins.

    Call this from ``run_in_executor`` to avoid blocking the event loop.
    """
    if _agent_cursor is None:
        return
    heading = _compute_heading_and_update(x, y)
    _agent_cursor.move_to(x, y, heading)


def move_to(x: int, y: int, heading: float = 0.785) -> None:
    """Fly the cursor to (x, y). Blocks until the Dubins path finishes
    and the spring overshoot begins.  Use ``animate_to`` for fire-and-forget.
    """
    global _last_target_x, _last_target_y
    if _agent_cursor is None:
        return
    with _last_target_lock:
        _last_target_x = float(x)
        _last_target_y = float(y)
    _agent_cursor.move_to(x, y, heading)


def set_initial_position(x: int, y: int, heading: float = 0.785) -> None:
    """Teleport the cursor without animation — use on first launch."""
    global _last_target_x, _last_target_y
    if _agent_cursor is None:
        return
    with _last_target_lock:
        _last_target_x = float(x)
        _last_target_y = float(y)
    _agent_cursor.set_initial_position(x, y, heading)


def shutdown() -> None:
    """Stop the animation thread and destroy the overlay window."""
    global _agent_cursor, _last_target_x, _last_target_y
    if _agent_cursor is not None:
        _agent_cursor.shutdown()
        _agent_cursor = None
    with _last_target_lock:
        _last_target_x = None
        _last_target_y = None
