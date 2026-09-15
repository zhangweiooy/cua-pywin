import json
import subprocess
import sys

import numpy as np
import torch
import torchvision
from PIL import Image
from ultralytics.data.augment import LetterBox

from scripts.poc_yolo_onnx import letterbox
from som.nms import nms


def test_onnx_poc_import_does_not_load_pytorch_runtime():
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json,sys; "
                "from scripts.poc_yolo_onnx import OnnxIconDetector; "
                "print(json.dumps(sorted(name for name in sys.modules "
                "if name.split('.', 1)[0] in "
                "{'torch', 'torchvision', 'ultralytics'})))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


def test_letterbox_pixels_match_ultralytics_for_desktop_shapes():
    random = np.random.default_rng(42)
    for width, height in ((1920, 1080), (1624, 1291), (1280, 960)):
        pixels = random.integers(0, 256, (height, width, 3), dtype=np.uint8)
        actual, transform = letterbox(Image.fromarray(pixels))
        expected = LetterBox(
            new_shape=(1280, 1280),
            auto=True,
            stride=32,
        )(image=pixels)

        np.testing.assert_array_equal(
            (actual[0].transpose(1, 2, 0) * 255).round().astype(np.uint8),
            expected,
        )
        assert actual.shape[2:] == expected.shape[:2]
        assert transform.input_width % 32 == 0
        assert transform.input_height % 32 == 0


def test_numpy_nms_matches_torchvision_for_distinct_scores():
    boxes = np.array(
        [
            [0.0, 0.0, 10.0, 10.0],
            [1.0, 1.0, 9.0, 9.0],
            [20.0, 20.0, 30.0, 30.0],
            [21.0, 21.0, 31.0, 31.0],
        ],
        dtype=np.float32,
    )
    scores = np.array([0.9, 0.8, 0.7, 0.6], dtype=np.float32)

    expected = torchvision.ops.nms(
        torch.from_numpy(boxes),
        torch.from_numpy(scores),
        0.5,
    ).tolist()

    assert nms(boxes, scores, 0.5) == expected
