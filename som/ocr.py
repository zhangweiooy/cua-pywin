from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import easyocr
import numpy as np
import torch
from PIL import Image

logger = logging.getLogger(__name__)


class TimeoutException(Exception):
    pass


class OCRProcessor:
    """Class for handling OCR text detection."""

    _shared_reader = None  # Class-level shared reader instance
    _shared_languages = None  # Class-level shared languages for cache key
    _shared_lock = threading.Lock()  # Protects _shared_reader / _shared_languages
    _ocr_executor = None  # Class-level executor reused across detect_text calls

    def __init__(self, languages: Optional[List[str]] = None):
        """Initialize the OCR processor.

        Args:
            languages: EasyOCR language codes. Default ['ch_sim', 'en'] for Chinese+English.
        """
        self.reader = None
        self.languages = languages or ["ch_sim", "en"]
        # Determine best available device
        self.device = "cpu"
        if torch.cuda.is_available():
            self.device = "cuda"
        elif (
            hasattr(torch, "backends")
            and hasattr(torch.backends, "mps")
            and torch.backends.mps.is_available()
        ):
            self.device = "mps"
        logger.info(f"OCR processor initialized with device: {self.device}")
        # Reuse a single executor across all detect_text calls
        if OCRProcessor._ocr_executor is None:
            OCRProcessor._ocr_executor = ThreadPoolExecutor(max_workers=1)

    def _ensure_reader(self):
        """Ensure EasyOCR reader is initialized.

        Uses a class-level cached reader to avoid reinitializing on every instance.
        Cache is keyed by language list — changing languages creates a new reader.
        Thread-safe via double-checked locking on _shared_lock.
        """
        cache_key = tuple(self.languages)

        # Fast path: already cached with matching languages
        if OCRProcessor._shared_reader is not None and OCRProcessor._shared_languages == cache_key:
            self.reader = OCRProcessor._shared_reader
            return

        # Slow path: initialize under lock
        with OCRProcessor._shared_lock:
            # Double-check after acquiring lock
            if OCRProcessor._shared_reader is not None and OCRProcessor._shared_languages == cache_key:
                self.reader = OCRProcessor._shared_reader
                return

            if self.reader is None:
                try:
                    logger.info("Initializing EasyOCR reader with languages: %s...", self.languages)
                    import easyocr

                    # Use GPU if available
                    use_gpu = self.device in ["cuda", "mps"]
                    self.reader = easyocr.Reader(self.languages, gpu=use_gpu)

                    # Verify reader initialization
                    if self.reader is None:
                        raise ValueError("Failed to initialize EasyOCR reader")

                    # Cache the reader at class level
                    OCRProcessor._shared_reader = self.reader
                    OCRProcessor._shared_languages = cache_key

                    logger.info(f"EasyOCR reader initialized successfully with GPU={use_gpu}")
                except Exception as e:
                    logger.error(f"Failed to initialize EasyOCR reader: {str(e)}")
                    self.reader = None
                    raise RuntimeError(f"EasyOCR initialization failed: {str(e)}") from e

    def detect_text(
        self, image: Image.Image, confidence_threshold: float = 0.5, timeout_seconds: int = 5
    ) -> List[Dict[str, Any]]:
        """Detect text in an image using EasyOCR.

        Args:
            image: PIL Image to process
            confidence_threshold: Minimum confidence for text detection
            timeout_seconds: Maximum time to wait for OCR

        Returns:
            List of text detection dictionaries
        """
        try:
            # Try to initialize reader, catch any exceptions
            try:
                self._ensure_reader()
            except Exception as e:
                logger.error(f"Failed to initialize OCR reader: {str(e)}")
                return []

            # Ensure reader was properly initialized
            if self.reader is None:
                logger.error("OCR reader is None after initialization")
                return []

            # Convert PIL Image to numpy array
            image_np = np.array(image)

            # Run readtext in a worker thread so we can enforce a real timeout.
            _fut = OCRProcessor._ocr_executor.submit(
                self.reader.readtext,
                image_np,
                paragraph=False,
                text_threshold=confidence_threshold,
            )
            try:
                results = _fut.result(timeout=timeout_seconds)
            except FutureTimeoutError:
                logger.warning("OCR timed out after %ss", timeout_seconds)
                return []
            except Exception as e:
                logger.warning("OCR failed: %s", e)
                return []

            detections = []
            img_width, img_height = image.size

            for box, text, conf in results:
                # Ensure conf is float
                conf_float = float(conf)
                if conf_float < confidence_threshold:
                    continue

                # Convert box format to [x1, y1, x2, y2]
                # Ensure box points are properly typed as float
                x1 = min(float(point[0]) for point in box) / img_width
                y1 = min(float(point[1]) for point in box) / img_height
                x2 = max(float(point[0]) for point in box) / img_width
                y2 = max(float(point[1]) for point in box) / img_height

                detections.append(
                    {
                        "type": "text",
                        "bbox": [x1, y1, x2, y2],
                        "content": text,
                        "confidence": conf,
                        "interactivity": False,  # Text is typically non-interactive
                    }
                )

            return detections
        except Exception as e:
            logger.error(f"Unexpected error in OCR processing: {str(e)}")
            return []
