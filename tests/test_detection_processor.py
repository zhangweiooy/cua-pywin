from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from som.detection import DetectionProcessor


def test_missing_yolo_model_fails_without_download(tmp_path: Path) -> None:
    processor = DetectionProcessor(model_path=tmp_path / "missing.onnx")

    with pytest.raises(RuntimeError, match="runtime model download is disabled"):
        processor.load_model()


def test_final_nms_removes_boxes_that_become_identical_after_clipping() -> None:
    raw = np.zeros((1, 5, 6), dtype=np.float32)
    raw[0, :, 0] = [-5120.0, 640.0, 12800.0, 1280.0, 0.9]
    raw[0, :, 1] = [6400.0, 640.0, 12800.0, 1280.0, 0.8]

    class FakeSession:
        def run(self, _outputs, _inputs):
            return [raw]

    processor = DetectionProcessor()
    processor.model = FakeSession()
    processor.input_name = "images"
    processor.output_name = "output0"

    detections = processor.detect_icons(
        Image.new("RGB", (100, 100), "white"),
        box_threshold=0.05,
        iou_threshold=0.1,
    )

    assert len(detections) == 1
    assert detections[0]["bbox"] == [0.0, 0.0, 1.0, 1.0]
