from __future__ import annotations

import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_OCR_TIMEOUT_SECONDS = 20.0

_CJK_RE = re.compile(r"[\u3400-\u9fff]")
_FALSE_VALUES = {"0", "false", "no", "off"}
_STRICT_VALUES = {"strict", "aggressive"}
_CJK_SHAPE_ARTIFACTS = {"\u53e3"}
_RAPIDOCR_MODEL_FILES = {
    "Det.model_path": "ch_PP-OCRv4_det_mobile.onnx",
    "Cls.model_path": "ch_ppocr_mobile_v2.0_cls_mobile.onnx",
    "Rec.model_path": "ch_PP-OCRv4_rec_mobile.onnx",
    "Rec.rec_keys_path": "ppocr_keys_v1.txt",
}


class TimeoutException(Exception):
    pass


class OCRProcessor:
    """OCR text detector with a stable output contract for SOM consumers."""

    _shared_reader = None
    _shared_key = None
    _shared_lock = threading.Lock()
    _ocr_executor = None

    def __init__(self, languages: Optional[List[str]] = None):
        """Initialize the OCR processor.

        Args:
            languages: Language hints retained for the shared OCR contract.
                RapidOCR uses its bundled Chinese+English PP-OCR model.
        """
        self.reader = None
        self.languages = languages or ["ch_sim", "en"]
        self.backend = "rapidocr"
        if OCRProcessor._ocr_executor is None:
            OCRProcessor._ocr_executor = ThreadPoolExecutor(max_workers=1)
        logger.info("OCR processor initialized with backend=%s", self.backend)

    def _ensure_reader(self):
        """Ensure the selected OCR reader is initialized and cached."""
        cache_key = (
            self.backend,
            tuple(self.languages),
            os.getenv("CUA_RAPIDOCR_DEVICE", "cpu"),
            os.getenv("CUA_RAPIDOCR_MODEL_DIR", ""),
        )

        if OCRProcessor._shared_reader is not None and OCRProcessor._shared_key == cache_key:
            self.reader = OCRProcessor._shared_reader
            return

        with OCRProcessor._shared_lock:
            if OCRProcessor._shared_reader is not None and OCRProcessor._shared_key == cache_key:
                self.reader = OCRProcessor._shared_reader
                return

            reader = self._create_rapidocr_reader()

            OCRProcessor._shared_reader = reader
            OCRProcessor._shared_key = cache_key
            self.reader = reader

    def _create_rapidocr_reader(self):
        device = os.getenv("CUA_RAPIDOCR_DEVICE", "cpu").strip().lower()
        if device != "cpu":
            logger.warning(
                "CUA_RAPIDOCR_DEVICE=%s is not supported in v1; using ONNXRuntime CPU",
                device,
            )
        try:
            from rapidocr import RapidOCR

            return RapidOCR(params=self._rapidocr_params())
        except Exception as exc:
            raise RuntimeError(f"RapidOCR initialization failed: {exc}") from exc

    def _rapidocr_params(self) -> Dict[str, Any]:
        model_dir = self._rapidocr_model_dir()
        paths = {
            key: model_dir / filename
            for key, filename in _RAPIDOCR_MODEL_FILES.items()
        }
        missing = [str(path) for path in paths.values() if not path.is_file()]
        if missing:
            raise RuntimeError(
                "Bundled RapidOCR model files are missing: " + ", ".join(missing)
            )
        params: Dict[str, Any] = {
            key: str(path)
            for key, path in paths.items()
        }
        params["Global.model_root_dir"] = str(model_dir)
        params["Global.log_level"] = os.getenv(
            "CUA_RAPIDOCR_LOG_LEVEL", "error"
        ).strip().lower()
        return params

    @staticmethod
    def _rapidocr_model_dir() -> Path:
        override = os.getenv("CUA_RAPIDOCR_MODEL_DIR", "").strip()
        if override:
            return Path(override).expanduser().resolve()
        return (
            Path(__file__).resolve().parents[1]
            / "computer_server"
            / "models"
            / "rapidocr_ppocrv4"
        )

    def detect_text(
        self,
        image: Image.Image,
        confidence_threshold: float = 0.5,
        timeout_seconds: float = DEFAULT_OCR_TIMEOUT_SECONDS,
    ) -> List[Dict[str, Any]]:
        """Detect text in an image.

        Returns normalized bbox, text content, confidence, and
        ``interactivity=False`` dictionaries.
        """
        try:
            try:
                self._ensure_reader()
            except Exception as exc:
                logger.error("Failed to initialize OCR reader: %s", exc)
                return []

            if self.reader is None:
                logger.error("OCR reader is None after initialization")
                return []

            image_np = np.array(image.convert("RGB"))
            threshold = float(confidence_threshold)
            timeout = self._effective_timeout(timeout_seconds)
            future = OCRProcessor._ocr_executor.submit(
                self.reader,
                image_np,
                text_score=threshold,
            )

            try:
                raw_results = future.result(timeout=timeout)
            except FutureTimeoutError:
                logger.warning("OCR timed out after %ss", timeout)
                return []
            except Exception as exc:
                logger.warning("OCR failed: %s", exc)
                return []

            return self._normalize_results(
                raw_results,
                image.size,
                confidence_threshold=threshold,
            )
        except Exception as exc:
            logger.error("Unexpected error in OCR processing: %s", exc)
            return []

    def _effective_timeout(self, timeout_seconds: float) -> float:
        env_timeout = os.getenv("CUA_OCR_TIMEOUT_SECONDS")
        if env_timeout:
            try:
                return max(0.1, float(env_timeout))
            except ValueError:
                logger.warning("Invalid CUA_OCR_TIMEOUT_SECONDS=%r", env_timeout)
        return max(0.1, float(timeout_seconds))

    def _normalize_results(
        self,
        raw_results: Any,
        image_size: Tuple[int, int],
        *,
        confidence_threshold: float,
    ) -> List[Dict[str, Any]]:
        detections: List[Dict[str, Any]] = []
        img_width, img_height = image_size
        for box, text, score in self._iter_raw_results(raw_results):
            try:
                conf = float(score)
            except (TypeError, ValueError):
                continue
            if conf < confidence_threshold:
                continue
            bbox = self._normalize_box(box, img_width, img_height)
            if bbox is None:
                continue
            text_content = self._clean_text_detection(str(text))
            if not self._should_keep_text_detection(text_content, bbox):
                continue
            detections.append(
                {
                    "type": "text",
                    "bbox": bbox,
                    "content": text_content,
                    "confidence": conf,
                    "interactivity": False,
                }
            )
        return detections

    def _clean_text_detection(self, text: str) -> str:
        if self._denoise_mode() == "off":
            return str(text).strip()
        compact = "".join(str(text).split())
        if compact in {"Q\u641c\u7d22", "O\u641c\u7d22", "0\u641c\u7d22"}:
            return "\u641c\u7d22"
        return str(text).strip()

    def _denoise_mode(self) -> str:
        value = os.getenv("CUA_OCR_DENOISE", "conservative").strip().lower()
        if value in _FALSE_VALUES:
            return "off"
        if value in _STRICT_VALUES:
            return "strict"
        return "conservative"

    def _should_keep_text_detection(self, text: str, bbox: Sequence[float]) -> bool:
        """Filter low-value OCR artifacts without dropping Chinese UI text."""
        compact = "".join(str(text).split())
        if not compact:
            return False
        if self._denoise_mode() != "strict":
            return True

        try:
            x1, y1, x2, y2 = [float(v) for v in bbox]
        except Exception:
            return True
        width = max(0.0, x2 - x1)
        height = max(0.0, y2 - y1)

        if (
            len(compact) == 1
            and compact in _CJK_SHAPE_ARTIFACTS
            and width <= 0.06
            and height <= 0.08
        ):
            return False
        if _CJK_RE.search(compact):
            return True
        if len(compact) >= 2:
            return any(ch.isalnum() for ch in compact)

        ch = compact[0]
        if not ch.isalnum():
            return False

        if width <= 0.04 and height <= 0.06:
            return False
        return True

    def _iter_raw_results(self, raw_results: Any) -> Iterable[Tuple[Any, Any, Any]]:
        """Yield ``(box, text, score)`` triples from RapidOCR result formats."""
        boxes = getattr(raw_results, "boxes", None)
        txts = getattr(raw_results, "txts", None)
        scores = getattr(raw_results, "scores", None)
        if boxes is not None and txts is not None and scores is not None:
            for box, text, score in zip(boxes, txts, scores):
                yield box, text, score
            return

        results = raw_results
        if isinstance(raw_results, tuple) and len(raw_results) == 2:
            first, second = raw_results
            if isinstance(first, (list, tuple)) and not isinstance(second, (str, bytes)):
                results = first

        if not isinstance(results, Sequence) or isinstance(results, (str, bytes)):
            return
        for item in results:
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 3:
                yield item[0], item[1], item[2]

    def _normalize_box(
        self,
        box: Any,
        img_width: int,
        img_height: int,
    ) -> Optional[List[float]]:
        try:
            arr = np.asarray(box, dtype=float)
        except Exception:
            return None
        if arr.size < 4 or img_width <= 0 or img_height <= 0:
            return None
        if arr.ndim >= 2 and arr.shape[-1] >= 2:
            points = arr.reshape(-1, arr.shape[-1])
            xs = points[:, 0]
            ys = points[:, 1]
        else:
            flat = arr.reshape(-1)
            if flat.size == 4:
                xs = np.array([flat[0], flat[2]], dtype=float)
                ys = np.array([flat[1], flat[3]], dtype=float)
            else:
                xs = flat[0::2]
                ys = flat[1::2]
        x1 = max(0.0, min(float(xs.min()) / img_width, 1.0))
        y1 = max(0.0, min(float(ys.min()) / img_height, 1.0))
        x2 = max(0.0, min(float(xs.max()) / img_width, 1.0))
        y2 = max(0.0, min(float(ys.max()) / img_height, 1.0))
        if x2 <= x1 or y2 <= y1:
            return None
        return [x1, y1, x2, y2]
