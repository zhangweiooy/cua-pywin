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
                kernel32 = ctypes.windll.kernel32
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, 9)  # SW_RESTORE

                current_thread = int(kernel32.GetCurrentThreadId())
                foreground_window = int(user32.GetForegroundWindow())
                thread_ids = {
                    int(user32.GetWindowThreadProcessId(hwnd, None)),
                }
                if foreground_window:
                    thread_ids.add(
                        int(
                            user32.GetWindowThreadProcessId(
                                foreground_window,
                                None,
                            )
                        )
                    )

                attached_threads = []
                try:
                    for thread_id in thread_ids:
                        if (
                            thread_id
                            and thread_id != current_thread
                            and user32.AttachThreadInput(
                                current_thread,
                                thread_id,
                                True,
                            )
                        ):
                            attached_threads.append(thread_id)
                    user32.BringWindowToTop(hwnd)
                    user32.SetForegroundWindow(hwnd)
                finally:
                    for thread_id in reversed(attached_threads):
                        user32.AttachThreadInput(
                            current_thread,
                            thread_id,
                            False,
                        )

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
