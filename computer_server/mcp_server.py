"""
MCP (Model Context Protocol) server interface for Computer Server.

This module provides an MCP interface to the Computer Server's functionality,
enabling Claude Code and other MCP-compatible tools to directly control computers.

Usage:
    python -m computer_server --mcp
    python -m computer_server --mcp --width 1512 --height 982
    python -m computer_server --mcp --detect-resolution
"""

import base64
import logging
import os
import sys

# Pre-import heavy C-extension modules at module level.
#
# In FastMCP's stdio transport, importing C-extension modules for
# the first time from inside a tool handler — which runs on the
# event-loop thread inside an anyio task group — causes a permanent
# hang.  The anyio event loop appears to block C-extension DLL
# initialisation.  Importing them here side-steps the issue.
#
# Must be imported before the anyio event loop starts.  First-time
# C-extension imports from inside a tool handler deadlock permanently.
# torch + cv2 + ultralytics add ~4-9 s to startup; warm_up() loads
# the actual model weights in the background after that.
import numpy as np  # noqa: F401

try:
    from computer_server.handlers.som_bridge import is_available as _pre_som_avail
    _pre_som_avail()  # triggers from som.detect import OmniParser → torch + cv2 + ultralytics
except Exception:
    pass
from typing import Any, Dict, List, Optional, Tuple

if sys.platform != "win32":
    raise RuntimeError("cua-pywin 仅支持 Windows 平台")

from fastmcp import FastMCP

# Agent cursor overlay — optional, degrades silently if init fails
try:
    from computer_server.overlay import init as _overlay_init
except Exception:
    _overlay_fly = None  # type: ignore[assignment]
    _overlay_init = None  # type: ignore[assignment]
from fastmcp.utilities.types import Image

logger = logging.getLogger(__name__)


async def _animate_cursor_to(x: int, y: int) -> None:
    """Fly cursor overlay to (x,y), blocking until spring starts.

    Lazy-initializes the overlay if server-startup init failed.
    """
    from computer_server.overlay import _agent_cursor, _compute_heading, _update_last_target, init as _cursor_init

    if _agent_cursor is None:
        try:
            _cursor_init()
        except Exception:
            return
    if _agent_cursor is None:
        return

    heading = _compute_heading(x, y)
    _update_last_target(x, y)
    _agent_cursor.move_to(x, y, heading)


# Lazy handler initialization to avoid crashes during import
_handlers = None

# Resolution scaling configuration
_scale_x: float = 1.0
_scale_y: float = 1.0
_target_width: Optional[int] = None
_target_height: Optional[int] = None
_actual_width: Optional[int] = None
_actual_height: Optional[int] = None

# Zoom metadata: per-(pid, window_id) record of last get_window_state screenshot dimensions
# Used by computer_zoom to map resized-image coordinates to full-resolution
_zoom_metadata: Dict[Tuple[int, int], Dict[str, Any]] = {}

# Popover dedup: per-pid set of seen popover hwnds
_seen_popovers: Dict[int, set] = {}

# UIA walk gate state — prevents concurrent walks of the same window from
# stacking up executor threads when a target window is hung.  Two structures:
#
# _uia_inflight: keys (pid, window_id) currently being walked.  A second call
#   on the same key is rejected immediately and falls through to SOM fallback.
#   The key is released ONLY when the underlying executor future actually
#   completes — never on wait_for timeout, because the COM call is still
#   running in a thread we cannot safely kill.  Releasing eagerly would let
#   the next request spawn another doomed walk and leak more executor threads.
# _uia_unhealthy_until: monotonic-time deadline before which the (pid, window_id)
#   key is treated as "known hung".  Set after a wait_for timeout as a
#   secondary defence (the in-flight set is the primary one); cleared
#   automatically when the deadline passes.
#
# Both are guarded by _uia_gate_lock (small, fast critical sections only —
# never held across await points or COM calls).
import threading as _threading
import time as _time
_uia_gate_lock = _threading.Lock()
_uia_inflight: set = set()
_uia_unhealthy_until: Dict[Tuple[int, int], float] = {}
_UIA_UNHEALTHY_COOLDOWN_S = 30.0


def _release_uia_inflight(gate_key: Tuple[int, int]) -> None:
    """Drop ``gate_key`` from the in-flight set under the gate lock.

    Used as an executor-future done callback so the slot is released only
    after the underlying COM walk thread actually returns — even if the
    awaiting coroutine timed out and gave up on the result.
    """
    with _uia_gate_lock:
        _uia_inflight.discard(gate_key)


def _classify_tree_quality(count: int) -> str:
    """Classify element count as tree quality: empty/poor/ok/rich."""
    if count == 0:
        return "empty"
    if count > 50:
        return "rich"
    if count > 15:
        return "ok"
    return "poor"


def _get_resize_ratio(pid: Optional[int], window_id: Optional[int]) -> float:
    """Return the stored compression ratio for this (pid, window_id), or 1.0 if not found."""
    if pid is not None and window_id is not None:
        meta = _zoom_metadata.get((pid, window_id), {})
        return float(meta.get("compression_ratio", 1.0))
    return 1.0


def _get_handlers():
    """Lazily initialize handlers on first use."""
    global _handlers
    if _handlers is None:
        from .handlers.factory import HandlerFactory

        _handlers = HandlerFactory.create_handlers()
    return _handlers


async def _detect_actual_resolution_async() -> Tuple[int, int]:
    """Detect the actual screen resolution using the automation handler."""
    try:
        automation_handler = _get_handlers().automation
        result = await automation_handler.get_screen_size()
        size = result.get("size", {})
        return size.get("width", 1920), size.get("height", 1080)
    except Exception as e:
        logger.warning(f"Failed to detect resolution via handler: {e}")
        return 1920, 1080  # Default fallback


def _detect_actual_resolution() -> Tuple[int, int]:
    """Detect the actual screen resolution (sync wrapper).

    Uses ``asyncio.run()`` when called at cold start (stdio mode, no event
    loop running).  Falls back to defaults when an event loop is already
    active (HTTP mode / in-tool calls).
    """
    import asyncio

    try:
        asyncio.get_running_loop()
        return 1920, 1080  # loop already running — use defaults
    except RuntimeError:
        pass  # no running loop — can safely use asyncio.run()

    try:
        return asyncio.run(_detect_actual_resolution_async())
    except Exception as e:
        logger.warning(f"Failed to detect resolution: {e}")
        return 1920, 1080


def _configure_scaling(
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    detect_resolution: bool = False,
) -> None:
    """Configure resolution scaling based on target and actual resolution."""
    global _scale_x, _scale_y, _target_width, _target_height, _actual_width, _actual_height

    _actual_width, _actual_height = _detect_actual_resolution()
    logger.info(f"Detected actual screen resolution: {_actual_width}x{_actual_height}")

    if target_width and target_height:
        _target_width = target_width
        _target_height = target_height
        _scale_x = _actual_width / target_width
        _scale_y = _actual_height / target_height
        logger.info(
            f"Target resolution: {target_width}x{target_height}, "
            f"Scale factors: x={_scale_x:.3f}, y={_scale_y:.3f}"
        )
    else:
        _target_width = _actual_width
        _target_height = _actual_height
        _scale_x = 1.0
        _scale_y = 1.0
        if detect_resolution:
            logger.info("Resolution detection enabled - coordinates will use actual resolution")


def _scale_to_actual(x: int, y: int) -> Tuple[int, int]:
    """Scale coordinates from target resolution to actual resolution."""
    return int(x * _scale_x), int(y * _scale_y)


def _scale_to_target(x: int, y: int) -> Tuple[int, int]:
    """Scale coordinates from actual resolution to target resolution."""
    if _scale_x == 0 or _scale_y == 0:
        return x, y
    return int(x / _scale_x), int(y / _scale_y)


# ---------------------------------------------------------------------------
# Helpers for from_zoom coordinate translation
# ---------------------------------------------------------------------------


def _resolve_from_zoom(
    x: int, y: int, from_zoom: bool = False, pid: Optional[int] = None, window_id: Optional[int] = None,
) -> Tuple[int, int]:
    """If from_zoom is True, translate zoomed-image coordinates back to screen space."""
    if not from_zoom:
        return x, y
    if pid is None:
        raise ValueError("pid is required when from_zoom=True")
    if window_id is None:
        raise ValueError("window_id is required when from_zoom=True")
    from computer_server.handlers.zoom_registry import get_zoom_registry

    return get_zoom_registry().unscale(pid, window_id, x, y)


def _resolve_element_center(
    pid: int,
    window_id: int,
    element_index: int,
) -> Tuple[int, int]:
    """Look up the center coordinates of a cached element and convert to screen space.

    Elements are stored in window-relative coordinates. This adds back the
    window's screen-space origin so the returned (x, y) can be used directly
    with _scale_to_actual for clicking.
    """
    from computer_server.handlers.element_cache import get_element_cache

    cache = get_element_cache()
    center = cache.get_center(pid, window_id, element_index)
    if center is None:
        raise ValueError(
            f"No cached element at index {element_index} for pid={pid}, "
            f"window_id={window_id}. Call computer_get_window_state first."
        )
    origin = cache.get_window_origin_optional(pid, window_id)
    if origin is None:
        raise ValueError(
            f"Element at index {element_index} found for pid={pid}, "
            f"window_id={window_id}, but window origin is missing from "
            f"the cache. Call computer_get_window_state to refresh."
        )
    return (origin[0] + center[0], origin[1] + center[1])


def _window_resized_to_screen(
    pid: Optional[int], window_id: Optional[int], x: int, y: int
) -> Tuple[int, int]:
    """Convert window-relative resized-screenshot coords to absolute screen coords.

    ``get_window_state`` returns a screenshot that is (a) cropped to the **visible**
    portion of the window rect and then (b) resized by ``compression_ratio`` to
    stay below ``max_image_dimension``. So a point ``(x, y)`` on that resized image
    maps to the screen-space pixel ``(crop_origin + int(x*ratio))``.

    When the window is fully on-screen, the visible crop origin equals the real
    window origin.  When the window has negative coordinates (multi-monitor left
    of primary) or extends beyond a screen edge, the visible crop origin is
    clamped to the screen bounds and differs from the real window origin.

    Uses the crop origin from ``_zoom_metadata`` when available; falls back to
    the window origin stored in the ElementCache for backward compatibility.

    If pid or window_id is None, returns (x, y) unchanged.
    """
    if pid is None or window_id is None:
        return x, y

    # Prefer the per-(pid,window_id) crop origin from the zoom metadata
    # populated by the most recent get_window_state call.  This is the
    # authoritative source: it records the exact screen point where the
    # visible cropped image starts.
    meta = _zoom_metadata.get((pid, window_id), {})
    if meta:
        origin_x = meta.get("crop_origin_screen_x", 0)
        origin_y = meta.get("crop_origin_screen_y", 0)
        ratio = float(meta.get("compression_ratio", 1.0))
        return (origin_x + int(x * ratio), origin_y + int(y * ratio))

    # Fallback: ElementCache stores the real window origin (may differ from
    # visible crop origin for off-screen / negative-coordinate windows).
    from computer_server.handlers.element_cache import get_element_cache

    origin = get_element_cache().get_window_origin_optional(pid, window_id)
    if origin is None:
        return x, y
    origin_x, origin_y = origin
    ratio = _get_resize_ratio(pid, window_id)
    return (origin_x + int(x * ratio), origin_y + int(y * ratio))


# ---------------------------------------------------------------------------
# Window change detection (mirrors Swift WindowChangeDetector)
# ---------------------------------------------------------------------------


def _window_snapshot() -> tuple:
    """Snapshot visible windows before an action.

    Returns (foreground_hwnd, frozenset_of_visible_hwnds).
    Mirrors Swift ``WindowChangeDetector.snapshot()``.
    """
    import win32gui

    hwnds = set()

    def _collect(hwnd, _ctx):
        if win32gui.IsWindowVisible(hwnd):
            hwnds.add(hwnd)
        return True

    win32gui.EnumWindows(_collect, None)
    return win32gui.GetForegroundWindow(), frozenset(hwnds)


def _window_detect_changes(
    snap_hwnds: frozenset,
    snap_fg_hwnd: int,
) -> Optional[Dict[str, Any]]:
    """Detect window changes since a snapshot, with metadata.

    Returns None if nothing changed. Otherwise returns a dict with:
      - new_windows: [{hwnd, pid, title, cross_app}] — windows that appeared
      - focus_stolen: {from_hwnd, to_hwnd, to_pid, to_title} — if foreground
        changed to a different PID

    Mirrors Swift ``WindowChangeDetector.detectChanges()``.
    """
    import win32gui

    fg_now, hwnds_now = _window_snapshot()
    changes: Dict[str, Any] = {}
    old_pid = None
    if snap_fg_hwnd:
        _, old_pid = win32gui.GetWindowThreadProcessId(snap_fg_hwnd)

    # New windows since snapshot
    appeared = hwnds_now - snap_hwnds
    if appeared:
        new_list: list = []
        for hwnd in appeared:
            title = win32gui.GetWindowText(hwnd) or ""
            _, pid = win32gui.GetWindowThreadProcessId(hwnd)
            new_list.append({
                "hwnd": hwnd, "pid": pid, "title": title,
                "cross_app": old_pid is not None and pid != old_pid,
            })
        changes["new_windows"] = new_list

    # Focus change: report if foreground moved to a different PID
    if snap_fg_hwnd and fg_now and fg_now != snap_fg_hwnd:
        _, new_pid = win32gui.GetWindowThreadProcessId(fg_now)
        if new_pid != old_pid:
            changes["focus_stolen"] = {
                "from_hwnd": snap_fg_hwnd, "to_hwnd": fg_now,
                "to_pid": new_pid,
                "to_title": win32gui.GetWindowText(fg_now) or "",
            }

    return changes or None


