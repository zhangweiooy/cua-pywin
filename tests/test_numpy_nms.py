import numpy as np
import pytest

from som.nms import nms


def test_nms_is_stable_for_equal_scores():
    boxes = np.array(
        [
            [0.0, 0.0, 1.0, 1.0],
            [0.0, 0.0, 1.0, 1.0],
            [2.0, 2.0, 3.0, 3.0],
        ]
    )
    scores = np.array([0.8, 0.8, 0.8])

    assert nms(boxes, scores, 0.5) == [0, 2]


def test_nms_keeps_iou_equal_to_threshold():
    boxes = np.array(
        [
            [0.0, 0.0, 2.0, 1.0],
            [1.0, 0.0, 3.0, 1.0],
        ]
    )

    assert nms(boxes, np.array([0.9, 0.8]), 1 / 3) == [0, 1]


def test_nms_rejects_invalid_shapes():
    with pytest.raises(ValueError, match="boxes"):
        nms(np.zeros((2, 5)), np.zeros(2), 0.5)
    with pytest.raises(ValueError, match="scores"):
        nms(np.zeros((2, 4)), np.zeros(3), 0.5)
