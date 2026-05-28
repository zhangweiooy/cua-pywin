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
import time
import warnings
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Dict, List, Optional

from computer_server.handlers.element_cache import CachedElement
from computer_server.services.icon_label_service import IconLabel

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public data structures
# ---------------------------------------------------------------------------


@dataclass
class VisionResult:
    """Result of a SOM vision detection pass."""

    elements: List[CachedElement]
    annotated_image_base64: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Lazy import / singleton state
# ---------------------------------------------------------------------------

_parser_instance: Optional[Any] = None
_parser_lock = threading.RLock()
_parser_condition = threading.Condition(_parser_lock)
_parse_lock = threading.Lock()
_parser_state = "idle"  # idle | loading | ready | failed
_parser_failure: Optional[str] = None
_som_available: Optional[bool] = None
_som_available_lock = threading.Lock()


def _set_parser_state(state: str, failure: Optional[str] = None) -> None:
    global _parser_state, _parser_failure
    with _parser_condition:
        _parser_state = state
        _parser_failure = failure
        _parser_condition.notify_all()


def parser_status() -> Dict[str, Optional[str]]:
    """Return current SOM parser lifecycle status."""
    with _parser_condition:
        return {"state": _parser_state, "failure": _parser_failure}


def _wait_if_loading(timeout: float = 30.0) -> bool:
    """Wait for background warm-up to finish. Returns False if still loading."""
    import time

    deadline = time.monotonic() + timeout
    with _parser_condition:
        while _parser_state == "loading":
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _parser_condition.wait(timeout=remaining)
        return _parser_state != "loading"


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
            with _parser_condition:
                if _parser_state in ("loading", "ready"):
                    logger.debug("SOM warm-up skipped; state=%s", _parser_state)
                    return
                _set_parser_state("loading")
            logger.info("SOM available, starting background warm-up...")

            from concurrent.futures import ThreadPoolExecutor

            parser = _get_parser()
            logger.info("SOM parser singleton created; loading YOLO + OCR + TinyCLIP in parallel...")

            with _parse_lock:
                with ThreadPoolExecutor(max_workers=3) as ex:
                    f_yolo = ex.submit(parser.detector.load_model)
                    f_ocr = ex.submit(parser.ocr._ensure_reader)
                    try:
                        from computer_server.services.icon_label_service import warm_up as _warm_icon_labels

                        f_icon = ex.submit(_warm_icon_labels)
                    except Exception:
                        f_icon = None
                    f_yolo.result()
                    f_ocr.result()
                    if f_icon is not None:
                        f_icon.result()

            _set_parser_state("ready")
            logger.info("SOM OmniParser pre-warmed — YOLO + OCR ready")
        except Exception as exc:
            _set_parser_state("failed", str(exc))
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
    icon_label: Optional[IconLabel] = None,
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

    metadata: Dict[str, Any] = {}
    if is_text:
        name: Optional[str] = getattr(elem, "content", None)
        actions: List[str] = []
    else:
        name = icon_label.label if icon_label is not None else None
        actions = ["click"]
        if icon_label is not None:
            metadata = {
                "icon_label": icon_label.label,
                "icon_label_score": icon_label.score,
                "icon_label_margin": icon_label.margin,
                "icon_label_source": "tinyclip",
            }

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
        metadata=metadata,
    )


def _crop_icon_elements(
    screenshot_bytes: bytes,
    raw_elements: List[Any],
    screen_width: int,
    screen_height: int,
) -> tuple[List[int], List[Any]]:
    """Return raw element positions and PIL crops for icon elements."""
    from PIL import Image

    image = Image.open(BytesIO(screenshot_bytes)).convert("RGB")
    positions: List[int] = []
    crops: List[Any] = []

    for pos, elem in enumerate(raw_elements):
        if getattr(elem, "type", None) != "icon":
            continue
        try:
            bbox = elem.bbox.coordinates
            left = max(0, int(bbox[0] * screen_width))
            top = max(0, int(bbox[1] * screen_height))
            right = min(screen_width, int(bbox[2] * screen_width))
            bottom = min(screen_height, int(bbox[3] * screen_height))
            if right <= left or bottom <= top:
                continue
            width = right - left
            height = bottom - top
            pad_x = max(2, int(width * 0.15))
            pad_y = max(2, int(height * 0.15))
            crop_box = (
                max(0, left - pad_x),
                max(0, top - pad_y),
                min(screen_width, right + pad_x),
                min(screen_height, bottom + pad_y),
            )
            positions.append(pos)
            crops.append(image.crop(crop_box))
        except Exception as exc:
            logger.debug("Skipping icon crop for TinyCLIP labeling: %s", exc)
    return positions, crops


