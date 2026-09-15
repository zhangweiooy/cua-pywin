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
import os
import re
import threading
import time
import warnings
from dataclasses import dataclass, field
from io import BytesIO
from typing import Any, Dict, List, Optional

from computer_server.handlers.element_cache import CachedElement
from computer_server.services.icon_caption_service import IconCaption

logger = logging.getLogger(__name__)

_ADVISORY_ONLY_CAPTION_WORDS = {
    "box",
    "button",
    "circle",
    "icon",
    "outline",
    "rectangle",
    "shape",
    "square",
    "symbol",
}


def _trace_id() -> str:
    return f"{os.getpid()}-{time.monotonic_ns()}"


def _exception_details(exc: BaseException) -> tuple[str, str, str]:
    current = exc
    messages = []
    seen = set()
    while id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current))
        next_exc = current.__cause__ or current.__context__
        if next_exc is None:
            break
        current = next_exc

    combined = " ".join(messages)
    if "Bundled RapidOCR model files are missing" in combined:
        error_code = "cua_rapidocr_bundle_missing"
    elif "model_root_dir" in combined and "does not exist" in combined:
        error_code = "rapidocr_model_root_missing"
    else:
        error_code = "unclassified"
    return type(exc).__name__, type(current).__name__, error_code


def _failure_fields(exc: BaseException) -> str:
    error_type, root_error_type, error_code = _exception_details(exc)
    return (
        f" error_type={error_type} root_error_type={root_error_type}"
        f" error_code={error_code}"
    )


def _log_complete(
    *,
    event: str,
    trace_field: str,
    trace_id: str,
    started: float,
    status: str,
    level: int = logging.INFO,
    details: str = "",
) -> None:
    logger.log(
        level,
        "event=%s %s=%s pid=%d total_duration_ms=%.2f status=%s%s",
        event,
        trace_field,
        trace_id,
        os.getpid(),
        (time.perf_counter() - started) * 1000,
        status,
        details,
    )


def _timed_stage(
    *,
    event: str,
    trace_field: str,
    trace_id: str,
    stage: str,
    operation: Any,
    details: str = "",
) -> Any:
    started = time.perf_counter()
    try:
        result = operation()
    except Exception as exc:
        error_type, root_error_type, error_code = _exception_details(exc)
        logger.warning(
            "event=%s %s=%s pid=%d stage=%s duration_ms=%.2f status=failed "
            "error_type=%s root_error_type=%s error_code=%s%s",
            event,
            trace_field,
            trace_id,
            os.getpid(),
            stage,
            (time.perf_counter() - started) * 1000,
            error_type,
            root_error_type,
            error_code,
            details,
        )
        raise
    logger.info(
        "event=%s %s=%s pid=%d stage=%s duration_ms=%.2f status=ready%s",
        event,
        trace_field,
        trace_id,
        os.getpid(),
        stage,
        (time.perf_counter() - started) * 1000,
        details,
    )
    return result


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
_parser_recovery_attempted = False
_som_available: Optional[bool] = None
_som_available_lock = threading.Lock()
_latest_warmup_id: Optional[str] = None


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
                    logger.warning("SOM not available: %s", exc, exc_info=True)
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


def _start_icon_semantic_warmup(warmup_id: str) -> None:
    """Load the selected semantic model after detector/OCR become ready."""

    def _init_semantics() -> None:
        try:
            from computer_server.services.icon_caption_service import (
                warm_up as _warm_icon_semantics,
            )

            ready = _timed_stage(
                event="som_warmup_stage",
                trace_field="warmup_id",
                trace_id=warmup_id,
                stage="florence2",
                operation=_warm_icon_semantics,
            )
            if not ready:
                logger.warning(
                    "event=som_warmup_optional_backend warmup_id=%s "
                    "stage=florence2 status=unavailable", warmup_id,
                )
        except Exception as icon_exc:
            error_type, root_error_type, error_code = _exception_details(icon_exc)
            logger.warning(
                "event=som_warmup_optional_backend warmup_id=%s pid=%d "
                "stage=florence2 status=failed error_type=%s "
                "root_error_type=%s error_code=%s",
                warmup_id,
                os.getpid(),
                error_type,
                root_error_type,
                error_code,
            )

    try:
        threading.Thread(
            target=_init_semantics,
            name="icon-semantic-warmup",
            daemon=True,
        ).start()
    except Exception:
        logger.warning("Could not schedule optional icon semantics", exc_info=True)


