"""Zoom context registry for coordinate back-translation.

Stores per-window zoom context so coordinates bound to a public ``zoom_id`` can
be translated back from zoomed-image space to original screen coordinates.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass
class ZoomContext:
    """Stores the context of a zoom operation for coordinate back-translation."""

    zoom_id: str
    pid: int
    window_id: int
    crop_rect: Tuple[int, int, int, int]  # (x1, y1, x2, y2) in screen coords (after padding)
    scale_x: float  # zoom output_width / crop_width
    scale_y: float  # zoom output_height / crop_height
    output_width: int
    output_height: int
    padding: int
    screenshot_id: Optional[str] = None


class ZoomRegistry:
    """Stores the last zoom context per window for coordinate translation.

    Thread-safe singleton.
    """

    _instance: Optional["ZoomRegistry"] = None
    _lock = threading.Lock()

    def __new__(cls) -> "ZoomRegistry":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if not hasattr(self, "_initialized"):
            self._data: Dict[Tuple[int, int], ZoomContext] = {}
            self._by_id: Dict[str, ZoomContext] = {}
            self._data_lock = threading.Lock()
            self._initialized = True

    def set(self, pid: int, window_id: int, ctx: ZoomContext) -> None:
        """Store zoom context for a (pid, window_id) pair."""
        with self._data_lock:
            self._data[(pid, window_id)] = ctx
            self._by_id[ctx.zoom_id] = ctx

    def get(self, pid: int, window_id: int) -> Optional[ZoomContext]:
        """Retrieve zoom context for a (pid, window_id) pair, or None."""
        with self._data_lock:
            return self._data.get((pid, window_id))

    def get_by_id(self, zoom_id: str) -> Optional[ZoomContext]:
        """Retrieve zoom context by stable zoom id, or None."""
        with self._data_lock:
            return self._by_id.get(zoom_id)

    def unscale(self, pid: int, window_id: int, x: int, y: int) -> Tuple[int, int]:
        """Convert coordinates from zoomed image space back to original screen space.

        Args:
            pid: The process that was zoomed.
            window_id: The window that was zoomed.
            x: X coordinate in the zoomed image.
            y: Y coordinate in the zoomed image.

        Returns:
            (original_x, original_y) in screen coordinates.

        Raises:
            ValueError: If no zoom context exists for this (pid, window_id).
        """
        with self._data_lock:
            ctx = self._data.get((pid, window_id))
            if ctx is None:
                raise ValueError(
                    f"No zoom context for pid {pid} window_id {window_id}. "
                    "Call computer_zoom first."
                )
            # Reverse the zoom: zoomed coords -> original coords
            # original = crop_origin + zoomed_coords / scale
            original_x = int(ctx.crop_rect[0] + x / ctx.scale_x)
            original_y = int(ctx.crop_rect[1] + y / ctx.scale_y)
            return original_x, original_y

    def unscale_by_id(self, zoom_id: str, x: int, y: int) -> Tuple[int, int]:
        """Convert zoom-image coordinates using a stable zoom id."""
        with self._data_lock:
            ctx = self._by_id.get(zoom_id)
            if ctx is None:
                raise ValueError(
                    f"No zoom context for zoom_id {zoom_id}. Call computer_zoom first."
                )
            return (
                int(ctx.crop_rect[0] + x / ctx.scale_x),
                int(ctx.crop_rect[1] + y / ctx.scale_y),
            )

    def clear(self, pid: int, window_id: int) -> None:
        """Remove zoom context for a (pid, window_id) pair."""
        with self._data_lock:
            self._data.pop((pid, window_id), None)
            for zoom_id, ctx in list(self._by_id.items()):
                if ctx.pid == pid and ctx.window_id == window_id:
                    self._by_id.pop(zoom_id, None)

    def clear_all(self) -> None:
        """Remove all zoom contexts."""
        with self._data_lock:
            self._data.clear()
            self._by_id.clear()


def get_zoom_registry() -> ZoomRegistry:
    """Get the global ZoomRegistry singleton."""
    return ZoomRegistry()
