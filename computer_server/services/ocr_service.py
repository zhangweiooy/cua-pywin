"""Shared OCR service for learning and future vision consumers."""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

logger = logging.getLogger(__name__)


class OCRService:
    """Lazy OCRProcessor wrapper with PNG decoding."""

    def __init__(self, languages: Optional[list[str]] = None) -> None:
        self._languages = languages or ["ch_sim", "en"]
        self._ocr = None
        self._failed = False

    def ensure_ready(self) -> bool:
        if self._ocr is not None:
            return True
        if self._failed:
            return False
        try:
            from som.ocr import OCRProcessor

            self._ocr = OCRProcessor(languages=self._languages)
            return True
        except ImportError:
            logger.warning("som OCR not available; vision enrichment disabled")
        except Exception:
            logger.warning("Failed to initialize OCR; vision enrichment disabled")
        self._failed = True
        return False

    def detect_png(
        self,
        png_bytes: bytes,
        confidence_threshold: float = 0.4,
        timeout_seconds: float = 3.0,
    ) -> list[dict] | None:
        """OCR a PNG image. Returns detection dicts or None on failure."""
        if not self.ensure_ready():
            return None
        try:
            from PIL import Image

            img = Image.open(BytesIO(png_bytes))
        except Exception:
            return None
        try:
            return self._ocr.detect_text(
                img,
                confidence_threshold=confidence_threshold,
                timeout_seconds=timeout_seconds,
            )
        except Exception:
            logger.debug("OCR detect_png failed", exc_info=True)
            return None


_ocr_service: OCRService | None = None


def get_ocr_service() -> OCRService:
    global _ocr_service
    if _ocr_service is None:
        _ocr_service = OCRService()
    return _ocr_service
