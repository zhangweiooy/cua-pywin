"""Shared window identity and stale-HWND resolution helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from computer_server.services.validation_service import ValidationError


WINDOW_SEARCH_ALIASES: tuple[tuple[str, ...], ...] = (
    ("微信", "weixin", "wechat"),
    ("钉钉", "dingtalk", "dingding"),
)


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
        import win32gui
        import win32process

        hwnd = int(window_id)
        if not win32gui.IsWindow(hwnd):
            return None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
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


def _is_meaningful_window(window_id: int) -> bool:
    """Conservative top-level window filter shared by discovery paths."""
    try:
        import win32con
        import win32gui

        if not win32gui.IsWindow(int(window_id)):
            return False
        style = win32gui.GetWindowLong(int(window_id), win32con.GWL_STYLE)
        if style & win32con.WS_CHILD:
            return False
        rect = win32gui.GetWindowRect(int(window_id))
        if rect[2] - rect[0] <= 0 or rect[3] - rect[1] <= 0:
            return False
        return True
    except Exception:
        return True


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


def is_window_minimized(
    window_id: Optional[int],
    rect: Optional[Tuple[int, int, int, int]] = None,
) -> Optional[bool]:
    """Return the HWND minimized state, with iconic coordinates as fallback."""
    if window_id is None:
        return None
    try:
        import win32gui

        return bool(win32gui.IsIconic(int(window_id)))
    except Exception:
        if rect is None:
            rect = _get_window_rect(window_id)
        if rect is None:
            return None
        return bool(rect[0] <= -32000 or rect[1] <= -32000)


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


def _fold_search_text(value: Any) -> str:
    return str(value or "").strip().casefold()


def _alias_expanded_terms(value: Any) -> set[str]:
    base = _fold_search_text(value)
    if not base:
        return set()
    terms = {base}
    path_name = Path(base).name
    if path_name:
        terms.add(path_name)
        stem = Path(path_name).stem
        if stem:
            terms.add(stem)
    for group in WINDOW_SEARCH_ALIASES:
        if any(term and term in terms for term in group):
            terms.update(group)
    return {term for term in terms if term}


def _record_search_terms(record: Mapping[str, Any]) -> set[str]:
    terms: set[str] = set()
    for key in (
        "title",
        "app",
        "process_name",
        "class_name",
        "id",
        "window_id",
        "pid",
    ):
        terms.update(_alias_expanded_terms(record.get(key)))
    return terms


def _record_matches_terms(
    record: Mapping[str, Any],
    filter_value: Optional[str],
    fields: tuple[str, ...],
) -> bool:
    terms = _alias_expanded_terms(filter_value)
    if not terms:
        return True
    haystack_terms: set[str] = set()
    for field in fields:
        haystack_terms.update(_alias_expanded_terms(record.get(field)))
    return any(needle in haystack for needle in terms for haystack in haystack_terms)


def _record_matches_query(record: Mapping[str, Any], query: Optional[str]) -> bool:
    terms = _alias_expanded_terms(query)
    if not terms:
        return True
    haystack_terms = _record_search_terms(record)
    return any(needle in haystack for needle in terms for haystack in haystack_terms)


def enumerate_window_records(
    *,
    pid: Optional[int] = None,
    on_screen_only: bool = False,
    require_meaningful: bool = True,
) -> list[Dict[str, Any]]:
    """Enumerate Win32 top-level windows with normalized metadata."""
    try:
        import win32con
        import win32gui
        import win32process
    except Exception:
        return []

    pid_filter = _coerce_optional_int("pid", pid)
    process_cache: dict[int, str] = {}
    records: list[Dict[str, Any]] = []

    def _process_name_cached(process_id: int) -> str:
        if process_id not in process_cache:
            process_cache[process_id] = _get_process_name(process_id)
        return process_cache[process_id]

    def _visit(hwnd: int, _extra: Any) -> bool:
        try:
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            if not (style & win32con.WS_VISIBLE):
                return True
            if require_meaningful and not _is_meaningful_window(hwnd):
                return True
            _, win_pid = win32process.GetWindowThreadProcessId(hwnd)
            win_pid = int(win_pid)
            if pid_filter is not None and win_pid != pid_filter:
                return True
            rect = win32gui.GetWindowRect(hwnd)
            width = int(rect[2] - rect[0])
            height = int(rect[3] - rect[1])
            if width <= 0 or height <= 0:
                return True
            visible = bool(win32gui.IsWindowVisible(hwnd))
            minimized = is_window_minimized(hwnd, rect)
            is_on_screen = bool(visible and minimized is not True)
            if on_screen_only and not is_on_screen:
                return True
            process_name = _process_name_cached(win_pid)
            record: Dict[str, Any] = {
                "id": int(hwnd),
                "window_id": int(hwnd),
                "pid": win_pid,
                "title": win32gui.GetWindowText(hwnd) or "",
                "x": int(rect[0]),
                "y": int(rect[1]),
                "width": width,
                "height": height,
                "is_on_screen": is_on_screen,
                "minimized": minimized,
            }
            if process_name:
                record["app"] = process_name
                record["process_name"] = process_name
            class_name = _get_window_class(hwnd)
            if class_name:
                record["class_name"] = class_name
            records.append(record)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_visit, None)
    except Exception:
        return []
    return records


def filter_window_records(
    records: list[Dict[str, Any]],
    *,
    app: Optional[str] = None,
    title: Optional[str] = None,
    query: Optional[str] = None,
) -> list[Dict[str, Any]]:
    """Apply the shared app/title/query matching contract."""
    result: list[Dict[str, Any]] = []
    for record in records:
        if not _record_matches_terms(record, app, ("app", "process_name", "title")):
            continue
        if not _record_matches_terms(record, title, ("title",)):
            continue
        if not _record_matches_query(record, query):
            continue
        result.append(record)
    return result


def _window_match_score(
    record: Mapping[str, Any],
    *,
    pid: Optional[int] = None,
    process_name: Optional[str] = None,
    app_hint: Optional[str] = None,
    title_hint: Optional[str] = None,
) -> int:
    score = 0
    if pid is not None and record.get("pid") == pid:
        score = max(score, 100)

    process_terms = _alias_expanded_terms(process_name)
    app_terms = _alias_expanded_terms(app_hint)
    title_terms = _alias_expanded_terms(title_hint)
    record_process_terms = _record_search_terms(
        {
            "app": record.get("app"),
            "process_name": record.get("process_name"),
        }
    )
    record_title_terms = _alias_expanded_terms(record.get("title"))
    record_terms = _record_search_terms(record)

    if process_terms and process_terms & record_process_terms:
        score = max(score, 90)
    elif process_terms and any(
        needle in haystack
        for needle in process_terms
        for haystack in record_process_terms
    ):
        score = max(score, 85)
    if app_terms and app_terms & record_process_terms:
        score = max(score, 80)
    elif app_terms and any(
        needle in haystack
        for needle in app_terms
        for haystack in record_terms
    ):
        score = max(score, 70)
    if title_terms and any(
        needle in haystack
        for needle in title_terms
        for haystack in record_title_terms
    ):
        score = max(score, 65)
    return score


def find_best_window_record(
    *,
    pid: Optional[int] = None,
    process_name: Optional[str] = None,
    app_hint: Optional[str] = None,
    title_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find the best live top-level window using the shared matching contract."""
    expected_pid = _coerce_optional_int("pid", pid)
    scored: list[tuple[int, int, int, int, Dict[str, Any]]] = []
    for record in enumerate_window_records():
        score = _window_match_score(
            record,
            pid=expected_pid,
            process_name=process_name,
            app_hint=app_hint,
            title_hint=title_hint,
        )
        if score <= 0:
            continue
        area = int(record.get("width", 0) or 0) * int(record.get("height", 0) or 0)
        has_title = 1 if record.get("title") else 0
        scored.append((score, has_title, area, int(record["window_id"]), record))
    if not scored:
        return None
    return max(scored, key=lambda item: item[:4])[4]


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
    result: Dict[str, Any] = {"success": True, "window": window}
    title = _get_window_title(wid)
    process_name = app or _get_process_name(window.get("pid"))
    if title:
        result["title"] = title
    if process_name:
        result["app"] = process_name
    return result