def _label_icon_elements(
    screenshot_bytes: bytes,
    raw_elements: List[Any],
    screen_width: int,
    screen_height: int,
) -> Dict[int, IconLabel]:
    """Batch-label all icon crops and return labels keyed by raw element position."""
    try:
        from computer_server.services.icon_label_service import label_icons

        positions, crops = _crop_icon_elements(
            screenshot_bytes,
            raw_elements,
            screen_width,
            screen_height,
        )
        if not crops:
            return {}
        labels = label_icons(crops)
        return {
            pos: label
            for pos, label in zip(positions, labels)
            if label is not None
        }
    except Exception as exc:
        logger.debug("TinyCLIP icon labeling skipped: %s", exc, exc_info=True)
        return {}


def _convert_parse_result(
    result: Any,
    screenshot_bytes: bytes,
    screen_width: int,
    screen_height: int,
    vision_index_start: int,
) -> VisionResult:
    raw_elements = list(getattr(result, "elements", []) or [])
    stage_start = time.perf_counter()
    icon_labels = _label_icon_elements(
        screenshot_bytes,
        raw_elements,
        screen_width,
        screen_height,
    )
    tinyclip_latency = time.perf_counter() - stage_start

    stage_start = time.perf_counter()
    cached: List[CachedElement] = []
    next_index = vision_index_start

    for raw_pos, elem in enumerate(raw_elements):
        try:
            cached.append(
                _element_to_cached(
                    elem,
                    next_index,
                    screen_width,
                    screen_height,
                    icon_label=icon_labels.get(raw_pos),
                )
            )
            next_index += 1
        except Exception as inner_exc:
            logger.warning("Skipping malformed SOM element: %s", inner_exc)
            continue

    metadata = _build_metadata(result)
    stage_timings = dict(metadata.get("stage_timings", {}) or {})
    stage_timings["tinyclip_latency"] = tinyclip_latency
    stage_timings["bridge_convert_latency"] = time.perf_counter() - stage_start
    metadata["stage_timings"] = stage_timings
    metadata["icon_label_count"] = len(icon_labels)
    metadata["icon_labeler"] = "tinyclip" if icon_labels else None
    return VisionResult(
        elements=cached,
        annotated_image_base64=getattr(result, "annotated_image_base64", None),
        metadata=metadata,
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
        "stage_timings": dict(getattr(md, "stage_timings", {}) or {}),
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
    """Deprecated async SOM detection wrapper.

    MCP stdio handlers should call detect_sync() after startup warm-up. The
    async executor path is retained for external callers that are not running
    under FastMCP's anyio stdio transport.

    Returns None if SOM is not available or if detection fails; never raises
    to the caller.
    """
    warnings.warn(
        "som_bridge.detect() is deprecated for MCP handlers; use detect_sync() "
        "after warm_up() has started.",
        DeprecationWarning,
        stacklevel=2,
    )
    if not is_available():
        return None
    if not _wait_if_loading(timeout=30.0):
        logger.info("SOM detect skipped: parser still warming up")
        return VisionResult(elements=[], metadata={"status": "warming_up"})

    try:
        with _parser_condition:
            if _parser_state == "failed":
                return VisionResult(elements=[], metadata={"status": "failed", "failure": _parser_failure})
            if _parser_state == "idle":
                _set_parser_state("loading")
        parser = _get_parser()
    except Exception as exc:
        _set_parser_state("failed", str(exc))
        logger.warning("Failed to initialize SOM OmniParser: %s", exc)
        return None

    loop = asyncio.get_running_loop()

    def _run_parse() -> Any:
        with _parse_lock:
            return parser.parse(
                screenshot_bytes,
                box_threshold=box_threshold,
                use_ocr=use_ocr,
            )

    try:
        result = await loop.run_in_executor(None, _run_parse)
        _set_parser_state("ready")
    except Exception as exc:
        _set_parser_state("failed", str(exc))
        logger.warning("SOM parse failed: %s", exc)
        return None

    try:
        return _convert_parse_result(
            result,
            screenshot_bytes,
            screen_width,
            screen_height,
            vision_index_start,
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
    if not _wait_if_loading(timeout=30.0):
        logger.info("SOM detect_sync: parser still warming up")
        return VisionResult(elements=[], metadata={"status": "warming_up"})

    try:
        with _parser_condition:
            if _parser_state == "failed":
                return VisionResult(elements=[], metadata={"status": "failed", "failure": _parser_failure})
            if _parser_state == "idle":
                _set_parser_state("loading")
        parser = _get_parser()
    except Exception as exc:
        _set_parser_state("failed", str(exc))
        logger.warning("SOM detect_sync: parser init failed: %s", exc)
        return None

    try:
        with _parse_lock:
            result = parser.parse(
                screenshot_bytes,
                box_threshold=box_threshold,
                use_ocr=use_ocr,
            )
        _set_parser_state("ready")
        logger.info("SOM detect_sync: parse complete — %d elements", len(getattr(result, "elements", []) or []))
    except Exception as exc:
        logger.warning("SOM detect_sync: parse failed: %s", exc)
        return None

    try:
        return _convert_parse_result(
            result,
            screenshot_bytes,
            screen_width,
            screen_height,
            vision_index_start,
        )
    except Exception as exc:
        logger.warning("Failed to convert SOM result: %s", exc)
        return None
