"""Lightweight post-action observation delta helpers."""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional


TEXT_KEYS = (
    "name",
    "title",
    "value",
    "content",
    "text",
    "normalized_label",
    "raw_caption",
    "icon_label",
)


def _node_text(node: Dict[str, Any]) -> str:
    for key in TEXT_KEYS:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    metadata = node.get("metadata")
    if isinstance(metadata, dict):
        for key in TEXT_KEYS:
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _node_identity(node: Dict[str, Any]) -> str:
    metadata = node.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    source = str(node.get("source") or metadata.get("source") or "unknown")
    element_index = node.get("element_index")
    if isinstance(element_index, int):
        return f"{source}:index:{element_index}"

    bbox = node.get("bbox")
    control_type = node.get("control_type") or node.get("role") or node.get("type") or ""
    text = _node_text(node)
    return f"{source}:shape:{control_type}:{text}:{bbox}"


def _node_summary(node: Dict[str, Any]) -> Dict[str, Any]:
    metadata = node.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    summary: Dict[str, Any] = {
        "element_index": node.get("element_index"),
        "source": node.get("source") or metadata.get("source"),
        "control_type": node.get("control_type") or node.get("role") or node.get("type"),
        "text": _node_text(node),
        "bbox": node.get("bbox"),
    }
    confidence = node.get("confidence")
    if confidence is not None:
        summary["confidence"] = confidence
    return {key: value for key, value in summary.items() if value not in (None, "")}


def _node_map(nodes: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    mapped: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        if isinstance(node, dict):
            mapped[_node_identity(node)] = node
    return mapped


def _int_value(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except Exception:
        return 0


def _capture_error(capture: Optional[Dict[str, Any]], label: str) -> Optional[str]:
    if not isinstance(capture, dict):
        return f"{label} capture is unavailable"
    if capture.get("success") is False:
        return str(capture.get("error") or f"{label} capture failed")
    return None


def build_post_action_delta(
    before_capture: Optional[Dict[str, Any]],
    after_capture: Optional[Dict[str, Any]],
    *,
    max_items: int = 20,
) -> Dict[str, Any]:
    """Return a small, stable summary of UI changes after an action."""
    before_error = _capture_error(before_capture, "before")
    after_error = _capture_error(after_capture, "after")
    if before_error or after_error:
        return {
            "success": False,
            "reason": "capture_unavailable",
            "before_error": before_error,
            "after_error": after_error,
        }

    before = before_capture or {}
    after = after_capture or {}
    before_nodes = _node_map(before.get("nodes") or [])
    after_nodes = _node_map(after.get("nodes") or [])

    before_keys = set(before_nodes)
    after_keys = set(after_nodes)
    added_keys = sorted(after_keys - before_keys)
    removed_keys = sorted(before_keys - after_keys)

    changed_text: List[Dict[str, Any]] = []
    for key in sorted(before_keys & after_keys):
        before_text = _node_text(before_nodes[key])
        after_text = _node_text(after_nodes[key])
        if before_text != after_text:
            changed_text.append(
                {
                    "before": _node_summary(before_nodes[key]),
                    "after": _node_summary(after_nodes[key]),
                }
            )

    before_window = (before.get("pid"), before.get("window_id"))
    after_window = (after.get("pid"), after.get("window_id"))
    return {
        "success": True,
        "before_snapshot_id": before.get("snapshot_id"),
        "after_snapshot_id": after.get("snapshot_id"),
        "before_screenshot_id": before.get("screenshot_id"),
        "after_screenshot_id": after.get("screenshot_id"),
        "window_changed": before_window != after_window,
        "before_window": {"pid": before_window[0], "window_id": before_window[1]},
        "after_window": {"pid": after_window[0], "window_id": after_window[1]},
        "node_count_delta": _int_value(after.get("node_count")) - _int_value(before.get("node_count")),
        "returned_node_count_delta": (
            _int_value(after.get("returned_node_count"))
            - _int_value(before.get("returned_node_count"))
        ),
        "added_node_count": len(added_keys),
        "removed_node_count": len(removed_keys),
        "changed_text_count": len(changed_text),
        "tree_quality_changed": before.get("tree_quality") != after.get("tree_quality"),
        "before_tree_quality": before.get("tree_quality"),
        "after_tree_quality": after.get("tree_quality"),
        "nodes_truncated": bool(before.get("nodes_truncated")) or bool(after.get("nodes_truncated")),
        "added_nodes": [_node_summary(after_nodes[key]) for key in added_keys[:max_items]],
        "removed_nodes": [_node_summary(before_nodes[key]) for key in removed_keys[:max_items]],
        "changed_text": changed_text[:max_items],
        "sample_limit": max_items,
    }
