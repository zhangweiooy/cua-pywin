from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import som.detection as detection_module
from som.detection import DetectionProcessor


def test_missing_yolo_model_fails_without_download(tmp_path: Path) -> None:
    processor = DetectionProcessor(model_path=tmp_path / "missing.onnx")

    with pytest.raises(RuntimeError, match="runtime model download is disabled"):
        processor.load_model()


def test_yolo_session_disables_cpu_arena_but_keeps_memory_pattern(
    monkeypatch,
    tmp_path: Path,
) -> None:
    model_path = tmp_path / "model.onnx"
    model_path.write_bytes(b"model")
    captured = {}

    class Options:
        graph_optimization_level = None
        intra_op_num_threads = None
        inter_op_num_threads = None
        enable_cpu_mem_arena = None
        enable_mem_pattern = None

    class Value:
        def __init__(self, name):
            self.name = name

    class Session:
        def get_inputs(self):
            return [Value("images")]

        def get_outputs(self):
            return [Value("output0")]

    class FakeOrt:
        SessionOptions = Options
        GraphOptimizationLevel = type("GraphOptimizationLevel", (), {"ORT_ENABLE_ALL": "all"})

        @staticmethod
        def InferenceSession(path, *, sess_options, providers):
            captured.update(path=path, options=sess_options, providers=providers)
            return Session()

    monkeypatch.setattr(detection_module, "ort", FakeOrt)

    DetectionProcessor(model_path=model_path).load_model()

    assert captured["options"].graph_optimization_level == "all"
    assert captured["options"].intra_op_num_threads == 4
    assert captured["options"].inter_op_num_threads == 1
    assert captured["options"].enable_cpu_mem_arena is False
    assert captured["options"].enable_mem_pattern is True
    assert captured["providers"] == ["CPUExecutionProvider"]


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
