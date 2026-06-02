from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)


class TimeoutException(Exception):
    pass


class OCRProcessor:
    """OCR text detector with a stable output contract for SOM consumers."""

    _shared_reader = None
    _shared_key = None
    _shared_lock = threading.Lock()
    _ocr_executor = None

    def __init__(self, languages: Optional[List[str]] = None, backend: Optional[str] = None):
        """Initialize the OCR processor.

        Args:
            languages: Language hints. RapidOCR uses its bundled Chinese+English
                PP-OCR model; EasyOCR fallback uses this list directly.
            backend: ``rapidocr`` (default) or ``easyocr``. Can also be set via
                ``CUA_OCR_BACKEND``.
        """
        self.reader = None
        self.languages = languages or ["ch_sim", "en"]
        self.backend = (backend or os.getenv("CUA_OCR_BACKEND", "rapidocr")).strip().lower()
        if self.backend not in {"rapidocr", "easyocr"}:
            logger.warning("Unknown OCR backend %r; falling back to rapidocr", self.backend)
            self.backend = "rapidocr"
        if OCRProcessor._ocr_executor is None:
            OCRProcessor._ocr_executor = ThreadPoolExecutor(max_workers=1)
        logger.info("OCR processor initialized with backend=%s", self.backend)

    def _ensure_reader(self):
        """Ensure the selected OCR reader is initialized and cached."""
        cache_key = (self.backend, tuple(self.languages), os.getenv("CUA_RAPIDOCR_DEVICE", "cpu"))

        if OCRProcessor._shared_reader is not None and OCRProcessor._shared_key == cache_key:
            self.reader = OCRProcessor._shared_reader
            return

        with OCRProcessor._shared_lock:
            if OCRProcessor._shared_reader is not None and OCRProcessor._shared_key == cache_key:
                self.reader = OCRProcessor._shared_reader
                return

            if self.backend == "easyocr":
                reader = self._create_easyocr_reader()
            else:
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

            return RapidOCR()
        except Exception as exc:
            raise RuntimeError(f"RapidOCR initialization failed: {exc}") from exc

    def _create_easyocr_reader(self):
        try:
            import easyocr
            import torch

            use_gpu = bool(torch.cuda.is_available())
            return easyocr.Reader(self.languages, gpu=use_gpu)
        except Exception as exc:
            raise RuntimeError(f"EasyOCR initialization failed: {exc}") from exc

    def detect_text(
        self,
        image: Image.Image,
        confidence_threshold: float = 0.5,
        timeout_seconds: float = 5,
    ) -> List[Dict[str, Any]]:
        """Detect text in an image.

        Returns a list of dictionaries compatible with the previous EasyOCR
        implementation: normalized bbox, text content, confidence, and
        ``interactivity=False``.
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
            if self.backend == "easyocr":
                future = OCRProcessor._ocr_executor.submit(
                    self.reader.readtext,
                    image_np,
                    paragraph=False,
                    text_threshold=threshold,
                )
            else:
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
            detections.append(
                {
                    "type": "text",
                    "bbox": bbox,
                    "content": str(text),
                    "confidence": conf,
                    "interactivity": False,
                }
            )
        return detections

    def _iter_raw_results(self, raw_results: Any) -> Iterable[Tuple[Any, Any, Any]]:
        """Yield ``(box, text, score)`` triples from RapidOCR/EasyOCR formats."""
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
