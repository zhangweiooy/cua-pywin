"""Replay argument resolution for stale windows and moved coordinates."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from computer_server.services.window_identity_service import resolve_live_window

logger = logging.getLogger(__name__)

REPLAY_META_KEYS = frozenset({"window_title", "process_name"})
REPLAY_COORD_TOOLS = frozenset({
    "computer_click",
    "computer_perform_secondary_action",
    "computer_drag",
    "computer_scroll",
    "computer_mouse",
})
REPLAY_WINDOW_ID_ARG_TOOLS = frozenset({
    "computer_click",
    "computer_perform_secondary_action",
    "computer_drag",
    "computer_scroll",
})
REPLAY_WINDOW_TOOLS = frozenset({"computer_manage_window"})
REPLAY_TARGET_WINDOW_TOOLS = frozenset({"computer_type", "computer_press_key"})
REPLAY_ELEMENT_TOOLS = frozenset({
    "computer_click",
    "computer_perform_secondary_action",
    "computer_drag",
    "computer_scroll",
    "computer_set_value",
})


def resolve_replay_args(
    tool_name: str,
    args: Dict[str, Any],
    window_ref: Optional[Dict[str, Any]] = None,
    element_ref: Optional[Dict[str, Any]] = None,
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
            if resolved.get("x") is not None and resolved.get("y") is not None:
                resolved["x"] = int(rect[0] + (resolved["x"] - ref_ox))
                resolved["y"] = int(rect[1] + (resolved["y"] - ref_oy))
            if (
                resolved.get("start_x") is not None
                and resolved.get("start_y") is not None
                and resolved.get("end_x") is not None
                and resolved.get("end_y") is not None
            ):
                resolved["start_x"] = int(rect[0] + (resolved["start_x"] - ref_ox))
                resolved["start_y"] = int(rect[1] + (resolved["start_y"] - ref_oy))
                resolved["end_x"] = int(rect[0] + (resolved["end_x"] - ref_ox))
                resolved["end_y"] = int(rect[1] + (resolved["end_y"] - ref_oy))

            ref_wid = window_ref.get("window_id")
            if ref_wid is not None and hwnd != int(ref_wid) and tool_name in REPLAY_WINDOW_ID_ARG_TOOLS:
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

    if window_ref and tool_name in REPLAY_TARGET_WINDOW_TOOLS:
        resolved_hwnd = resolve_live_window(window_ref)
        if resolved_hwnd is not None:
            hwnd, _rect = resolved_hwnd
            resolved["window_id"] = hwnd
            resolved.pop("window", None)
            try:
                from computer_server.services.window_identity_service import build_window

                public_window = build_window(hwnd)
                if public_window is not None and public_window.get("pid") is not None:
                    resolved["pid"] = public_window["pid"]
                else:
                    resolved.pop("pid", None)
            except Exception:
                resolved.pop("pid", None)
            logger.info(
                "Replay resolved target window to %s for %s",
                hwnd,
                tool_name,
            )
        else:
            logger.warning(
                "Replay: could not resolve target window for %s (window_ref=%s)",
                tool_name,
                window_ref,
            )

    if element_ref and tool_name in REPLAY_ELEMENT_TOOLS:
        resolved = resolve_replay_element_args(tool_name, resolved, element_ref)

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


def resolve_replay_element_args(
    tool_name: str,
    args: Dict[str, Any],
    element_ref: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve recorded element metadata to a current cached element."""
    resolved = dict(args)

    pid = _coerce_int(resolved.get("pid") or element_ref.get("pid"))
    window_id = _coerce_int(resolved.get("window_id") or element_ref.get("window_id"))
    if pid is None or window_id is None:
        return resolved

    match = find_replay_element(pid, window_id, element_ref)
    if match is None:
        logger.info(
            "Replay: no semantic element match for %s (pid=%s, window_id=%s, ref=%s)",
            tool_name,
            pid,
            window_id,
            {
                "name": element_ref.get("name"),
                "control_type": element_ref.get("control_type"),
                "source": element_ref.get("source"),
            },
        )
        return resolved

    resolved["pid"] = pid
    resolved["window_id"] = window_id
    resolved["element_index"] = match.element_index
    if tool_name == "computer_click":
        resolved.pop("x", None)
        resolved.pop("y", None)
        resolved.pop("from_zoom", None)
        resolved.pop("zoom_id", None)
    elif tool_name == "computer_scroll":
        resolved.pop("x", None)
        resolved.pop("y", None)
    elif tool_name == "computer_drag":
        # Preserve end_x/end_y as fallback target coordinates; drag starts from
        # the relocated element center because computer_drag prioritizes
        # element_index when present.
        resolved.pop("start_x", None)
        resolved.pop("start_y", None)

    logger.info(
        "Replay resolved %s element_index %s -> %s for pid=%s window_id=%s",
        tool_name,
        element_ref.get("recorded_element_index"),
        match.element_index,
        pid,
        window_id,
    )
    return resolved


def find_replay_element(pid: int, window_id: int, element_ref: Dict[str, Any]):
    """Find the best cached element matching recorded metadata."""
    from computer_server.handlers.element_cache import get_element_cache

    cache = get_element_cache()
    elements = cache.get_elements(pid, window_id)
    if not elements:
        return None

    best = None
    best_score = 0
    for element in elements:
        score = _score_element_match(element, element_ref)
        if score > best_score:
            best = element
            best_score = score

    return best if best_score >= 6 else None


def _score_element_match(element: Any, element_ref: Dict[str, Any]) -> int:
    score = 0
    ref_name = _norm(element_ref.get("name"))
    ref_value = _norm(element_ref.get("value"))
    ref_type = _norm(element_ref.get("control_type"))
    ref_source = _norm(element_ref.get("source"))
    ref_actions = set(element_ref.get("actions") or [])

    name = _norm(getattr(element, "name", None))
    value = _norm(getattr(element, "value", None))
    control_type = _norm(getattr(element, "control_type", None))
    source = _norm(getattr(element, "source", None))
    actions = set(getattr(element, "actions", None) or [])

    if ref_source and source == ref_source:
        score += 1
    if ref_type and control_type == ref_type:
        score += 3
    if ref_name and name == ref_name:
        score += 6
    elif ref_name and name and (ref_name in name or name in ref_name):
        score += 3
    if ref_value and value == ref_value:
        score += 4
    elif ref_value and value and (ref_value in value or value in ref_value):
        score += 2
    if ref_actions and actions:
        score += min(2, len(ref_actions.intersection(actions)))

    ref_center = _center_from_ref(element_ref)
    if ref_center is not None:
        dx = abs(int(getattr(element, "center_x", 0)) - ref_center[0])
        dy = abs(int(getattr(element, "center_y", 0)) - ref_center[1])
        distance = dx + dy
        if distance <= 10:
            score += 3
        elif distance <= 50:
            score += 1

    return score


def _center_from_ref(element_ref: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    x = _coerce_int(element_ref.get("center_x"))
    y = _coerce_int(element_ref.get("center_y"))
    if x is not None and y is not None:
        return x, y
    bbox = element_ref.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        left = _coerce_int(bbox[0])
        top = _coerce_int(bbox[1])
        right = _coerce_int(bbox[2])
        bottom = _coerce_int(bbox[3])
        if None not in (left, top, right, bottom):
            return (int(left + right) // 2, int(top + bottom) // 2)
    return None


def _coerce_int(value: Any) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _norm(value: Any) -> str:
    return str(value or "").strip().lower()
