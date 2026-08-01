"""Detect the bordered message composer in Windows WeChat screenshots."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from computer_server.handlers.element_cache import CachedElement


_DETECTOR_NAME = "wechat_compose_boundary_v1"
_CONTRAST_THRESHOLDS = (6, 10, 14, 18, 24)
_EDIT_CONTROL_TYPES = {"combobox", "document", "edit", "textinput"}


@dataclass(frozen=True)
class _Line:
    position: int
    start: int
    end: int
    coverage: float
    contrast: float


@dataclass(frozen=True)
class _BoundaryCandidate:
    bbox: Tuple[int, int, int, int]
    border_score: float
    contrast: float


@dataclass(frozen=True)
class ComposeDetection:
    element: Optional[CachedElement]
    diagnostics: Dict[str, Any]


def _node_value(node: Any, key: str, default: Any = None) -> Any:
    if isinstance(node, Mapping):
        return node.get(key, default)
    return getattr(node, key, default)


def _bbox(node: Any) -> Optional[Tuple[int, int, int, int]]:
    value = _node_value(node, "bbox")
    if not value or len(value) != 4:
        return None
    try:
        return tuple(int(v) for v in value)
    except (TypeError, ValueError):
        return None


def _long_runs(
    mask: np.ndarray,
    values: np.ndarray,
    *,
    offset: int,
    minimum_length: int,
    maximum_length: int,
    maximum_gap: int,
) -> List[Tuple[int, int, float, float]]:
    positions = np.flatnonzero(mask)
    if positions.size == 0:
        return []
    split_points = np.flatnonzero(np.diff(positions) > maximum_gap + 1) + 1
    groups = np.split(positions, split_points)
    runs: List[Tuple[int, int, float, float]] = []
    for group in groups:
        if group.size == 0:
            continue
        start = int(group[0])
        end = int(group[-1]) + 1
        span = end - start
        if span < minimum_length or span > maximum_length:
            continue
        coverage = float(group.size / span)
        if coverage < 0.65:
            continue
        contrast = float(np.median(values[group]))
        runs.append((start + offset, end + offset, coverage, contrast))
    return runs


def _horizontal_lines(
    vertical_gradient: np.ndarray,
    *,
    x_start: int,
    x_end: int,
    y_start: int,
    y_end: int,
    width: int,
    threshold: int,
) -> List[_Line]:
    minimum_length = int(width * 0.50)
    maximum_length = int(width * 0.75)
    lines: List[_Line] = []
    y_end = min(y_end, vertical_gradient.shape[0])
    roi = vertical_gradient[y_start:y_end, x_start:x_end]
    active = roi >= threshold
    # Most rows contain only sparse glyph/icon edges.  Prefilter all rows in one
    # NumPy operation so the more exact gap-tolerant run analysis is performed
    # only for plausible long borders.
    minimum_active = max(1, int(minimum_length * 0.65))
    plausible_rows = np.flatnonzero(np.count_nonzero(active, axis=1) >= minimum_active)
    for local_y in plausible_rows:
        values = roi[local_y]
        for start, end, coverage, contrast in _long_runs(
            active[local_y],
            values,
            offset=x_start,
            minimum_length=minimum_length,
            maximum_length=maximum_length,
            maximum_gap=max(3, int(width * 0.004)),
        ):
            lines.append(_Line(y_start + int(local_y) + 1, start, end, coverage, contrast))
    return lines


def _vertical_edge(
    horizontal_gradient: np.ndarray,
    *,
    expected_x: int,
    top: int,
    bottom: int,
    threshold: int,
    width: int,
) -> Tuple[float, float]:
    best_coverage = 0.0
    best_contrast = 0.0
    # Rounded composer corners can shorten the horizontal stroke before it
    # reaches the true vertical edge.  Search only the bounded corner radius;
    # the 80% vertical-coverage requirement still rejects unrelated edges.
    tolerance = max(4, int(width * 0.02))
    for x in range(
        max(0, expected_x - tolerance),
        min(horizontal_gradient.shape[1], expected_x + tolerance + 1),
    ):
        values = horizontal_gradient[top:bottom, x]
        if values.size == 0:
            continue
        active = values >= threshold
        coverage = float(np.count_nonzero(active) / values.size)
        if coverage > best_coverage:
            best_coverage = coverage
            best_contrast = float(np.median(values[active])) if np.any(active) else 0.0
    return best_coverage, best_contrast


def _dedupe_candidates(
    candidates: Iterable[_BoundaryCandidate], width: int, height: int
) -> List[_BoundaryCandidate]:
    tolerance_x = max(6, int(width * 0.008))
    tolerance_y = max(4, int(height * 0.008))
    deduped: List[_BoundaryCandidate] = []
    for candidate in sorted(candidates, key=lambda item: item.border_score, reverse=True):
        left, top, right, bottom = candidate.bbox
        if any(
            abs(left - other.bbox[0]) <= tolerance_x
            and abs(right - other.bbox[2]) <= tolerance_x
            and abs(top - other.bbox[1]) <= tolerance_y
            and abs(bottom - other.bbox[3]) <= tolerance_y
            for other in deduped
        ):
            continue
        deduped.append(candidate)
    return deduped


def _boundary_candidates(image: Any) -> List[_BoundaryCandidate]:
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    height, width = gray.shape
    if width < 320 or height < 240:
        return []

    vertical_gradient = np.maximum(gray[1:], gray[:-1]) - np.minimum(
        gray[1:], gray[:-1]
    )
    horizontal_gradient = np.maximum(gray[:, 1:], gray[:, :-1]) - np.minimum(
        gray[:, 1:], gray[:, :-1]
    )
    x_start = int(width * 0.25)
    x_end = min(width, int(width * 0.995))
    y_start = int(height * 0.55)
    y_end = min(height - 1, int(height * 0.995))
    candidates: List[_BoundaryCandidate] = []

    for threshold in _CONTRAST_THRESHOLDS:
        lines = _horizontal_lines(
            vertical_gradient,
            x_start=x_start,
            x_end=x_end,
            y_start=y_start,
            y_end=y_end,
            width=width,
            threshold=threshold,
        )
        top_lines = [line for line in lines if y_start <= line.position <= height * 0.88]
        bottom_lines = [line for line in lines if line.position >= height * 0.90]
        for top_line in top_lines:
            for bottom_line in bottom_lines:
                candidate_height = bottom_line.position - top_line.position
                if not height * 0.12 <= candidate_height <= height * 0.30:
                    continue
                left = max(top_line.start, bottom_line.start)
                right = min(top_line.end, bottom_line.end)
                candidate_width = right - left
                if not width * 0.50 <= candidate_width <= width * 0.75:
                    continue
                if not width * 0.25 <= left <= width * 0.50 or right < width * 0.90:
                    continue
                endpoint_tolerance = max(8, int(width * 0.02))
                if (
                    abs(top_line.start - bottom_line.start) > endpoint_tolerance
                    or abs(top_line.end - bottom_line.end) > endpoint_tolerance
                ):
                    continue

                left_coverage, left_contrast = _vertical_edge(
                    horizontal_gradient,
                    expected_x=left,
                    top=top_line.position,
                    bottom=bottom_line.position,
                    threshold=threshold,
                    width=width,
                )
                right_coverage, right_contrast = _vertical_edge(
                    horizontal_gradient,
                    expected_x=right,
                    top=top_line.position,
                    bottom=bottom_line.position,
                    threshold=threshold,
                    width=width,
                )
                if left_coverage < 0.80 or right_coverage < 0.80:
                    continue
                if top_line.coverage < 0.80 or bottom_line.coverage < 0.65:
                    continue

                border_score = min(
                    1.0,
                    (
                        top_line.coverage
                        + bottom_line.coverage
                        + left_coverage
                        + right_coverage
                    )
                    / 4.0,
                )
                contrast = float(
                    np.mean(
                        [
                            top_line.contrast,
                            bottom_line.contrast,
                            left_contrast,
                            right_contrast,
                        ]
                    )
                )
                candidates.append(
                    _BoundaryCandidate(
                        bbox=(
                            left,
                            top_line.position,
                            right,
                            bottom_line.position,
                        ),
                        border_score=border_score,
                        contrast=contrast,
                    )
                )
        # Thresholds are ordered from most sensitive to least sensitive.  Once
        # a complete four-sided boundary survives the geometry checks, stricter
        # thresholds can only rediscover the same rectangle.
        if candidates:
            break

    return _dedupe_candidates(candidates, width, height)


def _toolbar_cluster(
    elements: Sequence[Any], boundary: Tuple[int, int, int, int], height: int
) -> List[Any]:
    left, top, right, bottom = boundary
    minimum_y = top + int((bottom - top) * 0.55)
    icons = [
        element
        for element in elements
        if str(_node_value(element, "control_type", "")).lower() == "icon"
        and "click" in list(_node_value(element, "actions", []) or [])
        and left <= int(_node_value(element, "center_x", -1)) <= right
        and minimum_y <= int(_node_value(element, "center_y", -1)) <= bottom
        and _bbox(element) is not None
    ]
    if not icons:
        return []
    tolerance = max(5, int(height * 0.03))
    best: List[Any] = []
    for anchor in icons:
        anchor_y = int(_node_value(anchor, "center_y", -1))
        cluster = [
            icon
            for icon in icons
            if abs(int(_node_value(icon, "center_y", -1)) - anchor_y) <= tolerance
        ]
        if len(cluster) > len(best):
            best = cluster
    if len(best) < 4:
        return []
    return best


def _overlap_over_smaller(
    first: Tuple[int, int, int, int], second: Tuple[int, int, int, int]
) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[2], second[2])
    bottom = min(first[3], second[3])
    intersection = max(0, right - left) * max(0, bottom - top)
    first_area = max(0, first[2] - first[0]) * max(0, first[3] - first[1])
    second_area = max(0, second[2] - second[0]) * max(0, second[3] - second[1])
    smaller = min(first_area, second_area)
    return intersection / smaller if smaller > 0 else 0.0


def _has_existing_edit(
    nodes: Sequence[Any], boundary: Tuple[int, int, int, int]
) -> bool:
    for node in nodes:
        control = str(
            _node_value(node, "control_type", _node_value(node, "role", "")) or ""
        ).lower().replace("_", "").replace("-", "")
        node_bbox = _bbox(node)
        if control in _EDIT_CONTROL_TYPES and node_bbox is not None:
            if _overlap_over_smaller(boundary, node_bbox) >= 0.50:
                return True
    return False


def _has_send_text(elements: Sequence[Any], boundary: Tuple[int, int, int, int]) -> bool:
    left, top, right, bottom = boundary
    for element in elements:
        if str(_node_value(element, "control_type", "")).lower() != "text":
            continue
        name = str(_node_value(element, "name", "") or "").strip()
        center_x = int(_node_value(element, "center_x", -1))
        center_y = int(_node_value(element, "center_y", -1))
        if name == "发送" and left <= center_x <= right and top <= center_y <= bottom:
            return True
    return False


def detect_wechat_compose_region(
    image: Any,
    vision_elements: Sequence[Any],
    *,
    target_family: str,
    existing_nodes: Sequence[Any] = (),
) -> ComposeDetection:
    """Return a synthetic cached edit element when the WeChat composer is certain."""
    start = time.perf_counter()
    diagnostics: Dict[str, Any] = {
        "compose_region_count": 0,
        "compose_candidate_count": 0,
        "compose_layout_latency": 0.0,
    }
    if target_family != "wechat":
        diagnostics["compose_layout_latency"] = time.perf_counter() - start
        return ComposeDetection(None, diagnostics)

    candidates = _boundary_candidates(image)
    diagnostics["compose_candidate_count"] = len(candidates)
    width, height = image.size
    for candidate in candidates:
        if _has_existing_edit(existing_nodes, candidate.bbox):
            continue
        toolbar = _toolbar_cluster(vision_elements, candidate.bbox, height)
        if len(toolbar) < 4:
            continue

        send_text = _has_send_text(vision_elements, candidate.bbox)
        geometry_score = 1.0
        anchor_score = min(1.0, len(toolbar) / 4.0)
        confidence = (
            candidate.border_score * 0.45
            + anchor_score * 0.35
            + geometry_score * 0.10
            + (0.10 if send_text else 0.0)
        )
        if confidence < 0.80:
            continue

        inset = max(6, int(width * 0.006))
        toolbar_top = min(_bbox(icon)[1] for icon in toolbar if _bbox(icon) is not None)
        left, top, right, _bottom = candidate.bbox
        safe_bbox = (
            left + inset,
            top + inset,
            right - inset,
            toolbar_top - inset,
        )
        if (
            safe_bbox[2] - safe_bbox[0] < width * 0.45
            or safe_bbox[3] - safe_bbox[1] < max(20, height * 0.04)
        ):
            continue

        next_index = max(
            [9999]
            + [
                int(_node_value(element, "element_index", 9999))
                for element in vision_elements
            ]
        ) + 1
        element = CachedElement(
            element_index=next_index,
            source="vision",
            center_x=(safe_bbox[0] + safe_bbox[2]) // 2,
            center_y=(safe_bbox[1] + safe_bbox[3]) // 2,
            bbox=safe_bbox,
            control_type="edit",
            name="message input",
            actions=["click", "text"],
            platform_ref=None,
            metadata={
                "layout_detector": _DETECTOR_NAME,
                "layout_confidence": round(confidence, 3),
                "boundary_contrast": round(candidate.contrast, 2),
                "anchor_icon_count": len(toolbar),
            },
        )
        diagnostics["compose_region_count"] = 1
        diagnostics["compose_layout_latency"] = time.perf_counter() - start
        return ComposeDetection(element, diagnostics)

    diagnostics["compose_layout_latency"] = time.perf_counter() - start
    return ComposeDetection(None, diagnostics)
