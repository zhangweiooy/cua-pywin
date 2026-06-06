"""
MCP (Model Context Protocol) server interface for Computer Server.

This module provides an MCP interface to the Computer Server's functionality,
enabling Claude Code and other MCP-compatible tools to directly control computers.

Usage:
    python -m computer_server.mcp_server
"""

import base64
import logging
import os
import sys

_LIGHTWEIGHT_MCP_IMPORT = os.environ.get("CUA_LIGHTWEIGHT_MCP_IMPORT") == "1"

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
if not _LIGHTWEIGHT_MCP_IMPORT:
    import numpy as np  # noqa: F401

    try:
        from computer_server.handlers.som_bridge import is_available as _pre_som_avail
        _pre_som_avail()  # triggers from som.detect import OmniParser → torch + cv2 + ultralytics
    except Exception:
        pass
    try:
        # Transformers/tokenizers/safetensors can import native extensions.
        # Import them before FastMCP's anyio event loop starts; model weights are
        # still loaded by the background warm-up thread.
        from transformers import CLIPModel as _pre_clip_model  # noqa: F401
        from transformers import CLIPProcessor as _pre_clip_processor  # noqa: F401
        import safetensors.torch as _pre_safetensors_torch  # noqa: F401
    except Exception:
        pass
    try:
        # PyWinRT loads native extensions. Import before the anyio loop so WGC can
        # be used safely from get_window_state without first-time DLL loading there.
        from computer_server.handlers.windows_wgc import is_available as _pre_wgc_avail

        _pre_wgc_avail()
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


def _uia_action_timeout_s() -> float:
    """Return the per-action UIA COM timeout."""
    try:
        return max(0.1, float(os.getenv("CUA_UIA_ACTION_TIMEOUT", "2.0")))
    except Exception:
        return 2.0


async def _animate_cursor_to(x: int, y: int) -> None:
    """Fly cursor overlay to (x,y), blocking until spring starts.

    Lazy-initializes the overlay if server-startup init failed.
    """
    import computer_server.overlay as overlay

    if overlay.is_animation_suppressed():
        overlay.set_position_hidden(x, y)
        return

    if overlay._agent_cursor is None:
        try:
            overlay.init()
        except Exception:
            return
    if overlay._agent_cursor is None:
        return

    heading = overlay._compute_heading_and_update(x, y)
    overlay._agent_cursor.move_to(x, y, heading)


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

def _get_resize_ratio(pid: Optional[int], window_id: Optional[int]) -> float:
    """Return the stored compression ratio for this (pid, window_id), or 1.0 if not found."""
    from computer_server.services.coordinate_service import get_resize_ratio

    return get_resize_ratio(_zoom_metadata, pid, window_id)


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
    x: int,
    y: int,
    from_zoom: bool = False,
    pid: Optional[int] = None,
    window_id: Optional[int] = None,
    zoom_id: Optional[str] = None,
) -> Tuple[int, int]:
    """If from_zoom is True, translate zoomed-image coordinates back to screen space."""
    from computer_server.services.coordinate_service import resolve_from_zoom

    return resolve_from_zoom(
        x,
        y,
        from_zoom=from_zoom,
        pid=pid,
        window_id=window_id,
        zoom_id=zoom_id,
    )


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
    from computer_server.services.coordinate_service import resolve_element_center

    return resolve_element_center(pid, window_id, element_index)


