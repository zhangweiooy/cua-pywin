"""
Windows implementation of automation and accessibility handlers.

This implementation uses pynput for GUI automation and Windows-specific APIs
for accessibility and system operations.
"""

import asyncio
import base64
import functools
import logging
import os
import subprocess
import threading
from typing import Any, Callable, Dict, List, Optional, Tuple, TypeVar, Union

F = TypeVar("F", bound=Callable[..., Any])


def require_unlocked_desktop(func: F) -> F:
    """Decorator that checks if the Windows desktop is locked before executing.

    Returns an error response if the desktop is locked, preventing automation
    actions that would silently fail on the Windows Secure Desktop.
    """

    @functools.wraps(func)
    async def wrapper(
        self: "WindowsAutomationHandler", *args: Any, **kwargs: Any
    ) -> Dict[str, Any]:
        if self.is_desktop_locked():
            return {
                "success": False,
                "error": "Windows desktop is locked. Automation input is blocked by OS security.",
                "suggestion": "Unlock the Windows desktop (Win+L screen) and retry. All mouse/keyboard actions are blocked while locked.",
            }
        return await func(self, *args, **kwargs)

    return wrapper  # type: ignore[return-value]


from PIL import Image, ImageGrab
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key as KBKey
from pynput.mouse import Button as MouseButton
from pynput.mouse import Controller as MouseController

# Configure logger
logger = logging.getLogger(__name__)
_CLIPBOARD_LOCK = threading.Lock()

# Input injection uses pynput.

# Try to import Windows-specific modules
try:
    import win32api
    import win32con
    import win32gui

    logger.info("Windows API modules successfully imported")
    WINDOWS_API_AVAILABLE = True
except Exception as e:
    logger.error(
        f"Windows API modules import failed: {str(e)}. Some Windows-specific features will be unavailable."
    )
    WINDOWS_API_AVAILABLE = False

from .base import (
    BaseAccessibilityHandler,
    BaseAutomationHandler,
    normalize_screenshot_format,
)
from .screen_geometry import ensure_dpi_awareness, get_virtual_screen_geometry
from .screenshot_service import encode_image

ensure_dpi_awareness()


def _has_non_ascii(text: str) -> bool:
    """True when *text* contains any character outside the 7-bit ASCII range.

    Non-ASCII characters (Chinese, CJK, emoji, accented letters, etc.) cannot
    be reliably typed via synthetic key codes — pynput/pynput produce wrong
    output for these.  They must go through clipboard paste (Ctrl+V).
    """
    return any(ord(c) > 127 for c in text)


class WindowsAccessibilityHandler(BaseAccessibilityHandler):
    """Windows implementation of accessibility handler."""

    async def get_accessibility_tree(
        self, window_id: Optional[int] = None
    ) -> Dict[str, Any]:
        """Get the UIA accessibility tree for a window.

        Uses UI Automation instead of raw Win32 HWND enumeration so
        UWP/XAML (Calculator), Electron (VS Code), and Qt/DuiLib apps
        are all supported via the same code path.

        Args:
            window_id: Optional window handle.  Defaults to the
                foreground window if not specified.

        Returns:
            Dict with 'tree_markdown', 'node_count', and 'tree_quality'.
        """
        if not WINDOWS_API_AVAILABLE:
            return {"success": False, "error": "Windows API not available"}

        try:
            from computer_server.handlers.windows_uia import walk_uia_tree

            if window_id is None:
                hwnd = win32gui.GetForegroundWindow()
                if not hwnd:
                    return {"success": False, "error": "No foreground window found"}
                window_id = hwnd

            result = walk_uia_tree(window_id)

            return {
                "success": True,
                "tree_markdown": result.tree_markdown,
                "tree_quality": result.tree_quality,
                "node_count": result.total_elements,
            }
        except Exception as e:
            logger.error(f"Error getting accessibility tree: {e}")
            return {"success": False, "error": str(e)}

