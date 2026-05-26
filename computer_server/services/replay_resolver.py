"""Replay argument resolution for stale windows and moved coordinates."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from computer_server.services.window_identity_service import resolve_live_window

logger = logging.getLogger(__name__)

REPLAY_META_KEYS = frozenset({"window_title", "process_name"})
REPLAY_COORD_TOOLS = frozenset({"computer_click", "computer_drag", "computer_scroll"})
REPLAY_WINDOW_TOOLS = frozenset({"computer_activate_window"})


def resolve_replay_args(
    tool_name: str,
    args: Dict[str, Any],
    window_ref: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return tool args adjusted for current window position.

    The recorded args are never mutated. Replay-only metadata keys are stripped
    before the result is passed to the tool implementation.
    """
    resolved = dict(args)

    if window_ref and tool_name in REPLAY_COORD_TOOLS:
        resolved_hwnd = resolve_live_window(window_ref)
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

            ref_wid = window_ref.get("window_id")
            if ref_wid is not None and hwnd != int(ref_wid):
                resolved["window_id"] = str(hwnd)
                logger.info(
                    "Replay resolved stale window_id %s -> %s for %s",
                    ref_wid,
                    hwnd,
                    tool_name,
                )
        else:
            logger.warning(
                "Replay: could not resolve window for %s (window_ref=%s)",
                tool_name,
                window_ref,
            )

    wid = resolved.get("window_id")
    if wid is not None and tool_name in REPLAY_WINDOW_TOOLS:
        resolved_hwnd = resolve_live_window(resolved)
        if resolved_hwnd is not None:
            hwnd, _rect = resolved_hwnd
            if hwnd != int(wid):
                resolved["window_id"] = str(hwnd)
                logger.info("Replay resolved stale window_id %s -> %s", wid, hwnd)
        else:
            logger.warning(
                "Replay: could not resolve window for %s (args=%s)",
                tool_name,
                {k: resolved.get(k) for k in ("window_id", "process_name", "window_title")},
            )

    for key in REPLAY_META_KEYS:
        resolved.pop(key, None)

    return resolved
