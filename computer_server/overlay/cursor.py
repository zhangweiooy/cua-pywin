"""
Agent cursor overlay window for Windows.

Creates a 64x64 WS_EX_LAYERED topmost transparent window, pre-renders
a glowing arrow, and runs a 60 fps animation thread that flies the
cursor along a Dubins path with a spring overshoot/settle.
"""

import ctypes
import logging
import math
import threading
import time
from ctypes import wintypes

import numpy as np
import win32api
import win32con
import win32gui
from PIL import Image, ImageDraw

from .motion import (
    CLICK_OFFSET,
    DWELL_SECONDS,
    SPRING_OVERSHOOT,
    TURN_RADIUS,
    MotionState,
    plan_path,
    tick,
)

logger = logging.getLogger(__name__)

CURSOR_SIZE = 64

# ---------------------------------------------------------------------------
# ctypes GDI definitions (BITMAPINFO not exported by win32gui / win32con)
# ---------------------------------------------------------------------------


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", BITMAPINFOHEADER),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


# ---------------------------------------------------------------------------
# Polygon mask helper (no scikit-image dependency)
# ---------------------------------------------------------------------------


def _polygon_mask(verts: np.ndarray, w: int, h: int) -> np.ndarray:
    """Return boolean (h, w) mask with True inside the polygon."""
    img = Image.new("L", (w, h), 0)
    ImageDraw.Draw(img).polygon([tuple(v) for v in verts], fill=255)
    return np.array(img) > 0


# ---------------------------------------------------------------------------
# AgentCursor
# ---------------------------------------------------------------------------


