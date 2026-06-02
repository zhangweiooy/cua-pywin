"""Windows platform backend for the learning subsystem.

Four subsystems:
  InputCapture   — global keyboard / mouse / scroll hooks via pynput
  UIAEnricher    — UI Automation element metadata at click positions
  AppDetector    — foreground window and process detection
  Screenshotter  — screen capture via mss

All Windows API calls are encapsulated here. The rest of the learning
module has zero platform dependencies.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from typing import Callable, Optional

from computer_server.handlers.control_types import semantic_role

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# ---------------------------------------------------------------------------
# Constants (from Ghost OS Swift Learning subsystem)
# ---------------------------------------------------------------------------

# Maximum recording duration (safety limit): 10 minutes.
MAX_RECORDING_DURATION = 600.0

# Polling interval for hook creation verification.
HOOK_START_POLL_INTERVAL = 0.01
HOOK_START_MAX_WAIT_ATTEMPTS = 50

# Processes where recording is paused (Windows equivalents of Keychain, System Prefs).
RESTRICTED_PROCESSES: set[str] = {
    "CredentialUIBroker.exe",
    "SystemSettings.exe",
    "WindowsSecurity.exe",
    "LogonUI.exe",
    "consent.exe",
}

# Sensitive field name patterns (case-insensitive), same as Swift.
SENSITIVE_FIELD_PATTERNS: list[str] = [
    "password", "passwd", "secret", "token", "api_key", "api.key",
    "apikey", "credential", "private.key", "ssn", "social.security",
]

# Browser process names for URL detection.
BROWSER_PROCESSES: set[str] = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
    "opera.exe",
}

# Virtual key code to key name mapping.
KEY_CODE_TO_NAME: dict[int, str] = {
    0x0D: "return",
    0x09: "tab",
    0x20: "space",
    0x08: "delete",
    0x1B: "escape",
    0x25: "left",
    0x27: "right",
    0x28: "down",
    0x26: "up",
    0x24: "home",
    0x23: "end",
    0x21: "pageup",
    0x22: "pagedown",
    0x2E: "forwarddelete",
    0x70: "f1", 0x71: "f2", 0x72: "f3", 0x73: "f4",
    0x74: "f5", 0x75: "f6", 0x76: "f7", 0x77: "f8",
    0x78: "f9", 0x79: "f10", 0x7A: "f11", 0x7B: "f12",
}

VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt
VK_SHIFT = 0x10
VK_LWIN = 0x5B
VK_RWIN = 0x5C

# COM apartment-threaded constant
_COINIT_APARTMENTTHREADED = 0x2


# ---------------------------------------------------------------------------
# COM init helper
# ---------------------------------------------------------------------------

def _ensure_com_initialized() -> None:
    """Initialize COM for the calling thread if not already initialized.

    UIA (UI Automation) requires COINIT_APARTMENTTHREADED on every thread
    that calls UIA APIs.  pynput listener threads do not init COM automatically.
    """
    if not IS_WINDOWS:
        return
    try:
        hr = ctypes.windll.ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
        # S_OK (0) or S_FALSE (1 = already initialized) are both fine.
        if hr < 0 and hr != 0x80010106:  # RPC_E_CHANGED_MODE
            logger.debug("CoInitializeEx returned 0x%08X", hr)
    except OSError:
        logger.warning("COM initialization failed — UIA enrichment unavailable")
    except Exception:
        logger.warning("COM initialization failed unexpectedly")


# ============================================================
# InputCapture — global input hooks via pynput
# ============================================================

class InputCapture:
    """Captures global keyboard, mouse, and scroll events on Windows.

    Uses pynput which wraps SetWindowsHookExW (WH_KEYBOARD_LL /
    WH_MOUSE_LL).  On Windows, pynput creates a hidden message-only
    window to pump hook messages — no GUI framework required.

    Events are delivered on pynput's dedicated listener threads.

    Double-click detection: Windows reports double-clicks as two separate
    WM_LBUTTONDOWN events.  We buffer the first click for the system
    double-click interval and only dispatch on timeout or second click.
    """

    def __init__(self):
        self._keyboard_listener = None
        self._mouse_listener = None
        self._running = threading.Event()
        self._click_lock = threading.Lock()
        self._pending_click = None   # (x, y, button_name) or None
        self._click_timer = None     # threading.Timer or None
        self._click_gen = 0          # generation counter for stale timeouts

    def start(self,
              on_key_down: Callable,
              on_mouse_down: Callable,
              on_scroll: Callable) -> None:
        """Start capturing input events."""
        if self._running.is_set():
            return

        from pynput import keyboard, mouse

        self._on_key_down = on_key_down
        self._on_mouse_down = on_mouse_down
        self._on_scroll = on_scroll
        self._running.set()

        self._keyboard_listener = keyboard.Listener(
            on_press=self._handle_key_press,
        )
        self._mouse_listener = mouse.Listener(
            on_click=self._handle_mouse_click,
            on_scroll=self._handle_mouse_scroll,
        )

        self._keyboard_listener.start()
        self._mouse_listener.start()

        # Poll until both listeners are alive, then verify.
        for _ in range(HOOK_START_MAX_WAIT_ATTEMPTS):
            time.sleep(HOOK_START_POLL_INTERVAL)
            if self._keyboard_listener.is_alive() and self._mouse_listener.is_alive():
                break

        if not self._keyboard_listener.is_alive():
            self._running.clear()
            self._mouse_listener.stop()
            raise RuntimeError(
                "pynput keyboard listener failed to start. "
                "Ensure the process has standard user privileges."
            )
        if not self._mouse_listener.is_alive():
            self._running.clear()
            self._keyboard_listener.stop()
            raise RuntimeError(
                "pynput mouse listener failed to start."
            )

        logger.info("InputCapture: hooks active (kbd=%s mouse=%s)",
                     self._keyboard_listener.is_alive(),
                     self._mouse_listener.is_alive())

    def is_healthy(self) -> bool:
        """Return True if both listener threads are alive and running."""
        return (
            self._running.is_set()
            and self._keyboard_listener is not None
            and self._keyboard_listener.is_alive()
            and self._mouse_listener is not None
            and self._mouse_listener.is_alive()
        )

    def stop(self) -> None:
        """Stop capturing input events."""
        self._running.clear()
        # Flush any pending click before tearing down
        with self._click_lock:
            if self._click_timer is not None:
                self._click_timer.cancel()
                self._click_timer = None
            pending = self._pending_click
            self._pending_click = None
        if pending is not None:
            x, y, btn = pending
            try:
                self._on_mouse_down(x=x, y=y, button=btn, count=1)
            except Exception:
                pass
        if self._keyboard_listener:
            self._keyboard_listener.stop()
            self._keyboard_listener = None
        if self._mouse_listener:
            self._mouse_listener.stop()
            self._mouse_listener = None

    # ---- keyboard callbacks ----

    def _handle_key_press(self, key) -> Optional[bool]:
        if not self._running.is_set():
            return False

        try:
            from pynput.keyboard import Key, KeyCode

            if isinstance(key, KeyCode):
                vk = key.vk or 0
                char = key.char
            elif isinstance(key, Key):
                vk = key.value.vk or 0
                char = None
            else:
                return

            modifiers = _get_active_modifiers()
            self._on_key_down(
                vk_code=vk,
                char=char,
                modifiers=modifiers,
                is_special=(char is None),
            )
        except Exception:
            logger.debug("Error in keyboard callback", exc_info=True)

    # ---- mouse callbacks ----

    def _handle_mouse_click(self, x, y, button, pressed) -> Optional[bool]:
        if not self._running.is_set():
            return False
        if not pressed:
            return

        try:
            btn_name = _map_button_name(button)
            dbl_ms = _get_double_click_time_ms()

            dispatch = None  # (x, y, btn, count) or None

            with self._click_lock:
                if self._click_timer is not None:
                    self._click_timer.cancel()
                    self._click_timer = None

                prev = self._pending_click
                if prev is not None:
                    px, py, pb = prev
                    if (pb == btn_name
                            and abs(x - px) <= 4
                            and abs(y - py) <= 4):
                        # Double-click: same button within tolerance
                        self._pending_click = None
                        dispatch = (px, py, pb, 2)
                    else:
                        # Different button or position: flush previous as single
                        self._pending_click = (x, y, btn_name)
                        self._schedule_click_timeout(dbl_ms)
                        dispatch = (px, py, pb, 1)
                else:
                    # First click in a potential sequence: buffer it
                    self._pending_click = (x, y, btn_name)
                    self._schedule_click_timeout(dbl_ms)

            if dispatch is not None:
                self._on_mouse_down(
                    x=dispatch[0], y=dispatch[1],
                    button=dispatch[2], count=dispatch[3],
                )
        except Exception:
            logger.debug("Error in mouse click callback", exc_info=True)

    def _schedule_click_timeout(self, dbl_ms: int) -> None:
        self._click_gen += 1
        gen = self._click_gen
        self._click_timer = threading.Timer(
            dbl_ms / 1000.0, lambda: self._on_click_timeout(gen),
        )
        self._click_timer.daemon = True
        self._click_timer.start()

    def _on_click_timeout(self, gen: int) -> None:
        """Timer callback: flush the buffered click as a single click."""
        dispatch = None
        with self._click_lock:
            if gen != self._click_gen:
                return  # Stale timeout superseded by a newer click
            if self._pending_click is None:
                return
            x, y, btn = self._pending_click
            self._pending_click = None
            self._click_timer = None
            dispatch = (x, y, btn, 1)
        if dispatch is not None:
            try:
                self._on_mouse_down(
                    x=dispatch[0], y=dispatch[1],
                    button=dispatch[2], count=dispatch[3],
                )
            except Exception:
                pass

    def _handle_mouse_scroll(self, x, y, dx, dy) -> Optional[bool]:
        if not self._running.is_set():
            return False
        try:
            if dx != 0 or dy != 0:
                self._on_scroll(x=x, y=y, dx=int(dx), dy=int(dy))
        except Exception:
            logger.debug("Error in scroll callback", exc_info=True)


# ---- Modifier detection ----

def _get_active_modifiers() -> list[str]:
    """Query Windows for currently held modifier keys via GetAsyncKeyState.

    NOTE: GetAsyncKeyState queries state at call time, not at event time.
    The window between pynput event delivery and this call is < 10ms, so
    modifier misattribution is extremely rare in practice.
    """
    if not IS_WINDOWS:
        return []

    mods: list[str] = []
    def _is_down(vk: int) -> bool:
        return (ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000) != 0

    if _is_down(VK_CONTROL):
        mods.append("ctrl")
    if _is_down(VK_MENU):
        mods.append("alt")
    if _is_down(VK_SHIFT):
        mods.append("shift")
    if _is_down(VK_LWIN) or _is_down(VK_RWIN):
        mods.append("win")
    return mods


def _get_double_click_time_ms() -> int:
    """Get the system double-click interval in milliseconds via Windows API."""
    if not IS_WINDOWS:
        return 500
    try:
        return ctypes.windll.user32.GetDoubleClickTime()
    except Exception:
        return 500


def _map_button_name(button) -> str:
    from pynput.mouse import Button
    if button == Button.left:
        return "left"
    elif button == Button.right:
        return "right"
    elif button == Button.middle:
        return "middle"
    return str(button)


# ============================================================
# UIAEnricher — UI Automation element metadata
# ============================================================

class UIAEnricher:
    """Enriches click positions with UI Automation element metadata.

    Mirrors the AX enrichment in Ghost OS Swift EventHandlers.enrichClick().

    Each public method initializes COM on the calling thread before
    accessing UIA APIs.  This is necessary because pynput callbacks
    fire on threads that do not have COM pre-initialized.
    """

    def health_check(self) -> bool:
        """Verify UIA is functional by probing COM and UIA core APIs.

        Call once from the main thread before starting recording to
        fail early with a clear error instead of silently degrading.
        """
        if not IS_WINDOWS:
            return False
        _ensure_com_initialized()
        try:
            import uiautomation as auto
            _ = auto.GetRootControl()
            return True
        except Exception:
            logger.warning("UIA health check failed — element enrichment unavailable")
            return False

    def enrich_click(self, x: float, y: float) -> dict | None:
        """Query the UIA element at screen position (x, y).

        Returns an ElementContext-compatible dict, or None on failure.
        """
        if not IS_WINDOWS:
            return None
        _ensure_com_initialized()
        try:
            import uiautomation as auto

            el = auto.ControlFromPoint(int(x), int(y))
            if el is None:
                return None

            ctx: dict = {}

            role = self._map_control_type(el.ControlType)
            if role:
                ctx["role"] = role

            if el.Name:
                ctx["title"] = el.Name[:200]
                ctx["computed_name"] = el.Name[:200]

            if el.AutomationId:
                ctx["identifier"] = el.AutomationId[:200]

            try:
                parent = el.GetParentControl()
                if parent:
                    pr = self._map_control_type(parent.ControlType)
                    if pr:
                        ctx["parent_role"] = pr
            except Exception:
                pass

            return ctx if ctx else None

        except ImportError:
            logger.warning("uiautomation not installed — element enrichment disabled")
            return None
        except Exception:
            logger.debug("UIA enrichment at (%d,%d) failed", int(x), int(y), exc_info=True)
            return None

    def is_password_field_focused(self) -> bool:
        """Check if the focused control is a password field.

        Three-tier check:
        1. UIA IsPassword property
        2. LocalizedControlType contains 'password'
        3. Name/AutomationId keyword heuristic
        """
        if not IS_WINDOWS:
            return False
        _ensure_com_initialized()
        try:
            import uiautomation as auto

            focused = auto.GetFocusedControl()
            if focused is None:
                return False

            if getattr(focused, 'IsPassword', False):
                return True

            lct = getattr(focused, 'LocalizedControlType', '') or ''
            if 'password' in lct.lower():
                return True

            name = (focused.Name or '').lower()
            aid = (focused.AutomationId or '').lower()
            for pattern in SENSITIVE_FIELD_PATTERNS:
                if pattern in name or pattern in aid:
                    return True

            return False
        except ImportError:
            return False
        except Exception:
            logger.debug("Password field check failed", exc_info=True)
            return False

    def focused_element_context(self) -> dict | None:
        """Get UIA context for the currently focused element."""
        if not IS_WINDOWS:
            return None
        _ensure_com_initialized()
        try:
            import uiautomation as auto

            focused = auto.GetFocusedControl()
            if focused is None:
                return None

            ctx: dict = {}
            role = self._map_control_type(focused.ControlType)
            if role:
                ctx["role"] = role
            if focused.Name:
                ctx["title"] = focused.Name[:200]
                ctx["computed_name"] = focused.Name[:200]
            if focused.AutomationId:
                ctx["identifier"] = focused.AutomationId[:200]
            return ctx if ctx else None
        except ImportError:
            return None
        except Exception:
            logger.debug("Focused element context failed", exc_info=True)
            return None

    def read_focused_value(self) -> str | None:
        """Read the ValuePattern value of the currently focused element.

        Returns the element's text value, or None if unavailable.  Used to
        capture IME-committed text that pynput's raw-key callback misses.
        """
        if not IS_WINDOWS:
            return None
        _ensure_com_initialized()
        try:
            import uiautomation as auto
            from uiautomation import ValuePattern

            focused = auto.GetFocusedControl()
            if focused is None:
                return None
            if not focused.IsValuePatternAvailable():
                return None
            value = focused.GetValuePattern().Value or ""
            return value if value else None
        except Exception:
            logger.debug("read_focused_value failed", exc_info=True)
            return None

    def _map_control_type(self, ct) -> str | None:
        """Map UIA ControlType to AX role naming scheme."""
        return semantic_role(ct)


# ============================================================
# AppDetector — foreground app detection
# ============================================================

class AppDetector:
    """Detects the foreground application and its window title.

    Uses GetForegroundWindow() + psutil for process name.
    Tracks app switches by comparing against last recorded app.
    """

    def __init__(self):
        self._last_app_name: str = ""
        self._win32gui = None
        self._win32process = None
        self._psutil = None
        self._import_lock = threading.Lock()

    def _lazy_import(self) -> None:
        if self._win32gui is not None and self._psutil is not None:
            return
        with self._import_lock:
            if self._win32gui is None:
                import win32gui
                import win32process
                self._win32gui = win32gui
                self._win32process = win32process
            if self._psutil is None:
                import psutil
                self._psutil = psutil

    def current_app_info(self) -> tuple[str, str, str]:
        """Return (app_name, process_name, window_title) for the foreground window.

        Multi-pass name resolution (mirrors Swift AppLauncher three-pass):
          1. VERSIONINFO FileDescription from the exe (richest, locale-aware)
          2. psutil process name (fast, always works)
          3. Window title prefix (e.g. first segment before " - ")
        """
        if not IS_WINDOWS:
            return ("Unknown", "", "")
        try:
            self._lazy_import()
            hwnd = self._win32gui.GetForegroundWindow()
            if not hwnd:
                return ("Unknown", "", "")
            title = self._win32gui.GetWindowText(hwnd) or ""
            _, pid = self._win32process.GetWindowThreadProcessId(hwnd)
            try:
                proc = self._psutil.Process(pid)
                exe = proc.name() or ""
                exe_path = proc.exe() if exe else None
            except Exception:
                exe = ""
                exe_path = None

            # UWP apps all run under ApplicationFrameHost.exe — use window title
            if exe == "ApplicationFrameHost.exe":
                title_name = self._extract_app_name_from_title(title)
                name = title_name or title or exe
            else:
                # Pass 1: FileDescription from VERSIONINFO resource
                name = self._read_file_description(exe_path) if exe_path else ""

                # Pass 2: process name (fast, always works)
                if not name:
                    name = exe

                # Pass 3: window title prefix
                if not name or name == exe:
                    title_name = self._extract_app_name_from_title(title)
                    if title_name:
                        name = title_name

            return (name or "Unknown", exe, title or "")
        except ImportError:
            return ("Unknown", "", "")
        except Exception:
            logger.debug("current_app_info failed", exc_info=True)
            return ("Unknown", "", "")

    @staticmethod
    def _read_file_description(exe_path: str) -> str:
        """Read FileDescription from PE VERSIONINFO resource.

        Returns the locale-aware product description (e.g. '记事本'
        for notepad.exe on Chinese Windows, 'Microsoft Edge' for msedge).
        """
        try:
            import ctypes
            from ctypes import wintypes

            size = ctypes.windll.version.GetFileVersionInfoSizeW(exe_path, None)
            if not size:
                return ""
            buf = ctypes.create_string_buffer(size)
            if not ctypes.windll.version.GetFileVersionInfoW(exe_path, 0, size, buf):
                return ""

            # Get the first translation pair to build the language-specific path
            ptr = ctypes.c_void_p()
            ptr_len = ctypes.c_uint()
            if not ctypes.windll.version.VerQueryValueW(
                buf, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(ptr_len)
            ):
                return ""
            if ptr_len.value < 4:
                return ""

            lang_id, code_page = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * 2)).contents
            # Try FileDescription, then ProductName, then FileVersion as last resort
            for field in ("FileDescription", "ProductName"):
                sub_path = (
                    f"\\StringFileInfo\\{lang_id:04x}{code_page:04x}\\{field}"
                )
                fptr = ctypes.c_void_p()
                flen = ctypes.c_uint()
                if ctypes.windll.version.VerQueryValueW(
                    buf, sub_path, ctypes.byref(fptr), ctypes.byref(flen)
                ):
                    if flen.value:
                        return ctypes.wstring_at(fptr, flen.value - 1).strip()
            return ""
        except Exception:
            return ""

    @staticmethod
    def _extract_app_name_from_title(title: str) -> str:
        """Extract a likely app name from a window title.

        Most Windows apps follow the convention 'Document - AppName'
        or 'AppName - Document'. Returns the shorter segment if the
        title has a separator, or the full title otherwise.
        """
        if not title:
            return ""
        for sep in (" - ", " — ", " – "):
            if sep in title:
                parts = title.rsplit(sep, 1)
                if len(parts) == 2 and parts[1]:
                    return parts[1].strip()
                return parts[0].strip()
        return title.strip()

    def check_app_switch(self, recorder) -> bool:
        """Check if the foreground app changed. If so, record an appSwitch action."""
        name, exe, _title = self.current_app_info()
        return self._check_switch(name, exe, recorder)

    def check_app_switch_with_info(self, name: str, exe: str, recorder) -> bool:
        """Same as check_app_switch but uses caller-provided app info to
        avoid a redundant GetForegroundWindow + psutil.Process round-trip."""
        return self._check_switch(name, exe, recorder)

    def _check_switch(self, name: str, exe: str, recorder) -> bool:
        if not self._last_app_name:
            self._last_app_name = name
            return False

        if name == self._last_app_name:
            return False

        previous_app = self._last_app_name
        self._last_app_name = name

        if recorder:
            recorder._on_app_switch(
                to_app=name, to_bundle_id=exe, from_app=previous_app,
            )

        logger.debug("App switch: %s -> %s", previous_app, name)
        return True


# ============================================================
# Screenshotter — screen capture via mss
# ============================================================

class Screenshotter:
    """Captures screen regions for future YOLO+OCR enrichment."""

    def capture_full_with_origin(self) -> tuple[bytes | None, int | None, int | None]:
        """Capture the full virtual desktop and include its screen origin."""
        from computer_server.services.capture_service import capture_full_with_origin

        return capture_full_with_origin()

    def capture_full(self) -> bytes | None:
        """Capture the entire virtual desktop (all monitors) as PNG bytes."""
        png, _x, _y = self.capture_full_with_origin()
        return png

    def capture_active_window(self) -> tuple[bytes, int, int] | tuple[None, None, None]:
        """Capture the foreground window as PNG bytes.

        Returns (png_bytes, origin_x, origin_y) or (None, None, None) on failure.
        """
        from computer_server.services.capture_service import capture_active_window

        return capture_active_window()

    def capture_region(self, x: int, y: int, width: int, height: int) -> bytes | None:
        png, _x, _y = self.capture_region_with_origin(x, y, width, height)
        return png

    def capture_region_with_origin(
        self, x: int, y: int, width: int, height: int
    ) -> tuple[bytes | None, int | None, int | None]:
        """Capture a specific screen region as PNG bytes.

        The requested region is clamped to the virtual desktop (all monitors).
        Regions that lie entirely outside the virtual desktop (or have
        non-positive clamped dimensions) return None.
        """
        from computer_server.services.capture_service import capture_region_with_origin

        return capture_region_with_origin(x, y, width, height)


# ============================================================
# VisionEnricher — batch OCR enrichment at recording stop
# ============================================================


class VisionEnricher:
    """Batch-enriches recorded click and typeText actions with OCR.

    Called at recording-stop time.  Takes one full-screen screenshot, runs
    OCR once, then:
    - Clicks: matches each click to the nearest text detection, stores
      ``vision_label`` on element_context.
    - typeTexts: if the recorded text is all ASCII (pinyin), crops the
      region around the last click position and OCRs it.  If the OCR
      result contains non-ASCII text, replaces the action's text field.

    Reuses the existing SOM OCR processor (RapidOCR/PaddleOCR ONNX by default).
    """

    def __init__(self):
        from computer_server.services.ocr_service import get_ocr_service

        self._ocr_service = get_ocr_service()

    def _ensure_ocr(self):
        return self._ocr_service.ensure_ready()

    def _ocr_image(self, png_bytes: bytes, timeout_seconds: float = 3) -> list[dict] | None:
        """OCR a single PNG image. Returns [{text, bbox, confidence}] or None."""
        return self._ocr_service.detect_png(
            png_bytes,
            confidence_threshold=0.4,
            timeout_seconds=timeout_seconds,
        )

    def enrich_clicks(self, actions: list) -> None:
        """Enrich click actions with OCR vision labels.

        For clicks whose UIA element has no title/identifier, OCRs the
        pre-captured ``ocr_region`` (a small PNG region around the click
        point, captured at recording time).  Sets ``vision_label`` on
        the element_context.

        Modifies actions in-place.
        """
        from .session import ObservedActionType

        clicks = [
            a for a in actions
            if a.action_type == ObservedActionType.CLICK
            and a.x is not None and a.y is not None
        ]
        if not clicks:
            return

        if not self._ensure_ocr():
            return

        for action in clicks:
            ec = action.element_context
            if ec and (ec.title or ec.identifier):
                continue  # UIA already gave a label

            png_bytes = action.ocr_region
            if not png_bytes:
                continue

            detections = self._ocr_image(png_bytes, timeout_seconds=3)
            if not detections:
                continue

            # Use the highest-confidence detection as the label
            best = max(detections, key=lambda d: d.get("confidence", 0))
            label = best.get("content", "")
            if label:
                if ec is None:
                    from .session import ElementContext
                    action.element_context = ElementContext(vision_label=label)
                else:
                    ec.vision_label = label

    def enrich_type_texts(self, actions: list) -> None:
        """OCR IME-typed text for typeText actions whose text is still pinyin.

        Each typeText action has ``ocr_region`` (active-window PNG bytes)
        captured at recording time.  The full window image is OCR'd and all
        detected text is stored as ``ocr_text`` on the action — the model
        can use surrounding context to determine what was actually typed.

        Modifies actions in-place.
        """
        from .session import ObservedActionType

        type_texts = [
            a for a in actions
            if a.action_type == ObservedActionType.TYPE_TEXT
            and a.text and a.text.isascii()
            and a.ocr_region is not None
        ]
        if not type_texts:
            return

        if not self._ensure_ocr():
            return

        for action in type_texts:
            png_bytes = action.ocr_region
            detections = self._ocr_image(png_bytes, timeout_seconds=5)
            if not detections:
                continue

            # Store full OCR result with positions for model context
            ocr_items = []
            for d in detections:
                ocr_items.append({
                    "text": d.get("content", ""),
                    "bbox": d.get("bbox", []),
                    "confidence": d.get("confidence", 0),
                })
            action.ocr_text = ocr_items

            logger.debug(
                "VisionEnricher: OCR stored %d detections for typeText %r",
                len(ocr_items), action.text[:40],
            )
