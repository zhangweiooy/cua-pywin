"""SOM - Computer Vision and OCR library for detecting and analyzing UI elements."""

__version__ = "0.3.0"

from .detect import OmniParser
from .models import (
    BoundingBox,
    IconElement,
    ParseResult,
    ParserMetadata,
    TextElement,
    UIElement,
)

__all__ = [
    "OmniParser",
    "BoundingBox",
    "UIElement",
    "IconElement",
    "TextElement",
    "ParserMetadata",
    "ParseResult",
]
