"""Shared window identity and stale-HWND resolution helpers."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def _enrich_window_ref(base: Dict[str, Any]) -> Dict[str, Any]:
    """Add process/title/size metadata to a window ref when available."""
    wid = base.get("window_id")
    if wid is None:
        return base

    try:
        import psutil
        import win32process

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
