"""Small deterministic NumPy NMS shared by SOM detection stages."""

from __future__ import annotations

import numpy as np


def nms(
    boxes: np.ndarray,
    scores: np.ndarray,
    iou_threshold: float,
) -> list[int]:
    """Return kept input indices for ``xyxy`` boxes in stable score order."""
    boxes = np.asarray(boxes, dtype=np.float32)
    scores = np.asarray(scores, dtype=np.float32)
    if boxes.ndim != 2 or boxes.shape[1] != 4:
        raise ValueError("boxes must have shape [N, 4]")
    if scores.ndim != 1 or scores.shape[0] != boxes.shape[0]:
        raise ValueError("scores must have shape [N]")
    if not 0.0 <= iou_threshold <= 1.0:
        raise ValueError("iou_threshold must be between 0 and 1")
    if not len(scores):
        return []

    indices = np.arange(len(scores))
    order = np.lexsort((indices, -scores))
    kept: list[int] = []
    while order.size:
        current = int(order[0])
        kept.append(current)
        if order.size == 1:
            break

        remaining = order[1:]
        left = np.maximum(boxes[current, 0], boxes[remaining, 0])
        top = np.maximum(boxes[current, 1], boxes[remaining, 1])
        right = np.minimum(boxes[current, 2], boxes[remaining, 2])
        bottom = np.minimum(boxes[current, 3], boxes[remaining, 3])
        intersections = np.maximum(0.0, right - left) * np.maximum(
            0.0, bottom - top
        )
        current_area = max(
            0.0,
            float(boxes[current, 2] - boxes[current, 0]),
        ) * max(0.0, float(boxes[current, 3] - boxes[current, 1]))
        remaining_areas = np.maximum(
            0.0, boxes[remaining, 2] - boxes[remaining, 0]
        ) * np.maximum(0.0, boxes[remaining, 3] - boxes[remaining, 1])
        unions = current_area + remaining_areas - intersections
        ious = np.divide(
            intersections,
            unions,
            out=np.zeros_like(intersections),
            where=unions > 0,
        )
        order = remaining[ious <= iou_threshold]
    return kept
