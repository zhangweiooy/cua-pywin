"""SOM - Computer Vision and OCR library for detecting and analyzing UI elements."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__version__ = "0.3.2"

if TYPE_CHECKING:
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

_MODEL_EXPORTS = {
    "BoundingBox",
    "IconElement",
    "ParseResult",
    "ParserMetadata",
    "TextElement",
    "UIElement",
}


def __getattr__(name: str) -> Any:
    """Preserve public imports without loading the detector eagerly."""
    if name == "OmniParser":
        from .detect import OmniParser

        globals()[name] = OmniParser
        return OmniParser
    if name in _MODEL_EXPORTS:
        from . import models

        value = getattr(models, name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
