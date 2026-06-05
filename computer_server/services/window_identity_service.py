"""Shared window identity and stale-HWND resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional, Tuple

from computer_server.services.validation_service import ValidationError


@dataclass(frozen=True)
class ResolvedWindow:
    """Normalized target window identity."""

    pid: Optional[int]
    window_id: Optional[int]
    window: Optional[Dict[str, Any]]


def _coerce_optional_int(name: str, value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValidationError(f"{name} must be an integer")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be an integer") from None


def _window_value(window: Mapping[str, Any], *names: str) -> Any:
    for name in names:
        if name in window and window[name] is not None:
            return window[name]
    return None


def _get_pid_for_window(window_id: Optional[int]) -> Optional[int]:
    if window_id is None:
        return None
    try:
        import win32process

        _, pid = win32process.GetWindowThreadProcessId(int(window_id))
        return int(pid) if pid else None
    except Exception:
        return None


def _get_process_name(pid: Optional[int]) -> str:
    if not pid:
        return ""
    try:
        import psutil

        return psutil.Process(int(pid)).name() or ""
    except Exception:
        return ""


def _get_window_title(window_id: Optional[int]) -> str:
    if window_id is None:
        return ""
    try:
        import win32gui

        return win32gui.GetWindowText(int(window_id)) or ""
    except Exception:
        return ""


def _get_window_class(window_id: Optional[int]) -> str:
    if window_id is None:
        return ""
    try:
        import win32gui

        return win32gui.GetClassName(int(window_id)) or ""
    except Exception:
        return ""


def _get_window_rect(window_id: Optional[int]) -> Optional[Tuple[int, int, int, int]]:
    if window_id is None:
        return None
    try:
        import win32gui

        rect = win32gui.GetWindowRect(int(window_id))
        if rect:
            return int(rect[0]), int(rect[1]), int(rect[2]), int(rect[3])
    except Exception:
        return None
    return None


def _is_window_visible(window_id: Optional[int]) -> Optional[bool]:
    if window_id is None:
        return None
    try:
        import win32gui

        return bool(win32gui.IsWindowVisible(int(window_id)))
    except Exception:
        return None


def _foreground_window_id() -> Optional[int]:
    try:
        import win32gui

        hwnd = win32gui.GetForegroundWindow()
        return int(hwnd) if hwnd else None
    except Exception:
        return None


def is_live_window(window_id: Any) -> bool:
    """Return whether ``window_id`` currently identifies a live HWND."""
    wid = _coerce_optional_int("id", window_id)
    if wid is None or wid <= 0:
        return False
    try:
        import win32gui

        return bool(win32gui.IsWindow(wid))
    except Exception:
        return False


def rehydrate_window(window_id: Any, app: Optional[str] = None) -> Dict[str, Any]:
    """Rebuild a public canonical window object for a currently open HWND."""
    wid = _coerce_optional_int("id", window_id)
    if wid is None:
        raise ValidationError("id is required")
    if app is not None and not isinstance(app, str):
        raise ValidationError("app must be a string")
    if not is_live_window(wid):
        return {
            "success": False,
            "error": f"Window {wid} is not currently available",
            "suggestion": "Call computer_list_apps or computer_list_windows to select a current window.",
        }

    window = build_window(wid)
    if window is None:
        return {
            "success": False,
            "error": f"Window {wid} could not be resolved",
            "suggestion": "Call computer_list_apps or computer_list_windows to select a current window.",
        }
    if app and "app" not in window:
        window["app"] = app

    result: Dict[str, Any] = {"success": True, "window": window}
    for key in ("id", "window_id", "pid", "title", "app"):
        if key in window:
            result[key] = window[key]
    return result


def normalize_window_input(window: Optional[Mapping[str, Any]]) -> tuple[Optional[int], Optional[int]]:
    """Extract ``(pid, window_id)`` from a user-facing window object."""
    if window is None:
        return None, None
    if not isinstance(window, Mapping):
        raise ValidationError("window must be an object")

    wid_from_id = _coerce_optional_int("window.id", _window_value(window, "id"))
    wid_from_window_id = _coerce_optional_int(
        "window.window_id",
        _window_value(window, "window_id"),
    )
    if (
        wid_from_id is not None
        and wid_from_window_id is not None
        and wid_from_id != wid_from_window_id
    ):
        raise ValidationError(
            f"window.id/window.window_id mismatch: {wid_from_id} != {wid_from_window_id}"
        )

    pid = _coerce_optional_int("window.pid", _window_value(window, "pid"))
    window_id = wid_from_id if wid_from_id is not None else wid_from_window_id
    return pid, window_id


def resolve_window_input(
    *,
    window: Optional[Mapping[str, Any]] = None,
    pid: Optional[int] = None,
    window_id: Optional[int] = None,
) -> ResolvedWindow:
    """Resolve top-level ``pid/window_id`` and ``window`` inputs.

    The ``window`` object is additive.  Top-level values remain valid,
    but when both forms are present they must identify the same target.
    """
    top_level_pid = _coerce_optional_int("pid", pid)
    top_level_window_id = _coerce_optional_int("window_id", window_id)
    window_pid, window_hwnd = normalize_window_input(window)

    if top_level_pid is not None and window_pid is not None and top_level_pid != window_pid:
        raise ValidationError(
            f"window.pid mismatch: window has pid={window_pid}, got pid={top_level_pid}"
        )
    if (
        top_level_window_id is not None
        and window_hwnd is not None
        and top_level_window_id != window_hwnd
    ):
        raise ValidationError(
            "window.window_id mismatch: "
            f"window has window_id={window_hwnd}, got window_id={top_level_window_id}"
        )

    resolved_pid = top_level_pid if top_level_pid is not None else window_pid
    resolved_window_id = (
        top_level_window_id if top_level_window_id is not None else window_hwnd
    )
    if resolved_pid is None and resolved_window_id is not None:
        resolved_pid = _get_pid_for_window(resolved_window_id)
    return ResolvedWindow(
        pid=resolved_pid,
        window_id=resolved_window_id,
        window=build_window(
            resolved_window_id,
            pid=resolved_pid,
            app=_window_value(window, "app", "process_name") if window else None,
            title=_window_value(window, "title", "window_title") if window else None,
        )
        if resolved_window_id is not None
        else None,
    )


def build_window(
    window_id: Optional[int],
    *,
    pid: Optional[int] = None,
    title: Optional[str] = None,
    app: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Build the public canonical window object."""
    wid = _coerce_optional_int("window_id", window_id)
    if wid is None:
        return None

    resolved_pid = _coerce_optional_int("pid", pid)
    if resolved_pid is None:
        resolved_pid = _get_pid_for_window(wid)

    process_name = app or _get_process_name(resolved_pid)
    window_title = title or _get_window_title(wid)
    rect = _get_window_rect(wid)
    visible = _is_window_visible(wid)
    foreground = _foreground_window_id()

    result: Dict[str, Any] = {
        "id": wid,
        "window_id": wid,
    }
    if resolved_pid is not None:
        result["pid"] = resolved_pid
    if process_name:
        result["app"] = process_name
        result["process_name"] = process_name
    if window_title:
        result["title"] = window_title
    class_name = _get_window_class(wid)
    if class_name:
        result["class_name"] = class_name
    if rect is not None:
        left, top, right, bottom = rect
        minimized = left < -32000 or top < -32000
        result["bounds"] = {
            "x": left,
            "y": top,
            "width": int(right - left),
            "height": int(bottom - top),
        }
        result["state"] = {
            "visible": bool(visible) if visible is not None else None,
            "minimized": minimized,
            "foreground": foreground == wid if foreground is not None else None,
        }
    return result


