from __future__ import annotations

from typing import Any, Mapping, Sequence


DEFAULT_AGENT_TEXT_LIMIT = 500
MAX_AGENT_TEXT_LIMIT = 20_000


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _node_metadata(node: Mapping[str, Any]) -> Mapping[str, Any]:
    metadata = node.get("metadata")
    if isinstance(metadata, Mapping):
        return metadata
    return {}


def _node_label(node: Mapping[str, Any]) -> str:
    metadata = _node_metadata(node)
    return _first_text(
        node.get("name"),
        node.get("content"),
        node.get("text"),
        node.get("value"),
        node.get("title"),
        node.get("normalized_label"),
        node.get("raw_caption"),
        metadata.get("name"),
        metadata.get("content"),
        metadata.get("text"),
        metadata.get("value"),
        metadata.get("normalized_label"),
        metadata.get("raw_caption"),
        metadata.get("description"),
    )


def _node_role(node: Mapping[str, Any]) -> str:
    return _first_text(
        node.get("control_type"),
        node.get("role"),
        node.get("type"),
        _node_metadata(node).get("control_type"),
        _node_metadata(node).get("role"),
        "node",
    )


def _as_short_sequence(value: Any, *, max_items: int = 4) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    for item in value:
        text = str(item).strip()
        if text:
            result.append(text)
        if len(result) >= max_items:
            break
    return result


def _format_bbox(node: Mapping[str, Any]) -> str:
    bbox = node.get("bbox")
    if isinstance(bbox, Sequence) and not isinstance(bbox, (str, bytes, bytearray)) and len(bbox) >= 4:
        try:
            values = [int(round(float(v))) for v in bbox[:4]]
            return f"bbox={values}"
        except (TypeError, ValueError):
            pass
    center_x = node.get("center_x")
    center_y = node.get("center_y")
    if center_x is not None and center_y is not None:
        try:
            return f"center=({int(round(float(center_x)))},{int(round(float(center_y)))})"
        except (TypeError, ValueError):
            pass
    return ""


def _format_node(node: Mapping[str, Any]) -> str:
    index = node.get("element_index", node.get("index", "?"))
    role = _node_role(node)
    label = _node_label(node)
    source = _first_text(node.get("source"), _node_metadata(node).get("source"))
    flags: list[str] = []

    if source:
        flags.append(f"source={source}")
    actions = _as_short_sequence(node.get("action_hints")) or _as_short_sequence(node.get("actions"))
    if actions:
        flags.append("actions=" + ",".join(actions))
    if node.get("enabled") is False:
        flags.append("disabled")
    if node.get("focused") is True or node.get("is_focused") is True:
        flags.append("focused")
    if node.get("selected") is True or node.get("is_selected") is True:
        flags.append("selected")
    bbox = _format_bbox(node)
    if bbox:
        flags.append(bbox)

    quoted_label = f' "{label}"' if label else ""
    suffix = " " + " ".join(flags) if flags else ""
    return f"[{index}] {role}{quoted_label}{suffix}"


def _limit_text(text: str, limit: int) -> tuple[str, bool]:
    if limit <= 0 or len(text) <= limit:
        return text, False
    marker = "\n... truncated; pass show_full_text=true or raise agent_text_limit"
    if limit <= len(marker):
        return text[:limit], True
    return text[: limit - len(marker)].rstrip() + marker, True


def _quality_line(observation: Mapping[str, Any]) -> str:
    parts = [
        f"tree={observation.get('tree_quality', 'unknown')}",
        f"uia={observation.get('uia_quality', 'unknown')}",
        f"msaa={observation.get('msaa_quality', 'unknown')}",
        f"vision={observation.get('vision_quality', 'unknown')}",
    ]
    return "Quality: " + " ".join(parts)


def build_agent_state(
    observation: Mapping[str, Any],
    *,
    text_limit: int = DEFAULT_AGENT_TEXT_LIMIT,
    show_full_text: bool = False,
) -> dict[str, Any]:
    """Render a concise, agent-facing text view of a window observation."""
    window = observation.get("window")
    window_map = window if isinstance(window, Mapping) else {}
    title = _first_text(window_map.get("title"), observation.get("title"))
    app = _first_text(
        window_map.get("app"),
        window_map.get("process_name"),
        observation.get("process_name"),
    )
    pid = _first_text(window_map.get("pid"), observation.get("pid"))
    window_id = _first_text(
        window_map.get("id"),
        window_map.get("window_id"),
        observation.get("window_id"),
    )

    nodes = observation.get("nodes")
    node_list = list(nodes) if isinstance(nodes, list) else []
    lines: list[str] = []
    target = _first_text(app, "unknown-app")
    if pid:
        target += f" pid={pid}"
    if window_id:
        target += f" hwnd={window_id}"
    lines.append(f"Window: {target}")
    if title:
        lines.append(f'Title: "{title}"')
    lines.append(
        "Snapshot: "
        f"snapshot_id={observation.get('snapshot_id', '')} "
        f"screenshot_id={observation.get('screenshot_id', '')} "
        f"nodes={observation.get('returned_node_count', observation.get('node_count', len(node_list)))}"
        f"/{observation.get('filtered_node_count', len(node_list))}"
    )
    lines.append(_quality_line(observation))

    recommendation = _first_text(observation.get("observation_recommendation"))
    if recommendation:
        lines.append(f"Recommendation: {recommendation}")
    if observation.get("nodes_truncated"):
        lines.append(
            "Nodes are truncated; narrow with query or raise max_nodes if needed."
        )

    focused = [
        node
        for node in node_list
        if node.get("focused") is True or node.get("is_focused") is True
    ]
    selected = [
        node
        for node in node_list
        if node.get("selected") is True or node.get("is_selected") is True
    ]
    for label, group in (("Focused", focused), ("Selected", selected)):
        if group:
            lines.append(f"{label}: " + " | ".join(_format_node(node) for node in group[:3]))

    if node_list:
        lines.append("Nodes:")
        lines.extend(_format_node(node) for node in node_list)
    else:
        tree_markdown = _first_text(observation.get("tree_markdown"))
        if tree_markdown:
            first_lines = [line.strip() for line in tree_markdown.splitlines() if line.strip()]
            if first_lines:
                lines.append("Tree:")
                lines.extend(first_lines[:10])

    full_text = "\n".join(lines)
    effective_limit = 0 if show_full_text else int(text_limit)
    text, truncated = _limit_text(full_text, effective_limit)

    return {
        "text": text,
        "text_limit": int(text_limit),
        "show_full_text": bool(show_full_text),
        "truncated": truncated,
        "line_count": len(lines),
        "node_sample_count": len(node_list),
        "nodes_truncated": bool(observation.get("nodes_truncated")),
        "recommended_next_step": recommendation or None,
    }
