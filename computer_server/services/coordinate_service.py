"""Coordinate conversion helpers shared by MCP tools and tests."""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple


def get_resize_ratio(
    zoom_metadata: Dict[Tuple[int, int], Dict[str, Any]],
    pid: Optional[int],
    window_id: Optional[int],
) -> float:
    """Return stored screenshot resize ratio for a window."""
    if pid is not None and window_id is not None:
        meta = zoom_metadata.get((pid, window_id), {})
        return float(meta.get("compression_ratio", 1.0))
    return 1.0


def resolve_from_zoom(
    x: int,
    y: int,
    from_zoom: bool = False,
    pid: Optional[int] = None,
    window_id: Optional[int] = None,
) -> Tuple[int, int]:
    """Translate zoom-image coordinates back to screen coordinates."""
    if not from_zoom:
        return x, y
    if pid is None:
        raise ValueError("pid is required when from_zoom=True")
    if window_id is None:
        raise ValueError("window_id is required when from_zoom=True")

    from computer_server.handlers.zoom_registry import get_zoom_registry

    return get_zoom_registry().unscale(pid, window_id, x, y)


def resolve_element_center(
    pid: int,
    window_id: int,
    element_index: int,
) -> Tuple[int, int]:
    """Resolve a cached element center to screen coordinates."""
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


def window_resized_to_screen(
    pid: Optional[int],
    window_id: Optional[int],
    x: int,
    y: int,
    zoom_metadata: Optional[Dict[Tuple[int, int], Dict[str, Any]]] = None,
) -> Tuple[int, int]:
    """Convert resized window-screenshot coordinates to screen coordinates.

    Priority:
    1. ``zoom_metadata[(pid, window_id)]`` — the authoritative record produced
       by the most recent ``get_window_state`` call.  Holds the visible crop
       origin (which differs from real window origin on multi-monitor /
       partially off-screen windows) and the compression ratio.
    2. ``ElementCache.get_window_origin_optional`` — fallback when no metadata
       is recorded.  In this branch resize cannot have happened (no metadata
       means no prior get_window_state), so the ratio is 1.0 by definition.
    """
    if pid is None or window_id is None:
        return x, y

    zoom_metadata = zoom_metadata or {}
    meta = zoom_metadata.get((pid, window_id), {})
    if meta:
        origin_x = meta.get("crop_origin_screen_x", 0)
        origin_y = meta.get("crop_origin_screen_y", 0)
        ratio = float(meta.get("compression_ratio", 1.0))
        return (origin_x + int(x * ratio), origin_y + int(y * ratio))

    from computer_server.handlers.element_cache import get_element_cache

    origin = get_element_cache().get_window_origin_optional(pid, window_id)
    if origin is None:
        return x, y
    return (origin[0] + x, origin[1] + y)