class AgentCursor:
    CURSOR_SIZE = CURSOR_SIZE
    DWELL_SECONDS = DWELL_SECONDS

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._hwnd: int = 0
        self._arrow_rgba: np.ndarray | None = None
        self._frame_cache: dict[int, bytes] = {}
        self._state = MotionState()
        self._lock = threading.Lock()
        self._ready_event = threading.Event()
        self._window_ready = threading.Event()
        self._dwell_start: float = 0.0
        self._thread: threading.Thread | None = None
        self._stop = False
        self._hide_pending = False
        self._show_pending = False

        if not enabled:
            return

        try:
            self._arrow_rgba = self._make_arrow_rgba()
        except Exception:
            logger.warning("Agent cursor arrow render failed — degrading to invisible mode", exc_info=True)
            self.enabled = False
            return

        # Create the window synchronously — no thread-race on window creation.
        try:
            self._create_window()
            self._init_gdi()
        except Exception:
            logger.warning("Agent cursor window creation failed", exc_info=True)
            self.enabled = False
            return

        self._thread = threading.Thread(target=self._animate_loop, daemon=True, name="agent-cursor")
        self._thread.start()

    # ------------------------------------------------------------------
    # Window creation (called on animation thread)
    # ------------------------------------------------------------------

    def _create_window(self):
        module_handle = win32api.GetModuleHandle(None)

        wc = win32gui.WNDCLASS()
        wc.lpfnWndProc = self._wnd_proc
        wc.hInstance = module_handle
        wc.lpszClassName = "AgentCursorOverlay"
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass  # already registered

        self._hwnd = win32gui.CreateWindowEx(
            win32con.WS_EX_LAYERED
            | win32con.WS_EX_TRANSPARENT
            | win32con.WS_EX_TOPMOST
            | win32con.WS_EX_TOOLWINDOW
            | win32con.WS_EX_NOACTIVATE
            | 0x00200000,  # WS_EX_NOREDIRECTIONBITMAP
            "AgentCursorOverlay",
            "",
            win32con.WS_POPUP,
            0,
            0,
            self.CURSOR_SIZE,
            self.CURSOR_SIZE,
            0,
            0,
            module_handle,
            None,
        )

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        if msg == win32con.WM_DESTROY:
            win32gui.PostQuitMessage(0)
            return 0
        return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)

    # ------------------------------------------------------------------
    # Arrow pre-rendering (done once at init, on main thread)
    # ------------------------------------------------------------------

    def _make_arrow_rgba(self) -> np.ndarray:
        """Return (64, 64, 4) uint8 RGBA: shadow → bloom → arrow fill → stroke.

        Layers are alpha-composited bottom-to-top with source-over blending.
        """
        from PIL import ImageFilter

        S = self.CURSOR_SIZE
        bloom_color = np.array([0x5E, 0xC0, 0xE8], dtype=np.float64)
        ys, xs = np.ogrid[:S, :S]

        # Arrow polygon: Swift proportions (22pt×18pt) ×1.3, centred at (32,32)
        poly_verts = np.array([
            [50, 32], [22, 20], [28, 32], [22, 44],
        ])
        mask = _polygon_mask(poly_verts, S, S)

        # Helper: source-over composite `src` (RGBA float64) over `dst`
        def _over(src, dst):
            sa = src[:, :, 3:4] / 255.0
            da = dst[:, :, 3:4] / 255.0
            oa = sa + da * (1.0 - sa)
            safe_oa = np.where(oa > 0, oa, 1.0)
            out = dst.copy()
            out[:, :, :3] = (src[:, :, :3] * sa + dst[:, :, :3] * da * (1.0 - sa)) / safe_oa
            out[:, :, 3] = oa[:, :, 0] * 255.0
            return out

        # === Layer 1: drop shadow ===
        shadow_verts = poly_verts + np.array([3, 4])
        shadow_img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        ImageDraw.Draw(shadow_img).polygon(
            [tuple(v) for v in shadow_verts], fill=(0x5E, 0xC0, 0xE8, 70)
        )
        result = np.array(shadow_img.filter(ImageFilter.GaussianBlur(radius=3))).astype(np.float64)

        # === Layer 2: bloom glow (3-stop radial gradient) ===
        cx, cy = 34.0, 32.0
        dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)
        ba = np.where(
            dist < 11,
            0.55 + (0.15 - 0.55) * dist / 11.0,
            np.where(dist < 22, 0.15 * (1.0 - (dist - 11.0) / 11.0), 0.0),
        )
        bloom = np.zeros((S, S, 4), dtype=np.float64)
        bloom[:, :, :3] = ba[:, :, np.newaxis] * bloom_color[np.newaxis, np.newaxis, :]  # pre-multiplied RGB
        bloom[:, :, 3] = ba * 255.0
        result = _over(bloom, result)

        # === Layer 3: arrow fill (diagonal 3-stop gradient) ===
        c0 = np.array([0xDB, 0xEE, 0xFF], dtype=np.float64)  # ice-white
        c1 = np.array([0x5E, 0xC0, 0xE8], dtype=np.float64)  # cyan
        c2 = np.array([0x54, 0xCD, 0xA0], dtype=np.float64)  # mint-green
        gx0, gy0 = 50.0, 20.0
        gx1, gy1 = 22.0, 44.0
        gdx, gdy = gx0 - gx1, gy0 - gy1
        gt = np.clip((gdx * (xs - gx1) + gdy * (ys - gy1)) / (gdx * gdx + gdy * gdy), 0.0, 1.0)
        arrow = np.zeros((S, S, 4), dtype=np.float64)
        for i in range(3):
            lo = np.where(gt < 0.53, c0[i] + (c1[i] - c0[i]) * gt / 0.53, 0.0)
            hi = np.where(gt >= 0.53, c1[i] + (c2[i] - c1[i]) * (gt - 0.53) / 0.47, 0.0)
            arrow[:, :, i] = np.where(mask, lo + hi, 0.0)
        arrow[:, :, 3] = np.where(mask, 255.0, 0.0)
        result = _over(arrow, result)

        # === Layer 4: white 2px stroke (via PIL, no compositing needed) ===
        arr = result.clip(0, 255).astype(np.uint8)
        img = Image.fromarray(arr)
        ImageDraw.Draw(img).polygon(
            [tuple(v) for v in poly_verts], outline=(255, 255, 255, 255), width=2
        )

        return np.array(img)

    # ------------------------------------------------------------------
    # Public API (thread-safe, called from main thread)
    # ------------------------------------------------------------------

    def move_to(self, x: int, y: int, heading: float = 0.785) -> None:
        """Plan and execute fly-to animation. Blocks until spring settles."""
        if not self.enabled:
            return

        self._ready_event.clear()

        with self._lock:
            motion_heading = self._state.heading + math.pi
            target_motion = heading + math.pi
            # Offset Dubins target past click point so the spring
            # overshoots and settles back — matching Swift click_offset=16.
            ox = math.cos(heading) * CLICK_OFFSET
            oy = math.sin(heading) * CLICK_OFFSET

            self._state = plan_path(
                x0=self._state.x,
                y0=self._state.y,
                th0=motion_heading,
                x1=float(x) + ox,
                y1=float(y) + oy,
                th1=target_motion,
                end_visual_heading=heading,
                target_x=float(x) + ox,
                target_y=float(y) + oy,
            )
            self._show_pending = True

        self._ready_event.wait()

    def set_initial_position(self, x: int, y: int, heading: float = 0.785) -> None:
        with self._lock:
            self._state = MotionState(x=float(x), y=float(y), heading=heading)

    def set_position_visible(self, x: int, y: int) -> None:
        """Update position and keep the arrow visible — for drag tracking."""
        with self._lock:
            self._state.x = float(x)
            self._state.y = float(y)
            self._state.phase = "dwell"
        self._show_pending = True

    def show(self):
        self._show_pending = True

    def hide(self):
        self._hide_pending = True

    def shutdown(self):
        self._stop = True
        if self._thread is not None:
            self._thread.join(timeout=2)

    # ------------------------------------------------------------------
    # Animation loop (runs on animation thread — owns the window)
    # ------------------------------------------------------------------

    def _animate_loop(self):
        """60 fps animation thread.  Window was created on the main thread."""
        target_interval = 1.0 / 60.0
        _was_dwell = False
        _last_phase = "idle"

        while not self._stop:
            t0 = time.perf_counter()

            # Process pending hide/show requests
            if self._hide_pending:
                self._hide_pending = False
                if self._hwnd:
                    win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
                    # Restore system cursor when overlay hides
                    ctypes.windll.user32.ShowCursor(True)
            if self._show_pending:
                self._show_pending = False
                if self._hwnd:
                    win32gui.ShowWindow(self._hwnd, win32con.SW_SHOWNOACTIVATE)
                    # Hide system cursor when overlay is visible
                    ctypes.windll.user32.ShowCursor(False)

            with self._lock:
                phase = self._state.phase
                if phase in ("flying", "spring"):
                    tick(self._state, dt=target_interval)
                    phase = self._state.phase
                should_render = phase in ("flying", "spring", "dwell")
                if should_render:
                    render_x = self._state.x
                    render_y = self._state.y
                    render_heading = self._state.heading

            # Signal arrival as soon as the spring starts (Dubins path done).
            # The click should fire during the overshoot — matching Swift behaviour.
            if phase == "spring" and _last_phase == "flying":
                self._ready_event.set()

            if should_render:
                self._render_and_present(render_x, render_y, render_heading)

            if phase == "dwell":
                if not _was_dwell:
                    self._dwell_start = time.perf_counter()
                    _was_dwell = True
                if time.perf_counter() - self._dwell_start >= self.DWELL_SECONDS:
                    with self._lock:
                        self._state.phase = "done"
                    if self._hwnd:
                        win32gui.ShowWindow(self._hwnd, win32con.SW_HIDE)
            else:
                _was_dwell = False

            _last_phase = phase
            elapsed = time.perf_counter() - t0
            time.sleep(max(0.0, target_interval - elapsed))

        # Clean up GDI and window on the thread that owns them
        if hasattr(self, '_hbmp') and self._hbmp:
            ctypes.windll.gdi32.DeleteObject(self._hbmp)
            self._hbmp = 0
        if hasattr(self, '_hdc_mem') and self._hdc_mem:
            win32gui.DeleteDC(self._hdc_mem)
            self._hdc_mem = 0
        if hasattr(self, '_hdc_screen') and self._hdc_screen:
            win32gui.ReleaseDC(0, self._hdc_screen)
            self._hdc_screen = 0
        if self._hwnd:
            win32gui.DestroyWindow(self._hwnd)
            self._hwnd = 0

    # ------------------------------------------------------------------
    # Per-frame rendering (called on animation thread)
    # ------------------------------------------------------------------

    def _get_rotated_frame(self, heading: float) -> bytes:
        """Return premultiplied BGRA bytes for the given heading.

        Cached per 1-degree bucket to avoid re-rotating every frame.
        """
        deg = round(math.degrees(heading)) % 360
        cached = self._frame_cache.get(deg)
        if cached is not None:
            return cached

        # Pre-multiply alpha BEFORE rotation.  If we rotate in straight RGBA
        # space, PIL's bilinear interpolation blends the RGB of transparent
        # (0,0,0) pixels into the arrow's edges, producing a black halo.
        pm = np.array(self._arrow_rgba).astype(np.float64)
        alpha = pm[:, :, 3:4] / 255.0
        pm[:, :, :3] = pm[:, :, :3] * alpha

        rotated = Image.fromarray(pm.astype(np.uint8)).rotate(
            -deg, resample=Image.BILINEAR, expand=False
        )
        arr = np.array(rotated)

        bgra = arr[:, :, [2, 1, 0, 3]].astype(np.uint8)
        bgra_bytes = bgra.tobytes()

        if len(self._frame_cache) >= 360:
            self._frame_cache.clear()
        self._frame_cache[deg] = bgra_bytes

        return bgra_bytes

    def _init_gdi(self):
        """Create reusable GDI resources — called once after window creation."""
        self._hdc_screen = win32gui.GetDC(0)
        self._hdc_mem = win32gui.CreateCompatibleDC(self._hdc_screen)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = self.CURSOR_SIZE
        bmi.bmiHeader.biHeight = -self.CURSOR_SIZE
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0
        self._bmi = bmi

        gdi32 = ctypes.windll.gdi32
        ppv_bits = ctypes.c_void_p()
        self._hbmp = gdi32.CreateDIBSection(
            self._hdc_mem, ctypes.byref(self._bmi), 0, ctypes.byref(ppv_bits), None, 0
        )
        self._ppv_bits = ppv_bits

    def _render_and_present(self, pos_x: float, pos_y: float, heading: float):
        """Rotate arrow, position window, push bitmap via UpdateLayeredWindow."""
        bgra_bytes = self._get_rotated_frame(heading)
        x = int(pos_x - self.CURSOR_SIZE // 2)
        y = int(pos_y - self.CURSOR_SIZE // 2)

        ctypes.memmove(self._ppv_bits, bgra_bytes, len(bgra_bytes))

        old_bmp = win32gui.SelectObject(self._hdc_mem, self._hbmp)
        win32gui.UpdateLayeredWindow(
            self._hwnd, self._hdc_screen,
            (x, y), (self.CURSOR_SIZE, self.CURSOR_SIZE),
            self._hdc_mem, (0, 0), 0,
            (0, 0, 0xFF, 1), win32con.ULW_ALPHA,
        )
        win32gui.SelectObject(self._hdc_mem, old_bmp)
