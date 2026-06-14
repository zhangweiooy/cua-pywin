from pathlib import Path

import pytest

from som.detection import DetectionProcessor


def test_missing_yolo_model_fails_without_download(tmp_path: Path) -> None:
    processor = DetectionProcessor(model_path=tmp_path / "missing.pt")

    with pytest.raises(RuntimeError, match="runtime model download is disabled"):
        processor.load_model()
