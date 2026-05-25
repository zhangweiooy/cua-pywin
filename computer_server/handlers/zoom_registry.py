"""Zoom context registry for coordinate back-translation.

Stores per-window zoom context so that `from_zoom=True` click/drag coordinates
can be translated back from zoomed-image space to original screen coordinates.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Dict, Optional, Tuple


@dataclass
class ZoomContext:
    """Stores the context of a zoom operation for coordinate back-translation."""

    window_id: int
    crop_rect: Tuple[int, int, int, int]  # (x1, y1, x2, y2) in screen coords (after padding)
    scale_x: float  # zoom output_width / crop_width
    scale_y: float  # zoom output_height / crop_height
    output_width: int
    output_height: int
    padding: int


class ZoomRegistry:
    """Stores last zoom context per window for from_zoom coordinate translation.

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
            self._data: Dict[int, ZoomContext] = {}
            self._data_lock = threading.Lock()
            self._initialized = True

    def set(self, pid: int, ctx: ZoomContext) -> None:
        """Store zoom context for a process."""
        with self._data_lock:
            self._data[pid] = ctx

    def get(self, pid: int) -> Optional[ZoomContext]:
        """Retrieve zoom context for a process, or None."""
        with self._data_lock:
            return self._data.get(pid)

    def unscale(self, pid: int, x: int, y: int) -> Tuple[int, int]:
        """Convert coordinates from zoomed image space back to original screen space.

        Args:
            pid: The process that was zoomed.
            x: X coordinate in the zoomed image.
            y: Y coordinate in the zoomed image.

        Returns:
            (original_x, original_y) in screen coordinates.

        Raises:
            ValueError: If no zoom context exists for this process.
        """
        with self._data_lock:
            ctx = self._data.get(pid)
            if ctx is None:
                raise ValueError(
                    f"No zoom context for pid {pid}. "
                    "Call computer_zoom first."
                )
            # Reverse the zoom: zoomed coords -> original coords
            # original = crop_origin + zoomed_coords / scale
            original_x = int(ctx.crop_rect[0] + x / ctx.scale_x)
            original_y = int(ctx.crop_rect[1] + y / ctx.scale_y)
            return original_x, original_y

    def clear(self, pid: int) -> None:
        """Remove zoom context for a process."""
        with self._data_lock:
            self._data.pop(pid, None)

    def clear_all(self) -> None:
        """Remove all zoom contexts."""
        with self._data_lock:
            self._data.clear()


def get_zoom_registry() -> ZoomRegistry:
    """Get the global ZoomRegistry singleton."""
    return ZoomRegistry()
