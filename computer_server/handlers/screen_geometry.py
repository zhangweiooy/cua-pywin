"""Screen geometry helpers for Windows virtual-desktop coordinates."""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from typing import Any, Dict

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class VirtualScreenGeometry:
    """Virtual desktop bounds in physical screen coordinates."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width

    @property
    def bottom(self) -> int:
        return self.y + self.height

    def to_dict(self) -> Dict[str, int]:
        return {
            "x": self.x,
            "y": self.y,
            "width": self.width,
            "height": self.height,
            "right": self.right,
            "bottom": self.bottom,
        }


_DPI_AWARENESS_SET = False


def ensure_dpi_awareness() -> None:
    """Best-effort per-monitor DPI awareness for physical coordinates."""
    global _DPI_AWARENESS_SET
    if _DPI_AWARENESS_SET or sys.platform != "win32":
        return

    try:
        # PER_MONITOR_AWARE_V2. Available on Windows 10+.
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        _DPI_AWARENESS_SET = True
        return
    except Exception:
        pass

    try:
        # PROCESS_PER_MONITOR_DPI_AWARE.
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        _DPI_AWARENESS_SET = True
        return
    except Exception:
        pass

    try:
        ctypes.windll.user32.SetProcessDPIAware()
        _DPI_AWARENESS_SET = True
    except Exception as exc:
        logger.debug("Failed to set DPI awareness: %s", exc)


def get_virtual_screen_geometry() -> VirtualScreenGeometry:
    """Return Windows virtual desktop bounds, including negative origins."""
    if sys.platform == "win32":
        ensure_dpi_awareness()
        try:
            user32 = ctypes.windll.user32
            return VirtualScreenGeometry(
                x=int(user32.GetSystemMetrics(76)),   # SM_XVIRTUALSCREEN
                y=int(user32.GetSystemMetrics(77)),   # SM_YVIRTUALSCREEN
                width=int(user32.GetSystemMetrics(78)),   # SM_CXVIRTUALSCREEN
                height=int(user32.GetSystemMetrics(79)),  # SM_CYVIRTUALSCREEN
            )
        except Exception as exc:
            logger.debug("Virtual screen metrics failed: %s", exc)

    return VirtualScreenGeometry(x=0, y=0, width=0, height=0)


def geometry_from_mss_monitor(monitor: Dict[str, Any]) -> VirtualScreenGeometry:
    """Convert an mss monitor dict to VirtualScreenGeometry."""
    return VirtualScreenGeometry(
        x=int(monitor.get("left", 0)),
        y=int(monitor.get("top", 0)),
        width=int(monitor.get("width", 0)),
        height=int(monitor.get("height", 0)),
    )