def _run_warm_up(warmup_id: str) -> None:
    global _parser_recovery_attempted
    warmup_started = time.perf_counter()
    logger.info(
        "event=som_warmup_start warmup_id=%s pid=%d",
        warmup_id,
        os.getpid(),
    )

    try:
        if not is_available():
            _set_parser_state("idle")
            _log_complete(
                event="som_warmup_complete",
                trace_field="warmup_id",
                trace_id=warmup_id,
                started=warmup_started,
                status="unavailable",
            )
            return

        from concurrent.futures import ThreadPoolExecutor

        parser = _timed_stage(
            event="som_warmup_stage",
            trace_field="warmup_id",
            trace_id=warmup_id,
            stage="parser",
            operation=_get_parser,
        )

        with _parse_lock:
            ocr_backend = str(getattr(parser.ocr, "backend", "unknown"))
            ocr_details = f" backend={ocr_backend}"
            if ocr_backend == "rapidocr":
                model_dir = parser.ocr._rapidocr_model_dir()
                model_root_source = (
                    "override" if os.getenv("CUA_RAPIDOCR_MODEL_DIR") else "bundled"
                )
                ocr_details += (
                    f" model_root_source={model_root_source}"
                    f" model_root_exists={str(model_dir.is_dir()).lower()}"
                )
            with ThreadPoolExecutor(max_workers=2) as ex:
                f_yolo = ex.submit(
                    _timed_stage,
                    event="som_warmup_stage",
                    trace_field="warmup_id",
                    trace_id=warmup_id,
                    stage="yolo",
                    operation=parser.detector.load_model,
                )
                f_ocr = ex.submit(
                    _timed_stage,
                    event="som_warmup_stage",
                    trace_field="warmup_id",
                    trace_id=warmup_id,
                    stage="rapidocr",
                    operation=parser.ocr._ensure_reader,
                    details=ocr_details,
                )
                f_yolo.result()
                f_ocr.result()

        with _parser_condition:
            _parser_recovery_attempted = False
        _set_parser_state("ready")
        _log_complete(
            event="som_warmup_complete",
            trace_field="warmup_id",
            trace_id=warmup_id,
            started=warmup_started,
            status="ready",
            details=" captions=scheduled",
        )
        _start_icon_semantic_warmup(warmup_id)
    except Exception as exc:
        _set_parser_state("failed", str(exc))
        _log_complete(
            event="som_warmup_complete",
            trace_field="warmup_id",
            trace_id=warmup_id,
            started=warmup_started,
            status="failed",
            level=logging.WARNING,
            details=_failure_fields(exc),
        )


def _schedule_warm_up(*, retry_failed: bool = False) -> bool:
    global _latest_warmup_id, _parser_recovery_attempted
    schedule_started = time.perf_counter()
    warmup_id = _trace_id()
    with _parser_condition:
        state = _parser_state
        if state in {"loading", "ready"}:
            _log_complete(
                event="som_warmup_complete",
                trace_field="warmup_id",
                trace_id=warmup_id,
                started=schedule_started,
                status="skipped",
                details=f" parser_state={state}",
            )
            return False
        if state == "failed":
            if not retry_failed or _parser_recovery_attempted:
                reason = (
                    "already_attempted"
                    if _parser_recovery_attempted
                    else "retry_not_requested"
                )
                _log_complete(
                    event="som_warmup_complete",
                    trace_field="warmup_id",
                    trace_id=warmup_id,
                    started=schedule_started,
                    status="skipped",
                    details=f" parser_state=failed recovery_skip_reason={reason}",
                )
                return False
            _parser_recovery_attempted = True
        _set_parser_state("loading")
        _latest_warmup_id = warmup_id

    try:
        threading.Thread(
            target=lambda: _run_warm_up(warmup_id),
            name="som-warmup",
            daemon=True,
        ).start()
    except Exception as exc:
        _set_parser_state("failed", str(exc))
        _log_complete(
            event="som_warmup_complete",
            trace_field="warmup_id",
            trace_id=warmup_id,
            started=schedule_started,
            status="failed",
            level=logging.WARNING,
            details=_failure_fields(exc),
        )
        return False
    return True


