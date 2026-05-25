"""SOM (Set-of-Mark) Vision bridge for the computer-server.

Wraps the SOM OmniParser behind a stable, async-friendly API that produces
CachedElement records compatible with the rest of the handler stack.

SOM is an optional dependency; this module never forces the heavy ML stack to
load. OmniParser is expensive to construct (loads YOLO + OCR models), so a
module-level singleton is reused across calls.

OmniParser.parse() is synchronous and CPU/GPU bound; it is dispatched to
a thread-pool executor so the event loop stays responsive.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from computer_server.handlers.element_cache import CachedElement

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class VisionResult:
    """Result of a SOM vision detection pass."""

    elements: List[CachedElement]
    annotated_image_base64: Optional[str] = None
    metadata: Dict[str, Any] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


# ---------------------------------------------------------------------------
# Lazy import / singleton state
# ---------------------------------------------------------------------------

_parser_instance: Optional[Any] = None
_parser_lock = threading.Lock()
_som_available: Optional[bool] = None
_som_available_lock = threading.Lock()


def is_available() -> bool:
    """Return True if the som package can be imported."""
    global _som_available
    if _som_available is None:
        with _som_available_lock:
            if _som_available is None:
                try:
                    from som.detect import OmniParser  # noqa: F401

                    _som_available = True
                except Exception as exc:
                    logger.info("SOM not available: %s", exc)
                    _som_available = False
    return _som_available


def _get_parser() -> Any:
    """Return a lazily-constructed module-level OmniParser singleton."""
    global _parser_instance
    if _parser_instance is None:
        with _parser_lock:
            if _parser_instance is None:
                from som.detect import OmniParser

                _parser_instance = OmniParser(ocr_languages=["ch_sim", "en"])
    return _parser_instance


def warm_up() -> None:
    """Pre-initialize OmniParser in a background thread at server startup.

    YOLO model download/load and EasyOCR reader init run in parallel to
    minimize wall-clock time.  Fire-and-forget — does not block server startup.

    ALL heavy imports (torch, cv2, ultralytics, ...) are pushed into the
    background thread so the MCP server can start accepting requests
    immediately.

    Stdout is NOT redirected because contextlib.redirect_stdout is NOT
    thread-local on Python 3.12 on Windows — the background thread's
    redirect_stdout(StringIO()) replaces sys.stdout globally, which causes
    stdio_server() (running on the main thread) to crash with:
    AttributeError: '_io.StringIO' object has no attribute 'buffer'

    This is safe because warm_up() fires before mcp.run() starts the stdio
    transport — no client is connected yet, so there is no JSON-RPC stream
    for library output to corrupt.
    """

    def _init() -> None:

        try:
            if not is_available():
                logger.debug("SOM not available, skipping warm-up")
                return
            logger.info("SOM available, starting background warm-up...")

            from concurrent.futures import ThreadPoolExecutor

            parser = _get_parser()
            logger.info("SOM parser singleton created; loading YOLO + OCR in parallel...")

            with ThreadPoolExecutor(max_workers=2) as ex:
                f_yolo = ex.submit(parser.detector.load_model)
                f_ocr = ex.submit(parser.ocr._ensure_reader)
                f_yolo.result()
                f_ocr.result()

            logger.info("SOM OmniParser pre-warmed — YOLO + OCR ready")
        except Exception as exc:
            logger.warning("SOM pre-warm failed: %s", exc)

    t = threading.Thread(target=_init, name="som-warmup", daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _element_to_cached(
    elem: Any,
    index: int,
    screen_width: int,
    screen_height: int,
) -> CachedElement:
    """Convert a SOM UIElement to a CachedElement with pixel-space coordinates.

    SOM returns normalized (0-1) bounding boxes; we convert to the pixel-space
    dimensions of the screenshot the caller supplied.
    """
    bbox = elem.bbox.coordinates  # [x1, y1, x2, y2] normalized
    left = int(bbox[0] * screen_width)
    top = int(bbox[1] * screen_height)
    right = int(bbox[2] * screen_width)
    bottom = int(bbox[3] * screen_height)
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2

    elem_type = getattr(elem, "type", None) or "icon"
    is_text = elem_type == "text"

    if is_text:
        name: Optional[str] = getattr(elem, "content", None)
        actions: List[str] = []
    else:
        name = None
        actions = ["click"]

    return CachedElement(
        element_index=index,
        source="vision",
        center_x=center_x,
        center_y=center_y,
        bbox=(left, top, right, bottom),
        control_type=elem_type,
        name=name,
        actions=actions,
        platform_ref=None,
    )


def _build_metadata(result: Any) -> Dict[str, Any]:
    """Extract lightweight metadata from a ParseResult."""
    elements = getattr(result, "elements", []) or []
    num_icons = sum(1 for e in elements if getattr(e, "type", None) == "icon")
    num_text = sum(1 for e in elements if getattr(e, "type", None) == "text")
    md = getattr(result, "metadata", None)
    return {
        "num_icons": num_icons,
        "num_text": num_text,
        "latency": getattr(md, "latency", None),
        "device": getattr(md, "device", None),
    }


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------


async def detect(
    screenshot_bytes: bytes,
    screen_width: int,
    screen_height: int,
    box_threshold: float = 0.3,
    use_ocr: bool = True,
    vision_index_start: int = 10000,
) -> Optional[VisionResult]:
    """Run SOM detection on screenshot bytes and return a VisionResult.

    Returns None if SOM is not available or if detection fails; never raises
    to the caller.
    """
    if not is_available():
        return None

    try:
        parser = _get_parser()
    except Exception as exc:
        logger.warning("Failed to initialize SOM OmniParser: %s", exc)
        return None

    loop = asyncio.get_running_loop()

    def _run_parse() -> Any:
        return parser.parse(
            screenshot_bytes,
            box_threshold=box_threshold,
            use_ocr=use_ocr,
        )

    try:
        result = await loop.run_in_executor(None, _run_parse)
    except Exception as exc:
        logger.warning("SOM parse failed: %s", exc)
        return None

    try:
        raw_elements = list(getattr(result, "elements", []) or [])
        cached: List[CachedElement] = []
        next_index = vision_index_start

        for elem in raw_elements:
            try:
                cached.append(
                    _element_to_cached(elem, next_index, screen_width, screen_height)
                )
                next_index += 1
            except Exception as inner_exc:
                logger.warning("Skipping malformed SOM element: %s", inner_exc)
                continue

        return VisionResult(
            elements=cached,
            annotated_image_base64=getattr(result, "annotated_image_base64", None),
            metadata=_build_metadata(result),
        )
    except Exception as exc:
        logger.warning("Failed to convert SOM result: %s", exc)
        return None


def detect_sync(
    screenshot_bytes: bytes,
    screen_width: int,
    screen_height: int,
    box_threshold: float = 0.3,
    use_ocr: bool = True,
    vision_index_start: int = 10000,
) -> Optional[VisionResult]:
    """Synchronous SOM detection — safe to run in ``loop.run_in_executor``.

    Unlike :func:`detect`, this does NOT touch the asyncio event loop.
    The entire pipeline (import, model load, inference, conversion)
    runs on the calling thread.  Use this from async handlers to keep
    heavy ML imports off the event-loop thread.
    """
    if not is_available():
        logger.warning("SOM detect_sync: not available")
        return None

    try:
        parser = _get_parser()
    except Exception as exc:
        logger.warning("SOM detect_sync: parser init failed: %s", exc)
        return None

    try:
        result = parser.parse(
            screenshot_bytes,
            box_threshold=box_threshold,
            use_ocr=use_ocr,
        )
        logger.info("SOM detect_sync: parse complete — %d elements", len(getattr(result, "elements", []) or []))
    except Exception as exc:
        logger.warning("SOM detect_sync: parse failed: %s", exc)
        return None

    try:
        raw_elements = list(getattr(result, "elements", []) or [])
        cached: List[CachedElement] = []
        next_index = vision_index_start

        for elem in raw_elements:
            try:
                cached.append(
                    _element_to_cached(elem, next_index, screen_width, screen_height)
                )
                next_index += 1
            except Exception as inner_exc:
                logger.warning("Skipping malformed SOM element: %s", inner_exc)
                continue

        return VisionResult(
            elements=cached,
            annotated_image_base64=getattr(result, "annotated_image_base64", None),
            metadata=_build_metadata(result),
        )
    except Exception as exc:
        logger.warning("Failed to convert SOM result: %s", exc)
        return None