def add_window_field(record: Dict[str, Any]) -> Dict[str, Any]:
    """Attach a canonical ``window`` object to a window-shaped record."""
    window_id = record.get("window_id", record.get("id"))
    pid = record.get("pid")
    title = record.get("title", record.get("name"))
    app = record.get("app", record.get("process_name"))
    window = build_window(window_id, pid=pid, title=title, app=app)
    if window is not None:
        record["window"] = window
        for key in (
            "pid",
            "app",
            "process_name",
            "title",
            "class_name",
            "bounds",
            "state",
        ):
            if key in window and key not in record:
                record[key] = window[key]
    return record


def _enrich_window_ref(base: Dict[str, Any]) -> Dict[str, Any]:
    """Add process/title/size metadata to a window ref when available."""
    wid = base.get("window_id")
    if wid is None:
        return base

    base.setdefault("version", 1)

    pid_val = base.get("pid")
    if pid_val is None:
        pid_val = _get_pid_for_window(wid)
        if pid_val is not None:
            base["pid"] = pid_val

    process_name = base.get("process_name") or _get_process_name(pid_val)
    if process_name:
        base["process_name"] = process_name
        base.setdefault("app", process_name)

    title = base.get("window_title") or _get_window_title(wid)
    if title:
        base["window_title"] = title
        base.setdefault("title", title)

    class_name = base.get("class_name") or _get_window_class(wid)
    if class_name:
        base["class_name"] = class_name

    rect = _get_window_rect(wid)
    if rect:
        base.setdefault("origin_x", int(rect[0]))
        base.setdefault("origin_y", int(rect[1]))
        base["width"] = int(rect[2] - rect[0])
        base["height"] = int(rect[3] - rect[1])

    return base