def find_app_window(
    *,
    pid: Optional[int] = None,
    process_name: Optional[str] = None,
    app_hint: Optional[str] = None,
    title_hint: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Find the best live top-level window for a launched application."""
    record = find_best_window_record(
        pid=pid,
        process_name=process_name,
        app_hint=app_hint,
        title_hint=title_hint,
    )
    if record is None:
        return None
    return build_window(
        record["window_id"],
        pid=record.get("pid"),
        title=record.get("title"),
        app=record.get("process_name") or record.get("app"),
    )


def normalize_window_input(window: Optional[Mapping[str, Any]]) -> tuple[Optional[int], Optional[int]]:
    """Extract ``(pid, window_id)`` from a user-facing window object."""
    if window is None:
        return None, None
    if not isinstance(window, Mapping):
        raise ValidationError("window must be an object")

    if "window_id" in window or "windowId" in window:
        raise ValidationError(
            "window only accepts canonical fields: window.id and window.pid"
        )
    wid_from_id = _coerce_optional_int("window.id", _window_value(window, "id"))
    pid = _coerce_optional_int("window.pid", _window_value(window, "pid"))
    return pid, wid_from_id


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
    elif resolved_pid is not None and resolved_window_id is not None:
        live_pid = _get_pid_for_window(resolved_window_id)
        if live_pid is not None and live_pid != resolved_pid:
            raise ValidationError(
                "window.pid target mismatch: "
                f"window {resolved_window_id} belongs to pid={live_pid}, "
                f"got pid={resolved_pid}"
            )
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

    result: Dict[str, Any] = {"id": wid}
    if resolved_pid is not None:
        result["pid"] = resolved_pid
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

    record = find_best_window_record(
        process_name=source.get("process_name") or source.get("app"),
        app_hint=source.get("app"),
        title_hint=source.get("window_title") or source.get("title"),
    )
    if record is not None:
        try:
            import win32gui

            hwnd = int(record["window_id"])
            rect = win32gui.GetWindowRect(hwnd)
            if rect:
                return hwnd, rect
        except Exception:
            pass

    return None