def warm_up() -> None:
    """Pre-initialize OmniParser in a background thread at server startup.

    Bundled YOLO model load and OCR reader init run in parallel to
    minimize wall-clock time.  Fire-and-forget — does not block server startup.

    All heavy imports (NumPy, OpenCV, ONNX Runtime, ...) are pushed into the
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

    _schedule_warm_up()


def retry_failed_warm_up() -> bool:
    """Retry one failed SOM warm-up when explicitly requested by force_som."""
    return _schedule_warm_up(retry_failed=True)


# ---------------------------------------------------------------------------
# Conversion helpers
# ---------------------------------------------------------------------------


def _element_to_cached(
    elem: Any,
    index: int,
    screen_width: int,
    screen_height: int,
    icon_caption: Optional[IconCaption] = None,
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
    confidence = getattr(elem, "confidence", None)

    metadata: Dict[str, Any] = {}
    arbitration_metadata = dict(getattr(elem, "metadata", {}) or {})
    for key in (
        "semantic_collision_id",
        "semantic_collision_role",
        "semantic_uncertainty",
        "semantic_resolution",
        "semantic_alternatives",
    ):
        if key in arbitration_metadata:
            metadata[key] = arbitration_metadata[key]
    if confidence is not None:
        try:
            metadata["vision_confidence"] = float(confidence)
        except (TypeError, ValueError):
            pass
    if is_text:
        name: Optional[str] = getattr(elem, "content", None)
        actions: List[str] = []
        if "vision_confidence" in metadata:
            metadata["ocr_confidence"] = metadata["vision_confidence"]
    else:
        name = icon_caption.label if icon_caption is not None else None
        actions = ["click"]
        if icon_caption is not None:
            label_source = f"omniparser_v2_{icon_caption.backend}"
            metadata.update(
                {
                    "raw_caption": icon_caption.raw_caption,
                    "normalized_label": icon_caption.normalized_label,
                    "icon_label": icon_caption.normalized_label,
                    "icon_label_source": label_source,
                    "icon_label_confidence": icon_caption.confidence,
                    "icon_label_confidence_provenance": (
                        icon_caption.confidence_provenance
                    ),
                }
            )

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
            positions.append(pos)
            crops.append(
                image.crop((left, top, right, bottom)).resize(
                    (64, 64),
                    Image.Resampling.LANCZOS,
                )
            )
        except Exception as exc:
            logger.debug("Skipping icon crop for Florence-2 captioning: %s", exc)
    return positions, crops


def _label_icon_elements(
    screenshot_bytes: bytes,
    raw_elements: List[Any],
    screen_width: int,
    screen_height: int,
    *,
    retry_failed_warmup: bool = False,
) -> tuple[Dict[int, IconCaption], str]:
    """Batch-caption all icon crops and return captions by raw element position."""
    try:
        from computer_server.services.icon_caption_service import (
            get_icon_caption_service,
        )

        positions, crops = _crop_icon_elements(
            screenshot_bytes,
            raw_elements,
            screen_width,
            screen_height,
        )
        if not crops:
            return {}, "not_run"
        service = get_icon_caption_service()
        if retry_failed_warmup:
            service.start_background_warm_up(retry_failed=True)
        captions = service.caption_icons(crops)
        status = str(service.status.get("state") or "unknown")
        return (
            {
                pos: caption
                for pos, caption in zip(positions, captions)
                if caption is not None
            },
            status,
        )
    except Exception as exc:
        logger.debug("Florence-2 icon captioning skipped: %s", exc, exc_info=True)
        return {}, "inference_failed"


def _caption_supports_icon_preference(caption: Optional[IconCaption]) -> bool:
    """Require a specific caption before preferring an icon over OCR."""
    if caption is None or caption.label is None:
        return False
    words = re.findall(r"[a-z0-9]+", caption.label.casefold())
    return any(word not in _ADVISORY_ONLY_CAPTION_WORDS for word in words)


def _convert_parse_result(
    result: Any,
    screenshot_bytes: bytes,
    screen_width: int,
    screen_height: int,
    vision_index_start: int,
    *,
    retry_failed_warmup: bool = False,
) -> VisionResult:
    raw_elements = list(getattr(result, "elements", []) or [])
    stage_start = time.perf_counter()
    icon_captions, icon_semantic_status = _label_icon_elements(
        screenshot_bytes,
        raw_elements,
        screen_width,
        screen_height,
        retry_failed_warmup=retry_failed_warmup,
    )
    icon_semantic_latency = time.perf_counter() - stage_start

    resolved_collision_ids = {
        str((getattr(elem, "metadata", {}) or {}).get("semantic_collision_id"))
        for raw_pos, elem in enumerate(raw_elements)
        if getattr(elem, "type", None) == "icon"
        and (getattr(elem, "metadata", {}) or {}).get("semantic_collision_id")
        and _caption_supports_icon_preference(icon_captions.get(raw_pos))
    }
    collision_text_alternatives: Dict[str, List[Dict[str, Any]]] = {}
    for elem in raw_elements:
        elem_metadata = dict(getattr(elem, "metadata", {}) or {})
        collision_id = elem_metadata.get("semantic_collision_id")
        if not collision_id or getattr(elem, "type", None) != "text":
            continue
        alternatives = collision_text_alternatives.setdefault(str(collision_id), [])
        if len(alternatives) >= 3:
            continue
        alternative: Dict[str, Any] = {
            "control_type": "text",
            "name": str(getattr(elem, "content", "") or "")[:16],
        }
        confidence = getattr(elem, "confidence", None)
        if confidence is not None:
            try:
                alternative["confidence"] = float(confidence)
            except (TypeError, ValueError):
                pass
        alternatives.append(alternative)

    stage_start = time.perf_counter()
    cached: List[CachedElement] = []
    next_index = vision_index_start

    for raw_pos, elem in enumerate(raw_elements):
        try:
            arbitration_metadata = dict(getattr(elem, "metadata", {}) or {})
            collision_id = arbitration_metadata.get("semantic_collision_id")
            if (
                collision_id
                and str(collision_id) in resolved_collision_ids
                and getattr(elem, "type", None) == "text"
            ):
                continue
            if collision_id and str(collision_id) in resolved_collision_ids:
                arbitration_metadata["semantic_resolution"] = (
                    "icon_preferred_with_yolo_and_florence_evidence"
                )
                arbitration_metadata["semantic_uncertainty"] = (
                    "ocr_icon_collision_advisory_resolution"
                )
                arbitration_metadata["semantic_alternatives"] = (
                    collision_text_alternatives.get(str(collision_id), [])
                )
                elem.metadata = arbitration_metadata
            cached.append(
                _element_to_cached(
                    elem,
                    next_index,
                    screen_width,
                    screen_height,
                    icon_caption=icon_captions.get(raw_pos),
                )
            )
            next_index += 1
        except Exception as inner_exc:
            logger.warning("Skipping malformed SOM element: %s", inner_exc)
            continue

    metadata = _build_metadata(result)
    metadata["num_icons"] = sum(
        1 for element in cached if element.control_type == "icon"
    )
    metadata["num_text"] = sum(
        1 for element in cached if element.control_type == "text"
    )
    stage_timings = dict(metadata.get("stage_timings", {}) or {})
    stage_timings["tinyclip_latency"] = 0.0
    stage_timings["florence2_latency"] = icon_semantic_latency
    stage_timings["icon_semantic_latency"] = icon_semantic_latency
    stage_timings["bridge_convert_latency"] = time.perf_counter() - stage_start
    metadata["stage_timings"] = stage_timings
    metadata["icon_label_count"] = sum(
        1 for caption in icon_captions.values() if caption.label is not None
    )
    metadata["icon_caption_count"] = len(icon_captions)
    semantic_backend = "florence2_int8_community"
    if icon_captions:
        semantic_backend = next(iter(icon_captions.values())).backend
    else:
        try:
            from computer_server.services.icon_caption_service import (
                get_icon_caption_service,
            )

            semantic_backend = get_icon_caption_service().backend
        except Exception:
            pass
    metadata["icon_labeler"] = (
        f"omniparser_v2_{semantic_backend}"
        if icon_captions
        else None
    )
    metadata["icon_semantic_backend"] = semantic_backend
    metadata["icon_semantic_status"] = icon_semantic_status
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
        "icon_detection_error": getattr(md, "icon_detection_error", None),
        "raw_icon_count": int(getattr(md, "raw_icon_count", num_icons) or 0),
        "raw_text_count": int(getattr(md, "raw_text_count", num_text) or 0),
        "ocr_icon_collision_count": int(
            getattr(md, "ocr_icon_collision_count", 0) or 0
        ),
        "icons_suppressed_by_ocr": int(
            getattr(md, "icons_suppressed_by_ocr", 0) or 0
        ),
        "ambiguous_collisions_preserved": int(
            getattr(md, "ambiguous_collisions_preserved", 0) or 0
        ),
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
    *,
    retry_failed_warmup: bool = False,
) -> Optional[VisionResult]:
    """Synchronous SOM detection — safe to run in ``loop.run_in_executor``.

    Unlike :func:`detect`, this does NOT touch the asyncio event loop.
    The entire pipeline (import, model load, inference, conversion)
    runs on the calling thread.  Use this from async handlers to keep
    heavy ML imports off the event-loop thread.
    """
    detect_id = _trace_id()
    detect_started = time.perf_counter()
    available = is_available()
    if retry_failed_warmup and available:
        retry_failed_warm_up()
    correlated_warmup_id = _latest_warmup_id or "none"
    logger.info(
        "event=som_detect_sync_start detect_id=%s warmup_id=%s pid=%d",
        detect_id,
        correlated_warmup_id,
        os.getpid(),
    )
    if not available:
        _log_complete(
            event="som_detect_sync_complete",
            trace_field="detect_id",
            trace_id=detect_id,
            started=detect_started,
            status="unavailable",
            level=logging.WARNING,
        )
        return None
    wait_started = time.perf_counter()
    warmup_ready = _wait_if_loading(timeout=30.0)
    wait_duration_ms = (time.perf_counter() - wait_started) * 1000
    wait_state = parser_status()["state"]
    logger.info(
        "event=som_detect_sync_wait detect_id=%s pid=%d duration_ms=%.2f "
        "status=%s parser_state=%s",
        detect_id,
        os.getpid(),
        wait_duration_ms,
        "ready" if warmup_ready else "timeout",
        wait_state,
    )
    if not warmup_ready:
        _log_complete(
            event="som_detect_sync_complete",
            trace_field="detect_id",
            trace_id=detect_id,
            started=detect_started,
            status="warming_up",
        )
        return VisionResult(elements=[], metadata={"status": "warming_up"})

    try:
        with _parser_condition:
            if _parser_state == "failed":
                _log_complete(
                    event="som_detect_sync_complete",
                    trace_field="detect_id",
                    trace_id=detect_id,
                    started=detect_started,
                    status="failed",
                    level=logging.WARNING,
                    details=" stage=warmup",
                )
                return VisionResult(elements=[], metadata={"status": "failed", "failure": _parser_failure})
            if _parser_state == "idle":
                _set_parser_state("loading")
        parser = _timed_stage(
            event="som_detect_sync_stage",
            trace_field="detect_id",
            trace_id=detect_id,
            stage="parser",
            operation=_get_parser,
        )
    except Exception as exc:
        _set_parser_state("failed", str(exc))
        _log_complete(
            event="som_detect_sync_complete",
            trace_field="detect_id",
            trace_id=detect_id,
            started=detect_started,
            status="failed",
            level=logging.WARNING,
            details=" stage=parser" + _failure_fields(exc),
        )
        return None

    try:
        def _parse() -> Any:
            with _parse_lock:
                return parser.parse(
                    screenshot_bytes,
                    box_threshold=box_threshold,
                    use_ocr=use_ocr,
                )

        result = _timed_stage(
            event="som_detect_sync_stage",
            trace_field="detect_id",
            trace_id=detect_id,
            stage="parse",
            operation=_parse,
        )
        _set_parser_state("ready")
    except Exception as exc:
        _log_complete(
            event="som_detect_sync_complete",
            trace_field="detect_id",
            trace_id=detect_id,
            started=detect_started,
            status="failed",
            level=logging.WARNING,
            details=" stage=parse" + _failure_fields(exc),
        )
        return None

    try:
        converted = _timed_stage(
            event="som_detect_sync_stage",
            trace_field="detect_id",
            trace_id=detect_id,
            stage="convert",
            operation=lambda: _convert_parse_result(
                result,
                screenshot_bytes,
                screen_width,
                screen_height,
                vision_index_start,
                retry_failed_warmup=retry_failed_warmup,
            ),
        )
        _log_complete(
            event="som_detect_sync_complete",
            trace_field="detect_id",
            trace_id=detect_id,
            started=detect_started,
            status="ready",
            details=f" element_count={len(converted.elements)}",
        )
        return converted
    except Exception as exc:
        _log_complete(
            event="som_detect_sync_complete",
            trace_field="detect_id",
            trace_id=detect_id,
            started=detect_started,
            status="failed",
            level=logging.WARNING,
            details=" stage=convert" + _failure_fields(exc),
        )
        return None
