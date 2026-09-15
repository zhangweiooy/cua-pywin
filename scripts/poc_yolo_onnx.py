"""Compatibility imports for the production ONNX icon detector."""

from __future__ import annotations

from pathlib import Path

from som.detection import DetectionProcessor, LetterBoxTransform, letterbox

__all__ = ["LetterBoxTransform", "OnnxIconDetector", "letterbox"]


class OnnxIconDetector(DetectionProcessor):
    def __init__(self, model_path: str | Path, *, intra_op_num_threads: int = 4) -> None:
        super().__init__(
            model_path=Path(model_path),
            force_device="cpu",
            intra_op_num_threads=intra_op_num_threads,
        )