# ---------------------------------------------------------------------------
# Platform-specific helpers
# ---------------------------------------------------------------------------


def _build_window_ref(pid: Optional[int], window_id: Optional[int],
                      x: int, y: int) -> Optional[Dict[str, Any]]:
    """Return {window_id, origin_x, origin_y, process_name, window_title, width, height}
    for the window at (x, y). Extra fields are best-effort — never raise on failure.

    Resolution order:
      1. Element cache (by pid + window_id) — the window we INTENDED to click.
      2. GetWindowRect(window_id) — if the HWND is still valid.
      3. WindowFromPoint(x, y) → GetAncestor(GA_ROOT) — last resort.
    """
    base: Optional[Dict[str, Any]] = None

    if pid is not None and window_id is not None:
        try:
            from computer_server.handlers.element_cache import get_element_cache
            origin = get_element_cache().get_window_origin_optional(pid, window_id)
            if origin is not None:
                base = {"window_id": window_id, "origin_x": origin[0], "origin_y": origin[1]}
        except Exception:
            pass
        if base is None:
            try:
                import win32gui
                rect = win32gui.GetWindowRect(int(window_id))
                if rect:
                    base = {"window_id": window_id, "origin_x": rect[0], "origin_y": rect[1]}
            except Exception:
                pass

    if base is None:
        base = _get_window_ref(x, y)

    # Enrich with process_name, window_title, width, height (best-effort)
    if base is not None:
        wid = base.get("window_id")
        if wid is not None:
            try:
                import win32process
                import psutil
                _, pid_val = win32process.GetWindowThreadProcessId(int(wid))
                if pid_val:
                    base["process_name"] = psutil.Process(pid_val).name() or ""
            except Exception:
                pass
            try:
                import win32gui
                title = win32gui.GetWindowText(int(wid))
                if title:
                    base["window_title"] = title
            except Exception:
                pass
            try:
                import win32gui
                rect = win32gui.GetWindowRect(int(wid))
                if rect:
                    base["width"] = int(rect[2] - rect[0])
                    base["height"] = int(rect[3] - rect[1])
            except Exception:
                pass

    return base