def _window_resized_to_screen(
    pid: Optional[int],
    window_id: Optional[int],
    x: int,
    y: int,
    capture_geometry: Optional[Dict[str, Any]] = None,
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
    from computer_server.services.coordinate_service import window_resized_to_screen

    return window_resized_to_screen(
        pid,
        window_id,
        x,
        y,
        zoom_metadata=_zoom_metadata,
        capture_geometry=capture_geometry,
    )


def _window_full_res_to_screen(
    pid: Optional[int],
    window_id: Optional[int],
    x: int,
    y: int,
    capture_geometry: Optional[Dict[str, Any]] = None,
) -> Tuple[int, int]:
    from computer_server.services.coordinate_service import window_full_res_to_screen

    return window_full_res_to_screen(pid, window_id, x, y, capture_geometry=capture_geometry)


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
    """Return a replay-friendly window reference for an action point."""
    from computer_server.services.window_identity_service import build_window_ref

    return build_window_ref(pid, window_id, x, y)


def _get_window_ref(x: int, y: int, window_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """Resolve the top-level window at a screen point."""
    from computer_server.services.window_identity_service import get_window_ref_from_point

    return get_window_ref_from_point(x, y)


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
        type text, press keys, scroll, manage windows, inspect UI state, and control browser pages.

        Always take a screenshot first to see the current state before performing actions.
        After performing actions, take another screenshot to verify the result.""",
    )
    from computer_server.server_context import ServerContext

    context = ServerContext()

    async def _run_uia_action_with_timeout(
        pid: int,
        window_id: int,
        action_name: str,
        func: Any,
    ) -> Tuple[bool, Optional[str]]:
        """Run a UIA action behind the per-window action gate."""
        from computer_server.services.window_state_service import run_window_operation_with_gate

        def _wrapped() -> bool:
            func()
            return True

        result, error = await run_window_operation_with_gate(
            pid=pid,
            window_id=window_id,
            gate=context.uia_action_gate,
            func=_wrapped,
            timeout_s=_uia_action_timeout_s(),
        )
        if error is not None:
            logger.info(
                "UIA %s skipped/failed for pid=%s window_id=%s: %s",
                action_name,
                pid,
                window_id,
                error,
            )
        return bool(result), error

    def _resolve_snapshot_ref(
        snapshot_id: Optional[str],
        pid: Optional[int],
        window_id: Optional[int],
    ) -> Tuple[Optional[int], Optional[int], Optional[Dict[str, Any]]]:
        """Resolve an optional snapshot handle to pid/window_id."""
        if not snapshot_id:
            return pid, window_id, None

        snapshot = context.observations.get_snapshot(snapshot_id)
        if snapshot is None:
            return pid, window_id, {
                "success": False,
                "error": f"Unknown or expired snapshot_id: {snapshot_id}",
                "suggestion": "Call computer_get_window_state again and use the returned snapshot_id.",
            }
        if not context.observations.is_latest(snapshot_id):
            return pid, window_id, {
                "success": False,
                "error": f"snapshot_id is stale for pid={snapshot.pid}, window_id={snapshot.window_id}",
                "suggestion": "Use the latest snapshot_id returned by computer_get_window_state or capture_after.",
            }
        if pid is not None and int(pid) != snapshot.pid:
            return pid, window_id, {
                "success": False,
                "error": f"snapshot_id pid mismatch: snapshot has pid={snapshot.pid}, got pid={pid}",
            }
        if window_id is not None and int(window_id) != snapshot.window_id:
            return pid, window_id, {
                "success": False,
                "error": (
                    "snapshot_id window_id mismatch: "
                    f"snapshot has window_id={snapshot.window_id}, got window_id={window_id}"
                ),
            }
        return snapshot.pid, snapshot.window_id, None

    def _resolve_screenshot_ref(
        screenshot_id: Optional[str],
        snapshot_id: Optional[str],
        pid: Optional[int],
        window_id: Optional[int],
    ) -> Tuple[Optional[int], Optional[int], Optional[Any], Optional[Dict[str, Any]]]:
        """Resolve an optional screenshot handle to pid/window_id and capture geometry."""
        if not screenshot_id:
            return pid, window_id, None, None
        if not isinstance(screenshot_id, str) or not screenshot_id.strip():
            return pid, window_id, None, {
                "success": False,
                "error": "screenshot_id must be a non-empty string",
            }

        screenshot = context.observations.get_screenshot(screenshot_id)
        if screenshot is None:
            return pid, window_id, None, {
                "success": False,
                "error": f"Unknown or expired screenshot_id: {screenshot_id}",
                "suggestion": "Call computer_get_window_state again and use the returned screenshot_id.",
            }
        if not context.observations.is_latest_screenshot(screenshot_id):
            return pid, window_id, None, {
                "success": False,
                "error": (
                    "screenshot_id is stale for "
                    f"pid={screenshot.pid}, window_id={screenshot.window_id}"
                ),
                "suggestion": "Use the latest screenshot_id returned by computer_get_window_state or capture_after.",
            }
        if snapshot_id is not None and snapshot_id != screenshot.snapshot_id:
            return pid, window_id, None, {
                "success": False,
                "error": (
                    "screenshot_id snapshot mismatch: "
                    f"screenshot has snapshot_id={screenshot.snapshot_id}, got snapshot_id={snapshot_id}"
                ),
            }
        if pid is not None and int(pid) != screenshot.pid:
            return pid, window_id, None, {
                "success": False,
                "error": f"screenshot_id pid mismatch: screenshot has pid={screenshot.pid}, got pid={pid}",
            }
        if window_id is not None and int(window_id) != screenshot.window_id:
            return pid, window_id, None, {
                "success": False,
                "error": (
                    "screenshot_id window_id mismatch: "
                    f"screenshot has window_id={screenshot.window_id}, got window_id={window_id}"
                ),
            }
        return screenshot.pid, screenshot.window_id, screenshot, None

    def _public_capture_from_screenshot(screenshot: Any) -> Dict[str, Any]:
        return screenshot.to_public_capture()

    def _resolve_zoom_ref(
        zoom_id: Optional[str],
        pid: Optional[int],
        window_id: Optional[int],
    ) -> Tuple[Optional[int], Optional[int], Optional[Dict[str, Any]]]:
        """Resolve an optional zoom handle to pid/window_id."""
        if not zoom_id:
            return pid, window_id, None

        from computer_server.handlers.zoom_registry import get_zoom_registry

        zoom_ctx = get_zoom_registry().get_by_id(zoom_id)
        if zoom_ctx is None:
            return pid, window_id, {
                "success": False,
                "error": f"Unknown or expired zoom_id: {zoom_id}",
                "suggestion": "Call computer_zoom again and use the returned zoom_id.",
            }
        if pid is not None and int(pid) != zoom_ctx.pid:
            return pid, window_id, {
                "success": False,
                "error": f"zoom_id pid mismatch: zoom has pid={zoom_ctx.pid}, got pid={pid}",
            }
        if window_id is not None and int(window_id) != zoom_ctx.window_id:
            return pid, window_id, {
                "success": False,
                "error": (
                    "zoom_id window_id mismatch: "
                    f"zoom has window_id={zoom_ctx.window_id}, got window_id={window_id}"
                ),
            }
        return zoom_ctx.pid, zoom_ctx.window_id, None

    def _resolve_window_arg(
        window: Optional[Dict[str, Any]],
        pid: Optional[int],
        window_id: Optional[int],
    ) -> Tuple[Optional[int], Optional[int], Optional[Dict[str, Any]]]:
        """Resolve the public window object plus top-level pid/window_id args."""
        from computer_server.services.validation_service import error_result
        from computer_server.services.window_identity_service import resolve_window_input

        try:
            resolved = resolve_window_input(
                window=window,
                pid=pid,
                window_id=window_id,
            )
            return resolved.pid, resolved.window_id, None
        except ValueError as e:
            return pid, window_id, error_result(e)

    def _add_window_field(record: Dict[str, Any]) -> Dict[str, Any]:
        from computer_server.services.window_identity_service import add_window_field

        return add_window_field(record)

    def _build_public_window(
        pid: Optional[int],
        window_id: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        from computer_server.services.window_identity_service import build_window

        return build_window(window_id, pid=pid)

    async def _activate_input_window(
        window: Optional[Dict[str, Any]],
        pid: Optional[int],
        window_id: Optional[int],
        *,
        activate: bool = True,
    ) -> Tuple[Optional[int], Optional[int], Optional[Dict[str, Any]]]:
        """Resolve an optional target window and activate it before input."""
        resolved_pid, resolved_window_id, ref_error = _resolve_window_arg(
            window,
            pid,
            window_id,
        )
        if ref_error is not None:
            return resolved_pid, resolved_window_id, ref_error
        if window is not None and resolved_window_id is None:
            return resolved_pid, resolved_window_id, {
                "success": False,
                "error": "window.id or window.window_id is required for targeted input",
            }
        if resolved_window_id is None or not activate:
            return resolved_pid, resolved_window_id, None

        activation = await _get_handlers().window.activate_window(resolved_window_id)
        if activation.get("success") is False:
            return resolved_pid, resolved_window_id, {
                "success": False,
                "error": (
                    f"Failed to activate target window {resolved_window_id}: "
                    f"{activation.get('error', 'activation failed')}"
                ),
                "window_id": resolved_window_id,
                "activation": activation,
            }
        return resolved_pid, resolved_window_id, None

    def _attach_input_window(
        result: Dict[str, Any],
        pid: Optional[int],
        window_id: Optional[int],
        *,
        activated: bool = True,
    ) -> None:
        if window_id is None:
            return
        result["window_id"] = window_id
        if pid is not None:
            result["pid"] = pid
        public_window = _build_public_window(pid, window_id)
        if public_window is not None:
            result["window"] = public_window
        result["target_window_activated"] = activated

    def _attach_explicit_window_activation(
        result: Dict[str, Any],
        pid: Optional[int],
        window_id: Optional[int],
        *,
        activated: bool,
    ) -> None:
        """Expose explicit-window activation without changing legacy calls."""
        if not activated:
            return
        _attach_input_window(result, pid, window_id, activated=True)
        result["used_foreground"] = True
        action_path = result.get("action_path")
        if isinstance(action_path, dict):
            action_path["used_foreground"] = True

    def _build_element_ref(
        pid: Optional[int],
        window_id: Optional[int],
        element_index: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """Build replay metadata for a cached element."""
        if pid is None or window_id is None or element_index is None:
            return None
        try:
            from computer_server.handlers.element_cache import get_element_cache

            cache = get_element_cache()
            el = cache.get_element(int(pid), int(window_id), int(element_index))
            if el is None:
                return None
            origin = cache.get_window_origin_optional(int(pid), int(window_id))
            sensitive_label = f"{el.name or ''} {el.control_type or ''}".lower()
            redact_value = any(
                token in sensitive_label
                for token in ("password", "passcode", "pin", "密码", "口令")
            )
            ref: Dict[str, Any] = {
                "pid": int(pid),
                "window_id": int(window_id),
                "recorded_element_index": int(element_index),
                "source": el.source,
                "control_type": el.control_type,
                "name": el.name,
                "value": None if redact_value else el.value,
                "actions": list(el.actions or []),
                "enabled": el.enabled,
                "center_x": el.center_x,
                "center_y": el.center_y,
                "bbox": list(el.bbox),
            }
            if origin is not None:
                ref["origin_x"] = origin[0]
                ref["origin_y"] = origin[1]
                ref["screen_x"] = origin[0] + el.center_x
                ref["screen_y"] = origin[1] + el.center_y
            return ref
        except Exception:
            logger.debug("failed to build element_ref", exc_info=True)
            return None

    async def _release_tracked_inputs() -> Dict[str, Any]:
        """Release keys/buttons tracked by this server instance."""
        automation_handler = _get_handlers().automation
        released_keys: List[str] = []
        released_buttons: List[str] = []
        errors: List[str] = []

        for key in list(context.held_keys):
            try:
                result = await automation_handler.key_up(key)
                if result.get("success") is False:
                    errors.append(f"key {key}: {result.get('error', 'unknown error')}")
                else:
                    released_keys.append(key)
            except Exception as exc:
                errors.append(f"key {key}: {exc}")
            finally:
                context.held_keys.discard(key)

        for button in list(context.held_mouse_buttons):
            try:
                result = await automation_handler.mouse_up(button=button)
                if result.get("success") is False:
                    errors.append(f"mouse {button}: {result.get('error', 'unknown error')}")
                else:
                    released_buttons.append(button)
            except Exception as exc:
                errors.append(f"mouse {button}: {exc}")
            finally:
                context.held_mouse_buttons.discard(button)

        return {
            "success": not errors,
            "released_keys": released_keys,
            "released_mouse_buttons": released_buttons,
            "errors": errors,
        }

    def _suppress_learning_input(duration_seconds: float = 1.0) -> None:
        recorder = context.learning_recorder
        if recorder is None:
            return
        try:
            if getattr(recorder, "is_recording", False):
                recorder.suppress_agent_input(duration_seconds)
        except Exception:
            logger.debug("learning input suppression failed", exc_info=True)

    def _begin_learning_input_suppression() -> bool:
        recorder = context.learning_recorder
        if recorder is None:
            return False
        try:
            if getattr(recorder, "is_recording", False):
                recorder.begin_agent_input_suppression()
                return True
        except Exception:
            logger.debug("learning input suppression begin failed", exc_info=True)
        return False

    def _end_learning_input_suppression(active: bool, grace_seconds: float = 0.25) -> None:
        if not active:
            return
        recorder = context.learning_recorder
        if recorder is None:
            return
        try:
            recorder.end_agent_input_suppression(grace_seconds)
        except Exception:
            logger.debug("learning input suppression end failed", exc_info=True)

    def _clear_learning_input_suppression() -> None:
        recorder = context.learning_recorder
        if recorder is None:
            return
        try:
            recorder.clear_agent_input_suppression()
        except Exception:
            logger.debug("learning input suppression clear failed", exc_info=True)

    def _recorded_turn(
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any],
        click_point: Optional[Tuple[int, int]] = None,
        window_ref: Optional[Dict[str, Any]] = None,
        element_ref: Optional[Dict[str, Any]] = None,
    ) -> Any:
        from computer_server.recording.session import RecordedTurnContext

        return RecordedTurnContext(
            tool_name=tool_name,
            args=args,
            result=result,
            click_point=click_point,
            window_ref=window_ref,
            element_ref=element_ref,
        )

    async def _recording_hook(turn: Any) -> None:
        """Record a tool invocation for this MCP server instance."""
        try:
            from computer_server.recording.session import RECORDABLE_TOOLS

            if turn.tool_name not in RECORDABLE_TOOLS:
                return
            if context.recording_suppressed:
                return
            session = context.recording_session
            if session is None or not session.active:
                return

            screenshot_bytes = None
            if session.should_capture_screenshot():
                try:
                    from computer_server.services.capture_service import capture_png_from_automation

                    automation_handler = _get_handlers().automation
                    screenshot_bytes = await capture_png_from_automation(automation_handler)
                except Exception:
                    session.note_screenshot_error()

            await session.record_turn(
                tool_name=turn.tool_name,
                args=turn.args,
                result=turn.result,
                screenshot_bytes=screenshot_bytes,
                click_point=turn.click_point,
                window_ref=turn.window_ref,
                element_ref=turn.element_ref,
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
        query: Optional[str] = None,
        include_all_nodes: bool = False,
    ) -> Any:
        """Thin wrapper around computer_get_window_state for capture_after use.

        Returns a dict with screenshot, nodes, node_count, tree_quality keys,
        or an error key on failure. Never raises — failures are captured so
        the parent action result is not lost.
        """
        try:
            fresh = await computer_get_window_state(
                pid=pid,
                window_id=window_id,
                max_image_dimension=max_image_dimension,
                query=query,
                include_all_nodes=include_all_nodes,
            )
            if fresh.get("success") is False:
                return {"success": False, "error": fresh.get("error", "get_window_state returned success=False")}
            return {
                "screenshot": fresh.get("screenshot"),
                "screenshots": fresh.get("screenshots", []),
                "screenshot_id": fresh.get("screenshot_id"),
                "capture": fresh.get("capture"),
                "capture_metadata": fresh.get("capture_metadata"),
                "nodes": fresh.get("nodes", []),
                "node_count": fresh.get("node_count", 0),
                "returned_node_count": fresh.get("returned_node_count", fresh.get("node_count", 0)),
                "unfiltered_node_count": fresh.get("unfiltered_node_count"),
                "nodes_filtered": fresh.get("nodes_filtered", False),
                "tree_markdown": fresh.get("tree_markdown", ""),
                "tree_quality": fresh.get("tree_quality", "empty"),
                "uia_quality": fresh.get("uia_quality"),
                "msaa_quality": fresh.get("msaa_quality"),
                "vision_quality": fresh.get("vision_quality"),
                "node_sources": fresh.get("node_sources", []),
                "snapshot_id": fresh.get("snapshot_id"),
                "snapshot_latest": fresh.get("snapshot_latest", True),
                "query": query,
                "success": True,
            }
        except Exception as _cap_err:
            logger.warning("capture_after failed: %s", _cap_err)
            return {"success": False, "error": str(_cap_err)}

    def _annotate_action_path(
        result: Dict[str, Any],
        *,
        method: str,
        used_foreground: bool,
        uses_system_input: bool,
        requires_visible_target: bool,
        coordinate_source: Optional[str] = None,
        input_screenshot_id: Optional[str] = None,
        capture_backend: Optional[str] = None,
        capture_coordinate_space: Optional[str] = None,
    ) -> None:
        """Attach transparent action-path metadata to a tool result."""
        result.setdefault("method", method)
        result["used_foreground"] = used_foreground
        result["uses_system_input"] = uses_system_input
        result["requires_visible_target"] = requires_visible_target
        result["action_path"] = {
            "method": method,
            "used_foreground": used_foreground,
            "uses_system_input": uses_system_input,
            "requires_visible_target": requires_visible_target,
        }
        if coordinate_source:
            result["action_path"]["coordinate_source"] = coordinate_source
        if input_screenshot_id:
            result["action_path"]["input_screenshot_id"] = input_screenshot_id
        if capture_backend:
            result["action_path"]["capture_backend"] = capture_backend
        if capture_coordinate_space:
            result["action_path"]["capture_coordinate_space"] = capture_coordinate_space

    def _verification_from_capture(
        verify_query: str,
        capture: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not capture.get("success"):
            return {
                "success": False,
                "query": verify_query,
                "matched": False,
                "error": capture.get("error", "post-action capture failed"),
            }

        matched_node_count = int(capture.get("returned_node_count", capture.get("node_count", 0)) or 0)
        tree_markdown = str(capture.get("tree_markdown") or "")
        matched_tree = verify_query.lower() in tree_markdown.lower()
        matched = matched_node_count > 0 or matched_tree
        return {
            "success": True,
            "query": verify_query,
            "matched": matched,
            "matched_node_count": matched_node_count,
            "matched_tree": matched_tree,
            "snapshot_id": capture.get("snapshot_id"),
            "screenshot_id": capture.get("screenshot_id"),
            "capture": capture.get("capture"),
            "tree_quality": capture.get("tree_quality"),
            "node_sources": capture.get("node_sources", []),
        }

    async def _attach_post_action_observation(
        result: Dict[str, Any],
        pid: Optional[int],
        window_id: Optional[int],
        *,
        capture_after: bool,
        verify_query: Optional[str],
        require_verification: bool,
    ) -> Dict[str, Any]:
        """Attach capture_after and optional post-action verification."""
        if pid is None or window_id is None:
            if verify_query:
                result["post_action_verification"] = {
                    "success": False,
                    "query": verify_query,
                    "matched": False,
                    "error": "pid and window_id are required for post-action verification",
                }
                if require_verification:
                    result["action_success"] = bool(result.get("success"))
                    result["success"] = False
                    result["error_code"] = "post_action_verification_failed"
                    result["error"] = "pid and window_id are required for post-action verification"
            return result

        if capture_after:
            result["capture_after"] = await _capture_and_detect(pid, window_id)

        if verify_query:
            verification_capture = await _capture_and_detect(
                pid,
                window_id,
                query=verify_query,
                include_all_nodes=False,
            )
            verification = _verification_from_capture(verify_query, verification_capture)
            result["post_action_verification"] = verification
            if require_verification and not verification.get("matched", False):
                result["action_success"] = bool(result.get("success"))
                result["success"] = False
                result["error_code"] = "post_action_verification_failed"
                result["error"] = f"Post-action verification failed for query: {verify_query}"
        return result

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

        from computer_server.handlers.screenshot_service import (
            encode_for_mcp_image,
            resize_to_max_dimension,
        )
        from computer_server.services.validation_service import (
            error_result,
            validate_screenshot_request,
        )

        try:
            fmt, quality = validate_screenshot_request(format, quality)
        except ValueError as e:
            return error_result(e)

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

    async def _click_element_uia_invoke(
        pid: int,
        window_id: int,
        element_index: int,
        el: Any,
        recorded_element_ref: Optional[Dict[str, Any]],
        capture_after: bool,
        verify_query: Optional[str],
        require_verification: bool,
    ) -> Optional[Dict[str, Any]]:
        """Try UIA Invoke for an enabled cached element; return None to fall back."""
        if getattr(el, "source", None) == "msaa":
            return None
        if sys.platform != "win32" or "invoke" not in (el.actions or []):
            return None

        import asyncio

        try:
            import win32gui as _wg
            from computer_server.handlers.element_cache import get_element_cache

            # Snapshot visible windows before click so cross-app side-effects can be reported.
            snap_fg_hwnd, snap_hwnds = _window_snapshot()

            try:
                screen_pt = _resolve_element_center(pid, window_id, element_index)
            except ValueError:
                origin_x, origin_y = get_element_cache().get_window_origin(pid, window_id)
                screen_pt = (origin_x + el.center_x, origin_y + el.center_y)
            await _animate_cursor_to(int(screen_pt[0]), int(screen_pt[1]))

            invoked, invoke_error = await _run_uia_action_with_timeout(
                pid,
                window_id,
                "invoke",
                lambda: _invoke_via_uia_point(int(screen_pt[0]), int(screen_pt[1])),
            )
            if not invoked:
                logger.debug("UIA Invoke unavailable; falling back to pixel click: %s", invoke_error)
                return None
            result: Dict[str, Any] = {
                "success": True,
                "action": "invoke",
                "element_index": element_index,
                "method": "uia_invoke",
            }
            _annotate_action_path(
                result,
                method="uia_invoke",
                used_foreground=False,
                uses_system_input=False,
                requires_visible_target=False,
                coordinate_source="element",
            )

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

            edit_types = {"Edit", "ComboBox", "Document", "RichEdit"}
            if el.control_type in edit_types:
                await asyncio.sleep(0.8)

            await _attach_post_action_observation(
                result,
                pid,
                window_id,
                capture_after=capture_after,
                verify_query=verify_query,
                require_verification=require_verification,
            )
            wr = _build_window_ref(pid, window_id, screen_pt[0], screen_pt[1])
            await _recording_hook(_recorded_turn(
                "computer_click",
                {
                    "element_index": element_index,
                    "pid": pid,
                    "window_id": window_id,
                    "verify_query": verify_query,
                    "require_verification": require_verification,
                },
                result,
                click_point=screen_pt,
                window_ref=wr,
                element_ref=recorded_element_ref,
            ))
            return result
        except Exception as exc:
            logger.debug("UIA Invoke failed (falling back to pixel click): %s", exc)
            return None

    def _resolve_msaa_expand_point(
        pid: int,
        window_id: int,
        element_index: int,
        el: Any,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[int]]:
        """Resolve the expand point for an MSAA dropdown-style element."""
        from computer_server.handlers.element_cache import get_element_cache

        metadata = getattr(el, "metadata", {}) or {}
        role_id = metadata.get("msaa_role_id")
        dropdown_roles = {0x38, 0x39, 0x3A, 0x3E}
        if getattr(el, "source", None) != "msaa":
            return {
                "success": False,
                "error": "action='expand' is supported only for MSAA dropdown/splitbutton elements",
                "suggestion": "Use computer_click for the element's primary action",
            }, None, None
        if role_id not in dropdown_roles:
            return {
                "success": False,
                "error": (
                    f"action='expand' requested for MSAA element [{element_index}], "
                    f"but its role ({metadata.get('msaa_role') or el.control_type}) is not a dropdown button"
                ),
                "suggestion": "Use computer_click or pick an element whose actions include 'expand'",
            }, None, None

        bbox = el.bbox or (0, 0, 0, 0)
        if len(bbox) != 4 or bbox == (0, 0, 0, 0):
            return {
                "success": False,
                "error": f"MSAA element [{element_index}] has no usable bbox for action='expand'",
                "suggestion": f"Call get_window_state(pid={pid}, window_id={window_id}) to refresh it",
            }, None, None
        origin = get_element_cache().get_window_origin_optional(pid, window_id)
        if origin is None:
            return {
                "success": False,
                "error": (
                    f"MSAA element [{element_index}] found for pid={pid}, "
                    f"window_id={window_id}, but window origin is missing from the cache"
                ),
                "suggestion": f"Call get_window_state(pid={pid}, window_id={window_id}) to refresh it",
            }, None, None
        target_x = int(origin[0] + bbox[2] - 4)
        target_y = int(origin[1] + ((bbox[1] + bbox[3]) // 2))
        return None, target_x, target_y

    async def _resolve_click_element(
        pid: Optional[int],
        window_id: Optional[int],
        element_index: Optional[int],
        from_zoom: bool,
        capture_after: bool,
        verify_query: Optional[str],
        require_verification: bool,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[int], Optional[Dict[str, Any]]]:
        """Resolve element_index mode.

        Returns (final_result, resolved_x, resolved_y, element_ref). If
        final_result is not None, the caller should return it immediately.
        Otherwise resolved_x/resolved_y are screen coordinates for pixel click.
        """
        if element_index is None:
            return None, None, None, None
        if from_zoom:
            return {
                "success": False,
                "error": "element_index and from_zoom are mutually exclusive — element_index already returns screen coords",
                "suggestion": "Remove from_zoom=True or use x/y coordinates with from_zoom instead",
            }, None, None, None
        if pid is None or window_id is None:
            return {
                "success": False,
                "error": "pid and window_id are required with element_index",
                "suggestion": "Provide pid and window_id from a previous get_window_state call",
            }, None, None, None

        from computer_server.handlers.element_cache import get_element_cache

        el = get_element_cache().get_element(pid, window_id, element_index)
        if el is None:
            return {
                "success": False,
                "error": f"No cached element at index {element_index} for pid={pid}, window_id={window_id}",
                "suggestion": (
                    f"The element cache may have expired. Call get_window_state(pid={pid}, "
                    f"window_id={window_id}) to refresh it, then use the new element_index"
                ),
            }, None, None, None

        recorded_element_ref = _build_element_ref(pid, window_id, element_index)
        if el.enabled is False:
            return {
                "success": False,
                "error": f"Element [{element_index}] is disabled (enabled=false) — the window may not be frontmost",
                "suggestion": (
                    f"Call computer_manage_window(action='activate', window_id={window_id}) "
                    "first, then re-snapshot"
                ),
            }, None, None, recorded_element_ref

        invoke_result = await _click_element_uia_invoke(
            pid,
            window_id,
            element_index,
            el,
            recorded_element_ref,
            capture_after,
            verify_query,
            require_verification,
        )
        if invoke_result is not None:
            return invoke_result, None, None, recorded_element_ref

        try:
            resolved_x, resolved_y = _resolve_element_center(pid, window_id, element_index)
        except ValueError as exc:
            return {
                "success": False,
                "error": str(exc),
                "suggestion": f"Call get_window_state(pid={pid}, window_id={window_id}) to refresh the element cache",
            }, None, None, recorded_element_ref
        return None, resolved_x, resolved_y, recorded_element_ref

    def _resolve_click_coordinates(
        x: Optional[int],
        y: Optional[int],
        pid: Optional[int],
        window_id: Optional[int],
        element_index: Optional[int],
        coordinate_space: str,
        zoom_id: Optional[str],
        capture_geometry: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Optional[Dict[str, Any]], Optional[int], Optional[int]]:
        """Convert click input coordinates into absolute screen coordinates."""
        if x is None or y is None:
            return {
                "success": False,
                "error": "x and y are required (or element_index + pid + window_id)",
                "suggestion": (
                    "Provide (x, y) pixel coordinates or (element_index + pid + window_id) "
                    "from a previous get_window_state call"
                ),
            }, None, None

        if element_index is None and coordinate_space == "window_resized":
            x, y = _window_resized_to_screen(pid, window_id, x, y, capture_geometry)
        elif element_index is None and coordinate_space == "window_full_res":
            try:
                x, y = _window_full_res_to_screen(pid, window_id, x, y, capture_geometry)
            except ValueError as exc:
                return {"success": False, "error": str(exc)}, None, None

        if coordinate_space == "zoom" and window_id is None:
            return {
                "success": False,
                "error": "window_id is required when from_zoom=True",
                "suggestion": "Provide the window_id used in the prior computer_zoom call",
            }, None, None
        try:
            x, y = _resolve_from_zoom(
                x,
                y,
                from_zoom=coordinate_space == "zoom",
                pid=pid,
                window_id=window_id,
                zoom_id=zoom_id,
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}, None, None
        return None, x, y

    async def _write_click_debug_image(
        automation_handler: Any,
        actual_x: int,
        actual_y: int,
        debug_image_out: Optional[str],
    ) -> None:
        if not debug_image_out:
            return
        try:
            screenshot_result = await automation_handler.screenshot()
            if screenshot_result.get("success") and "image" in screenshot_result:
                from PIL import ImageDraw

                img = screenshot_result["image"]
                draw = ImageDraw.Draw(img)
                r = 12
                draw.ellipse(
                    [actual_x - r, actual_y - r, actual_x + r, actual_y + r],
                    outline="red",
                    width=3,
                )
                draw.line(
                    [actual_x - r - 4, actual_y, actual_x + r + 4, actual_y],
                    fill="red",
                    width=2,
                )
                draw.line(
                    [actual_x, actual_y - r - 4, actual_x, actual_y + r + 4],
                    fill="red",
                    width=2,
                )
                img.save(debug_image_out)
            else:
                logger.warning("debug_image_out: screenshot failed for %s", debug_image_out)
        except Exception as exc:
            logger.warning("debug_image_out: %s", exc)

    async def _force_frontmost_click(
        pid: Optional[int],
        window_id: Optional[int],
        x: int,
        y: int,
        actual_x: int,
        actual_y: int,
        button: str,
        count: int,
        modifier: Optional[List[str]],
        capture_after: bool,
        verify_query: Optional[str],
        require_verification: bool,
        coordinate_source: str,
        recorded_element_ref: Optional[Dict[str, Any]],
        input_screenshot_id: Optional[str] = None,
        capture_backend: Optional[str] = None,
        capture_coordinate_space: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Perform force-frontmost click when requested by the caller."""
        if sys.platform != "win32" or pid is None or window_id is None:
            return None
        if modifier:
            return {
                "success": False,
                "error": (
                    "force_frontmost does not support modifier keys — use two separate calls: "
                    "computer_press_key to hold the modifier, then force_frontmost click without modifier"
                ),
                "suggestion": "Split into separate key_down + click + key_up calls",
            }
        try:
            import asyncio
            import win32gui as _wg

            await _animate_cursor_to(actual_x, actual_y)
            fg_ok = _wg.SetForegroundWindow(int(window_id))
            if not fg_ok:
                logger.debug(
                    "SetForegroundWindow returned 0 for window_id=%d — click may land on wrong window",
                    window_id,
                )
            await asyncio.sleep(0.05)
            _mouse_event_click(actual_x, actual_y, button, count)
            result: Dict[str, Any] = {
                "success": True,
                "method": "mouse_event",
                "x": actual_x,
                "y": actual_y,
                "button": button,
                "count": count,
                "force_frontmost": True,
            }
            _annotate_action_path(
                result,
                method="mouse_event",
                used_foreground=True,
                uses_system_input=True,
                requires_visible_target=True,
                coordinate_source=coordinate_source,
                input_screenshot_id=input_screenshot_id,
                capture_backend=capture_backend,
                capture_coordinate_space=capture_coordinate_space,
            )
            await _attach_post_action_observation(
                result,
                pid,
                window_id,
                capture_after=capture_after,
                verify_query=verify_query,
                require_verification=require_verification,
            )
            wr = _build_window_ref(pid, window_id, actual_x, actual_y)
            await _recording_hook(_recorded_turn(
                "computer_click",
                {
                    "x": x,
                    "y": y,
                    "button": button,
                    "force_frontmost": True,
                    "count": count,
                    "screenshot_id": input_screenshot_id,
                    "verify_query": verify_query,
                    "require_verification": require_verification,
                },
                result,
                click_point=(actual_x, actual_y),
                window_ref=wr,
                element_ref=recorded_element_ref,
            ))
            return result
        except Exception as exc:
            return {
                "success": False,
                "error": f"force_frontmost click failed: {exc}",
                "suggestion": "Try without force_frontmost=True, or use computer_press_key with a hotkey to bring the window to front first",
            }

    async def _perform_pixel_click(
        automation_handler: Any,
        actual_x: int,
        actual_y: int,
        button: str,
        count: int,
        modifier: Optional[List[str]],
    ) -> Dict[str, Any]:
        import asyncio

        if modifier:
            for mod in modifier:
                await automation_handler.key_down(mod)
        try:
            click_methods = {
                "left": automation_handler.left_click,
                "right": automation_handler.right_click,
                "middle": automation_handler.middle_click,
            }
            click_method = click_methods.get(button, automation_handler.left_click)
            result = await click_method(actual_x, actual_y)
            for _ in range(count - 1):
                await asyncio.sleep(0.05)
                await click_method(actual_x, actual_y)
            return result
        finally:
            if modifier:
                for mod in reversed(modifier):
                    await automation_handler.key_up(mod)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_click(
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
        element_index: Optional[int] = None,
        window_id: Optional[int] = None,
        pid: Optional[int] = None,
        window: Optional[Dict[str, Any]] = None,
        snapshot_id: Optional[str] = None,
        screenshot_id: Optional[str] = None,
        from_zoom: bool = False,
        zoom_id: Optional[str] = None,
        coordinate_space: Optional[str] = None,
        modifier: Optional[List[str]] = None,
        count: int = 1,
        debug_image_out: Optional[str] = None,
        force_frontmost: bool = False,
        capture_after: bool = False,
        verify_query: Optional[str] = None,
        require_verification: bool = False,
    ) -> Any:
        """
        Click at the specified screen coordinates or on a cached element.

        Addressing modes:
        - coordinate_space='screen': x/y are absolute screen coordinates.
        - coordinate_space='window_resized': x/y are coordinates in the resized
          get_window_state screenshot for pid/window_id.
        - coordinate_space='window_full_res': x/y are full-resolution
          window-local coordinates.
        - coordinate_space='zoom': x/y are zoom-image coordinates.
        - coordinate_space='element': element_index addresses a cached node.

        If coordinate_space is omitted, default inference is preserved:
        element_index -> element, zoom args -> zoom, pid/window_id ->
        window_resized, otherwise screen.

        Args:
            x: X coordinate in pixels (ignored when element_index is set)
            y: Y coordinate in pixels (ignored when element_index is set)
            button: Mouse button - 'left', 'right', or 'middle' (default: 'left')
            element_index: Index from get_window_state nodes to click via element cache
            window_id: Top-level window ID (required with element_index)
            pid: Top-level process ID (required with element_index)
            window: Canonical window object from list/get_window_state output.
            snapshot_id: Snapshot id returned by computer_get_window_state.
            screenshot_id: Screenshot id returned by computer_get_window_state for bound coordinate conversion.
            from_zoom: When True, x/y are zoomed-image coords and get translated back
            zoom_id: Zoom id returned by computer_zoom.
            coordinate_space: Explicit coordinate space for x/y or element_index.
            modifier: Modifier keys held during click (ctrl, alt, shift, win)
            count: Click count - 1 (single), 2 (double), 3 (triple). Default 1.
            debug_image_out: Optional path to save a debug image with click crosshair.
            force_frontmost: When True, calls SetForegroundWindow before click.
            capture_after: When True, take a fresh screenshot + SOM after the action.
            verify_query: Optional query to search in a fresh window snapshot after the action.
            require_verification: When True, return success=False if verify_query is not found.
        """
        from computer_server.services.validation_service import (
            error_result,
            validate_click_count,
            validate_mouse_button,
            validate_modifiers,
            validate_optional_coordinate,
            validate_optional_query,
            resolve_coordinate_space,
        )

        try:
            button = validate_mouse_button(button)
            count = validate_click_count(count)
            x = validate_optional_coordinate("x", x)
            y = validate_optional_coordinate("y", y)
            modifier = validate_modifiers(modifier)
            verify_query = validate_optional_query("verify_query", verify_query)
            if not isinstance(require_verification, bool):
                raise ValueError("require_verification must be a boolean")
            if require_verification and verify_query is None:
                raise ValueError("require_verification=True requires verify_query")
        except ValueError as e:
            return error_result(e)

        _suppress_learning_input(3.0)

        pid, window_id, ref_error = _resolve_window_arg(window, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, ref_error = _resolve_snapshot_ref(snapshot_id, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, screenshot_ctx, ref_error = _resolve_screenshot_ref(
            screenshot_id,
            snapshot_id,
            pid,
            window_id,
        )
        if ref_error is not None:
            return ref_error
        if zoom_id:
            if element_index is not None:
                return {
                    "success": False,
                    "error": "element_index and zoom_id are mutually exclusive",
                    "suggestion": "Use either a snapshot element_index or raw x/y coordinates from the zoom image.",
                }
            pid, window_id, ref_error = _resolve_zoom_ref(zoom_id, pid, window_id)
            if ref_error is not None:
                return ref_error
            from_zoom = True

        try:
            coordinate_space = resolve_coordinate_space(
                coordinate_space,
                element_index=element_index,
                from_zoom=from_zoom,
                zoom_id=zoom_id,
                pid=pid,
                window_id=window_id,
            )
        except ValueError as e:
            return error_result(e)
        if coordinate_space == "zoom":
            from_zoom = True

        explicit_window_activated = False
        if window is not None:
            pid, window_id, ref_error = await _activate_input_window(
                window,
                pid,
                window_id,
            )
            if ref_error is not None:
                return ref_error
            explicit_window_activated = True

        element_result, element_x, element_y, recorded_element_ref = await _resolve_click_element(
            pid,
            window_id,
            element_index,
            from_zoom,
            capture_after,
            verify_query,
            require_verification,
        )
        if element_result is not None:
            _attach_explicit_window_activation(
                element_result,
                pid,
                window_id,
                activated=explicit_window_activated,
            )
            return element_result
        if element_x is not None and element_y is not None:
            x, y = element_x, element_y

        coord_error, resolved_x, resolved_y = _resolve_click_coordinates(
            x,
            y,
            pid,
            window_id,
            element_index,
            coordinate_space,
            zoom_id,
            screenshot_ctx.capture_geometry if screenshot_ctx is not None else None,
        )
        if coord_error is not None:
            return coord_error
        x, y = resolved_x, resolved_y
        if x is None or y is None:
            return {"success": False, "error": "internal error: click coordinates were not resolved"}

        actual_x, actual_y = _scale_to_actual(x, y)

        # Agent cursor overlay: fly to target before the action
        await _animate_cursor_to(actual_x, actual_y)

        automation_handler = _get_handlers().automation

        await _write_click_debug_image(automation_handler, actual_x, actual_y, debug_image_out)

        if force_frontmost:
            result = await _force_frontmost_click(
                pid,
                window_id,
                x,
                y,
                actual_x,
                actual_y,
                button,
                count,
                modifier,
                capture_after,
                verify_query,
                require_verification,
                "element" if element_index is not None else coordinate_space,
                recorded_element_ref,
                input_screenshot_id=screenshot_ctx.screenshot_id if screenshot_ctx is not None else None,
                capture_backend=(
                    screenshot_ctx.capture_metadata.get("capture_backend")
                    if screenshot_ctx is not None
                    else None
                ),
                capture_coordinate_space=(
                    coordinate_space if screenshot_ctx is not None else None
                ),
            )
            if result is not None:
                _attach_explicit_window_activation(
                    result,
                    pid,
                    window_id,
                    activated=explicit_window_activated,
                )
                return result

        # Animate cursor overlay to click point
        try:
            await _animate_cursor_to(actual_x, actual_y)
        except Exception:
            pass

        result = await _perform_pixel_click(
            automation_handler,
            actual_x,
            actual_y,
            button,
            count,
            modifier,
        )
        if element_index is not None:
            method = "element_center_pixel_click"
        else:
            method = "pixel_click"
        _annotate_action_path(
            result,
            method=method,
            used_foreground=True,
            uses_system_input=True,
            requires_visible_target=True,
            coordinate_source="element" if element_index is not None else coordinate_space,
            input_screenshot_id=screenshot_ctx.screenshot_id if screenshot_ctx is not None else None,
            capture_backend=(
                screenshot_ctx.capture_metadata.get("capture_backend")
                if screenshot_ctx is not None
                else None
            ),
            capture_coordinate_space=(
                coordinate_space if screenshot_ctx is not None else None
            ),
        )
        _attach_explicit_window_activation(
            result,
            pid,
            window_id,
            activated=explicit_window_activated,
        )
        await _attach_post_action_observation(
            result,
            pid,
            window_id,
            capture_after=capture_after,
            verify_query=verify_query,
            require_verification=require_verification,
        )

        wr = _build_window_ref(pid, window_id, actual_x, actual_y)
        await _recording_hook(_recorded_turn(
            "computer_click",
            {
                "x": x,
                "y": y,
                "button": button,
                "count": count,
                "screenshot_id": screenshot_id,
                "verify_query": verify_query,
                "require_verification": require_verification,
            },
            result,
            click_point=(actual_x, actual_y),
            window_ref=wr,
            element_ref=recorded_element_ref,
        ))

        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_perform_secondary_action(
        element_index: int,
        action: str,
        window_id: Optional[int] = None,
        pid: Optional[int] = None,
        window: Optional[Dict[str, Any]] = None,
        snapshot_id: Optional[str] = None,
        capture_after: bool = False,
        verify_query: Optional[str] = None,
        require_verification: bool = False,
    ) -> Any:
        """
        Perform a non-primary action exposed by a cached accessibility node.

        The node must come from computer_get_window_state. Primary actions stay
        on computer_click; window activation stays on computer_manage_window.
        Currently supported secondary actions: 'expand' for MSAA
        dropdown/splitbutton nodes whose actions include 'expand'.

        Args:
            element_index: Cached node index from computer_get_window_state.
            action: Secondary action name. Currently only 'expand'.
            window_id: Top-level window ID associated with the cached node.
            pid: Top-level process ID associated with the cached node.
            window: Canonical window object from list/get_window_state output.
            snapshot_id: Snapshot id used to reject stale node observations.
            capture_after: Capture a fresh window state after the action.
            verify_query: Query to verify in the post-action window state.
            require_verification: Fail when verify_query is not observed.
        """
        from computer_server.handlers.element_cache import get_element_cache
        from computer_server.services.validation_service import (
            error_result,
            validate_int_range,
            validate_optional_query,
            validate_secondary_action,
        )

        try:
            element_index = validate_int_range(
                "element_index",
                element_index,
                0,
                1_000_000,
            )
            action = validate_secondary_action(action)
            verify_query = validate_optional_query("verify_query", verify_query)
            if not isinstance(require_verification, bool):
                raise ValueError("require_verification must be a boolean")
            if require_verification and verify_query is None:
                raise ValueError("require_verification=True requires verify_query")
        except ValueError as exc:
            return error_result(exc)

        _suppress_learning_input(3.0)

        pid, window_id, ref_error = _resolve_window_arg(window, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, ref_error = _resolve_snapshot_ref(snapshot_id, pid, window_id)
        if ref_error is not None:
            return ref_error
        if pid is None or window_id is None:
            return {
                "success": False,
                "error": "pid and window_id are required with element_index",
                "suggestion": "Pass the window and snapshot_id from computer_get_window_state",
            }

        element = get_element_cache().get_element(pid, window_id, element_index)
        if element is None:
            return {
                "success": False,
                "error": f"No cached element at index {element_index} for pid={pid}, window_id={window_id}",
                "suggestion": "Call computer_get_window_state again and use the new element_index",
            }
        if action not in (element.actions or []):
            return {
                "success": False,
                "error": (
                    f"Element [{element_index}] does not expose secondary action "
                    f"'{action}'"
                ),
                "available_actions": list(element.actions or []),
            }

        recorded_element_ref = _build_element_ref(pid, window_id, element_index)
        action_error, target_x, target_y = _resolve_msaa_expand_point(
            pid,
            window_id,
            element_index,
            element,
        )
        if action_error is not None:
            return action_error
        if target_x is None or target_y is None:
            return {"success": False, "error": "internal error: secondary action point was not resolved"}

        activation = await _get_handlers().window.activate_window(window_id)
        if activation.get("success") is False:
            return {
                "success": False,
                "error": (
                    f"Failed to activate target window {window_id}: "
                    f"{activation.get('error', 'activation failed')}"
                ),
                "activation": activation,
            }

        actual_x, actual_y = _scale_to_actual(target_x, target_y)
        await _animate_cursor_to(actual_x, actual_y)
        result = await _perform_pixel_click(
            _get_handlers().automation,
            actual_x,
            actual_y,
            "left",
            1,
            None,
        )
        result["action"] = action
        result["element_index"] = element_index
        result["pid"] = pid
        result["window_id"] = window_id
        _annotate_action_path(
            result,
            method="msaa_expand_edge_click",
            used_foreground=True,
            uses_system_input=True,
            requires_visible_target=True,
            coordinate_source="element",
        )
        await _attach_post_action_observation(
            result,
            pid,
            window_id,
            capture_after=capture_after,
            verify_query=verify_query,
            require_verification=require_verification,
        )

        wr = _build_window_ref(pid, window_id, actual_x, actual_y)
        await _recording_hook(_recorded_turn(
            "computer_perform_secondary_action",
            {
                "element_index": element_index,
                "action": action,
                "pid": pid,
                "window_id": window_id,
                "verify_query": verify_query,
                "require_verification": require_verification,
            },
            result,
            click_point=(actual_x, actual_y),
            window_ref=wr,
            element_ref=recorded_element_ref,
        ))
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_mouse(
        action: str,
        x: Optional[int] = None,
        y: Optional[int] = None,
        button: str = "left",
    ) -> Any:
        """Run a low-level mouse primitive: move, down, or up.

        Higher-level callers should prefer computer_click, computer_drag, and
        computer_scroll. Use this only for explicit primitive sequences.

        Args:
            action: move|down|up.
            x/y: Screen coordinates. Required for move; optional for down/up.
            button: Mouse button for down/up.
        """
        from computer_server.services.validation_service import (
            error_result,
            validate_choice,
            validate_mouse_button,
            validate_optional_coordinate,
        )

        try:
            normalized_action = validate_choice("action", action, {"move", "down", "up"})
            button = validate_mouse_button(button)
            x = validate_optional_coordinate("x", x)
            y = validate_optional_coordinate("y", y)
        except ValueError as e:
            return error_result(e)

        if normalized_action == "move" and (x is None or y is None):
            return {"success": False, "error": "x and y are required for action=move"}
        if normalized_action in {"down", "up"} and ((x is None) != (y is None)):
            return {"success": False, "error": "x and y must be provided together"}

        automation_handler = _get_handlers().automation
        actual_x: Optional[int] = None
        actual_y: Optional[int] = None
        if x is not None and y is not None:
            actual_x, actual_y = _scale_to_actual(x, y)
            await _animate_cursor_to(actual_x, actual_y)

        if normalized_action == "move":
            result = await automation_handler.move_cursor(actual_x, actual_y)
        elif normalized_action == "down":
            already_held = button in context.held_mouse_buttons
            suppression_active = False if already_held else _begin_learning_input_suppression()
            try:
                result = await automation_handler.mouse_down(actual_x, actual_y, button=button)
            except Exception:
                if suppression_active:
                    _end_learning_input_suppression(suppression_active)
                raise
            if result.get("success") is not False:
                context.held_mouse_buttons.add(button)
            elif suppression_active:
                _end_learning_input_suppression(suppression_active)
        else:
            _suppress_learning_input(1.0)
            was_held = button in context.held_mouse_buttons
            result = await automation_handler.mouse_up(actual_x, actual_y, button=button)
            if result.get("success") is not False:
                context.held_mouse_buttons.discard(button)
                if was_held:
                    _end_learning_input_suppression(True)

        click_point = (
            (actual_x, actual_y)
            if actual_x is not None and actual_y is not None
            else None
        )
        wr = (
            _build_window_ref(None, None, actual_x, actual_y)
            if actual_x is not None and actual_y is not None
            else None
        )
        await _recording_hook(_recorded_turn(
            "computer_mouse",
            {"action": normalized_action, "x": x, "y": y, "button": button},
            result,
            click_point=click_point,
            window_ref=wr,
        ))
        return result

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
        window: Optional[Dict[str, Any]] = None,
        snapshot_id: Optional[str] = None,
        screenshot_id: Optional[str] = None,
        from_zoom: bool = False,
        zoom_id: Optional[str] = None,
        coordinate_space: Optional[str] = None,
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
            window_id: Top-level window ID (required with element_index or from_zoom)
            pid: Top-level process ID (required with element_index)
            window: Canonical window object from list/get_window_state output.
            snapshot_id: Snapshot id returned by computer_get_window_state.
            screenshot_id: Screenshot id returned by computer_get_window_state for bound coordinate conversion.
            from_zoom: When True, coords are in zoomed-image space
            zoom_id: Zoom id returned by computer_zoom.
            coordinate_space: Explicit coordinate space for start/end coords.
            modifier: Modifier keys held during drag (cmd, shift, option, ctrl)
        """
        import asyncio

        from computer_server.services.validation_service import (
            error_result,
            validate_drag_duration,
            validate_mouse_button,
            validate_modifiers,
            validate_optional_coordinate,
            resolve_coordinate_space,
        )

        try:
            button = validate_mouse_button(button)
            duration = validate_drag_duration(duration)
            start_x = validate_optional_coordinate("start_x", start_x)
            start_y = validate_optional_coordinate("start_y", start_y)
            end_x = validate_optional_coordinate("end_x", end_x)
            end_y = validate_optional_coordinate("end_y", end_y)
            modifier = validate_modifiers(modifier)
        except ValueError as e:
            return error_result(e)

        _suppress_learning_input(max(1.0, duration + 0.75))

        pid, window_id, ref_error = _resolve_window_arg(window, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, ref_error = _resolve_snapshot_ref(snapshot_id, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, screenshot_ctx, ref_error = _resolve_screenshot_ref(
            screenshot_id,
            snapshot_id,
            pid,
            window_id,
        )
        if ref_error is not None:
            return ref_error
        if zoom_id:
            if element_index is not None:
                return {
                    "success": False,
                    "error": "element_index and zoom_id are mutually exclusive",
                    "suggestion": "Use either a snapshot element_index or raw coordinates from the zoom image.",
                }
            pid, window_id, ref_error = _resolve_zoom_ref(zoom_id, pid, window_id)
            if ref_error is not None:
                return ref_error
            from_zoom = True

        try:
            coordinate_space = resolve_coordinate_space(
                coordinate_space,
                element_index=element_index,
                from_zoom=from_zoom,
                zoom_id=zoom_id,
                pid=pid,
                window_id=window_id,
            )
        except ValueError as e:
            return error_result(e)
        if coordinate_space == "zoom":
            from_zoom = True

        explicit_window_activated = False
        if window is not None:
            pid, window_id, ref_error = await _activate_input_window(
                window,
                pid,
                window_id,
            )
            if ref_error is not None:
                return ref_error
            explicit_window_activated = True

        recorded_element_ref = None

        if element_index is not None:
            if from_zoom:
                return {"success": False, "error": "element_index and from_zoom are mutually exclusive — element_index already returns screen coords", "suggestion": "Remove from_zoom=True or use raw coordinates with from_zoom instead"}
            if pid is None or window_id is None:
                return {"success": False, "error": "pid and window_id are required with element_index", "suggestion": "Provide pid and window_id from a previous get_window_state call"}
            recorded_element_ref = _build_element_ref(pid, window_id, element_index)
            try:
                start_x, start_y = _resolve_element_center(pid, window_id, element_index)
            except ValueError as e:
                return {"success": False, "error": str(e), "suggestion": f"Call get_window_state(pid={pid}, window_id={window_id}) to refresh the element cache"}

        if start_x is None or start_y is None or end_x is None or end_y is None:
            return {"success": False, "error": "start_x, start_y, end_x, end_y are required", "suggestion": "Provide all four coordinates: start_x, start_y (from cursor position or element), and end_x, end_y (target position)"}

        # Convert explicit/default non-screen spaces to absolute screen coords.
        if element_index is None and coordinate_space == "window_resized":
            capture_geometry = (
                screenshot_ctx.capture_geometry if screenshot_ctx is not None else None
            )
            start_x, start_y = _window_resized_to_screen(
                pid,
                window_id,
                start_x,
                start_y,
                capture_geometry,
            )
            end_x, end_y = _window_resized_to_screen(
                pid,
                window_id,
                end_x,
                end_y,
                capture_geometry,
            )
        elif element_index is None and coordinate_space == "window_full_res":
            capture_geometry = (
                screenshot_ctx.capture_geometry if screenshot_ctx is not None else None
            )
            try:
                start_x, start_y = _window_full_res_to_screen(
                    pid,
                    window_id,
                    start_x,
                    start_y,
                    capture_geometry,
                )
                end_x, end_y = _window_full_res_to_screen(
                    pid,
                    window_id,
                    end_x,
                    end_y,
                    capture_geometry,
                )
            except ValueError as e:
                return {"success": False, "error": str(e)}

        if coordinate_space == "zoom" and window_id is None:
            return {"success": False, "error": "window_id is required when from_zoom=True", "suggestion": "Provide the window_id used in the prior computer_zoom call"}
        try:
            start_x, start_y = _resolve_from_zoom(
                start_x,
                start_y,
                from_zoom=coordinate_space == "zoom",
                pid=pid,
                window_id=window_id,
                zoom_id=zoom_id,
            )
            end_x, end_y = _resolve_from_zoom(
                end_x,
                end_y,
                from_zoom=coordinate_space == "zoom",
                pid=pid,
                window_id=window_id,
                zoom_id=zoom_id,
            )
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
        import computer_server.overlay as overlay

        _agent_cursor = overlay._agent_cursor

        btn = automation_handler._map_button(button)
        await automation_handler.mouse_down(actual_start_x, actual_start_y, button=button)
        steps = max(1, int(duration / 0.02))
        for i in range(1, steps + 1):
            t = i / steps
            ix = int(actual_start_x + (actual_end_x - actual_start_x) * t)
            iy = int(actual_start_y + (actual_end_y - actual_start_y) * t)
            automation_handler.mouse.position = (ix, iy)
            # Track overlay along the drag path and keep it visible
            if (
                _agent_cursor is not None
                and not overlay.is_animation_suppressed()
            ):
                _agent_cursor.set_position_visible(ix, iy)
            await asyncio.sleep(duration / steps)
        automation_handler.mouse.position = (actual_end_x, actual_end_y)
        await automation_handler.mouse_up(actual_end_x, actual_end_y, button=button)
        if overlay.is_animation_suppressed():
            overlay.set_position_hidden(actual_end_x, actual_end_y)
        result = {"success": True}
        _annotate_action_path(
            result,
            method="pixel_drag",
            used_foreground=True,
            uses_system_input=True,
            requires_visible_target=True,
            coordinate_source="element" if element_index is not None else coordinate_space,
            input_screenshot_id=screenshot_ctx.screenshot_id if screenshot_ctx is not None else None,
            capture_backend=(
                screenshot_ctx.capture_metadata.get("capture_backend")
                if screenshot_ctx is not None
                else None
            ),
            capture_coordinate_space=coordinate_space if screenshot_ctx is not None else None,
        )
        _attach_explicit_window_activation(
            result,
            pid,
            window_id,
            activated=explicit_window_activated,
        )
        wr = _build_window_ref(pid, window_id, actual_start_x, actual_start_y)
        await _recording_hook(_recorded_turn(
            "computer_drag",
            {
                "start_x": start_x, "start_y": start_y,
                "end_x": end_x, "end_y": end_y, "button": button,
                "screenshot_id": screenshot_id,
            },
            result,
            click_point=(actual_start_x, actual_start_y),
            window_ref=wr,
            element_ref=recorded_element_ref,
        ))
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
        window: Optional[Dict[str, Any]] = None,
        snapshot_id: Optional[str] = None,
        screenshot_id: Optional[str] = None,
        from_zoom: bool = False,
        zoom_id: Optional[str] = None,
        coordinate_space: Optional[str] = None,
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
            window_id: Top-level window ID (required with element_index)
            pid: Top-level process ID (required with element_index)
            window: Canonical window object from list/get_window_state output.
            snapshot_id: Snapshot id returned by computer_get_window_state.
            screenshot_id: Screenshot id returned by computer_get_window_state for bound coordinate conversion.
            from_zoom: When True, x/y are zoomed-image coords and get translated back
            zoom_id: Zoom id returned by computer_zoom.
            coordinate_space: Explicit coordinate space for x/y or element_index.
            capture_after: When True, take a fresh screenshot + nodes after the action.
        """
        from computer_server.services.validation_service import (
            error_result,
            resolve_coordinate_space,
            validate_optional_coordinate,
            validate_scroll_amount,
            validate_scroll_by,
            validate_scroll_delta,
            validate_scroll_direction,
        )

        try:
            x = validate_optional_coordinate("x", x)
            y = validate_optional_coordinate("y", y)
            scroll_x = validate_scroll_delta("scroll_x", scroll_x)
            scroll_y = validate_scroll_delta("scroll_y", scroll_y)
            direction = validate_scroll_direction(direction)
            by = validate_scroll_by(by)
            amount = validate_scroll_amount(amount)
        except ValueError as e:
            return error_result(e)

        _suppress_learning_input(max(1.0, abs(scroll_x) * 0.02, abs(scroll_y) * 0.02, amount * 0.02))

        pid, window_id, ref_error = _resolve_window_arg(window, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, ref_error = _resolve_snapshot_ref(snapshot_id, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, screenshot_ctx, ref_error = _resolve_screenshot_ref(
            screenshot_id,
            snapshot_id,
            pid,
            window_id,
        )
        if ref_error is not None:
            return ref_error
        if zoom_id:
            if element_index is not None:
                return {
                    "success": False,
                    "error": "element_index and zoom_id are mutually exclusive",
                    "suggestion": "Use either a snapshot element_index or raw coordinates from the zoom image.",
                }
            pid, window_id, ref_error = _resolve_zoom_ref(zoom_id, pid, window_id)
            if ref_error is not None:
                return ref_error
            from_zoom = True

        try:
            coordinate_space = resolve_coordinate_space(
                coordinate_space,
                element_index=element_index,
                from_zoom=from_zoom,
                zoom_id=zoom_id,
                pid=pid,
                window_id=window_id,
            )
        except ValueError as e:
            return error_result(e)
        if coordinate_space == "zoom":
            from_zoom = True

        explicit_window_activated = False
        if window is not None:
            pid, window_id, ref_error = await _activate_input_window(
                window,
                pid,
                window_id,
            )
            if ref_error is not None:
                return ref_error
            explicit_window_activated = True

        if direction:
            if direction == "up":
                scroll_y = amount
            elif direction == "down":
                scroll_y = -amount
            elif direction == "left":
                scroll_x = -amount
            elif direction == "right":
                scroll_x = amount

        recorded_element_ref = None

        if element_index is not None:
            if pid is None or window_id is None:
                return {"success": False, "error": "pid and window_id are required with element_index", "suggestion": "Provide pid and window_id from a previous get_window_state call"}
            recorded_element_ref = _build_element_ref(pid, window_id, element_index)
            try:
                x, y = _resolve_element_center(pid, window_id, element_index)
            except ValueError as e:
                return {"success": False, "error": str(e), "suggestion": f"Call get_window_state(pid={pid}, window_id={window_id}) to refresh the element cache"}

        # Convert explicit/default non-screen spaces to absolute screen coords.
        # Scroll only uses (x, y) when both are provided; partial coords go
        # through as-is to preserve "scroll current cursor" behavior.
        if (
            element_index is None
            and x is not None
            and y is not None
            and coordinate_space == "window_resized"
        ):
            x, y = _window_resized_to_screen(
                pid,
                window_id,
                x,
                y,
                screenshot_ctx.capture_geometry if screenshot_ctx is not None else None,
            )
        elif (
            element_index is None
            and x is not None
            and y is not None
            and coordinate_space == "window_full_res"
        ):
            try:
                x, y = _window_full_res_to_screen(
                    pid,
                    window_id,
                    x,
                    y,
                    screenshot_ctx.capture_geometry if screenshot_ctx is not None else None,
                )
            except ValueError as e:
                return {"success": False, "error": str(e)}
        elif (
            element_index is None
            and x is not None
            and y is not None
            and coordinate_space == "zoom"
        ):
            if window_id is None:
                return {"success": False, "error": "window_id is required when from_zoom=True", "suggestion": "Provide the window_id used in the prior computer_zoom call"}
            try:
                x, y = _resolve_from_zoom(
                    x,
                    y,
                    from_zoom=True,
                    pid=pid,
                    window_id=window_id,
                    zoom_id=zoom_id,
                )
            except ValueError as e:
                return {"success": False, "error": str(e)}

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
        _annotate_action_path(
            result,
            method="pixel_scroll",
            used_foreground=x is not None and y is not None,
            uses_system_input=True,
            requires_visible_target=True,
            coordinate_source="element" if element_index is not None else coordinate_space,
            input_screenshot_id=screenshot_ctx.screenshot_id if screenshot_ctx is not None else None,
            capture_backend=(
                screenshot_ctx.capture_metadata.get("capture_backend")
                if screenshot_ctx is not None
                else None
            ),
            capture_coordinate_space=coordinate_space if screenshot_ctx is not None else None,
        )
        _attach_explicit_window_activation(
            result,
            pid,
            window_id,
            activated=explicit_window_activated,
        )
        if scroll_ax is not None and pid is not None and window_id is not None:
            wr = _build_window_ref(pid, window_id, scroll_ax, scroll_ay)
            await _recording_hook(_recorded_turn(
                "computer_scroll",
                {
                    "x": x,
                    "y": y,
                    "scroll_x": scroll_x,
                    "scroll_y": scroll_y,
                    "screenshot_id": screenshot_id,
                },
                result,
                click_point=(scroll_ax, scroll_ay),
                window_ref=wr,
                element_ref=recorded_element_ref,
            ))
        else:
            await _recording_hook(_recorded_turn(
                "computer_scroll",
                {
                    "x": x,
                    "y": y,
                    "scroll_x": scroll_x,
                    "scroll_y": scroll_y,
                    "screenshot_id": screenshot_id,
                },
                result,
                element_ref=recorded_element_ref,
            ))

        # capture_after
        if capture_after and pid is not None and window_id is not None:
            result["capture_after"] = await _capture_and_detect(pid, window_id)

        return result

    # ============================================================
    # KEYBOARD ACTIONS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_type(
        text: str,
        capture_after: bool = False,
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
        window: Optional[Dict[str, Any]] = None,
        sensitive: bool = False,
    ) -> Any:
        """
        Type text at the current cursor position via clipboard paste (Ctrl+V).

        All text — ASCII and non-ASCII — goes through the clipboard to
        avoid triggering unwanted IME (Chinese input method) behaviour
        when the user is in Chinese mode.

        Args:
            text: The text to type
            capture_after: When True, take a fresh screenshot + nodes after the action.
            pid: Top-level process ID for the target/capture_after look-up.
            window_id: Top-level window ID to activate before typing.
            window: Canonical window object from list/get_window_state output.
            sensitive: When True, redact text in recording metadata.
        """
        pid, window_id, ref_error = await _activate_input_window(
            window,
            pid,
            window_id,
        )
        if ref_error is not None:
            return ref_error

        automation_handler = _get_handlers().automation
        estimated_seconds = min(60.0, max(1.0, (len(text) / 3.0) * 0.12 + 0.75))
        _suppress_learning_input(estimated_seconds)
        result = await automation_handler.type_text(text)
        _attach_input_window(result, pid, window_id)
        recorded_text = "<redacted>" if sensitive else text
        record_args: Dict[str, Any] = {
            "text": recorded_text,
            "sensitive": sensitive,
        }
        if window_id is not None:
            record_args["window_id"] = window_id
        if pid is not None:
            record_args["pid"] = pid
        window_ref = (
            _build_window_ref(pid, window_id, 0, 0)
            if window_id is not None
            else None
        )
        await _recording_hook(_recorded_turn(
            "computer_type",
            record_args,
            result,
            window_ref=window_ref,
        ))

        # capture_after
        if capture_after and pid is not None and window_id is not None:
            result["capture_after"] = await _capture_and_detect(pid, window_id)

        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_press_key(
        key: Optional[str] = None,
        keys: Optional[List[str]] = None,
        action: str = "press",
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
        window: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """
        Press, hold, or release a key. Also supports key combinations.

        Args:
            key: Key or '+'-separated key combination, e.g. 'Return' or 'Control_L+s'.
            keys: Optional list form for a key combination, e.g. ['ctrl', 's'].
            action: press|down|up. Key combinations only support action=press.
            pid: Top-level process ID of the target window.
            window_id: Top-level window ID to activate before key input.
            window: Canonical window object from list/get_window_state output.
        """
        from computer_server.services.validation_service import error_result, validate_choice

        try:
            normalized_action = validate_choice("action", action, {"press", "down", "up"})
        except ValueError as e:
            return error_result(e)

        normalized_key: Optional[str] = None
        normalized_keys: Optional[List[str]] = None
        if keys is not None:
            if normalized_action != "press":
                return {"success": False, "error": "keys can only be used with action=press"}
            if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and k.strip() for k in keys):
                return {"success": False, "error": "keys must be a non-empty list of strings"}
            normalized_keys = [k.strip() for k in keys]
        elif key is None or not isinstance(key, str) or not key.strip():
            return {"success": False, "error": "key or keys is required"}
        else:
            normalized_key = key.strip()

        if normalized_key is not None and "+" in normalized_key:
            if normalized_action != "press":
                return {"success": False, "error": "'+' key combinations can only be used with action=press"}
            normalized_keys = [part.strip() for part in normalized_key.split("+") if part.strip()]
            if len(normalized_keys) < 2:
                return {"success": False, "error": "key combination must contain at least two keys"}

        pid, window_id, ref_error = await _activate_input_window(
            window,
            pid,
            window_id,
            activate=normalized_action != "up",
        )
        if ref_error is not None:
            return ref_error

        automation_handler = _get_handlers().automation
        if normalized_keys is not None:
            _suppress_learning_input(1.0)
            result = await automation_handler.hotkey(normalized_keys)
        elif normalized_action == "press":
            _suppress_learning_input(1.0)
            result = await automation_handler.press_key(normalized_key)
        elif normalized_action == "down":
            already_held = normalized_key in context.held_keys
            suppression_active = False if already_held else _begin_learning_input_suppression()
            try:
                result = await automation_handler.key_down(normalized_key)
            except Exception:
                if suppression_active:
                    _end_learning_input_suppression(suppression_active)
                raise
            if result.get("success") is not False:
                context.held_keys.add(normalized_key)
            elif suppression_active:
                _end_learning_input_suppression(suppression_active)
        else:
            _suppress_learning_input(1.0)
            was_held = normalized_key in context.held_keys
            result = await automation_handler.key_up(normalized_key)
            if result.get("success") is not False:
                context.held_keys.discard(normalized_key)
                if was_held:
                    _end_learning_input_suppression(True)

        _attach_input_window(
            result,
            pid,
            window_id,
            activated=normalized_action != "up",
        )
        record_args: Dict[str, Any] = {"action": normalized_action}
        if normalized_key is not None:
            record_args["key"] = normalized_key
        if normalized_keys is not None:
            record_args["keys"] = normalized_keys
        if window_id is not None:
            record_args["window_id"] = window_id
        if pid is not None:
            record_args["pid"] = pid
        window_ref = (
            _build_window_ref(pid, window_id, 0, 0)
            if window_id is not None
            else None
        )
        await _recording_hook(_recorded_turn(
            "computer_press_key",
            record_args,
            result,
            window_ref=window_ref,
        ))
        return result

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": True})
    async def computer_release_all_inputs() -> Any:
        """
        Release all keys and mouse buttons currently tracked as held down.

        Use this after interrupted computer_press_key(action="down") or
        computer_mouse(action="down") sequences, or failed replay runs, to
        return input state to neutral.
        """
        _suppress_learning_input(1.0)
        result = await _release_tracked_inputs()
        _clear_learning_input_suppression()
        return result

    # ============================================================
    # ELEMENT VALUE ACTIONS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_set_value(
        element_index: int,
        value: str,
        window_id: Optional[int] = None,
        pid: Optional[int] = None,
        window: Optional[Dict[str, Any]] = None,
        sensitive: bool = False,
        snapshot_id: Optional[str] = None,
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
            window_id: Top-level window ID.
            pid: Top-level process ID.
            window: Canonical window object from list/get_window_state output.
            sensitive: When True, redact value in result and recording metadata.
            snapshot_id: Snapshot id returned by computer_get_window_state.

        Returns:
            Dictionary with success status.
        """
        import sys

        from computer_server.handlers.element_cache import get_element_cache

        pid, window_id, ref_error = _resolve_window_arg(window, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, ref_error = _resolve_snapshot_ref(snapshot_id, pid, window_id)
        if ref_error is not None:
            return ref_error
        if pid is None or window_id is None:
            return {
                "success": False,
                "error": "pid and window_id are required, or provide snapshot_id",
            }

        el = get_element_cache().get_element(pid, window_id, element_index)
        if el is None:
            return {
                "success": False,
                "error": f"No cached element at index {element_index} for pid={pid}, window_id={window_id}",
                "suggestion": f"The element cache may have expired. Call get_window_state(pid={pid}, window_id={window_id}) to refresh it",
            }
        recorded_element_ref = _build_element_ref(pid, window_id, element_index)

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
        estimated_seconds = min(60.0, max(1.0, (len(value) / 3.0) * 0.12 + 1.0))
        _suppress_learning_input(estimated_seconds)

        ct = (el.control_type or "").lower()
        label = f"{el.name or ''} {el.control_type or ''}".lower()
        auto_sensitive = any(token in label for token in ("password", "passcode", "pin", "密码", "口令"))
        redact_value = sensitive or auto_sensitive
        response_value = "<redacted>" if redact_value else value
        record_args = {
            "element_index": element_index,
            "value": response_value,
            "pid": pid,
            "window_id": window_id,
            "sensitive": redact_value,
        }
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
                value_set, value_error = await _run_uia_action_with_timeout(
                    pid,
                    window_id,
                    "set_value",
                    lambda: _set_value_via_uia_point(screen_cx, screen_cy, value),
                )
                if value_set:
                    result = {
                        "success": True,
                        "element_index": element_index,
                        "value": response_value,
                        "method": "uia",
                    }
                    await _recording_hook(_recorded_turn(
                        "computer_set_value",
                        record_args,
                        result,
                        element_ref=recorded_element_ref,
                    ))
                    return result
                logger.debug("UIA ValuePattern set skipped (will fall back): %s", value_error)
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
            result = {"success": True, "element_index": element_index, "value": response_value, "method": "click"}
            await _recording_hook(_recorded_turn(
                "computer_set_value",
                record_args,
                result,
                element_ref=recorded_element_ref,
            ))
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
            result = {"success": True, "element_index": element_index, "value": response_value, "method": "click_type"}
            await _recording_hook(_recorded_turn(
                "computer_set_value",
                record_args,
                result,
                element_ref=recorded_element_ref,
            ))
            return result

        return {
            "success": False,
            "error": f"Cannot set value on element type '{el.control_type}'. Element has no text pattern and is not checkable.",
            "suggestion": "Try computer_click on the element first to focus it, then use computer_type to enter the value",
        }

    # ============================================================
    # WINDOW MANAGEMENT
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_open_url(url: str, browser: str = "") -> Any:
        """Open an HTTP/HTTPS URL in the desktop browser.

        Uses the specified browser if given (e.g. "chrome", "edge",
        "firefox"), falling back to the system default if the browser
        isn't found.

        Args:
            url: HTTP/HTTPS URL to open.
            browser: Preferred browser for URLs — "chrome", "edge",
                "firefox", etc.
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return {
                "success": False,
                "error": "url must be an absolute http or https URL",
            }

        window_handler = _get_handlers().window
        result = await window_handler.open(url, browser=browser)
        await _recording_hook(_recorded_turn(
            "computer_open_url",
            {"url": url, "browser": browser},
            result,
        ))
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
        await _recording_hook(_recorded_turn(
            "computer_launch_app",
            {"app": app, "args": args},
            result,
        ))
        return result

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_window(
        id: Optional[int] = None,
        app: Optional[str] = None,
        active: bool = False,
    ) -> Any:
        """Get a canonical window object by id or for the active window.

        Use this after stale handles, lost bindings, or context resets. The
        optional app value is a carry-forward hint only; it is not used as a
        strict mismatch check.

        Args:
            id: Window id / HWND from a previously returned window object.
            app: Optional app hint from a previously returned window object.
            active: When True, return the currently active window.

        Returns:
            Dictionary with success status and a canonical `window` object.
        """
        from computer_server.services.validation_service import error_result
        from computer_server.services.window_identity_service import rehydrate_window

        if active:
            window_handler = _get_handlers().window
            result = await window_handler.get_current_window_id()
            if result.get("success"):
                if "id" not in result and "window_id" in result:
                    result["id"] = result["window_id"]
                _add_window_field(result)
            return result
        if id is None:
            return {
                "success": False,
                "error": "id is required unless active=True",
            }
        try:
            return rehydrate_window(id, app=app)
        except ValueError as e:
            return error_result(e)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_manage_window(
        action: str,
        window: Optional[Dict[str, Any]] = None,
        window_id: Optional[int] = None,
        pid: Optional[int] = None,
        x: Optional[int] = None,
        y: Optional[int] = None,
        width: Optional[int] = None,
        height: Optional[int] = None,
        bounds: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Manage a window: activate, minimize, maximize, close, move, or resize.

        Args:
            action: activate|minimize|maximize|close|move|resize.
            window: Canonical window object from list/get_window_state output.
            window_id: Top-level window id / HWND.
            pid: Optional top-level process id for consistency checks with window.
            x/y: Required for action=move.
            width/height: Required for action=resize.
            bounds: Optional object containing x/y/width/height.
        """
        from computer_server.services.validation_service import (
            error_result,
            validate_coordinate,
            validate_choice,
            validate_int_range,
        )

        try:
            normalized_action = validate_choice(
                "action",
                action,
                {
                    "activate",
                    "minimize",
                    "maximize",
                    "close",
                    "move",
                    "position",
                    "resize",
                    "size",
                },
            )
        except ValueError as e:
            return error_result(e)

        if bounds is not None:
            if not isinstance(bounds, dict):
                return {"success": False, "error": "bounds must be an object"}
            x = bounds.get("x", x)
            y = bounds.get("y", y)
            width = bounds.get("width", width)
            height = bounds.get("height", height)

        pid, resolved_window_id, ref_error = _resolve_window_arg(window, pid, window_id)
        if ref_error is not None:
            return ref_error
        if resolved_window_id is None:
            return {"success": False, "error": "window or window_id is required"}

        window_handler = _get_handlers().window
        record_args: Dict[str, Any] = {
            "action": normalized_action,
            "window_id": resolved_window_id,
        }
        if pid is not None:
            record_args["pid"] = pid
        public_window = _build_public_window(pid, resolved_window_id)
        if public_window is not None:
            if "title" in public_window:
                record_args["window_title"] = public_window["title"]
            if "process_name" in public_window:
                record_args["process_name"] = public_window["process_name"]

        if normalized_action == "activate":
            result = await window_handler.activate_window(resolved_window_id)
        elif normalized_action == "minimize":
            result = await window_handler.minimize_window(resolved_window_id)
        elif normalized_action == "maximize":
            result = await window_handler.maximize_window(resolved_window_id)
        elif normalized_action == "close":
            result = await window_handler.close_window(resolved_window_id)
        elif normalized_action in {"move", "position"}:
            if x is None or y is None:
                return {"success": False, "error": "x and y are required for action=move"}
            try:
                x = validate_coordinate("x", x)
                y = validate_coordinate("y", y)
            except ValueError as e:
                return error_result(e)
            record_args.update({"x": x, "y": y})
            result = await window_handler.set_window_position(resolved_window_id, x, y)
            normalized_action = "move"
        else:
            if width is None or height is None:
                return {"success": False, "error": "width and height are required for action=resize"}
            try:
                width = validate_int_range("width", width, 1, 20000)
                height = validate_int_range("height", height, 1, 20000)
            except ValueError as e:
                return error_result(e)
            record_args.update({"width": width, "height": height})
            result = await window_handler.set_window_size(resolved_window_id, width, height)
            normalized_action = "resize"

        record_args["action"] = normalized_action
        result.setdefault("action", normalized_action)
        result.setdefault("window_id", resolved_window_id)
        if normalized_action != "close":
            public_window = _build_public_window(pid, resolved_window_id)
            if public_window is not None:
                result["window"] = public_window
        await _recording_hook(_recorded_turn(
            "computer_manage_window",
            record_args,
            result,
        ))
        return result

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
                    title = win32gui.GetWindowText(hwnd) or ""
                    if hwnd_pid not in app_map:
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
                            "windows": [],
                        }
                    app_map[hwnd_pid]["windows"].append(_add_window_field({
                        "id": hwnd,
                        "window_id": hwnd,
                        "pid": hwnd_pid,
                        "title": title,
                    }))
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
        app: Optional[str] = None,
        title: Optional[str] = None,
        query: Optional[str] = None,
        on_screen_only: bool = False,
        include_xaml: bool = False,
    ) -> Any:
        """
        List windows, optionally filtered by process ID, app/process, title, or query.

        Windows 11 XAML popups (Start menu, notifications, settings
        flyouts) don't use traditional HWNDs and are invisible to
        EnumWindows.  Set include_xaml=True to also detect these via
        UI Automation.

        Args:
            pid: Optional process ID to filter by.
            app: Optional process/app substring. Numeric app values are treated as PID.
            title: Optional title substring.
            query: Optional smart substring across title, app/process name, class, and id.
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
            from computer_server.services.window_identity_service import (
                is_window_minimized,
            )

            app_text = app.strip() if isinstance(app, str) else ""
            if app_text:
                try:
                    app_pid = int(app_text)
                    if pid is None:
                        pid = app_pid
                except (TypeError, ValueError):
                    pass
            app_filter = app_text.lower() if app_text and not app_text.isdigit() else None
            title_filter = title.lower() if isinstance(title, str) and title else None
            query_filter = query.lower() if isinstance(query, str) and query else None

            def _matches_filters(record: Dict[str, Any]) -> bool:
                record_title = str(record.get("title") or "").lower()
                record_app = str(record.get("app") or record.get("process_name") or "").lower()
                record_class = str(record.get("class_name") or "").lower()
                record_id = str(record.get("window_id") or record.get("id") or "").lower()
                if app_filter and app_filter not in record_app and app_filter not in record_title:
                    return False
                if title_filter and title_filter not in record_title:
                    return False
                if query_filter:
                    haystack = " ".join((record_title, record_app, record_class, record_id))
                    if query_filter not in haystack:
                        return False
                return True

            windows: List[Dict[str, Any]] = []

            def _enum_windows(hwnd, _):
                try:
                    visible = win32gui.IsWindowVisible(hwnd)
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
                    minimized = is_window_minimized(hwnd, rect)
                    is_on_screen = bool(visible and minimized is not True)
                    if on_screen_only and not is_on_screen:
                        return True
                    w, h = rect[2] - rect[0], rect[3] - rect[1]
                    record = _add_window_field({
                        "id": hwnd,
                        "window_id": hwnd,
                        "pid": win_pid,
                        "title": title,
                        "x": rect[0],
                        "y": rect[1],
                        "width": w,
                        "height": h,
                        "is_on_screen": is_on_screen,
                        "minimized": minimized,
                    })
                    if _matches_filters(record):
                        windows.append(record)
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
                            record = _add_window_field({
                                "id": hwnd or 0,
                                "window_id": hwnd or 0,
                                "pid": child.ProcessId or 0,
                                "title": name,
                                "x": left, "y": top,
                                "width": w, "height": h,
                                "is_on_screen": True,
                                "minimized": False,
                                "source": "uia_xaml",
                            })
                            if _matches_filters(record):
                                windows.append(record)
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

    # ============================================================
    # RICH WINDOW STATE OBSERVATION (Element Cache + UIA + Vision)
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": True})
    async def computer_get_window_state(
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
        window: Optional[Dict[str, Any]] = None,
        query: Optional[str] = None,
        screenshot_out_file: Optional[str] = None,
        max_image_dimension: int = 1280,
        force_som: bool = False,
        include_all_nodes: bool = False,
    ) -> Any:
        """
        Get a rich snapshot of a window's UI state: screenshot + element tree + indexed nodes.

        Combines accessibility tree walking with optional SOM vision detection.
        Each actionable element gets an element_index that can be used with
        computer_click, computer_double_click, computer_scroll, etc.

        Args:
            pid: Top-level process ID of the target application.
            window_id: Top-level window ID to snapshot.
            window: Canonical window object from list/get_window_state output.
            query: Optional substring filter for the debug tree markdown and returned nodes.
            screenshot_out_file: Optional path to write screenshot PNG (skips inline base64).
            max_image_dimension: Maximum dimension for the screenshot (default 1280).
            force_som: When True, run SOM vision detection regardless of tree quality.
            include_all_nodes: When True, return all nodes even if query is set.

        Returns:
            Dictionary with:
            - screenshot: base64-encoded JPEG or path to file
            - nodes: list of elements with element_index, bounds, center, control_type, name, actions
            - node_count: total number of nodes
            - tree_markdown: formatted markdown representation of the UI tree
            - tree_quality: 'rich', 'ok', 'poor', or 'empty'
            - total_elements: total elements visited
        """
        from computer_server.services.window_state_service import get_window_state_snapshot
        from computer_server.services.validation_service import (
            error_result,
            validate_max_image_dimension,
        )

        try:
            max_image_dimension = validate_max_image_dimension(max_image_dimension)
        except ValueError as e:
            return error_result(e)

        pid, window_id, ref_error = _resolve_window_arg(window, pid, window_id)
        if ref_error is not None:
            return ref_error
        if pid is None or window_id is None:
            return {
                "success": False,
                "error": "pid and window_id are required, or provide window",
            }

        automation_handler = _get_handlers().automation
        result = await get_window_state_snapshot(
            automation_handler,
            pid,
            window_id,
            query=query,
            screenshot_out_file=screenshot_out_file,
            max_image_dimension=max_image_dimension,
            force_som=force_som,
            include_all_nodes=include_all_nodes,
            zoom_metadata=_zoom_metadata,
            walk_gate=context.uia_walk_gate,
            msaa_gate=context.msaa_walk_gate,
            seen_popovers=context.seen_popovers,
        )
        if result.get("success"):
            capture_geometry = result.pop("_capture_geometry", None)
            related_screenshots = result.pop("_related_screenshots", [])
            snapshot = context.observations.create_snapshot(
                pid=pid,
                window_id=window_id,
                node_count=result.get("node_count", 0),
                unfiltered_node_count=result.get(
                    "unfiltered_node_count",
                    result.get("node_count", 0),
                ),
                screen_width=result.get("screen_width", 0),
                screen_height=result.get("screen_height", 0),
                capture_geometry=capture_geometry,
                capture_metadata=result.get("capture_metadata", {}),
            )
            result["snapshot_id"] = snapshot.snapshot_id
            result["screenshot_id"] = snapshot.screenshot_id
            result["snapshot_latest"] = True
            screenshot = context.observations.get_screenshot(snapshot.screenshot_id)
            public_window = _build_public_window(pid, window_id)
            screenshots: List[Dict[str, Any]] = []
            if screenshot is not None:
                public_capture = _public_capture_from_screenshot(screenshot)
                result["capture"] = public_capture
                screenshots.append(
                    {
                        "id": screenshot.screenshot_id,
                        "screenshot_id": screenshot.screenshot_id,
                        "snapshot_id": snapshot.snapshot_id,
                        "role": "target",
                        "zIndex": 0,
                        "pid": pid,
                        "window_id": window_id,
                        "window": public_window,
                        "screenshot": result.get("screenshot"),
                        "path": result.get("screenshot"),
                        "width": screenshot.screen_width,
                        "height": screenshot.screen_height,
                        "originX": int(
                            screenshot.capture_geometry.get(
                                "crop_origin_screen_x",
                                screenshot.capture_geometry.get("window_origin_x", 0),
                            )
                            or 0
                        ),
                        "originY": int(
                            screenshot.capture_geometry.get(
                                "crop_origin_screen_y",
                                screenshot.capture_geometry.get("window_origin_y", 0),
                            )
                            or 0
                        ),
                        "capture": public_capture,
                    }
                )
            for related in related_screenshots:
                related_geometry = related.pop("_capture_geometry", {})
                related_screenshot = context.observations.create_screenshot(
                    snapshot_id=snapshot.snapshot_id,
                    pid=related["pid"],
                    window_id=related["window_id"],
                    screen_width=related.get("screen_width", 0),
                    screen_height=related.get("screen_height", 0),
                    capture_geometry=related_geometry,
                    capture_metadata=related.get("capture_metadata", {}),
                )
                related_window = _build_public_window(
                    related["pid"],
                    related["window_id"],
                )
                screenshots.append(
                    {
                        "id": related_screenshot.screenshot_id,
                        "screenshot_id": related_screenshot.screenshot_id,
                        "snapshot_id": snapshot.snapshot_id,
                        "role": "transient",
                        "zIndex": int(related.get("z_index", 1) or 1),
                        "pid": related["pid"],
                        "window_id": related["window_id"],
                        "window": related_window,
                        "parent_window_id": window_id,
                        "screenshot": related.get("screenshot"),
                        "path": related.get("screenshot"),
                        "width": related_screenshot.screen_width,
                        "height": related_screenshot.screen_height,
                        "originX": int(
                            related_geometry.get(
                                "crop_origin_screen_x",
                                related_geometry.get("window_origin_x", 0),
                            )
                            or 0
                        ),
                        "originY": int(
                            related_geometry.get(
                                "crop_origin_screen_y",
                                related_geometry.get("window_origin_y", 0),
                            )
                            or 0
                        ),
                        "capture": _public_capture_from_screenshot(
                            related_screenshot
                        ),
                    }
                )
            result["screenshots"] = screenshots
            if public_window is not None:
                result["window"] = public_window
            window_ref = _build_window_ref(pid, window_id, 0, 0)
            if window_ref is not None:
                result["window_ref"] = window_ref
        return result

    # ============================================================
    # ZOOM
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_zoom(
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        pid: Optional[int] = None,
        window_id: Optional[int] = None,
        window: Optional[Dict[str, Any]] = None,
        screenshot_id: Optional[str] = None,
    ) -> Any:
        """
        Zoom into a rectangular region of a window screenshot at full resolution.

        This undoes the downscaling of get_window_state screenshots so small text
        and icons are readable. Coordinates are in the pixel space of the
        screenshot returned by computer_get_window_state (i.e. the resized image).

        After calling computer_zoom, use from_zoom=True on click/drag with the
        same pid and window_id to target coordinates within the zoomed image.

        Args:
            pid: Top-level process ID — must match a prior computer_get_window_state call.
            window_id: Top-level window ID — must match the same window used in the prior
                computer_get_window_state call.
            window: Canonical window object from list/get_window_state output.
            screenshot_id: Screenshot id returned by computer_get_window_state.
            x1: Left edge of the zoom region (resized-image pixels).
            y1: Top edge of the zoom region (resized-image pixels).
            x2: Right edge of the zoom region (resized-image pixels).
            y2: Bottom edge of the zoom region (resized-image pixels).

        Returns:
            Dictionary with zoomed image (base64 PNG), dimensions, and crop_rect.
        """
        from computer_server.services.window_state_service import create_zoom_image
        from computer_server.services.validation_service import (
            error_result,
            validate_zoom_rect,
        )

        try:
            x1, y1, x2, y2 = validate_zoom_rect(x1, y1, x2, y2)
        except ValueError as e:
            return error_result(e)

        pid, window_id, ref_error = _resolve_window_arg(window, pid, window_id)
        if ref_error is not None:
            return ref_error
        pid, window_id, screenshot_ctx, ref_error = _resolve_screenshot_ref(
            screenshot_id,
            None,
            pid,
            window_id,
        )
        if ref_error is not None:
            return ref_error
        if pid is None or window_id is None:
            return {
                "success": False,
                "error": "pid and window_id are required, or provide window",
            }

        automation_handler = _get_handlers().automation
        zoom_metadata = _zoom_metadata
        if screenshot_ctx is not None:
            zoom_metadata = dict(_zoom_metadata)
            zoom_metadata[(pid, window_id)] = {
                **screenshot_ctx.capture_geometry,
                "capture_backend": screenshot_ctx.capture_metadata.get("capture_backend"),
            }
        result = await create_zoom_image(
            automation_handler,
            pid,
            window_id,
            x1,
            y1,
            x2,
            y2,
            zoom_metadata,
        )
        if result.get("success"):
            if screenshot_ctx is not None:
                result["parent_screenshot_id"] = screenshot_ctx.screenshot_id
            public_window = _build_public_window(pid, window_id)
            if public_window is not None:
                result["window"] = public_window
        return result

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
        allow_javascript: bool = False,
        port: Optional[int] = None,
        tab_index: int = 0,
        target_id: Optional[str] = None,
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
            allow_javascript: Required to execute arbitrary JS. Defaults to False.
            port: Chrome remote debugging port. If None, auto-discovered from pid
                command line.
            tab_index: Initial tab index (default 0) when target_id is not provided.
            target_id: Stable CDP target id. Prefer this after get_page_info to
                keep subsequent calls pinned to the same tab.

        Returns:
            Dictionary with action-specific results.
        """
        from computer_server.handlers.browser_cdp import get_cdp_client
        from computer_server.services.validation_service import (
            error_result,
            validate_dom_attributes,
            validate_javascript,
            validate_page_action,
            validate_port,
            validate_tab_index,
        )
        from computer_server.services.permission_service import RuntimePermissionPolicy

        try:
            action = validate_page_action(action)
            port = validate_port(port)
            tab_index = validate_tab_index(tab_index)
            attributes = validate_dom_attributes(attributes, allow_html)
            if css_selector is not None and len(css_selector) > 2048:
                return {"success": False, "error": "css_selector must be 2048 characters or fewer"}
        except ValueError as e:
            return error_result(e)

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
            try:
                resolved_port = validate_port(resolved_port)
            except ValueError as e:
                return error_result(e)

        assert resolved_port is not None  # guaranteed by fallback above
        cdp = get_cdp_client()
        result: Dict[str, Any]

        if action == "get_text":
            text_result = await cdp.evaluate(
                port=resolved_port,
                expression="document.body.innerText",
                tab_index=tab_index,
                target_id=target_id,
            )
            if text_result.get("error"):
                return {"success": False, "error": text_result["error"], "target_id": text_result.get("target_id")}
            result = {
                "success": True,
                "text": text_result.get("result") or "",
                "target_id": text_result.get("target_id"),
            }
        elif action == "query_dom":
            if not css_selector:
                return {"success": False, "error": "css_selector is required for query_dom"}
            dom_result = await cdp.query_dom(
                port=resolved_port, selector=css_selector,
                attributes=attributes, tab_index=tab_index,
                allow_html=allow_html, target_id=target_id,
            )
            if dom_result.get("error"):
                return {"success": False, "error": dom_result["error"], "target_id": dom_result.get("target_id")}
            result = {
                "success": True,
                "elements": dom_result.get("elements", []),
                "target_id": dom_result.get("target_id"),
            }
        elif action == "get_page_info":
            info = await cdp.get_page_info(
                port=resolved_port,
                tab_index=tab_index,
                target_id=target_id,
            )
            if info.get("error"):
                return {"success": False, **info}
            result = {"success": True, **info}
        elif action == "execute_javascript":
            try:
                RuntimePermissionPolicy.from_env().require_cdp_javascript()
                javascript = validate_javascript(javascript, allow_javascript)
            except (PermissionError, ValueError) as e:
                return error_result(e)
            js_result = await cdp.evaluate(
                port=resolved_port,
                expression=javascript,
                tab_index=tab_index,
                target_id=target_id,
            )
            if js_result.get("error"):
                return {"success": False, "error": js_result["error"], "target_id": js_result.get("target_id")}
            result = {
                "success": True,
                "result": js_result.get("result"),
                "target_id": js_result.get("target_id"),
            }
        else:
            return {"success": False, "error": f"Unknown action: {action}. Use 'execute_javascript', 'get_text', 'query_dom', or 'get_page_info'."}

        record_args = {
            "pid": pid, "window_id": window_id, "action": action,
            "port": resolved_port, "tab_index": tab_index,
            "target_id": result.get("target_id") or target_id,
        }
        if javascript is not None:
            record_args["javascript"] = javascript
        if css_selector is not None:
            record_args["css_selector"] = css_selector
        if attributes is not None:
            record_args["attributes"] = attributes
        if allow_html:
            record_args["allow_html"] = allow_html
        if allow_javascript:
            record_args["allow_javascript"] = allow_javascript
        await _recording_hook(_recorded_turn("computer_page", record_args, result))
        return result

    # ============================================================
    # RECORDING TOOLS
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_recording(
        action: str,
        output_dir: Optional[str] = None,
        capture_screenshots: bool = True,
        screenshot_every_n: int = 1,
    ) -> Any:
        """
        Manage recording tool invocations to turn folders.

        When started, every action tool call (click, type, scroll, etc.)
        writes a turn-NNNNN/ folder to output_dir containing action.json,
        screenshot.png, and click.png (for click actions).

        Args:
            action: start|stop|status.
            output_dir: Directory for turn folders (required when action=start).
            capture_screenshots: Include screenshots in turn folders.
            screenshot_every_n: Capture one screenshot every N turns.

        Returns:
            Dictionary with recording state summary.
        """
        from computer_server.recording.session import RecordingSession
        from computer_server.services.validation_service import (
            error_result,
            validate_choice,
            validate_int_range,
        )

        try:
            normalized_action = validate_choice("action", action, {"start", "stop", "status"})
            screenshot_every_n = validate_int_range("screenshot_every_n", screenshot_every_n, 1, 1000)
        except ValueError as e:
            return error_result(e)

        if normalized_action == "status":
            session = context.recording_session
            if session is None:
                return {"active": False, "turn_count": 0}
            return session.state

        if normalized_action == "start":
            if not output_dir:
                return {"success": False, "error": "output_dir is required when action=start"}
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

        session = context.recording_session
        if session is not None:
            summary = session.stop()
            context.recording_session = None
            return {"success": True, **summary}
        return {"success": True, "message": "Recording was not active"}

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
        from computer_server.services.replay_resolver import resolve_replay_args
        from computer_server.services.validation_service import error_result, validate_int_range

        try:
            delay_ms = validate_int_range("delay_ms", delay_ms, 0, 60000)
        except ValueError as e:
            return error_result(e)

        recording_dir = os.path.expanduser(recording_dir)

        tool_map = await _get_tool_map(mcp)

        async def tool_executor(
            tool_name: str,
            args: Dict[str, Any],
            window_ref: Optional[Dict[str, Any]] = None,
            element_ref: Optional[Dict[str, Any]] = None,
        ) -> Any:
            if tool_name not in tool_map:
                return {"success": False, "error": f"Unknown tool: {tool_name}"}

            resolved = resolve_replay_args(tool_name, args, window_ref=window_ref)
            if element_ref is not None and tool_name in {
                "computer_click",
                "computer_perform_secondary_action",
                "computer_drag",
                "computer_scroll",
                "computer_set_value",
            }:
                refresh_pid = resolved.get("pid") or element_ref.get("pid")
                refresh_window_id = resolved.get("window_id") or element_ref.get("window_id")
                if refresh_pid is not None and refresh_window_id is not None:
                    try:
                        await computer_get_window_state(
                            pid=int(refresh_pid),
                            window_id=int(refresh_window_id),
                            query=element_ref.get("name") or None,
                            include_all_nodes=True,
                        )
                    except Exception:
                        logger.debug("Replay element cache refresh failed", exc_info=True)
                resolved = resolve_replay_args(
                    tool_name,
                    resolved,
                    element_ref=element_ref,
                )

            try:
                return await tool_map[tool_name](**resolved)
            except Exception as e:
                return {"success": False, "error": str(e)}

        previous_suppressed = context.recording_suppressed
        context.recording_suppressed = True
        try:
            replay_result = await _replay(
                recording_dir=recording_dir,
                tool_executor=tool_executor,
                delay_ms=delay_ms,
                stop_on_error=stop_on_error,
            )
            release_result = await _release_tracked_inputs()
            if (
                release_result.get("released_keys")
                or release_result.get("released_mouse_buttons")
                or release_result.get("errors")
            ):
                replay_result["release_all_inputs"] = release_result
            return replay_result
        finally:
            await _release_tracked_inputs()
            _clear_learning_input_suppression()
            context.recording_suppressed = previous_suppressed

    # ============================================================
    # LEARNING ACTIONS — human user recording with UIA enrichment
    # ============================================================

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": True})
    async def computer_learn(
        action: str,
        task_description: Optional[str] = None,
        output_dir: Optional[str] = None,
    ) -> Any:
        """Manage human workflow learning: start, stop, or status.

        Start records keyboard typing, mouse clicks, and scroll events enriched
        with UI Automation (UIA) element metadata. Stop returns the action
        sequence. Status returns current listener state.

        Args:
            action: start|stop|status.
            task_description: Description for action=start.
            output_dir: Directory for saved results on action=start.
        """
        from computer_server.services.validation_service import error_result, validate_choice

        try:
            normalized_action = validate_choice("action", action, {"start", "stop", "status"})
        except ValueError as e:
            return error_result(e)

        if normalized_action == "status":
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

        if normalized_action == "start":
            try:
                from computer_server.learning.recorder import LearningRecorder
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
                               "Call computer_learn(action='stop') when done.",
                },
            }

        recorder = context.learning_recorder
        if recorder is None:
            return {
                "success": False,
                "error": "No learning session is active",
                "suggestion": "Call computer_learn(action='start') first to begin recording.",
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

    # Pre-warm SOM OmniParser in background so first get_window_state is fast.
    # Skip when CUA_SKIP_SOM_WARMUP=1 (e.g. one-shot test invocations where
    # the heavy ML imports would contend with the asyncio event loop for the
    # GIL and import lock, causing tool-call timeouts).
    if not _LIGHTWEIGHT_MCP_IMPORT and not os.environ.get("CUA_SKIP_SOM_WARMUP"):
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
