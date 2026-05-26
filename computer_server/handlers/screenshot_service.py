"""Shared screenshot image sizing and encoding helpers."""

from __future__ import annotations

from io import BytesIO
from typing import Tuple

from PIL import Image


def resize_to_max_dimension(img: Image.Image, max_dimension: int) -> Tuple[Image.Image, bool]:
    """Resize image to fit within max_dimension while preserving aspect ratio."""
    width, height = img.size
    if max(width, height) <= max_dimension:
        return img, False
    if width > height:
        new_width = max_dimension
        new_height = int(height * max_dimension / width)
    else:
        new_height = max_dimension
        new_width = int(width * max_dimension / height)
    return img.resize((new_width, new_height), Image.Resampling.LANCZOS), True


def encode_image(img: Image.Image, fmt: str, quality: int = 95) -> bytes:
    """Encode a PIL image as PNG/JPEG bytes."""
    buffered = BytesIO()
    if fmt == "jpeg":
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.save(buffered, format="JPEG", quality=quality, optimize=True)
    else:
        img.save(buffered, format="PNG", optimize=True)
    return buffered.getvalue()


def encode_for_mcp_image(
    img: Image.Image,
    fmt: str,
    quality: int,
    max_bytes: int = 900_000,
) -> bytes:
    """Encode image and lower JPEG quality once if it exceeds max_bytes."""
    image_data = encode_image(img, fmt, quality)
    if fmt == "jpeg" and len(image_data) > max_bytes:
        image_data = encode_image(img, fmt, 70)
    return image_data