class WindowsAutomationHandler(BaseAutomationHandler):
    """Windows implementation of automation handler using pynput and Windows APIs."""

    mouse = MouseController()
    keyboard = KeyboardController()

    def is_desktop_locked(self) -> bool:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            return hwnd == 0
        except Exception:
            return False

    def _map_button(self, button: str) -> MouseButton:
        """Map a string button name to pynput MouseButton."""
        b = (button or "left").lower()
        if b == "left":
            return MouseButton.left
        if b == "right":
            return MouseButton.right
        if b == "middle":
            return MouseButton.middle
        # default to left
        return MouseButton.left

    def _key_from_string(self, key: str):
        """Convert a key string (e.g., 'enter', 'ctrl', 'a') to pynput Key or char."""
        if not key:
            return None
        lk = key.lower()
        special = {
            "enter": KBKey.enter,
            "return": KBKey.enter,
            "esc": KBKey.esc,
            "escape": KBKey.esc,
            "space": KBKey.space,
            "tab": KBKey.tab,
            "backspace": KBKey.backspace,
            "delete": KBKey.delete,
            "home": KBKey.home,
            "end": KBKey.end,
            "pageup": KBKey.page_up,
            "pagedown": KBKey.page_down,
            "up": KBKey.up,
            "down": KBKey.down,
            "left": KBKey.left,
            "right": KBKey.right,
            "shift": KBKey.shift,
            "ctrl": KBKey.ctrl,
            "control": KBKey.ctrl,
            "alt": KBKey.alt,
            "option": KBKey.alt,
            "cmd": KBKey.cmd,
            "win": KBKey.cmd,
            "meta": KBKey.cmd,
            "capslock": KBKey.caps_lock,
            "f1": KBKey.f1,
            "f2": KBKey.f2,
            "f3": KBKey.f3,
            "f4": KBKey.f4,
            "f5": KBKey.f5,
            "f6": KBKey.f6,
            "f7": KBKey.f7,
            "f8": KBKey.f8,
            "f9": KBKey.f9,
            "f10": KBKey.f10,
            "f11": KBKey.f11,
            "f12": KBKey.f12,
        }
        if lk in special:
            return special[lk]
        # single character
        if len(key) == 1:
            return key
        return None

    # Mouse Actions
    @require_unlocked_desktop
    async def mouse_down(
        self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left"
    ) -> Dict[str, Any]:
        """Press and hold a mouse button at the specified coordinates.

        Args:
            x (Optional[int]): The x-coordinate to move to before pressing. If None, uses current position.
            y (Optional[int]): The y-coordinate to move to before pressing. If None, uses current position.
            button (str): The mouse button to press ("left", "right", or "middle").

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.press(self._map_button(button))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def mouse_up(
        self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left"
    ) -> Dict[str, Any]:
        """Release a mouse button at the specified coordinates.

        Args:
            x (Optional[int]): The x-coordinate to move to before releasing. If None, uses current position.
            y (Optional[int]): The y-coordinate to move to before releasing. If None, uses current position.
            button (str): The mouse button to release ("left", "right", or "middle").

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.release(self._map_button(button))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def move_cursor(self, x: int, y: int) -> Dict[str, Any]:
        """Move the mouse cursor to the specified coordinates.

        Args:
            x (int): The x-coordinate to move to.
            y (int): The y-coordinate to move to.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            self.mouse.position = (x, y)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def left_click(self, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """Perform a left mouse click at the specified coordinates.

        Args:
            x (Optional[int]): The x-coordinate to click at. If None, clicks at current position.
            y (Optional[int]): The y-coordinate to click at. If None, clicks at current position.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.click(MouseButton.left, 1)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def right_click(self, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        """Perform a right mouse click at the specified coordinates.

        Args:
            x (Optional[int]): The x-coordinate to click at. If None, clicks at current position.
            y (Optional[int]): The y-coordinate to click at. If None, clicks at current position.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.click(MouseButton.right, 1)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def middle_click(
        self, x: Optional[int] = None, y: Optional[int] = None
    ) -> Dict[str, Any]:
        """Perform a middle mouse click at the specified coordinates.

        Args:
            x (Optional[int]): The x-coordinate to click at. If None, clicks at current position.
            y (Optional[int]): The y-coordinate to click at. If None, clicks at current position.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.click(MouseButton.middle, 1)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def double_click(
        self, x: Optional[int] = None, y: Optional[int] = None
    ) -> Dict[str, Any]:
        """Perform a double left mouse click at the specified coordinates.

        Args:
            x (Optional[int]): The x-coordinate to double-click at. If None, clicks at current position.
            y (Optional[int]): The y-coordinate to double-click at. If None, clicks at current position.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.click(MouseButton.left, 2)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def drag_to(
        self, x: int, y: int, button: str = "left", duration: float = 0.5
    ) -> Dict[str, Any]:
        """Drag from the current position to the specified coordinates.

        Args:
            x (int): The x-coordinate to drag to.
            y (int): The y-coordinate to drag to.
            button (str): The mouse button to use for dragging ("left", "right", or "middle").
            duration (float): The time in seconds to take for the drag operation.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            btn = self._map_button(button)
            self.mouse.press(btn)
            start_x, start_y = self.mouse.position
            steps = max(1, int(duration / 0.02))
            for i in range(1, steps + 1):
                t = i / steps
                ix = int(start_x + (x - start_x) * t)
                iy = int(start_y + (y - start_y) * t)
                self.mouse.position = (ix, iy)
                await asyncio.sleep(duration / steps)
            self.mouse.position = (x, y)
            self.mouse.release(btn)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def drag(
        self, path: List[Tuple[int, int]], button: str = "left", duration: float = 0.5
    ) -> Dict[str, Any]:
        """Drag the mouse through a series of coordinates.

        Args:
            path (List[Tuple[int, int]]): A list of (x, y) coordinate tuples to drag through.
            button (str): The mouse button to use for dragging ("left", "right", or "middle").
            duration (float): The total time in seconds for the entire drag operation.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            if not path:
                return {"success": False, "error": "Path is empty"}

            btn = self._map_button(button)
            total_points = len(path) - 1
            self.mouse.position = path[0]
            self.mouse.press(btn)

            for i in range(1, len(path)):
                x, y = path[i]
                step_duration = duration / total_points if total_points > 0 else duration
                start_x, start_y = self.mouse.position
                steps = max(1, int(step_duration / 0.02))
                for j in range(1, steps + 1):
                    t = j / steps
                    self.mouse.position = (
                        int(start_x + (x - start_x) * t),
                        int(start_y + (y - start_y) * t),
                    )
                    await asyncio.sleep(step_duration / steps)
                self.mouse.position = (x, y)

            self.mouse.release(btn)

            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Keyboard Actions
    @require_unlocked_desktop
    async def key_down(self, key: str) -> Dict[str, Any]:
        """Press and hold a keyboard key.

        Args:
            key (str): The key to press down (e.g., 'ctrl', 'shift', 'a').

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            k = self._key_from_string(key)
            if k is None:
                return {"success": False, "error": f"Unknown key: {key}"}
            self.keyboard.press(k)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def key_up(self, key: str) -> Dict[str, Any]:
        """Release a keyboard key.

        Args:
            key (str): The key to release (e.g., 'ctrl', 'shift', 'a').

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            k = self._key_from_string(key)
            if k is None:
                return {"success": False, "error": f"Unknown key: {key}"}
            self.keyboard.release(k)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def type_text(self, text: str, chunk_size: int = 3) -> Dict[str, Any]:
        """Type the specified text via clipboard paste in small chunks.

        Clipboard paste is used for ALL text — both ASCII and non-ASCII.
        The text is split into small chunks and pasted one at a time with
        brief delays to simulate a typewriter streaming effect.

        Args:
            text (str): The text to type.
            chunk_size (int): Characters per paste chunk (default 3).
                Set to 0 to paste all at once.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            return await self._type_text_via_clipboard(text, chunk_size=chunk_size)
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def _type_text_via_clipboard(self, text: str, chunk_size: int = 3) -> Dict[str, Any]:
        """Type text by clipboard paste (Ctrl+V) in small chunks for a
        typewriter streaming effect.

        Saves the original clipboard content and restores it after pasting.
        """
        import asyncio

        import pyperclip

        acquired = await asyncio.to_thread(_CLIPBOARD_LOCK.acquire)
        original = None
        try:
            try:
                original = pyperclip.paste()
            except Exception:
                original = None

            # Paste all at once if chunk_size is 0 or text is short
            if chunk_size <= 0 or len(text) <= chunk_size:
                try:
                    pyperclip.copy(text)
                except Exception as e:
                    return {"success": False, "error": f"Clipboard copy failed: {e}"}
                try:
                    self.keyboard.press(KBKey.ctrl)
                    self.keyboard.press("v")
                    self.keyboard.release("v")
                    self.keyboard.release(KBKey.ctrl)
                except Exception as e:
                    return {"success": False, "error": f"Clipboard paste failed: {e}"}
            else:
                # Paste in chunks for streaming effect
                for i in range(0, len(text), chunk_size):
                    chunk = text[i:i + chunk_size]
                    try:
                        pyperclip.copy(chunk)
                    except Exception as e:
                        return {"success": False, "error": f"Clipboard copy failed: {e}"}
                    try:
                        self.keyboard.press(KBKey.ctrl)
                        self.keyboard.press("v")
                        self.keyboard.release("v")
                        self.keyboard.release(KBKey.ctrl)
                    except Exception as e:
                        return {"success": False, "error": f"Clipboard paste failed: {e}"}
                    await asyncio.sleep(0.08)
        finally:
            if original is not None:
                try:
                    await asyncio.sleep(0.05)
                    pyperclip.copy(original)
                except Exception:
                    pass
            if acquired:
                _CLIPBOARD_LOCK.release()

        return {"success": True, "method": "clipboard"}

    @require_unlocked_desktop
    async def press_key(self, key: str) -> Dict[str, Any]:
        """Press and release a keyboard key.

        Args:
            key (str): The key to press (e.g., 'enter', 'space', 'tab').

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            k = self._key_from_string(key)
            if k is None:
                return {"success": False, "error": f"Unknown key: {key}"}
            self.keyboard.press(k)
            self.keyboard.release(k)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def hotkey(self, keys: List[str]) -> Dict[str, Any]:
        """Press a combination of keys simultaneously.

        Args:
            keys (List[str]): The keys to press together (e.g., ['ctrl', 'c'], ['alt', 'tab']).

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            # press keys sequentially while holding modifiers
            resolved = [self._key_from_string(k) for k in keys]
            if any(k is None for k in resolved):
                return {"success": False, "error": "Unknown key in hotkey sequence"}
            seq: List[Union[str, KBKey]] = [k for k in resolved if k is not None]  # type: ignore[assignment]
            if not seq:
                return {"success": False, "error": "Empty hotkey sequence"}
            # hold all except the last
            for k in seq[:-1]:
                self.keyboard.press(k)
            # tap last
            last = seq[-1]
            self.keyboard.press(last)
            self.keyboard.release(last)
            # release modifiers
            for k in reversed(seq[:-1]):
                self.keyboard.release(k)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Scrolling Actions
    @require_unlocked_desktop
    async def scroll(self, x: int, y: int) -> Dict[str, Any]:
        """Scroll vertically at the current cursor position.

        Args:
            x (int): Horizontal scroll amount.
            y (int): Vertical scroll amount. Positive values scroll up, negative values scroll down.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            self.mouse.scroll(x, y)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def scroll_down(self, clicks: int = 1) -> Dict[str, Any]:
        """Scroll down by the specified number of clicks.

        Args:
            clicks (int): The number of scroll clicks to perform downward.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            # negative y to scroll down
            self.mouse.scroll(0, -abs(clicks))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @require_unlocked_desktop
    async def scroll_up(self, clicks: int = 1) -> Dict[str, Any]:
        """Scroll up by the specified number of clicks.

        Args:
            clicks (int): The number of scroll clicks to perform upward.

        Returns:
            Dict[str, Any]: A dictionary with success status and optional error message.
        """
        try:
            self.mouse.scroll(0, abs(clicks))
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Screen Actions
    @require_unlocked_desktop
    async def screenshot(self, format: str = "png", quality: int = 95) -> Dict[str, Any]:
        """Capture a screenshot of the entire screen.

        Args:
            format: "png" (lossless, default), "jpeg" or "jpg" (lossy, smaller).
            quality: JPEG quality 1-95 (clamped); ignored for PNG.
        """
        try:
            fmt, quality = normalize_screenshot_format(format, quality)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        try:
            screenshot = ImageGrab.grab(all_screens=True)
            if not isinstance(screenshot, Image.Image):
                return {"success": False, "error": "Failed to capture screenshot"}

            image_data = base64.b64encode(
                encode_image(screenshot, fmt, quality)
            ).decode()
            geometry = get_virtual_screen_geometry()
            virtual_screen = geometry.to_dict()
            return {
                "success": True,
                "image_data": image_data,
                "format": fmt,
                "origin": {"x": geometry.x, "y": geometry.y},
                "virtual_screen": virtual_screen,
                "capture_metadata": {
                    "capture_mode": "screen_full",
                    "capture_source": "pil_imagegrab",
                    "target": "virtual_screen",
                    "occlusion_risk": False,
                    "occlusion_detection": "not_applicable",
                    "virtual_screen": virtual_screen,
                    "output": {
                        "width": screenshot.width,
                        "height": screenshot.height,
                        "format": fmt,
                    },
                    "warnings": [],
                },
            }
        except Exception as e:
            return {"success": False, "error": f"Screenshot error: {str(e)}"}

    async def get_screen_size(self) -> Dict[str, Any]:
        """Get the size of the screen in pixels.

        Returns:
            Dict[str, Any]: A dictionary containing the success status and either
                           screen size information or an error message.
                           Structure: {"success": bool, "size": {"width": int, "height": int}} or
                                    {"success": bool, "error": str}
        """
        try:
            if WINDOWS_API_AVAILABLE:
                geometry = get_virtual_screen_geometry()
                return {
                    "success": True,
                    "size": {
                        "width": geometry.width,
                        "height": geometry.height,
                        "x": geometry.x,
                        "y": geometry.y,
                    },
                    "virtual_screen": geometry.to_dict(),
                }
            else:
                # Fallback: use ImageGrab
                img = ImageGrab.grab(all_screens=True)
                return {"success": True, "size": {"width": img.width, "height": img.height, "x": 0, "y": 0}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_cursor_position(self) -> Dict[str, Any]:
        """Get the current position of the mouse cursor.

        Returns:
            Dict[str, Any]: A dictionary containing the success status and either
                           cursor position or an error message.
                           Structure: {"success": bool, "position": {"x": int, "y": int}} or
                                    {"success": bool, "error": str}
        """
        try:
            if WINDOWS_API_AVAILABLE:
                pos = win32gui.GetCursorPos()
                return {"success": True, "position": {"x": pos[0], "y": pos[1]}}
            else:
                # Fallback: use pynput controller
                x, y = self.mouse.position
                return {"success": True, "position": {"x": int(x), "y": int(y)}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_window_bounds(self, pid: int, window_id: int) -> Optional[Dict[str, Any]]:
        """Get the window's screen-space bounds via GetWindowRect."""
        if not WINDOWS_API_AVAILABLE:
            return None
        try:
            hwnd = int(window_id)
            # Minimized windows return iconic position (-32000, -32000) — skip
            if win32gui.IsIconic(hwnd):
                return None
            rect = win32gui.GetWindowRect(hwnd)
            return {"x": rect[0], "y": rect[1], "width": rect[2] - rect[0], "height": rect[3] - rect[1]}
        except Exception:
            return None

    async def copy_to_clipboard(self) -> Dict[str, Any]:
        """Get the current clipboard content under the shared clipboard lock."""
        import asyncio

        import pyperclip

        acquired = False
        try:
            acquired = await asyncio.to_thread(_CLIPBOARD_LOCK.acquire)
            content = pyperclip.paste()
            return {"success": True, "content": content}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            if acquired:
                _CLIPBOARD_LOCK.release()

    async def set_clipboard(self, text: str) -> Dict[str, Any]:
        """Set clipboard content under the shared clipboard lock."""
        import asyncio

        import pyperclip

        acquired = False
        try:
            acquired = await asyncio.to_thread(_CLIPBOARD_LOCK.acquire)
            pyperclip.copy(text)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
        finally:
            if acquired:
                _CLIPBOARD_LOCK.release()

    # Command Execution (Windows override for multi-encoding support)
    async def run_command(self, command: str, shell: str = "powershell", timeout: Optional[float] = None) -> Dict[str, Any]:
        """Execute a shell command asynchronously.

        Args:
            command (str): The shell command to execute.
            shell (str): Shell to use — "powershell" (default) or "cmd".
            timeout (Optional[float]): Optional timeout in seconds.  When
                ``None`` (default), waits indefinitely.  SDK callers set
                this via ``sb.shell.run(cmd, timeout=...)``.

        Returns:
            Dict[str, Any]: A dictionary containing the success status and either
                           command output or an error message.
                           Structure: {"success": bool, "stdout": str, "stderr": str, "return_code": int} or
                                    {"success": bool, "error": str}
        """

        def decode_output(data: bytes) -> str:
            if not data:
                return ""
            encodings = ["utf-8", "gbk", "gb2312", "cp936", "latin1"]
            for enc in encodings:
                try:
                    return data.decode(enc)
                except (UnicodeDecodeError, LookupError):
                    continue
            return data.decode("utf-8", errors="replace")

        try:
            if shell == "cmd":
                process = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            else:
                process = await asyncio.create_subprocess_exec(
                    "powershell", "-NoProfile", "-NonInteractive", "-Command", command,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                )
            try:
                if timeout is None:
                    stdout, stderr = await process.communicate()
                else:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                process.kill()
                # Close the transport to prevent ResourceWarnings.
                # process.wait() blocks here because _wait() depends on
                # Proactor _exit_waiters callbacks, which are stalled by
                # cancelled communicate() leftover overlapped I/O.
                # _transport.close() tears down pipes synchronously.
                try:
                    process._transport.close()
                except Exception:
                    pass
                return {
                    "success": False,
                    "stdout": "",
                    "stderr": f"Command timed out after {timeout}s",
                    "return_code": -1,
                }
            return {
                "success": True,
                "stdout": decode_output(stdout),
                "stderr": decode_output(stderr),
                "return_code": process.returncode,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