def get_window_ref_from_point(x: int, y: int) -> Optional[Dict[str, Any]]:
    """Resolve the top-level window at a screen point."""
    try:
        import ctypes
        import win32gui

        hwnd = win32gui.WindowFromPoint((x, y))
        if not hwnd:
            return None
        root = ctypes.windll.user32.GetAncestor(hwnd, 2)  # GA_ROOT
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


def build_window_ref(
    pid: Optional[int],
    window_id: Optional[int],
    x: int,
    y: int,
) -> Optional[Dict[str, Any]]:
    """Build a replay-friendly window reference for an action.

    Resolution order:
    1. Element cache origin for the intended (pid, window_id).
    2. Current GetWindowRect(window_id).
    3. Top-level window at the action point.
    """
    base: Optional[Dict[str, Any]] = None

    if pid is not None and window_id is not None:
        try:
            from computer_server.handlers.element_cache import get_element_cache

            origin = get_element_cache().get_window_origin_optional(pid, window_id)
            if origin is not None:
                base = {
                    "version": 1,
                    "pid": pid,
                    "window_id": window_id,
                    "origin_x": origin[0],
                    "origin_y": origin[1],
                }
        except Exception:
            pass

        if base is None:
            try:
                import win32gui

                rect = win32gui.GetWindowRect(int(window_id))
                if rect:
                    base = {
                        "version": 1,
                        "pid": pid,
                        "window_id": window_id,
                        "origin_x": rect[0],
                        "origin_y": rect[1],
                    }
            except Exception:
                pass

    if base is None:
        base = get_window_ref_from_point(x, y)

    if base is not None:
        return _enrich_window_ref(base)
    return None


def resolve_live_window(source: Dict[str, Any]) -> Optional[Tuple[int, tuple]]:
    """Resolve a live HWND and rect from recorded window metadata.

    Priority:
    1. Original window_id if still valid.
    2. Visible window with matching process_name.
    3. Visible window whose title contains recorded window_title.
    """
    ref_wid = source.get("window_id")
    if ref_wid is not None:
        try:
            import win32gui

            if win32gui.IsWindow(int(ref_wid)):
                rect = win32gui.GetWindowRect(int(ref_wid))
                if rect:
                    return int(ref_wid), rect
        except Exception:
            pass

    proc_name = source.get("process_name", "")
    if proc_name:
        try:
            import psutil
            import win32gui
            import win32process

            candidates: list[int] = []

            def _by_proc(hwnd, ctx):
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    if (
                        pid
                        and psutil.Process(pid).name().lower() == proc_name.lower()
                        and win32gui.IsWindowVisible(hwnd)
                    ):
                        ctx.append(hwnd)
                except Exception:
                    pass

            win32gui.EnumWindows(_by_proc, candidates)
            if candidates:
                hwnd = candidates[0]
                rect = win32gui.GetWindowRect(hwnd)
                if rect:
                    return hwnd, rect
        except Exception:
            pass

    title = source.get("window_title", "")
    if title:
        try:
            import win32gui

            candidates: list[int] = []

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
                    return hwnd, rect
        except Exception:
            pass

    return None
