"""Shared screen capture helpers for recording and learning."""

from __future__ import annotations

import base64
import logging
import sys
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

CaptureTuple = Tuple[Optional[bytes], Optional[int], Optional[int]]


async def capture_png_from_automation(automation_handler: Any) -> Optional[bytes]:
    """Capture a PNG screenshot through an automation handler."""
    result = await automation_handler.screenshot(format="png")
    if result.get("success") and result.get("image_data"):
        return base64.b64decode(result["image_data"])
    return None


def capture_full_with_origin() -> CaptureTuple:
    """Capture the full virtual desktop as PNG bytes plus screen origin."""
    if sys.platform != "win32":
        return (None, None, None)
    try:
        import mss
        import mss.tools

        with mss.mss() as sct:
            monitor = sct.monitors[0]
            screenshot = sct.grab(monitor)
            return (
                mss.tools.to_png(screenshot.rgb, screenshot.size),
                int(monitor["left"]),
                int(monitor["top"]),
            )
    except ImportError:
        logger.warning("mss not installed; screenshots unavailable")
        return (None, None, None)
    except Exception:
        logger.debug("Screenshot capture failed", exc_info=True)
        return (None, None, None)


def capture_full() -> Optional[bytes]:
    """Capture the full virtual desktop as PNG bytes."""
    png, _x, _y = capture_full_with_origin()
    return png


def capture_region_with_origin(
    x: int,
    y: int,
    width: int,
    height: int,
) -> CaptureTuple:
    """Capture a screen region clamped to the virtual desktop."""
    if sys.platform != "win32":
        return (None, None, None)
    try:
        import mss
        import mss.tools

        with mss.mss() as sct:
            vd = sct.monitors[0]
            vd_left = vd["left"]
            vd_top = vd["top"]
            vd_right = vd_left + vd["width"]
            vd_bottom = vd_top + vd["height"]

            clamped_left = max(x, vd_left)
            clamped_top = max(y, vd_top)
            clamped_right = min(x + width, vd_right)
            clamped_bottom = min(y + height, vd_bottom)
            clamped_width = clamped_right - clamped_left
            clamped_height = clamped_bottom - clamped_top

            if clamped_width <= 0 or clamped_height <= 0:
                return (None, None, None)

            region = {
                "left": clamped_left,
                "top": clamped_top,
                "width": clamped_width,
                "height": clamped_height,
            }
            screenshot = sct.grab(region)
            return (
                mss.tools.to_png(screenshot.rgb, screenshot.size),
                clamped_left,
                clamped_top,
            )
    except ImportError:
        return (None, None, None)
    except Exception:
        logger.debug("Region capture failed", exc_info=True)
        return (None, None, None)


def capture_region(x: int, y: int, width: int, height: int) -> Optional[bytes]:
    """Capture a screen region as PNG bytes."""
    png, _x, _y = capture_region_with_origin(x, y, width, height)
    return png


def capture_active_window() -> CaptureTuple:
    """Capture the foreground window as PNG bytes plus window origin."""
    if sys.platform != "win32":
        return (None, None, None)
    try:
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return (None, None, None)
        rect = win32gui.GetWindowRect(hwnd)
        if not rect:
            return (None, None, None)
        left, top, right, bottom = rect
        if right <= left or bottom <= top:
            return (None, None, None)
        png = capture_region(left, top, right - left, bottom - top)
        return (png, left, top)
    except Exception:
        logger.debug("Active window capture failed", exc_info=True)
        return (None, None, None)
