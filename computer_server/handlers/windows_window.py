"""Windows-specific window handler extensions."""

from __future__ import annotations

import asyncio
from typing import Any, Dict

from .generic import GenericWindowHandler, platform, pwc


class WindowsWindowHandler(GenericWindowHandler):
    """Window handler with Windows foreground-lock bypass behavior."""

    async def activate_window(self, window_id: int | str) -> Dict[str, Any]:
        try:
            if pwc is None:
                return {"success": False, "error": "pywinctl not available"}
            w = self._get_window_by_id(window_id)
            if not w:
                return {"success": False, "error": "Window not found"}

            hwnd = int(window_id)
            if platform.system() == "Windows":
                import ctypes

                user32 = ctypes.windll.user32
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                user32.keybd_event(0x12, 0, 0, 0)  # VK_MENU (Alt) down
                user32.keybd_event(0x12, 0, 2, 0)  # Alt up
                user32.SetForegroundWindow(hwnd)
                await asyncio.sleep(0.05)
                foreground = int(user32.GetForegroundWindow())
                if foreground != hwnd:
                    return {
                        "success": False,
                        "error": "SetForegroundWindow did not make the target window foreground",
                        "window_id": hwnd,
                        "foreground_window_id": foreground,
                    }
            else:
                w.activate()
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
