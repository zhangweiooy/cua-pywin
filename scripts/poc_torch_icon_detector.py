"""Build-time PyTorch baseline for ONNX detector equivalence benchmarks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from som.nms import nms


class TorchIconDetector:
    def __init__(self, model_path: str | Path, *, compute_threads: int = 4) -> None:
        self.model_path = Path(model_path)
        self.model = None
        self.compute_threads = compute_threads

    def load_model(self) -> None:
        if self.model is None:
            from ultralytics import YOLO
            from ultralytics import utils
            from ultralytics.utils import torch_utils

            utils.NUM_THREADS = self.compute_threads
            torch_utils.NUM_THREADS = self.compute_threads
            self.model = YOLO(str(self.model_path))

    def detect_icons(
        self,
        image: Image.Image,
        box_threshold: float = 0.05,
        iou_threshold: float = 0.1,
        multi_scale: bool = True,
    ) -> list[dict[str, Any]]:
        del multi_scale
        self.load_model()
        assert self.model is not None
        width, height = image.size
        results = self.model.predict(
            source=image,
            conf=box_threshold,
            iou=iou_threshold,
            max_det=1000,
            verbose=False,
            augment=False,
            agnostic_nms=True,
            imgsz=1280,
            device="cpu",
        )
        detections: list[dict[str, Any]] = []
        for result in results:
            for confidence, bbox in zip(result.boxes.conf.cpu(), result.boxes.xyxy.cpu()):
                x1, y1, x2, y2 = bbox.tolist()
                detections.append(
                    {
                        "type": "icon",
                        "confidence": confidence.item(),
                        "bbox": [x1 / width, y1 / height, x2 / width, y2 / height],
                        "scale": 1280,
                        "interactivity": True,
                    }
                )
        if not detections:
            return []
        boxes = np.asarray([item["bbox"] for item in detections], dtype=np.float32)
        scores = np.asarray(
            [item["confidence"] for item in detections], dtype=np.float32
        )
        return [detections[index] for index in nms(boxes, scores, iou_threshold)]
