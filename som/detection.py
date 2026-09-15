"""PyTorch-free ONNX Runtime icon detection for OmniParser V2."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

_DLL_DIRECTORY_HANDLES = []


def _add_packaged_dll_paths() -> None:
    bases = [Path(__file__).resolve().parents[1], Path(sys.executable).resolve().parent]
    for base in bases:
        for relative in ("cv2", "numpy.libs", "scipy.libs", "onnxruntime/capi"):
            path = base / relative
            if not path.exists():
                continue
            os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(path)))
                except OSError:
                    pass


_add_packaged_dll_paths()

import cv2  # noqa: E402
import numpy as np  # noqa: E402
import onnxruntime as ort  # noqa: E402
from PIL import Image  # noqa: E402

from .nms import nms  # noqa: E402

_TARGET_SIZE = 1280
_MODEL_STRIDE = 32
_MAX_NMS_CANDIDATES = 30_000
_MAX_DETECTIONS = 1_000
_ORT_INTRA_OP_THREADS = 4


@dataclass(frozen=True)
class LetterBoxTransform:
    ratio: float
    left: int
    top: int
    input_width: int
    input_height: int
    original_width: int
    original_height: int


def letterbox(
    image: Image.Image,
    *,
    target_size: int = _TARGET_SIZE,
    stride: int = _MODEL_STRIDE,
) -> tuple[np.ndarray, LetterBoxTransform]:
    """Match Ultralytics minimum-rectangle LetterBox for one RGB image."""
    rgb = np.asarray(image.convert("RGB"))
    original_height, original_width = rgb.shape[:2]
    ratio = min(target_size / original_height, target_size / original_width)
    resized_width = round(original_width * ratio)
    resized_height = round(original_height * ratio)
    pad_width = (target_size - resized_width) % stride
    pad_height = (target_size - resized_height) % stride
    half_width = pad_width / 2
    half_height = pad_height / 2
    left = round(half_width - 0.1)
    right = round(half_width + 0.1)
    top = round(half_height - 0.1)
    bottom = round(half_height + 0.1)

    if (resized_width, resized_height) != (original_width, original_height):
        rgb = cv2.resize(
            rgb,
            (resized_width, resized_height),
            interpolation=cv2.INTER_LINEAR,
        )
    rgb = cv2.copyMakeBorder(
        rgb,
        top,
        bottom,
        left,
        right,
        cv2.BORDER_CONSTANT,
        value=(114, 114, 114),
    )
    tensor = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], dtype=np.float32)
    tensor /= 255.0
    return tensor, LetterBoxTransform(
        ratio=ratio,
        left=left,
        top=top,
        input_width=rgb.shape[1],
        input_height=rgb.shape[0],
        original_width=original_width,
        original_height=original_height,
    )


class DetectionProcessor:
    """Run the bundled single-class YOLO ONNX graph on CPU."""

    def __init__(
        self,
        model_path: Optional[Path] = None,
        force_device: Optional[str] = None,
        intra_op_num_threads: int = _ORT_INTRA_OP_THREADS,
    ) -> None:
        if force_device not in {None, "cpu"}:
            raise ValueError("The production ONNX icon detector supports CPU only")
        self.model_path = model_path
        self.model: Optional[ort.InferenceSession] = None
        self.input_name = ""
        self.output_name = ""
        self.device = "cpu"
        if intra_op_num_threads < 1:
            raise ValueError("intra_op_num_threads must be positive")
        self.intra_op_num_threads = intra_op_num_threads

    def load_model(self) -> None:
        """Load the bundled ONNX model without network or PyTorch fallback."""
        if self.model is not None:
            return
        if self.model_path is None:
            self.model_path = (
                Path(__file__).parent / "weights" / "icon_detect" / "model.onnx"
            )
        if not self.model_path.is_file():
            raise RuntimeError(
                "Bundled OmniParser YOLO ONNX model is missing: "
                f"{self.model_path}. Reinstall cua-pywin or use a complete "
                "developer build; runtime model download is disabled."
            )

        options = ort.SessionOptions()
        options.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        options.intra_op_num_threads = self.intra_op_num_threads
        options.inter_op_num_threads = 1
        try:
            session = ort.InferenceSession(
                str(self.model_path),
                sess_options=options,
                providers=["CPUExecutionProvider"],
            )
            inputs = session.get_inputs()
            outputs = session.get_outputs()
            if len(inputs) != 1 or len(outputs) != 1:
                raise ValueError("detector graph must have exactly one input and output")
            self.input_name = inputs[0].name
            self.output_name = outputs[0].name
            self.model = session
        except Exception as exc:
            self.model = None
            raise RuntimeError(
                f"Failed to initialize detection model: {exc}"
            ) from exc

    def detect_icons(
        self,
        image: Image.Image,
        box_threshold: float = 0.05,
        iou_threshold: float = 0.1,
        multi_scale: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return normalized icon detections in the existing SOM contract."""
        del multi_scale
        self.load_model()
        assert self.model is not None

        tensor, transform = letterbox(image)
        raw = np.asarray(
            self.model.run([self.output_name], {self.input_name: tensor})[0],
            dtype=np.float32,
        )
        if raw.ndim != 3 or raw.shape[0] != 1:
            raise ValueError(f"unexpected detector output shape: {raw.shape}")
        predictions = raw[0]
        if predictions.shape[0] < predictions.shape[1]:
            predictions = predictions.T
        if predictions.ndim != 2 or predictions.shape[1] < 5:
            raise ValueError(f"unexpected detector output shape: {raw.shape}")

        scores = predictions[:, 4:].max(axis=1)
        selected = np.flatnonzero(scores > box_threshold)
        if not selected.size:
            return []
        score_order = np.argsort(-scores[selected], kind="stable")
        selected = selected[score_order[:_MAX_NMS_CANDIDATES]]
        selected_scores = scores[selected]

        xywh = predictions[selected, :4]
        boxes = np.empty_like(xywh)
        boxes[:, 0] = xywh[:, 0] - xywh[:, 2] / 2
        boxes[:, 1] = xywh[:, 1] - xywh[:, 3] / 2
        boxes[:, 2] = xywh[:, 0] + xywh[:, 2] / 2
        boxes[:, 3] = xywh[:, 1] + xywh[:, 3] / 2
        kept = nms(boxes, selected_scores, iou_threshold)[:_MAX_DETECTIONS]
        boxes = boxes[kept]
        selected_scores = selected_scores[kept]

        boxes[:, [0, 2]] -= transform.left
        boxes[:, [1, 3]] -= transform.top
        boxes /= transform.ratio
        boxes[:, [0, 2]] = boxes[:, [0, 2]].clip(0, transform.original_width)
        boxes[:, [1, 3]] = boxes[:, [1, 3]].clip(0, transform.original_height)
        boxes[:, [0, 2]] /= transform.original_width
        boxes[:, [1, 3]] /= transform.original_height
        final_kept = nms(boxes, selected_scores, iou_threshold)
        boxes = boxes[final_kept]
        selected_scores = selected_scores[final_kept]

        return [
            {
                "type": "icon",
                "confidence": float(score),
                "bbox": [float(value) for value in box],
                "scale": _TARGET_SIZE,
                "interactivity": True,
            }
            for box, score in zip(boxes, selected_scores)
        ]