def _get_window_ref(x: int, y: int, window_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Resolve the window at screen point (x, y) and return its ref.

    Returns {window_id, origin_x, origin_y} or None on failure.
    Uses GetAncestor(GA_ROOT) to get the top-level owner window.
    """
    try:
        import ctypes
        import win32gui

        hwnd = win32gui.WindowFromPoint((x, y))
        if not hwnd:
            return None
        # Walk up to the root owner (top-level window, not a child control)
        GA_ROOT = 2
        root = ctypes.windll.user32.GetAncestor(hwnd, GA_ROOT)
        if root:
            hwnd = root
        rect = win32gui.GetWindowRect(hwnd)
        if not rect:
            return None
        return {
            "window_id": hwnd,
            "origin_x": rect[0],
            "origin_y": rect[1],
        }
    except Exception:
        return None


def _invoke_via_uia_point(screen_x: int, screen_y: int) -> None:
    """Resolve the current UIA element at a point and invoke it."""
    from computer_server.handlers.windows_uia import invoke_at_point

    invoke_at_point(screen_x, screen_y)


def _set_value_via_uia_point(screen_x: int, screen_y: int, value: str) -> None:
    """Resolve the current UIA element at a point and set ValuePattern."""
    from computer_server.handlers.windows_uia import set_value_at_point

    set_value_at_point(screen_x, screen_y, value)


def _mouse_event_click(x: int, y: int, button: str, count: int = 1) -> None:
    """Perform a click via win32api.mouse_event at (x, y).

    Uses win32api.mouse_event instead of pynput so the click is synthesized
    as a real HID input event. Required for WebView2 / UWP targets that
    filter per-pid or PostMessage events when the window is frontmost.

    Args:
        x: Screen X coordinate in physical pixels.
        y: Screen Y coordinate in physical pixels.
        button: 'left', 'right', or 'middle'.
        count: Number of clicks (1=single, 2=double, 3=triple).
    """
    import win32api
    import win32con

    win32api.SetCursorPos((x, y))

    if button == "right":
        down_event = win32con.MOUSEEVENTF_RIGHTDOWN
        up_event = win32con.MOUSEEVENTF_RIGHTUP
    elif button == "middle":
        down_event = win32con.MOUSEEVENTF_MIDDLEDOWN
        up_event = win32con.MOUSEEVENTF_MIDDLEUP
    else:
        down_event = win32con.MOUSEEVENTF_LEFTDOWN
        up_event = win32con.MOUSEEVENTF_LEFTUP

    for _ in range(count):
        win32api.mouse_event(down_event, 0, 0, 0, 0)
        win32api.mouse_event(up_event, 0, 0, 0, 0)


# ---------------------------------------------------------------------------
# Tool map for replay support
# ---------------------------------------------------------------------------


async def _get_tool_map(mcp: FastMCP) -> Any:
    """Build a tool-name → callable map for replay_trajectory.

    Returns a dict mapping tool names to async callables that accept
    keyword arguments matching the tool's parameter schema.
    """
    tool_map: Dict[str, Any] = {}
    try:
        tools = await mcp.list_tools()
        for tool in tools:
            name = getattr(tool, "name", None)
            fn = getattr(tool, "fn", None)
            if name and fn:
                tool_map[name] = fn
    except Exception:
        logger.warning("Could not enumerate tools for replay; replay will fail for unknown tools", exc_info=True)
    return tool_map


def create_mcp_server() -> FastMCP:
    """
    Create and configure the MCP server with all computer control tools.

    Returns:
        FastMCP: Configured MCP server instance
    """
    mcp = FastMCP(
        name="cua-pywin",
        instructions="""You are connected to a computer control server that provides low-level
        primitives for interacting with a desktop computer. You can take screenshots, click,
        type text, press keys, scroll, manage windows, read/write files, and run commands.

        Always take a screenshot first to see the current state before performing actions.
        After performing actions, take another screenshot to verify the result.""",
    )
    from computer_server.server_context import ServerContext

    context = ServerContext()

    async def _recording_hook(
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        click_point: Optional[Tuple[int, int]] = None,
        window_ref: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record a tool invocation for this MCP server instance."""
        try:
            from computer_server.recording.session import RECORDABLE_TOOLS

            if tool_name not in RECORDABLE_TOOLS:
                return
            session = context.recording_session
            if session is None or not session.active:
                return

            screenshot_bytes = None
            if session.should_capture_screenshot():
                try:
                    automation_handler = _get_handlers().automation
                    scr_result = await automation_handler.screenshot(format="png")
                    if scr_result.get("success") and scr_result.get("image_data"):
                        screenshot_bytes = base64.b64decode(scr_result["image_data"])
                except Exception:
                    session.note_screenshot_error()

            await session.record_turn(
                tool_name=tool_name,
                args=args,
                result=result,
                screenshot_bytes=screenshot_bytes,
                click_point=click_point,
                window_ref=window_ref,
            )
        except Exception:
            logger.debug("recording hook failed", exc_info=True)

    # ============================================================
    # Helper: capture_after pipeline (screenshot + SOM on a window)
    # ============================================================

    async def _capture_and_detect(
        pid: int,
        window_id: int,
        max_image_dimension: int = 1280,
    ) -> Any:
        """Thin wrapper around computer_get_window_state for capture_after use.

        Returns a dict with screenshot, nodes, node_count, tree_quality keys,
        or an error key on failure. Never raises — failures are captured so
        the parent action result is not lost.
        """
        try:
            fresh = await computer_get_window_state(
                pid=pid, window_id=window_id, max_image_dimension=max_image_dimension,
            )
            if fresh.get("success") is False:
                return {"success": False, "error": fresh.get("error", "get_window_state returned success=False")}
            return {
                "screenshot": fresh.get("screenshot"),
                "nodes": fresh.get("nodes", []),
                "node_count": fresh.get("node_count", 0),
                "tree_quality": fresh.get("tree_quality", "empty"),
                "success": True,
            }
        except Exception as _cap_err:
            logger.warning("capture_after failed: %s", _cap_err)
            return {"success": False, "error": str(_cap_err)}

    # ============================================================
    # SCREEN & MOUSE ACTIONS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_screenshot(
        format: str = "jpeg",
        quality: int = 85,
    ) -> Any:
        """
        Capture a screenshot of the entire screen.

        Images larger than 1280px in either dimension are automatically
        downscaled to fit within a 1280px bounding box.

        Args:
            format: 'jpeg' (smaller, default) or 'png' (lossless).
            quality: JPEG quality 1-95, ignored for PNG. Default 85.
        """
        from PIL import Image as PILImage

        from computer_server.handlers.base import normalize_screenshot_format
        from computer_server.handlers.screenshot_service import (
            encode_for_mcp_image,
            resize_to_max_dimension,
        )

        try:
            fmt, quality = normalize_screenshot_format(format, quality)
        except ValueError as e:
            fmt, quality = "jpeg", 85

        automation_handler = _get_handlers().automation
        result = await automation_handler.screenshot(format=fmt, quality=quality)
        if not result.get("success"):
            return {"success": False, "error": "Screenshot failed: " + result.get("error", "unknown")}
        image_data = base64.b64decode(result["image_data"])
        from io import BytesIO
        img = PILImage.open(BytesIO(image_data))

        # If target resolution is set, resize to that
        if _target_width and _target_height and (_scale_x != 1.0 or _scale_y != 1.0):
            img = img.resize((_target_width, _target_height), PILImage.Resampling.LANCZOS)
        else:
            # Resize large images to keep under 1MB limit
            max_dimension = 1280
            img, resized = resize_to_max_dimension(img, max_dimension)
            if resized and not _target_width:
                _configure_scaling(target_width=img.width, target_height=img.height)

        image_data = encode_for_mcp_image(img, fmt, quality)

        return Image(data=image_data, format=fmt)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_screen_size() -> Any:
        """
        Get the screen dimensions.

        Returns:
            Dictionary with 'width' and 'height' keys in pixels.
        """
        if _target_width is not None and _target_height is not None:
            return {"width": int(_target_width), "height": int(_target_height)}
        automation_handler = _get_handlers().automation
        raw = await automation_handler.get_screen_size()
        size = raw.get("size", raw)
        response = {"width": size.get("width", 0), "height": size.get("height", 0)}
        if "x" in size or "y" in size:
            response["x"] = size.get("x", 0)
            response["y"] = size.get("y", 0)
        if "virtual_screen" in raw:
            response["virtual_screen"] = raw["virtual_screen"]
        return response

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_cursor_position() -> Any:
        """
        Get the current cursor position.

        Returns:
            Dictionary with 'x' and 'y' coordinates.
        """
        automation_handler = _get_handlers().automation
        result = await automation_handler.get_cursor_position()
        pos = result.get("position", {})
        if "x" in pos and "y" in pos:
            scaled_x, scaled_y = _scale_to_target(int(pos["x"]), int(pos["y"]))
            return {"x": scaled_x, "y": scaled_y}
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_click(
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        element_index: Optional[int] = None,
        window_id: Optional[int] = None,
        pid: Optional[int] = None,
        from_zoom: bool = False,
        modifier: Optional[List[str]] = None,
        count: int = 1,
        debug_image_out: Optional[str] = None,
        force_frontmost: bool = False,
        action: str = "press",
        capture_after: bool = False,
    ) -> Any:
        """
        Click at the specified screen coordinates or on a cached element.

        Two addressing modes:
        - (x, y): click at pixel coordinates in screenshot space
        - element_index + window_id + pid: click a cached element from get_window_state

        Args:
            x: X coordinate in pixels (ignored when element_index is set)
            y: Y coordinate in pixels (ignored when element_index is set)
            button: Mouse button - 'left', 'right', or 'middle' (default: 'left')
            element_index: Index from get_window_state nodes to click via element cache
            window_id: Window ID (required with element_index)
            pid: Process ID (required with element_index)
            from_zoom: When True, x/y are zoomed-image coords and get translated back
            modifier: Modifier keys held during click (ctrl, alt, shift, win)
            count: Click count - 1 (single), 2 (double), 3 (triple). Default 1.
            debug_image_out: Optional path to save a debug image with click crosshair.
            force_frontmost: When True, calls SetForegroundWindow before click.
            action: 'press' (click) or 'raise' (bring window to top). For element_index only.
            capture_after: When True, take a fresh screenshot + SOM after the action.
        """
        import sys
        import asyncio

        from computer_server.handlers.element_cache import get_element_cache

        # --- Resolve element_index ---
        if element_index is not None:
            if from_zoom:
                return {"success": False, "error": "element_index and from_zoom are mutually exclusive — element_index already returns screen coords", "suggestion": "Remove from_zoom=True or use x/y coordinates with from_zoom instead"}
            if pid is None or window_id is None:
                return {"success": False, "error": "pid and window_id are required with element_index", "suggestion": "Provide pid and window_id from a previous get_window_state call"}

            # Resolve element from cache
            el = get_element_cache().get_element(pid, window_id, element_index)
            if el is None:
                return {"success": False, "error": f"No cached element at index {element_index} for pid={pid}, window_id={window_id}", "suggestion": f"The element cache may have expired. Call get_window_state(pid={pid}, window_id={window_id}) to refresh it, then use the new element_index"}

            # Dispatch action
            if action == "raise":
                try:
                    automation_handler = _get_handlers().automation
                    await automation_handler.activate_window(str(window_id))
                    result: Dict[str, Any] = {"success": True, "action": "raise",
                                               "element_index": element_index,
                                               "window_id": window_id}
                    wr = _build_window_ref(pid, window_id, 0, 0)
                    await _recording_hook("computer_click",
                        {"element_index": element_index, "pid": pid, "window_id": window_id, "action": "raise"},
                        result, window_ref=wr)
                    if capture_after:
                        result["capture_after"] = await _capture_and_detect(pid, window_id)
                    return result
                except Exception as e:
                    return {"success": False, "error": f"Raise action failed: {e}", "suggestion": "Try clicking the element instead with action='press' (the default)"}

            if el.enabled is False:
                return {"success": False,
                        "error": f"Element [{element_index}] is disabled (enabled=false) — the window may not be frontmost",
                        "suggestion": f"Call computer_click(pid={pid}, window_id={window_id}, element_index={element_index}, action='raise') first to activate the window, then re-snapshot"}
            else:
                # Enabled element: attempt UIA Invoke on Windows by resolving
                # a fresh element at the cached center point.  The cache only
                # stores geometry/metadata; COM UIA objects are apartment-bound
                # and must not be reused across MCP calls.
                if sys.platform == "win32" and "invoke" in (el.actions or []):
                    try:
                        import win32gui as _wg

                        # Snapshot visible windows before click
                        # (mirrors Swift WindowChangeDetector.snapshot()).
                        # If the action triggers a cross-app side-effect
                        # (URL handoff, new dialog, focus theft), we
                        # detect it and report it to the caller.
                        snap_fg_hwnd, snap_hwnds = _window_snapshot()

                        # Agent cursor overlay: fly to element, then click
                        try:
                            screen_pt = _resolve_element_center(pid, window_id, element_index)
                        except ValueError:
                            origin_x, origin_y = get_element_cache().get_window_origin(pid, window_id)
                            screen_pt = (origin_x + el.center_x, origin_y + el.center_y)
                        await _animate_cursor_to(int(screen_pt[0]), int(screen_pt[1]))

                        _invoke_via_uia_point(int(screen_pt[0]), int(screen_pt[1]))
                        result = {"success": True, "action": "invoke",
                                  "element_index": element_index, "method": "uia_invoke"}

                        # Detect + report window changes caused by the click.
                        # Restore focus if stolen by a different app.
                        changes = _window_detect_changes(snap_hwnds, snap_fg_hwnd)
                        if changes:
                            result["window_changes"] = changes
                            if "focus_stolen" in changes:
                                stolen = changes["focus_stolen"]
                                logger.warning(
                                    "click side-effect: focus stolen by pid=%d (%s) — restoring to hwnd=%d",
                                    stolen.get("to_pid", 0),
                                    stolen.get("to_title", ""),
                                    snap_fg_hwnd,
                                )
                                try:
                                    _wg.SetForegroundWindow(snap_fg_hwnd)
                                    result["focus_restored"] = True
                                except Exception:
                                    logger.debug("SetForegroundWindow restore failed", exc_info=True)
                            if "new_windows" in changes:
                                logger.info(
                                    "click side-effect: %d new window(s) appeared — %s",
                                    len(changes["new_windows"]),
                                    [
                                        (w["title"], w["pid"], "cross_app" if w["cross_app"] else "same_app")
                                        for w in changes["new_windows"]
                                    ],
                                )

                        wr = _build_window_ref(pid, window_id, screen_pt[0], screen_pt[1])
                        await _recording_hook("computer_click",
                            {"element_index": element_index, "pid": pid, "window_id": window_id},
                            result, click_point=screen_pt, window_ref=wr)

                        # For editable controls (Edit, ComboBox, Document),
                        # UIA Invoke establishes focus asynchronously —
                        # a follow-up type_set_value/computer_type races with
                        # the app's focus setup and silently drops input.
                        # 800ms matches the Swift AXPress delay validated
                        # against WebKit/Chromium async DOM focus.
                        edit_types = {"Edit", "ComboBox", "Document", "RichEdit"}
                        if el.control_type in edit_types:
                            await asyncio.sleep(0.8)

                        # capture_after
                        if capture_after:
                            cap = await _capture_and_detect(pid, window_id)
                            result["capture_after"] = cap
                        return result
                    except Exception as e:
                        logger.debug("UIA Invoke failed (falling back to pixel click): %s", e)

            # Resolve center coords (screen space) for pixel click
            try:
                resolved_x, resolved_y = _resolve_element_center(pid, window_id, element_index)
            except ValueError as e:
                return {"success": False, "error": str(e), "suggestion": f"Call get_window_state(pid={pid}, window_id={window_id}) to refresh the element cache"}
            x, y = resolved_x, resolved_y

        if x is None or y is None:
            return {"success": False, "error": "x and y are required (or element_index + pid + window_id)", "suggestion": "Provide (x, y) pixel coordinates or (element_index + pid + window_id) from a previous get_window_state call"}

        # For pixel clicks (no element_index), if pid+window_id are provided,
        # add window origin to convert window-relative screenshot coords to screen coords.
        # Never do this for from_zoom — zoom coords go through _resolve_from_zoom which
        # already translates to screen space via the stored crop_rect.
        if element_index is None and pid is not None and window_id is not None and not from_zoom:
            x, y = _window_resized_to_screen(pid, window_id, x, y)

        # Translate from_zoom coords
        if from_zoom and window_id is None:
            return {"success": False, "error": "window_id is required when from_zoom=True", "suggestion": "Provide the window_id used in the prior computer_zoom call"}
        try:
            x, y = _resolve_from_zoom(x, y, from_zoom=from_zoom, pid=pid, window_id=window_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        actual_x, actual_y = _scale_to_actual(x, y)

        # Agent cursor overlay: fly to target before the action
        await _animate_cursor_to(actual_x, actual_y)

        automation_handler = _get_handlers().automation

        # Write debug crosshair image if requested
        if debug_image_out:
            try:
                screenshot_result = await automation_handler.screenshot()
                if screenshot_result.get("success") and "image" in screenshot_result:
                    from PIL import Image, ImageDraw
                    img = screenshot_result["image"]
                    draw = ImageDraw.Draw(img)
                    r = 12
                    draw.ellipse([actual_x - r, actual_y - r, actual_x + r, actual_y + r],
                                 outline="red", width=3)
                    draw.line([actual_x - r - 4, actual_y, actual_x + r + 4, actual_y],
                              fill="red", width=2)
                    draw.line([actual_x, actual_y - r - 4, actual_x, actual_y + r + 4],
                              fill="red", width=2)
                    img.save(debug_image_out)
                else:
                    logger.warning("debug_image_out: screenshot failed for %s", debug_image_out)
            except Exception as _dbg_exc:
                logger.warning("debug_image_out: %s", _dbg_exc)

        # force_frontmost: activate the window and send a HID-level click.
        # Required for WebView2 / UWP targets that filter per-pid events.
        if force_frontmost and sys.platform == "win32" and pid is not None and window_id is not None:
            if modifier:
                return {"success": False, "error": "force_frontmost does not support modifier keys — use two separate calls: computer_press_key to hold the modifier, then force_frontmost click without modifier", "suggestion": "Split into separate key_down + click + key_up calls"}
            try:
                await _animate_cursor_to(actual_x, actual_y)
                import win32gui as _wg
                fg_ok = _wg.SetForegroundWindow(int(window_id))
                if not fg_ok:
                    logger.debug("SetForegroundWindow returned 0 for window_id=%d — click may land on wrong window", window_id)
                await asyncio.sleep(0.05)  # 50ms settle
                _mouse_event_click(actual_x, actual_y, button, count)
                result = {"success": True, "method": "mouse_event",
                           "x": actual_x, "y": actual_y, "button": button,
                           "count": count, "force_frontmost": True}
                wr = _build_window_ref(pid, window_id, actual_x, actual_y)
                await _recording_hook("computer_click",
                    {"x": x, "y": y, "button": button, "force_frontmost": True, "count": count},
                    result, click_point=(actual_x, actual_y), window_ref=wr)
                if capture_after and pid is not None and window_id is not None:
                    cap = await _capture_and_detect(pid, window_id)
                    result["capture_after"] = cap
                return result
            except Exception as _e:
                return {"success": False, "error": f"force_frontmost click failed: {_e}", "suggestion": "Try without force_frontmost=True, or use computer_hotkey to bring the window to front first"}

        # Animate cursor overlay to click point
        try:
            await _animate_cursor_to(actual_x, actual_y)
        except Exception:
            pass

        # Hold modifier keys during click if requested
        if modifier:
            for mod in modifier:
                await automation_handler.key_down(mod)

        if button == "left":
            result = await automation_handler.left_click(actual_x, actual_y)
            for _ in range(count - 1):
                await asyncio.sleep(0.05)
                await automation_handler.left_click(actual_x, actual_y)
        elif button == "right":
            result = await automation_handler.right_click(actual_x, actual_y)
            for _ in range(count - 1):
                await asyncio.sleep(0.05)
                await automation_handler.right_click(actual_x, actual_y)
        elif button == "middle":
            result = await automation_handler.middle_click(actual_x, actual_y)
            for _ in range(count - 1):
                await asyncio.sleep(0.05)
                await automation_handler.middle_click(actual_x, actual_y)
        else:
            result = await automation_handler.left_click(actual_x, actual_y)
            for _ in range(count - 1):
                await asyncio.sleep(0.05)
                await automation_handler.left_click(actual_x, actual_y)

        if modifier:
            for mod in reversed(modifier):
                await automation_handler.key_up(mod)

        wr = _build_window_ref(pid, window_id, actual_x, actual_y)
        await _recording_hook("computer_click", {"x": x, "y": y, "button": button, "count": count}, result, click_point=(actual_x, actual_y), window_ref=wr)

        # capture_after
        if capture_after and pid is not None and window_id is not None:
            cap = await _capture_and_detect(pid, window_id)
            result["capture_after"] = cap

        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_move(x: int, y: int) -> Any:
        """
        Move the mouse cursor to (x, y) screen coordinates.

        Use this for positioning the cursor before a click, drag, or scroll.

        Args:
            x: X coordinate in screen pixels
            y: Y coordinate in screen pixels
        """
        actual_x, actual_y = _scale_to_actual(x, y)
        await _animate_cursor_to(actual_x, actual_y)
        automation_handler = _get_handlers().automation
        return await automation_handler.move_cursor(actual_x, actual_y)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_drag(
        start_x: Optional[int] = None,
        start_y: Optional[int] = None,
        end_x: Optional[int] = None,
        end_y: Optional[int] = None,
        button: str = "left",
        duration: float = 0.5,
        element_index: Optional[int] = None,
        window_id: Optional[int] = None,
        pid: Optional[int] = None,
        from_zoom: bool = False,
        modifier: Optional[List[str]] = None,
    ) -> Any:
        """
        Drag from start coordinates to end coordinates.

        Args:
            start_x: Starting X coordinate
            start_y: Starting Y coordinate
            end_x: Ending X coordinate
            end_y: Ending Y coordinate
            button: Mouse button to use (default: 'left')
            duration: Duration of the drag in seconds (default: 0.5)
            element_index: Index from get_window_state to use as drag source
            window_id: Window ID (required with element_index or from_zoom)
            pid: Process ID (required with element_index)
            from_zoom: When True, coords are in zoomed-image space
            modifier: Modifier keys held during drag (cmd, shift, option, ctrl)
        """
        if element_index is not None:
            if from_zoom:
                return {"success": False, "error": "element_index and from_zoom are mutually exclusive — element_index already returns screen coords", "suggestion": "Remove from_zoom=True or use raw coordinates with from_zoom instead"}
            if pid is None or window_id is None:
                return {"success": False, "error": "pid and window_id are required with element_index", "suggestion": "Provide pid and window_id from a previous get_window_state call"}
            try:
                start_x, start_y = _resolve_element_center(pid, window_id, element_index)
            except ValueError as e:
                return {"success": False, "error": str(e), "suggestion": f"Call get_window_state(pid={pid}, window_id={window_id}) to refresh the element cache"}

        if start_x is None or start_y is None or end_x is None or end_y is None:
            return {"success": False, "error": "start_x, start_y, end_x, end_y are required", "suggestion": "Provide all four coordinates: start_x, start_y (from cursor position or element), and end_x, end_y (target position)"}

        # Pixel coords: add window origin to convert window-relative to screen coords.
        # Skip for from_zoom — _resolve_from_zoom already translates to screen space.
        if element_index is None and pid is not None and window_id is not None and not from_zoom:
            start_x, start_y = _window_resized_to_screen(pid, window_id, start_x, start_y)
            end_x, end_y = _window_resized_to_screen(pid, window_id, end_x, end_y)

        if from_zoom and window_id is None:
            return {"success": False, "error": "window_id is required when from_zoom=True", "suggestion": "Provide the window_id used in the prior computer_zoom call"}
        try:
            start_x, start_y = _resolve_from_zoom(start_x, start_y, from_zoom=from_zoom, pid=pid, window_id=window_id)
            end_x, end_y = _resolve_from_zoom(end_x, end_y, from_zoom=from_zoom, pid=pid, window_id=window_id)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        actual_start_x, actual_start_y = _scale_to_actual(start_x, start_y)
        actual_end_x, actual_end_y = _scale_to_actual(end_x, end_y)
        await _animate_cursor_to(actual_start_x, actual_start_y)
        automation_handler = _get_handlers().automation
        await automation_handler.move_cursor(actual_start_x, actual_start_y)

        # Step the drag manually so the overlay arrow tracks the path.
        # automation_handler.drag_to() does its own internal stepping, but
        # the overlay would stay at the start point the whole time.
        from computer_server.overlay import _agent_cursor

        btn = automation_handler._map_button(button)
        await automation_handler.mouse_down(actual_start_x, actual_start_y, button=button)
        steps = max(1, int(duration / 0.02))
        for i in range(1, steps + 1):
            t = i / steps
            ix = int(actual_start_x + (actual_end_x - actual_start_x) * t)
            iy = int(actual_start_y + (actual_end_y - actual_start_y) * t)
            automation_handler.mouse.position = (ix, iy)
            # Track overlay along the drag path and keep it visible
            if _agent_cursor is not None:
                _agent_cursor.set_position_visible(ix, iy)
            await asyncio.sleep(duration / steps)
        automation_handler.mouse.position = (actual_end_x, actual_end_y)
        await automation_handler.mouse_up(actual_end_x, actual_end_y, button=button)
        result = {"success": True}
        wr = _build_window_ref(pid, window_id, actual_start_x, actual_start_y)
        await _recording_hook("computer_drag", {
            "start_x": start_x, "start_y": start_y,
            "end_x": end_x, "end_y": end_y, "button": button,
        }, result, click_point=(actual_start_x, actual_start_y), window_ref=wr)
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_scroll(
        x: Optional[int] = None,
        y: Optional[int] = None,
        scroll_x: int = 0,
        scroll_y: int = 0,
        direction: Optional[str] = None,
        by: str = "line",
        amount: int = 3,
        smooth: bool = True,
        element_index: Optional[int] = None,
        window_id: Optional[int] = None,
        pid: Optional[int] = None,
        capture_after: bool = False,
    ) -> Any:
        """
        Scroll at the specified position or on a cached element.

        Args:
            x: X coordinate where to scroll
            y: Y coordinate where to scroll
            scroll_x: Horizontal scroll amount (positive = right, negative = left)
            scroll_y: Vertical scroll amount (positive = up, negative = down)
            direction: Shorthand - 'up', 'down', 'left', or 'right' (overrides scroll_x/scroll_y)
            by: Scroll granularity - 'line' or 'page' (default: 'line')
            amount: Number of scroll steps (default: 3)
            smooth: When True, emit scroll as many small wheel ticks with 15ms spacing
                for human-like smooth scrolling (default: True).
            element_index: Index from get_window_state to scroll on
            window_id: Window ID (required with element_index)
            pid: Process ID (required with element_index)
            capture_after: When True, take a fresh screenshot + nodes after the action.
        """
        if direction:
            direction = direction.lower()
            if direction == "up":
                scroll_y = amount
            elif direction == "down":
                scroll_y = -amount
            elif direction == "left":
                scroll_x = -amount
            elif direction == "right":
                scroll_x = amount

        if element_index is not None:
            if pid is None or window_id is None:
                return {"success": False, "error": "pid and window_id are required with element_index", "suggestion": "Provide pid and window_id from a previous get_window_state call"}
            try:
                x, y = _resolve_element_center(pid, window_id, element_index)
            except ValueError as e:
                return {"success": False, "error": str(e), "suggestion": f"Call get_window_state(pid={pid}, window_id={window_id}) to refresh the element cache"}

        # Pixel coords: add window origin to convert window-relative to screen coords.
        # scroll only uses (x, y) when both are provided; partial coords go through as-is.
        if (
            element_index is None
            and pid is not None
            and window_id is not None
            and x is not None
            and y is not None
        ):
            x, y = _window_resized_to_screen(pid, window_id, x, y)

        automation_handler = _get_handlers().automation

        scroll_ax: Optional[int] = None
        scroll_ay: Optional[int] = None
        if x is not None and y is not None:
            scroll_ax, scroll_ay = _scale_to_actual(x, y)
            await _animate_cursor_to(scroll_ax, scroll_ay)
            await automation_handler.move_cursor(scroll_ax, scroll_ay)

        if smooth and scroll_y != 0:
            # Emit many small individual wheel ticks for smooth human-like scrolling
            direction = 1 if scroll_y > 0 else -1
            ticks = abs(scroll_y)
            for _ in range(ticks):
                automation_handler.mouse.scroll(0, direction)
                await asyncio.sleep(0.015)
            result = {"success": True}
        elif smooth and scroll_x != 0:
            direction = 1 if scroll_x > 0 else -1
            ticks = abs(scroll_x)
            for _ in range(ticks):
                automation_handler.mouse.scroll(direction, 0)
                await asyncio.sleep(0.015)
            result = {"success": True}
        else:
            result = await automation_handler.scroll(scroll_x, scroll_y)
        if scroll_ax is not None and pid is not None and window_id is not None:
            wr = _build_window_ref(pid, window_id, scroll_ax, scroll_ay)
            await _recording_hook("computer_scroll", {
                "x": x, "y": y, "scroll_x": scroll_x, "scroll_y": scroll_y,
            }, result, click_point=(scroll_ax, scroll_ay), window_ref=wr)
        else:
            await _recording_hook("computer_scroll", {
                "x": x, "y": y, "scroll_x": scroll_x, "scroll_y": scroll_y,
            }, result)

        # capture_after
        if capture_after and pid is not None and window_id is not None:
            result["capture_after"] = await _capture_and_detect(pid, window_id)

        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_mouse_down(
        x: Optional[int] = None, y: Optional[int] = None, button: str = "left"
    ) -> Any:
        """
        Press and hold a mouse button.

        Args:
            x: Optional X coordinate (uses current position if not specified)
            y: Optional Y coordinate (uses current position if not specified)
            button: Mouse button - 'left', 'right', or 'middle' (default: 'left')
        """
        actual_x, actual_y = None, None
        if x is not None and y is not None:
            actual_x, actual_y = _scale_to_actual(x, y)
            await _animate_cursor_to(actual_x, actual_y)
        automation_handler = _get_handlers().automation
        return await automation_handler.mouse_down(actual_x, actual_y, button=button)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_mouse_up(
        x: Optional[int] = None, y: Optional[int] = None, button: str = "left"
    ) -> Any:
        """
        Release a mouse button.

        Args:
            x: Optional X coordinate (uses current position if not specified)
            y: Optional Y coordinate (uses current position if not specified)
            button: Mouse button - 'left', 'right', or 'middle' (default: 'left')
        """
        actual_x, actual_y = None, None
        if x is not None and y is not None:
            actual_x, actual_y = _scale_to_actual(x, y)
            await _animate_cursor_to(actual_x, actual_y)
        automation_handler = _get_handlers().automation
        return await automation_handler.mouse_up(actual_x, actual_y, button=button)

    # ============================================================
    # KEYBOARD ACTIONS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_type(
        text: str,
        capture_after: bool = False,
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
    ) -> Any:
        """
        Type text at the current cursor position via clipboard paste (Ctrl+V).

        All text — ASCII and non-ASCII — goes through the clipboard to
        avoid triggering unwanted IME (Chinese input method) behaviour
        when the user is in Chinese mode.

        Args:
            text: The text to type
            capture_after: When True, take a fresh screenshot + nodes after the action.
            pid: Process ID for capture_after look-up.
            window_id: Window ID for capture_after look-up.
        """
        automation_handler = _get_handlers().automation
        result = await automation_handler.type_text(text)
        await _recording_hook("computer_type", {"text": text}, result)

        # capture_after
        if capture_after and pid is not None and window_id is not None:
            result["capture_after"] = await _capture_and_detect(pid, window_id)

        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_press_key(key: str) -> Any:
        """
        Press a single key.

        Args:
            key: The key to press (e.g., 'enter', 'tab', 'escape', 'a', 'b', etc.)
        """
        automation_handler = _get_handlers().automation
        result = await automation_handler.press_key(key)
        await _recording_hook("computer_press_key", {"key": key}, result)
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_hotkey(keys: List[str]) -> Any:
        """
        Press a key combination (hotkey).

        Args:
            keys: List of keys to press together (e.g., ['ctrl', 'c'] for copy,
                  ['ctrl', 'c'] for copy, ['ctrl', 'v'] for paste, ['alt', 'tab'] for window switch)
        """
        automation_handler = _get_handlers().automation
        result = await automation_handler.hotkey(keys)
        await _recording_hook("computer_hotkey", {"keys": keys}, result)
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_key_down(key: str) -> Any:
        """
        Press and hold a key.

        Args:
            key: The key to hold down
        """
        automation_handler = _get_handlers().automation
        result = await automation_handler.key_down(key)
        await _recording_hook("computer_key_down", {"key": key}, result)
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_key_up(key: str) -> Any:
        """
        Release a held key.

        Args:
            key: The key to release
        """
        automation_handler = _get_handlers().automation
        result = await automation_handler.key_up(key)
        await _recording_hook("computer_key_up", {"key": key}, result)
        return result

    # ============================================================
    # ELEMENT VALUE ACTIONS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_set_value(
        element_index: int,
        value: str,
        window_id: int,
        pid: int,
    ) -> Any:
        """
        Set a value on a UI element (text field, slider, checkbox, select).

        Strategy (best-effort cross-platform):
        - Text-like elements: click to focus, select all (Ctrl+A / Cmd+A), type value.
        - Windows UIA elements with ValuePattern: set value directly via COM.
        - Checkbox/radio: click the element to toggle.

        For select dropdowns, prefer using computer_click on the dropdown first,
        then computer_click on the target option.

        Args:
            element_index: Index from get_window_state nodes.
            value: New value to set.
            window_id: Window ID.
            pid: Process ID.

        Returns:
            Dictionary with success status.
        """
        import sys

        from computer_server.handlers.element_cache import get_element_cache

        el = get_element_cache().get_element(pid, window_id, element_index)
        if el is None:
            return {
                "success": False,
                "error": f"No cached element at index {element_index} for pid={pid}, window_id={window_id}",
                "suggestion": f"The element cache may have expired. Call get_window_state(pid={pid}, window_id={window_id}) to refresh it",
            }

        # Convert window-relative element coords to screen space for clicking
        origin = get_element_cache().get_window_origin_optional(pid, window_id)
        if origin is None:
            return {
                "success": False,
                "error": (
                    f"Element at index {element_index} found for pid={pid}, "
                    f"window_id={window_id}, but window origin is missing from "
                    f"the cache"
                ),
                "suggestion": (
                    f"Call get_window_state(pid={pid}, window_id={window_id}) "
                    f"to refresh the element cache"
                ),
            }
        origin_x, origin_y = origin
        screen_cx = origin_x + el.center_x
        screen_cy = origin_y + el.center_y

        # Activate the target window first — otherwise clicks and keystrokes
        # land on whatever window happens to be foreground.
        window_handler = _get_handlers().window
        await window_handler.activate_window(window_id)
        await _animate_cursor_to(screen_cx, screen_cy)

        ct = (el.control_type or "").lower()
        is_text_like = ct in (
            "edit", "text", "document", "spinner",
        )
        is_checkable = ct in (
            "checkbox", "radio", "radiobutton",
            "toggle", "switch",
        )

        # Platform-specific direct value set (Windows UIA ValuePattern).
        # Resolve a fresh UIA element from the current point; cached COM refs
        # are intentionally not reused across calls / apartments.
        if sys.platform == "win32" and is_text_like:
            try:
                _set_value_via_uia_point(screen_cx, screen_cy, value)
                result = {"success": True, "element_index": element_index, "value": value, "method": "uia"}
                await _recording_hook("computer_set_value", {
                    "element_index": element_index, "value": value, "pid": pid, "window_id": window_id,
                }, result)
                return result
            except Exception as e:
                logger.debug("UIA ValuePattern set failed (will fall back): %s", e)

        # For checkable elements, click to toggle
        if is_checkable:
            automation_handler = _get_handlers().automation
            actual_cx, actual_cy = _scale_to_actual(screen_cx, screen_cy)
            await automation_handler.move_cursor(actual_cx, actual_cy)
            click_result = await automation_handler.left_click(actual_cx, actual_cy)
            if not click_result.get("success"):
                return click_result
            result = {"success": True, "element_index": element_index, "value": value, "method": "click"}
            await _recording_hook("computer_set_value", {
                "element_index": element_index, "value": value, "pid": pid, "window_id": window_id,
            }, result)
            return result

        # For text-like elements: click to focus, select all, type value
        if is_text_like or "click" in (el.actions or []):
            automation_handler = _get_handlers().automation
            actual_cx, actual_cy = _scale_to_actual(screen_cx, screen_cy)

            # Click to focus the element
            await automation_handler.move_cursor(actual_cx, actual_cy)
            click_result = await automation_handler.left_click(actual_cx, actual_cy)
            if not click_result.get("success"):
                return click_result

            # Select all existing text and type new value
            import asyncio
            await asyncio.sleep(0.05)

            await automation_handler.hotkey(["ctrl", "a"])

            await automation_handler.type_text(value)
            result = {"success": True, "element_index": element_index, "value": value, "method": "click_type"}
            await _recording_hook("computer_set_value", {
                "element_index": element_index, "value": value, "pid": pid, "window_id": window_id,
            }, result)
            return result

        return {
            "success": False,
            "error": f"Cannot set value on element type '{el.control_type}'. Element has no text pattern and is not checkable.",
            "suggestion": "Try computer_click on the element first to focus it, then use computer_type to enter the value",
        }

    # ============================================================
    # CLIPBOARD ACTIONS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_clipboard_get() -> Any:
        """
        Get the current clipboard content.

        Returns:
            Dictionary with clipboard content.
        """
        automation_handler = _get_handlers().automation
        return await automation_handler.copy_to_clipboard()

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_clipboard_set(text: str) -> Any:
        """
        Set the clipboard content.

        Args:
            text: Text to copy to clipboard
        """
        automation_handler = _get_handlers().automation
        return await automation_handler.set_clipboard(text)

    # ============================================================
    # SHELL COMMANDS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_run_command(command: str, shell: str = "powershell") -> Any:
        """
        Execute a shell command and return the output.

        Args:
            command: The shell command to execute.
            shell: "powershell" (default) or "cmd".

        Returns:
            Dictionary with 'stdout', 'stderr', and 'return_code' keys.
        """
        automation_handler = _get_handlers().automation
        return await automation_handler.run_command(command, shell=shell)

    # ============================================================
    # FILE SYSTEM OPERATIONS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_file_read(path: str) -> Any:
        """Read the text content of a file."""
        file_handler = _get_handlers().file
        return await file_handler.read_text(path)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_file_write(path: str, content: str) -> Any:
        """Write text content to a file."""
        file_handler = _get_handlers().file
        return await file_handler.write_text(path, content)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_file_exists(path: str) -> Any:
        """Check if a file exists."""
        file_handler = _get_handlers().file
        return await file_handler.file_exists(path)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_directory_exists(path: str) -> Any:
        """Check if a directory exists."""
        file_handler = _get_handlers().file
        return await file_handler.directory_exists(path)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_list_directory(path: str) -> Any:
        """List the contents of a directory."""
        file_handler = _get_handlers().file
        return await file_handler.list_dir(path)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_create_directory(path: str) -> Any:
        """Create a directory."""
        file_handler = _get_handlers().file
        return await file_handler.create_dir(path)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_delete_file(path: str) -> Any:
        """Delete a file."""
        file_handler = _get_handlers().file
        return await file_handler.delete_file(path)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_delete_directory(path: str) -> Any:
        """Delete a directory."""
        file_handler = _get_handlers().file
        return await file_handler.delete_dir(path)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_file_size(path: str) -> Any:
        """Get the size of a file in bytes."""
        file_handler = _get_handlers().file
        return await file_handler.get_file_size(path)

    # ============================================================
    # WINDOW MANAGEMENT
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_open(target: str, browser: str = "") -> Any:
        """Open a file or URL.

        For URLs (http/https), uses the specified browser if given
        (e.g. "chrome", "edge", "firefox"), falling back to the
        system default if the browser isn't found.

        Args:
            target: File path or URL to open.
            browser: Preferred browser for URLs — "chrome", "edge",
                "firefox", etc. Ignored for files.
        """
        window_handler = _get_handlers().window
        result = await window_handler.open(target, browser=browser)
        await _recording_hook("computer_open", {"target": target, "browser": browser}, result)
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_launch_app(app: str, args: Optional[List[str]] = None) -> Any:
        """Launch an application by name, path, or alias.

        Three-pass name resolution on Windows:
          1. Direct — absolute path or executable found on PATH
          2. shell:AppsFolder — unified app registry (desktop + UWP apps)
          3. Start Menu scan — case-insensitive .lnk shortcut name match

        Examples: "notepad", "chrome", "Calculator", "C:\\app\\myapp.exe"
        """
        window_handler = _get_handlers().window
        result = await window_handler.launch(app, args)
        await _recording_hook("computer_launch_app", {"app": app, "args": args}, result)
        return result

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_active_window() -> Any:
        """Get the currently active window ID."""
        window_handler = _get_handlers().window
        return await window_handler.get_current_window_id()

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_window_name(window_id: str) -> Any:
        """Get the title/name of a window."""
        window_handler = _get_handlers().window
        return await window_handler.get_window_name(window_id)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_window_size(window_id: str) -> Any:
        """Get the size of a window."""
        window_handler = _get_handlers().window
        return await window_handler.get_window_size(window_id)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_window_position(window_id: str) -> Any:
        """Get the position of a window."""
        window_handler = _get_handlers().window
        return await window_handler.get_window_position(window_id)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_set_window_size(window_id: str, width: int, height: int) -> Any:
        """Set the size of a window."""
        window_handler = _get_handlers().window
        return await window_handler.set_window_size(window_id, width, height)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_set_window_position(window_id: str, x: int, y: int) -> Any:
        """Set the position of a window."""
        window_handler = _get_handlers().window
        return await window_handler.set_window_position(window_id, x, y)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_activate_window(window_id: str) -> Any:
        """Bring a window to the foreground and focus it.

        If the window is minimized, it is restored to normal size first
        (SW_RESTORE), then focused.  Use this instead of maximize_window
        to bring back a minimized window at its original size.
        """
        window_handler = _get_handlers().window
        result = await window_handler.activate_window(window_id)
        # Record window title + process name for stale-HWND resolution during replay
        window_title = ""
        process_name = ""
        try:
            import win32gui, win32process, psutil
            hwnd = int(window_id)
            window_title = win32gui.GetWindowText(hwnd) or ""
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid:
                process_name = psutil.Process(pid).name() or ""
        except Exception:
            pass
        await _recording_hook("computer_activate_window",
            {"window_id": window_id, "window_title": window_title, "process_name": process_name}, result)
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_minimize_window(window_id: str) -> Any:
        """Minimize a window."""
        window_handler = _get_handlers().window
        return await window_handler.minimize_window(window_id)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_maximize_window(window_id: str) -> Any:
        """Maximize a window."""
        window_handler = _get_handlers().window
        return await window_handler.maximize_window(window_id)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_close_window(window_id: str) -> Any:
        """Close a window."""
        window_handler = _get_handlers().window
        return await window_handler.close_window(window_id)

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_app_windows(app: str) -> Any:
        """
        Find windows by process name, title, or PID.

        A single argument smart-dispatches:
        - Numeric value → treated as a PID
        - String value → searched against BOTH window title AND process
          .exe name (case-insensitive substring match)

        Args:
            app: PID (numeric), process name, or window title substring.

        Returns a list of windows with 'id' and 'title' fields so you
        can identify the main window without extra calls.
        """
        window_handler = _get_handlers().window
        return await window_handler.get_application_windows(app)

    # ============================================================
    # APP & WINDOW LISTING
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_list_apps() -> Any:
        """
        List all running and installed applications.

        Returns:
            Dictionary with 'apps' list containing app info (name, pid, bundle_id, running, active).
        """
        import sys

        try:
            import win32gui
            import win32process
            import win32api

            app_map: Dict[int, Dict[str, Any]] = {}

            def _enum_apps(hwnd, _):
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                try:
                    _, hwnd_pid = win32process.GetWindowThreadProcessId(hwnd)
                    if hwnd_pid in app_map:
                        return True
                    title = win32gui.GetWindowText(hwnd) or ""
                    try:
                        handle = win32api.OpenProcess(0x0400 | 0x0010, False, hwnd_pid)
                        exe = win32process.GetModuleFileNameEx(handle, 0)
                        name = exe.rsplit("\\", 1)[-1]
                        win32api.CloseHandle(handle)
                    except Exception:
                        name = title or f"PID:{hwnd_pid}"
                    app_map[hwnd_pid] = {
                        "name": name,
                        "pid": hwnd_pid,
                        "running": True,
                    }
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_enum_apps, None)
            return {"success": True, "apps": list(app_map.values())}
        except Exception as e:
            return {"success": False, "error": str(e)}

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_list_windows(
        pid: Optional[int] = None,
        on_screen_only: bool = False,
        include_xaml: bool = False,
    ) -> Any:
        """
        List all windows, optionally filtered by process ID.

        Windows 11 XAML popups (Start menu, notifications, settings
        flyouts) don't use traditional HWNDs and are invisible to
        EnumWindows.  Set include_xaml=True to also detect these via
        UI Automation.

        Args:
            pid: Optional process ID to filter by.
            on_screen_only: When True, only return windows on screen.
            include_xaml: When True, also detect XAML popups via UIA.

        Returns:
            Dictionary with 'windows' list. Each window has 'minimized',
            and XAML entries carry 'source': 'uia_xaml'.
        """
        import sys

        try:
            import win32gui
            import win32process
            import win32con
            from computer_server.handlers.generic import _is_meaningful_window

            windows: List[Dict[str, Any]] = []

            def _enum_windows(hwnd, _):
                try:
                    visible = win32gui.IsWindowVisible(hwnd)
                    if on_screen_only and not visible:
                        return True
                    style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
                    if not (style & win32con.WS_VISIBLE):
                        return True
                    _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
                    if pid is not None and win_pid != pid:
                        return True
                    if not _is_meaningful_window(hwnd):
                        return True
                    title = win32gui.GetWindowText(hwnd) or ""
                    rect = win32gui.GetWindowRect(hwnd)
                    w, h = rect[2] - rect[0], rect[3] - rect[1]
                    windows.append({
                        "window_id": hwnd,
                        "pid": win_pid,
                        "title": title,
                        "x": rect[0],
                        "y": rect[1],
                        "width": w,
                        "height": h,
                        "is_on_screen": visible,
                        "minimized": rect[0] < -32000 or rect[1] < -32000,
                    })
                except Exception:
                    pass
                return True

            win32gui.EnumWindows(_enum_windows, None)

            # XAML popup detection via UIA (optional, off by default)
            if include_xaml:
                try:
                    import uiautomation as auto

                    seen_hwnds = {w["window_id"] for w in windows}
                    root = auto.GetRootControl()
                    for child in root.GetChildren():
                        try:
                            ct = child.ControlType
                            hwnd = child.NativeWindowHandle or 0
                            if hwnd and hwnd in seen_hwnds:
                                continue  # already in EnumWindows list
                            name = child.Name or ""
                            rect = child.BoundingRectangle
                            if not rect:
                                continue
                            left, top, right, bottom = rect.left, rect.top, rect.right, rect.bottom
                            w, h = right - left, bottom - top
                            if w <= 0 or h <= 0:
                                continue
                            if pid is not None:
                                if child.ProcessId != pid:
                                    continue
                            windows.append({
                                "window_id": hwnd or 0,
                                "pid": child.ProcessId or 0,
                                "title": name,
                                "x": left, "y": top,
                                "width": w, "height": h,
                                "is_on_screen": True,
                                "minimized": False,
                                "source": "uia_xaml",
                            })
                        except Exception:
                            pass
                except ImportError:
                    pass  # uiautomation not available
                except Exception:
                    logger.debug("XAML popup detection failed", exc_info=True)

            return {"success": True, "windows": windows}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ============================================================
    # DESKTOP ENVIRONMENT
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_desktop_environment() -> Any:
        """Get information about the current desktop environment."""
        desktop_handler = _get_handlers().desktop
        return await desktop_handler.get_desktop_environment()

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_set_wallpaper(path: str) -> Any:
        """Set the desktop wallpaper."""
        desktop_handler = _get_handlers().desktop
        return await desktop_handler.set_wallpaper(path)

    # ============================================================
    # ACCESSIBILITY
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_accessibility_tree(
        window_id: int = 0,
    ) -> Any:
        """
        Get the UIA accessibility tree for a window.

        Uses UI Automation — works with UWP/XAML, Electron, Qt, and
        classic Win32 apps.  Returns a markdown tree and element count.

        Args:
            window_id: Target window handle.  Defaults to 0 = foreground window.

        Returns:
            Dictionary with 'tree_markdown', 'node_count', 'tree_quality'.
        """
        accessibility_handler = _get_handlers().accessibility
        target = window_id if window_id else None
        return await accessibility_handler.get_accessibility_tree(target)

    # ============================================================
    # RICH WINDOW STATE OBSERVATION (Element Cache + UIA + Vision)
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_window_state(
        pid: int,
        window_id: int,
        query: Optional[str] = None,
        screenshot_out_file: Optional[str] = None,
        max_image_dimension: int = 1280,
        force_som: bool = False,
    ) -> Any:
        """
        Get a rich snapshot of a window's UI state: screenshot + element tree + indexed nodes.

        Combines accessibility tree walking with optional SOM vision detection.
        Each actionable element gets an element_index that can be used with
        computer_click, computer_double_click, computer_scroll, etc.

        Args:
            pid: Process ID of the target application.
            window_id: Window ID to snapshot.
            query: Optional substring filter for the debug tree markdown.
            screenshot_out_file: Optional path to write screenshot PNG (skips inline base64).
            max_image_dimension: Maximum dimension for the screenshot (default 1280).
            force_som: When True, run SOM vision detection regardless of tree quality.

        Returns:
            Dictionary with:
            - screenshot: base64-encoded JPEG or path to file
            - nodes: list of elements with element_index, bounds, center, control_type, name, actions
            - node_count: total number of nodes
            - tree_markdown: formatted markdown representation of the UI tree
            - tree_quality: 'rich', 'ok', 'poor', or 'empty'
            - total_elements: total elements visited
        """
        import asyncio
        import sys
        from io import BytesIO

        from PIL import Image as PILImage

        from computer_server.handlers.element_cache import CachedElement, get_element_cache

        automation_handler = _get_handlers().automation

        # 0. Bring the target window to the foreground so the screenshot
        #    captures its actual content, not overlapping windows.
        if sys.platform == "win32":
            try:
                import win32gui as _wg
                _wg.SetForegroundWindow(int(window_id))
                await asyncio.sleep(0.05)
            except Exception:
                pass

        # 1. Take a full-screen screenshot
        result = await automation_handler.screenshot(format="jpeg", quality=85)
        if not result.get("success"):
            return {"success": False, "error": "Screenshot failed: " + result.get("error", "unknown")}
        img_bytes = base64.b64decode(result["image_data"])
        img = PILImage.open(BytesIO(img_bytes))
        full_w, full_h = img.size
        screenshot_origin = result.get("origin") or {}
        screenshot_origin_x = int(screenshot_origin.get("x", 0) or 0)
        screenshot_origin_y = int(screenshot_origin.get("y", 0) or 0)

        # 2. Get window bounds and crop screenshot to window
        bounds = await automation_handler.get_window_bounds(pid, window_id)
        win_origin_x = 0
        win_origin_y = 0
        crop_origin_screen_x = 0
        crop_origin_screen_y = 0
        if bounds is not None and bounds["width"] > 0 and bounds["height"] > 0:
            wx = bounds["x"]
            wy = bounds["y"]
            ww = bounds["width"]
            wh = bounds["height"]
            # Clamp crop to image bounds (PIL requires non-negative coords)
            # Preserve real window origin — multi-monitor Windows can have negative coords.
            # ImageGrab(all_screens=True) returns a virtual-desktop image whose
            # pixel (0,0) maps to screenshot_origin, not necessarily screen (0,0).
            crop_x1 = max(0, wx - screenshot_origin_x)
            crop_y1 = max(0, wy - screenshot_origin_y)
            crop_x2 = min(full_w, wx + ww - screenshot_origin_x)
            crop_y2 = min(full_h, wy + wh - screenshot_origin_y)
            if crop_x2 > crop_x1 and crop_y2 > crop_y1:
                img = img.crop((crop_x1, crop_y1, crop_x2, crop_y2))
                win_origin_x = wx
                win_origin_y = wy
                crop_origin_screen_x = crop_x1 + screenshot_origin_x
                crop_origin_screen_y = crop_y1 + screenshot_origin_y

        # Offset from real window origin to visible crop origin.
        # Zero when the window is fully on-screen; non-zero when the window
        # has negative coordinates or extends beyond a screen edge.
        crop_origin_window_x = crop_origin_screen_x - win_origin_x
        crop_origin_window_y = crop_origin_screen_y - win_origin_y

        original_width, original_height = img.size

        # 3. Resize if needed, track compression ratio for zoom
        if original_width > max_image_dimension or original_height > max_image_dimension:
            if original_width > original_height:
                new_w = max_image_dimension
                new_h = int(original_height * max_image_dimension / original_width)
            else:
                new_h = max_image_dimension
                new_w = int(original_width * max_image_dimension / original_height)
            compression_ratio = original_width / new_w
            img = img.resize((new_w, new_h), PILImage.Resampling.LANCZOS)
            screen_width, screen_height = new_w, new_h
        else:
            compression_ratio = 1.0
            screen_width, screen_height = original_width, original_height

        # Store zoom metadata for computer_zoom coordinate translation
        _zoom_metadata[(pid, window_id)] = {
            "compression_ratio": compression_ratio,
            "original_width": original_width,
            "original_height": original_height,
            "window_origin_x": win_origin_x,
            "window_origin_y": win_origin_y,
            "crop_origin_screen_x": crop_origin_screen_x,
            "crop_origin_screen_y": crop_origin_screen_y,
            "crop_origin_window_x": crop_origin_window_x,
            "crop_origin_window_y": crop_origin_window_y,
            "screenshot_origin_x": screenshot_origin_x,
            "screenshot_origin_y": screenshot_origin_y,
        }

        # Store window origin in element cache for click back-translation
        cache = get_element_cache()
        cache.set_window_origin(pid, window_id, win_origin_x, win_origin_y)

        # 4. Walk UI tree to build nodes
        nodes: List[Dict[str, Any]] = []
        tree_markdown: str = ""
        uia_quality: str = "empty"
        vision_quality: str = "empty"
        tree_quality: str = "empty"
        total_elements: int = 0
        uia_node_count: int = 0
        vision_node_count: int = 0

        # Per-window UIA gate: skip the walk if another call is already in
        # flight for the same (pid, window_id), or if a recent walk timed
        # out (unhealthy cooldown).  This prevents executor threads from
        # piling up on a hung window — every fresh request would otherwise
        # spawn another walk that blocks for the full 30s timeout.
        gate_key = (int(pid), int(window_id))
        skip_uia_reason: Optional[str] = None
        claimed_gate = False
        now = _time.monotonic()
        with _uia_gate_lock:
            unhealthy_until = _uia_unhealthy_until.get(gate_key, 0.0)
            if unhealthy_until > now:
                skip_uia_reason = (
                    "UIA walk skipped — window is in unhealthy cooldown "
                    "(%.1fs remaining); falling back to vision."
                    % (unhealthy_until - now)
                )
            elif gate_key in _uia_inflight:
                skip_uia_reason = (
                    "UIA walk skipped — another walk is already in flight "
                    "for this window; falling back to vision."
                )
            else:
                _uia_inflight.add(gate_key)
                claimed_gate = True
                # Opportunistic cleanup of stale unhealthy entries
                if unhealthy_until and unhealthy_until <= now:
                    _uia_unhealthy_until.pop(gate_key, None)

        if skip_uia_reason is not None:
            logger.info(skip_uia_reason)
            tree_quality = "empty"
        else:
            uia_future = None
            try:
                from computer_server.handlers.windows_uia import walk_uia_tree

                loop = asyncio.get_running_loop()
                # Keep an explicit reference to the executor future so we can
                # attach a done callback that releases the in-flight gate
                # only when the underlying thread actually returns.  If we
                # released eagerly in `finally`, a wait_for TimeoutError
                # would re-open the slot while the COM call is still running
                # in the leaked thread, allowing the next request to stack
                # another doomed walk on top.
                uia_future = loop.run_in_executor(
                    None, walk_uia_tree, int(window_id), query
                )
                if claimed_gate:
                    try:
                        uia_future.add_done_callback(
                            lambda _f, _k=gate_key: _release_uia_inflight(_k)
                        )
                        # Callback owns the release; don't double-release in finally.
                        claimed_gate = False
                    except Exception:
                        # add_done_callback essentially never raises, but if
                        # it does the future is already scheduled and we have
                        # no way to be notified when it ends.  Pin the gate
                        # closed for this key (claimed_gate=False stops the
                        # `finally` from releasing it) and set the unhealthy
                        # cooldown so callers eventually fall through to SOM
                        # if they keep retrying.
                        claimed_gate = False
                        with _uia_gate_lock:
                            _uia_unhealthy_until[gate_key] = (
                                _time.monotonic() + _UIA_UNHEALTHY_COOLDOWN_S
                            )
                        raise
                uia_result = await asyncio.wait_for(
                    asyncio.shield(uia_future),
                    timeout=30.0,
                )
                for n in uia_result.indexed_nodes:
                    # Convert screen-space coords to window-relative
                    cx = max(0, n.center_x - win_origin_x)
                    cy = max(0, n.center_y - win_origin_y)
                    bbox = n.bbox
                    w_bbox = (
                        max(0, bbox[0] - win_origin_x),
                        max(0, bbox[1] - win_origin_y),
                        max(0, bbox[2] - win_origin_x),
                        max(0, bbox[3] - win_origin_y),
                    )
                    nodes.append({
                        "element_index": n.element_index,
                        "source": "uia",
                        "control_type": n.control_type,
                        "name": n.name,
                        "value": n.value,
                        "center_x": cx,
                        "center_y": cy,
                        "bbox": list(w_bbox),
                        "actions": n.actions,
                        "enabled": n.is_enabled,
                        "depth": n.depth,
                    })
                total_elements = uia_result.total_elements
                uia_node_count = len(uia_result.indexed_nodes)
                tree_markdown = uia_result.tree_markdown
                uia_quality = uia_result.tree_quality
                tree_quality = uia_quality
            except asyncio.TimeoutError:
                # The walk thread is still running and we cannot kill it
                # safely.  Do NOT release the in-flight gate here — the
                # done-callback attached to ``uia_future`` will release it
                # only when the underlying COM call eventually returns (or
                # when the window is closed and the call unblocks).  Until
                # then, every fresh request for the same key must keep
                # skipping UIA so we don't stack more doomed walks on top
                # of the leaked thread.
                #
                # Also set an unhealthy cooldown as a secondary defence
                # (e.g. the in-flight set is the primary one but a future
                # done callback could fail to fire if the executor is being
                # shut down).
                with _uia_gate_lock:
                    _uia_unhealthy_until[gate_key] = (
                        _time.monotonic() + _UIA_UNHEALTHY_COOLDOWN_S
                    )
                logger.warning(
                    "Windows UIA tree walk timed out after 30s — window may be "
                    "hung; keeping (pid=%s, window_id=%s) in-flight until the "
                    "leaked walk thread returns; marking unhealthy for %.0fs as "
                    "a secondary guard",
                    pid, window_id, _UIA_UNHEALTHY_COOLDOWN_S,
                )
                tree_quality = "empty"
            except Exception as e:
                logger.warning("Windows UIA tree walk failed: %s", e)
            finally:
                # Normally the future's done-callback releases the gate.
                # ``claimed_gate`` is only still True if we never managed
                # to schedule the executor task at all (e.g. the import
                # raised before run_in_executor returned), in which case
                # nothing else will ever release it for us.
                if claimed_gate:
                    with _uia_gate_lock:
                        _uia_inflight.discard(gate_key)
        # 5. Populate element cache (window-relative coords already stored)
        cached_elements: List[CachedElement] = []
        for i, node in enumerate(nodes):
            bbox = node.get("bbox") or (0, 0, 0, 0)
            cached = CachedElement(
                element_index=node.get("element_index", i),
                source="uia",
                center_x=node.get("center_x", (bbox[0] + bbox[2]) // 2),
                center_y=node.get("center_y", (bbox[1] + bbox[3]) // 2),
                bbox=bbox if len(bbox) == 4 else (0, 0, 0, 0),
                control_type=node.get("control_type") or node.get("role"),
                name=node.get("name") or node.get("title"),
                value=node.get("value"),
                actions=node.get("actions", []),
                platform_ref=None,
                enabled=node.get("enabled", True),
            )
            cached_elements.append(cached)
        cache.update(pid, window_id, cached_elements)
        cache.set_quality(pid, window_id, tree_quality)

        # 6. Popover detection — find other visible windows belonging to the same pid
        popover_candidates_text: str = ""
        if sys.platform == "win32":
            import win32gui, win32process
            seen = _seen_popovers.setdefault(pid, set())
            candidates: List[Dict[str, Any]] = []
            def _enum_popover(hwnd: int, _lparam: Any) -> bool:
                if hwnd == window_id or not win32gui.IsWindowVisible(hwnd):
                    return True
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid != pid:
                    return True
                r = win32gui.GetWindowRect(hwnd)
                w, h = r[2] - r[0], r[3] - r[1]
                if w <= 1 or h <= 1:
                    return True
                if hwnd in seen:
                    return True
                seen.add(hwnd)
                candidates.append({
                    "window_id": hwnd,
                    "title": win32gui.GetWindowText(hwnd),
                    "x": r[0],
                    "y": r[1],
                    "width": w,
                    "height": h,
                })
                return True
            win32gui.EnumWindows(_enum_popover, None)
            if candidates:
                popover_candidates_text = "## Popover / Overlay Windows\n\nThe following windows belong to the same pid and are on the current screen but are not the requested window. They may be search popovers, context menus, or dialogs — call `get_window_state(pid={}, window_id=<id>)` to inspect:\n\n".format(pid)
                for c in candidates:
                    popover_candidates_text += '- window_id={} "{}" at ({},{}) {}×{}\n'.format(
                        c["window_id"],
                        c["title"] or "(no title)",
                        c["x"], c["y"],
                        c["width"], c["height"],
                    )

        # 7. SOM vision detection — only when UIA/accessibility tree is sparse.
        #    Rich UIA trees (>15 elements) already have all the interactivity
        #    info needed; running SOM would add latency (~20s import + model load)
        #    without benefit.
        if force_som or tree_quality in ("poor", "empty"):
            try:
                from computer_server.handlers.som_bridge import detect_sync

                vis_buf = BytesIO()
                img.save(vis_buf, format="PNG")
                vis_bytes = vis_buf.getvalue()
                # Synchronous only — anyio task group blocks background
                # threads from starting Python code.  warm_up() pre-loads
                # models at startup, so subsequent calls complete in < 3 s.
                vision_result = detect_sync(
                    vis_bytes, screen_width, screen_height,
                    0.3, True, 10000,
                )
                if vision_result is not None and vision_result.elements:
                    # Scale vision coords from resized space to original resolution
                    # so they match AX/UIA element coordinates in the cache
                    z = compression_ratio
                    for el in vision_result.elements:
                        el.center_x = int(el.center_x * z) + crop_origin_window_x
                        el.center_y = int(el.center_y * z) + crop_origin_window_y
                        el.bbox = (
                            int(el.bbox[0] * z) + crop_origin_window_x,
                            int(el.bbox[1] * z) + crop_origin_window_y,
                            int(el.bbox[2] * z) + crop_origin_window_x,
                            int(el.bbox[3] * z) + crop_origin_window_y,
                        )
                    cache.append_vision_elements(pid, window_id, vision_result.elements)
                    vision_node_count = len(vision_result.elements)
                    for el in vision_result.elements:
                        nodes.append({
                            "element_index": el.element_index,
                            "source": "vision",
                            "center_x": el.center_x,
                            "center_y": el.center_y,
                            "bbox": list(el.bbox),
                            "control_type": el.control_type,
                            "name": el.name,
                            "actions": el.actions,
                        })
                    total_elements += len(vision_result.elements)
                    vision_quality = _classify_tree_quality(vision_node_count)
                    cache.set_quality(pid, window_id, uia_quality)
            except Exception as e:
                logger.warning("SOM vision detection skipped: %s", e, exc_info=True)

        # 8. Handle screenshot output — always file to avoid huge inline base64
        import tempfile

        if not screenshot_out_file:
            screenshots_dir = os.path.join(tempfile.gettempdir(), "cua-screenshots")
            os.makedirs(screenshots_dir, exist_ok=True)
            import time as _time
            ts = _time.strftime("%H%M%S")
            screenshot_out_file = os.path.join(
                screenshots_dir,
                f"{pid}_{window_id}_{ts}.jpg",
            )
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(screenshot_out_file, format="JPEG", quality=70, optimize=True)
        screenshot_field: Any = screenshot_out_file

        response_dict: Dict[str, Any] = {
            "success": True,
            "screenshot": screenshot_field,
            "nodes": nodes,
            "node_count": len(nodes),
            "tree_markdown": tree_markdown,
            "tree_quality": tree_quality,
            "uia_quality": uia_quality,
            "vision_quality": vision_quality,
            "merged_node_count": len(nodes),
            "uia_node_count": uia_node_count,
            "vision_node_count": vision_node_count,
            "total_elements": total_elements,
            "pid": pid,
            "window_id": window_id,
            "screen_width": screen_width,
            "screen_height": screen_height,
            "screenshot_origin": {"x": screenshot_origin_x, "y": screenshot_origin_y},
        }
        if popover_candidates_text:
            response_dict["popover_candidates_text"] = popover_candidates_text
        return response_dict

    # ============================================================
    # ZOOM
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_zoom(
        pid: int,
        window_id: int,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
    ) -> Any:
        """
        Zoom into a rectangular region of a window screenshot at full resolution.

        This undoes the downscaling of get_window_state screenshots so small text
        and icons are readable. Coordinates are in the pixel space of the
        screenshot returned by computer_get_window_state (i.e. the resized image).

        After calling computer_zoom, use from_zoom=True on click/drag with the
        same pid and window_id to target coordinates within the zoomed image.

        Args:
            pid: Process ID — must match a prior computer_get_window_state call.
            window_id: Window ID — must match the same window used in the prior
                computer_get_window_state call.
            x1: Left edge of the zoom region (resized-image pixels).
            y1: Top edge of the zoom region (resized-image pixels).
            x2: Right edge of the zoom region (resized-image pixels).
            y2: Bottom edge of the zoom region (resized-image pixels).

        Returns:
            Dictionary with zoomed image (base64 PNG), dimensions, and crop_rect.
        """
        from io import BytesIO

        from PIL import Image as PILImage

        from computer_server.handlers.zoom_registry import ZoomContext, get_zoom_registry

        # Look up the compression ratio from the last get_window_state for this (pid, window_id)
        meta = _zoom_metadata.get((pid, window_id))
        if meta is None:
            return {
                "success": False,
                "error": f"No screenshot metadata for pid {pid} window_id {window_id}. Call computer_get_window_state(pid={pid}, window_id={window_id}) first.",
            }

        z = meta["compression_ratio"]
        orig_w = meta["original_width"]
        orig_h = meta["original_height"]
        wx = meta.get("window_origin_x", 0)
        wy = meta.get("window_origin_y", 0)

        # Scale coordinates from resized-image space to full-resolution window-relative space
        rx1 = int(x1 * z)
        ry1 = int(y1 * z)
        rx2 = int(x2 * z)
        ry2 = int(y2 * z)

        # Clamp to full-resolution window bounds
        rx1 = max(0, min(rx1, orig_w - 1))
        ry1 = max(0, min(ry1, orig_h - 1))
        rx2 = max(rx1 + 1, min(rx2, orig_w))
        ry2 = max(ry1 + 1, min(ry2, orig_h))

        # Add 20% padding in full-resolution space
        crop_w = rx2 - rx1
        crop_h = ry2 - ry1
        pad_x = int(crop_w * 0.2)
        pad_y = int(crop_h * 0.2)

        padded_x1 = max(0, rx1 - pad_x)
        padded_y1 = max(0, ry1 - pad_y)
        padded_x2 = min(orig_w, rx2 + pad_x)
        padded_y2 = min(orig_h, ry2 + pad_y)

        # Translate to screen coordinates for cropping the full-screen screenshot.
        # Use the visible crop origin (where the resized screenshot starts on
        # screen) instead of the real window origin.  The two are equal when the
        # window is fully on-screen, but differ when the window has negative
        # coordinates or extends beyond a screen edge.
        csx = meta.get("crop_origin_screen_x", wx)
        csy = meta.get("crop_origin_screen_y", wy)
        screen_x1 = csx + padded_x1
        screen_y1 = csy + padded_y1
        screen_x2 = csx + padded_x2
        screen_y2 = csy + padded_y2

        # Take a fresh full-resolution screenshot
        automation_handler = _get_handlers().automation
        result = await automation_handler.screenshot(format="png", quality=95)
        img_bytes = base64.b64decode(result["image_data"])
        img = PILImage.open(BytesIO(img_bytes))
        screenshot_origin = result.get("origin") or {}
        screenshot_origin_x = int(screenshot_origin.get("x", 0) or 0)
        screenshot_origin_y = int(screenshot_origin.get("y", 0) or 0)

        # Clamp screen coordinates to actual screenshot dimensions
        image_x1 = screen_x1 - screenshot_origin_x
        image_y1 = screen_y1 - screenshot_origin_y
        image_x2 = screen_x2 - screenshot_origin_x
        image_y2 = screen_y2 - screenshot_origin_y
        image_x1 = max(0, min(image_x1, img.width - 1))
        image_y1 = max(0, min(image_y1, img.height - 1))
        image_x2 = max(image_x1 + 1, min(image_x2, img.width))
        image_y2 = max(image_y1 + 1, min(image_y2, img.height))
        screen_x1 = image_x1 + screenshot_origin_x
        screen_y1 = image_y1 + screenshot_origin_y
        screen_x2 = image_x2 + screenshot_origin_x
        screen_y2 = image_y2 + screenshot_origin_y

        # Crop at native resolution
        cropped = img.crop((image_x1, image_y1, image_x2, image_y2))
        padded_w = screen_x2 - screen_x1
        padded_h = screen_y2 - screen_y1

        # Store zoom context keyed by (pid, window_id) for from_zoom translation
        registry = get_zoom_registry()
        zoom_ctx = ZoomContext(
            window_id=window_id,
            crop_rect=(screen_x1, screen_y1, screen_x2, screen_y2),
            scale_x=cropped.width / padded_w if padded_w > 0 else 1.0,
            scale_y=cropped.height / padded_h if padded_h > 0 else 1.0,
            output_width=cropped.width,
            output_height=cropped.height,
            padding=max(pad_x, pad_y),
        )
        registry.set(pid, window_id, zoom_ctx)

        buffered = BytesIO()
        cropped.save(buffered, format="PNG", optimize=True)
        buffered.seek(0)

        return {
            "success": True,
            "image_data": base64.b64encode(buffered.getvalue()).decode(),
            "format": "png",
            "width": cropped.width,
            "height": cropped.height,
            "crop_rect": [screen_x1, screen_y1, screen_x2, screen_y2],
            "pid": pid,
            "window_id": window_id,
        }

    # ============================================================
    # BROWSER PAGE INTERACTION (CDP)
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_page(
        pid: int,
        window_id: int,
        action: str,
        javascript: Optional[str] = None,
        css_selector: Optional[str] = None,
        attributes: Optional[List[str]] = None,
        allow_html: bool = False,
        port: Optional[int] = None,
        tab_index: int = 0,
    ) -> Any:
        """
        Lightweight browser read/execute via Chrome DevTools Protocol.

        This is NOT a full browser automation tool. Use for quick page content
        extraction or script injection when a browser is already open. For
        multi-step browser workflows (click/form-fill/navigation flows), use a
        dedicated tool like Playwright or browser-use.

        Browser must be launched with --remote-debugging-port (Chrome/Chromium).

        Four action modes:
        - 'get_page_info': get page title, URL, and domain.
        - 'get_text': return document.body.innerText (faster than AX for plain text).
        - 'query_dom': run querySelectorAll and extract matching element attributes.
        - 'execute_javascript': run JS in the active tab and return the result.

        Args:
            pid: Browser process ID. Soft-validated — mismatches are warned but
                non-fatal; CDP connection is attempted regardless.
            window_id: Browser window ID.
            action: 'get_page_info', 'get_text', 'query_dom', or 'execute_javascript'.
            javascript: JS to execute (action='execute_javascript').
            css_selector: CSS selector (action='query_dom').
            attributes: Element attributes to extract (action='query_dom').
            allow_html: Allow query_dom to return innerHTML. Defaults to False.
            port: Chrome remote debugging port. If None, auto-discovered from pid
                command line.
            tab_index: Tab index (default 0).

        Returns:
            Dictionary with action-specific results.
        """
        import sys
        from computer_server.handlers.browser_cdp import get_cdp_client
        from computer_server.handlers.element_cache import get_element_cache

        # --- Soft-validate pid + window_id (best-effort, non-fatal) ---
        try:
            import ctypes
            import win32process

            SYNCHRONIZE = 0x00100000
            hproc = ctypes.windll.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if hproc:
                ctypes.windll.kernel32.CloseHandle(hproc)
            else:
                logger.warning("computer_page: pid=%d not accessible, will try CDP anyway", pid)

            hwnd = int(window_id)
            if ctypes.windll.user32.IsWindow(hwnd):
                _, found_pid = win32process.GetWindowThreadProcessId(hwnd)
                if found_pid and found_pid != pid:
                    logger.warning(
                        "computer_page: window_id=%d belongs to pid=%d not pid=%d, will try CDP anyway",
                        hwnd, found_pid, pid,
                    )
            else:
                logger.warning("computer_page: window_id=%d is not a valid window, will try CDP anyway", hwnd)
        except Exception:
            logger.debug("pid/window_id validation skipped", exc_info=True)

        # --- Auto-discover CDP port from pid ---
        resolved_port = port
        if resolved_port is None:
            try:
                import win32process
                import win32api
                hproc = win32api.OpenProcess(0x0400 | 0x0010, False, pid)  # PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
                cmdline = ""
                try:
                    cmdline = win32process.GetProcessCommandLine(hproc) or ""
                except Exception:
                    pass
                win32api.CloseHandle(hproc)
            except Exception:
                cmdline = ""

            if cmdline:
                import re
                m = re.search(r'--remote-debugging-port[= ](\d+)', cmdline)
                if m:
                    resolved_port = int(m.group(1))
                    logger.debug("Auto-discovered CDP port %d from pid %d cmdline", resolved_port, pid)

            if resolved_port is None:
                return {
                    "success": False,
                    "error": (
                        "Chrome DevTools Protocol port was not found for this "
                        "process. Start the browser with --remote-debugging-port "
                        "or pass the port argument explicitly."
                    ),
                    "pid": pid,
                    "window_id": window_id,
                }

        assert resolved_port is not None  # guaranteed by fallback above
        cdp = get_cdp_client()
        result: Dict[str, Any]

        if action == "get_text":
            text = await cdp.get_page_text(port=resolved_port, tab_index=tab_index)
            result = {"success": True, "text": text}
        elif action == "query_dom":
            if not css_selector:
                return {"success": False, "error": "css_selector is required for query_dom"}
            elements = await cdp.query_dom(
                port=resolved_port, selector=css_selector,
                attributes=attributes, tab_index=tab_index,
                allow_html=allow_html,
            )
            result = {"success": True, "elements": elements}
        elif action == "get_page_info":
            info = await cdp.get_page_info(port=resolved_port, tab_index=tab_index)
            result = {"success": True, **info}
        elif action == "execute_javascript":
            if not javascript:
                return {"success": False, "error": "javascript is required for execute_javascript"}
            js_result = await cdp.evaluate(port=resolved_port, expression=javascript, tab_index=tab_index)
            if js_result.get("error"):
                return {"success": False, "error": js_result["error"]}
            result = {"success": True, "result": js_result.get("result")}
        else:
            return {"success": False, "error": f"Unknown action: {action}. Use 'execute_javascript', 'get_text', 'query_dom', or 'get_page_info'."}

        await _recording_hook("computer_page", {
            "pid": pid, "window_id": window_id, "action": action,
            "port": resolved_port, "tab_index": tab_index,
        }, result)
        return result

    # ============================================================
    # RECORDING TOOLS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_set_recording(
        enabled: bool,
        output_dir: Optional[str] = None,
        capture_screenshots: bool = True,
        screenshot_every_n: int = 1,
    ) -> Any:
        """
        Start or stop recording tool invocations to turn folders.

        When enabled, every action tool call (click, type, scroll, etc.)
        writes a turn-NNNNN/ folder to output_dir containing action.json,
        screenshot.png, and click.png (for click actions).

        Args:
            enabled: True to start recording, False to stop.
            output_dir: Directory for turn folders (required when enabled=True).
            capture_screenshots: Include screenshots in turn folders.
            screenshot_every_n: Capture one screenshot every N turns.

        Returns:
            Dictionary with recording state summary.
        """
        from computer_server.recording.session import RecordingSession

        if enabled:
            if not output_dir:
                return {"success": False, "error": "output_dir is required when enabled=True"}
            import os
            output_dir = os.path.expanduser(output_dir)
            session = RecordingSession(
                output_dir=output_dir,
                capture_screenshots=capture_screenshots,
                screenshot_every_n=screenshot_every_n,
            )
            session.start()
            context.recording_session = session
            return {"success": True, **session.state}
        else:
            session = context.recording_session
            if session is not None:
                summary = session.stop()
                context.recording_session = None
                return {"success": True, **summary}
            return {"success": True, "message": "Recording was not active"}

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_recording_state() -> Any:
        """
        Get the current recording state.

        Returns:
            Dictionary with active flag, turn count, output directory, and duration.
        """
        session = context.recording_session
        if session is None:
            return {"active": False, "turn_count": 0}
        return session.state

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_replay_trajectory(
        recording_dir: str,
        delay_ms: int = 500,
        stop_on_error: bool = True,
    ) -> Any:
        """
        Replay a recorded trajectory by re-invoking each turn's tool call.

        Args:
            recording_dir: Path to recording directory (containing turn-NNNNN folders).
            delay_ms: Delay between actions in milliseconds (default 500).
            stop_on_error: Stop replay on first failed action (default True).

        Returns:
            {turns_replayed, turns_failed, turns_total, errors}
        """
        import os

        from computer_server.recording.replay import replay_trajectory as _replay

        recording_dir = os.path.expanduser(recording_dir)

        tool_map = await _get_tool_map(mcp)

        # Metadata keys used for replay resolution, not tool parameters
        _REPLAY_META_KEYS = frozenset({"window_title", "process_name"})
        _REPLAY_COORD_TOOLS = frozenset({"computer_click", "computer_drag", "computer_scroll"})
        _REPLAY_WINDOW_TOOLS = frozenset({"computer_activate_window"})

        async def tool_executor(tool_name: str, args: Dict[str, Any],
                                window_ref: Optional[Dict[str, Any]] = None) -> Any:
            if tool_name not in tool_map:
                return {"success": False, "error": f"Unknown tool: {tool_name}"}

            # --- Replay-layer coordinate / window resolution ---
            # Mutate a copy so the original recording stays untouched.
            resolved = dict(args)

            # --- Helper: resolve current HWND from a source dict ---
            def _resolve_window_hwnd(source: Dict[str, Any]) -> Optional[Tuple[int, tuple]]:
                """Resolve a live HWND and its rect from a window_ref or args dict.

                Priority: 1) original window_id if still valid,
                          2) process_name search,
                          3) window_title search.
                Returns (hwnd, (left, top, right, bottom)) or None.
                """
                ref_wid = source.get("window_id")
                # Priority 1: original HWND still valid
                if ref_wid is not None:
                    try:
                        import win32gui
                        if win32gui.IsWindow(int(ref_wid)):
                            rect = win32gui.GetWindowRect(int(ref_wid))
                            if rect:
                                return (int(ref_wid), rect)
                    except Exception:
                        pass
                # Priority 2: search by process_name
                proc_name = source.get("process_name", "")
                if proc_name:
                    try:
                        import win32gui, win32process, psutil
                        candidates: list = []
                        def _by_proc(hwnd, ctx):
                            try:
                                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                                if pid and psutil.Process(pid).name().lower() == proc_name.lower() and win32gui.IsWindowVisible(hwnd):
                                    ctx.append(hwnd)
                            except Exception:
                                pass
                        win32gui.EnumWindows(_by_proc, candidates)
                        if candidates:
                            hwnd = candidates[0]
                            rect = win32gui.GetWindowRect(hwnd)
                            if rect:
                                return (hwnd, rect)
                    except Exception:
                        pass
                # Priority 3: search by window_title
                title = source.get("window_title", "")
                if title:
                    try:
                        import win32gui
                        candidates = []
                        def _by_title(hwnd, ctx):
                            try:
                                wt = win32gui.GetWindowText(hwnd) or ""
                                if title.lower() in wt.lower() and win32gui.IsWindowVisible(hwnd):
                                    ctx.append(hwnd)
                            except Exception:
                                pass
                        win32gui.EnumWindows(_by_title, candidates)
                        if candidates:
                            hwnd = candidates[0]
                            rect = win32gui.GetWindowRect(hwnd)
                            if rect:
                                return (hwnd, rect)
                    except Exception:
                        pass
                return None

            # --- Coordinate tools: resolve window, adjust coordinates ---
            if window_ref and tool_name in _REPLAY_COORD_TOOLS:
                resolved_hwnd = _resolve_window_hwnd(window_ref)
                if resolved_hwnd is not None:
                    hwnd, rect = resolved_hwnd
                    ref_ox = window_ref.get("origin_x", 0)
                    ref_oy = window_ref.get("origin_y", 0)
                    if "x" in resolved:
                        resolved["x"] = int(rect[0] + (resolved["x"] - ref_ox))
                        resolved["y"] = int(rect[1] + (resolved["y"] - ref_oy))
                    if "start_x" in resolved:
                        resolved["start_x"] = int(rect[0] + (resolved["start_x"] - ref_ox))
                        resolved["start_y"] = int(rect[1] + (resolved["start_y"] - ref_oy))
                        resolved["end_x"] = int(rect[0] + (resolved["end_x"] - ref_ox))
                        resolved["end_y"] = int(rect[1] + (resolved["end_y"] - ref_oy))
                    # Sync window_id if HWND changed
                    ref_wid = window_ref.get("window_id")
                    if ref_wid is not None and hwnd != int(ref_wid):
                        resolved["window_id"] = str(hwnd)
                        logger.info(
                            "Replay resolved stale window_id %s → %s for %s",
                            ref_wid, hwnd, tool_name,
                        )
                else:
                    logger.warning(
                        "Replay: could not resolve window for %s (window_ref=%s)",
                        tool_name, window_ref,
                    )

            # --- Window tools: resolve stale window_id ---
            wid = resolved.get("window_id")
            if wid is not None and tool_name in _REPLAY_WINDOW_TOOLS:
                resolved_hwnd = _resolve_window_hwnd(resolved)
                if resolved_hwnd is not None:
                    hwnd, _rect = resolved_hwnd
                    if hwnd != int(wid):
                        resolved["window_id"] = str(hwnd)
                        logger.info(
                            "Replay resolved stale window_id %s → %s", wid, hwnd,
                        )
                else:
                    logger.warning(
                        "Replay: could not resolve window for %s (args=%s)",
                        tool_name,
                        {k: resolved.get(k) for k in ("window_id", "process_name", "window_title")},
                    )

            # Strip replay-only metadata keys before calling the tool
            for k in _REPLAY_META_KEYS:
                resolved.pop(k, None)

            try:
                return await tool_map[tool_name](**resolved)
            except Exception as e:
                return {"success": False, "error": str(e)}

        return await _replay(
            recording_dir=recording_dir,
            tool_executor=tool_executor,
            delay_ms=delay_ms,
            stop_on_error=stop_on_error,
        )

    # ============================================================
    # LEARNING ACTIONS — human user recording with UIA enrichment
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_learn_start(
        task_description: Optional[str] = None,
        output_dir: Optional[str] = None,
    ) -> Any:
        """
        Start observing the user's actions for workflow learning.

        Records keyboard typing, mouse clicks, and scroll events enriched
        with UI Automation (UIA) element metadata at interaction points.
        Password/sensitive fields are automatically redacted.

        Results are saved to output_dir/session.json and screenshot.png
        when computer_learn_stop is called.

        Windows only — requires pynput + uiautomation + pywin32 + psutil.
        Auto-stops after 10 minutes (safety limit).

        Args:
            task_description: Brief description of what the user is about to do.
            output_dir: Directory for saved results (default: %TEMP%/cua-learn-<timestamp>).
        """
        try:
            from computer_server.learning.recorder import LearningRecorder
            from computer_server.learning.session import LearningError
        except ImportError as e:
            return {
                "success": False,
                "error": f"Learning module unavailable: {e}",
                "suggestion": "Install Windows dependencies: pip install pynput uiautomation pywin32 psutil",
            }

        recorder = context.learning_recorder
        if recorder is None:
            recorder = LearningRecorder()
            context.learning_recorder = recorder

        import os, time as _time
        if not output_dir:
            output_dir = os.path.join(
                os.environ.get("TEMP", os.path.expanduser("~")),
                f"cua-learn-{_time.strftime('%Y%m%d-%H%M%S')}",
            )
        else:
            output_dir = os.path.expanduser(output_dir)

        error = recorder.start(task_description=task_description, output_dir=output_dir)
        if error is not None:
            return {
                "success": False,
                "error": error.value,
                "suggestion": error.suggestion(),
            }
        return {
            "success": True,
            "data": {
                "status": "recording",
                "output_dir": output_dir,
                "message": "Recording started. The user can now perform the task. "
                           "Call computer_learn_stop when done.",
            },
        }

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_learn_stop() -> Any:
        """
        Stop observing and return the recorded action sequence.

        Each action includes UIA-enriched element context (role, title,
        identifier, parent_role). Password fields are auto-redacted as
        SECURE_FIELD entries.

        The returned actions can be fed to an AI agent to synthesize a
        parameterized, replayable workflow (recipe).

        Returns:
            Dictionary with task_description, duration_seconds, action_count,
            apps, urls, and the full actions list.
        """
        recorder = context.learning_recorder
        if recorder is None:
            return {
                "success": False,
                "error": "No learning session is active",
                "suggestion": "Call computer_learn_start first to begin recording.",
            }

        success, result = recorder.stop()

        if not success:
            from computer_server.learning.session import LearningError
            err: LearningError = result
            return {
                "success": False,
                "error": err.value,
                "suggestion": err.suggestion(),
            }

        import time
        session, actions = result
        duration = time.time() - session.start_time
        serialized = [a.to_dict() for a in actions]

        return {
            "success": True,
            "data": {
                "task_description": session.task_description or "",
                "duration_seconds": int(duration),
                "action_count": len(actions),
                "apps": list(session.apps),
                "urls": list(session.urls),
                "actions": serialized,
            },
        }

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_learn_status() -> Any:
        """
        Check if learning mode is active and get recording stats.

        Returns recording status, action count, duration, current
        foreground app, and listener health.
        """
        recorder = context.learning_recorder
        if recorder is None:
            return {
                "success": True,
                "data": {"recording": False, "action_count": 0, "duration_seconds": 0},
            }

        is_rec, count, duration, app, healthy = recorder.status()
        data: Dict[str, Any] = {
            "recording": is_rec,
            "action_count": count,
            "duration_seconds": int(duration),
        }
        if app:
            data["current_app"] = app
        if healthy is not None:
            data["listener_healthy"] = healthy
        return {"success": True, "data": data}

    # Pre-warm SOM OmniParser in background so first get_window_state is fast.
    # Skip when CUA_SKIP_SOM_WARMUP=1 (e.g. one-shot test invocations where
    # the heavy ML imports would contend with the asyncio event loop for the
    # GIL and import lock, causing tool-call timeouts).
    if not os.environ.get("CUA_SKIP_SOM_WARMUP"):
        try:
            from computer_server.handlers.som_bridge import warm_up as _warm_som

            _warm_som()
        except Exception as _warm_exc:
            logger.debug("SOM warm-up skipped: %s", _warm_exc)

    return mcp


def run_mcp_server(
    target_width: Optional[int] = None,
    target_height: Optional[int] = None,
    detect_resolution: bool = False,
) -> None:
    """Run the MCP server.

    Args:
        target_width: Target width for screenshots (coordinates will be scaled accordingly)
        target_height: Target height for screenshots (coordinates will be scaled accordingly)
        detect_resolution: If True, auto-detect and log the actual screen resolution
    """
    logger.info("Starting CUA Computer MCP server...")

    # Init agent cursor overlay (fires a daemon thread; degrades silently)
    if _overlay_init is not None:
        _overlay_init()

    _configure_scaling(
        target_width=target_width,
        target_height=target_height,
        detect_resolution=detect_resolution,
    )

    mcp = create_mcp_server()
    mcp.run()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        stream=sys.stderr,
    )
    run_mcp_server()
