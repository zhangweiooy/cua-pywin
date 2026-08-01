# ruff: noqa: E402
import argparse
import base64
import io
import logging
import os
import sys
import time

from pathlib import Path
from typing import Dict, List, Optional, Tuple, Union, cast

_DLL_DIRECTORY_HANDLES = []


def _add_packaged_dll_paths() -> None:
    bases = [Path(__file__).resolve().parents[1], Path(sys.executable).resolve().parent]
    for base in bases:
        for relative in ("cv2", "numpy.libs", "scipy.libs", "torch/lib", "onnxruntime/capi"):
            path = base / relative
            if not path.exists():
                continue
            os.environ["PATH"] = str(path) + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    _DLL_DIRECTORY_HANDLES.append(os.add_dll_directory(str(path)))
                except OSError:
                    pass


_add_packaged_dll_paths()

# Heavy C-extension imports must happen after packaged DLL directories are
# registered for the standalone Nuitka runtime.
import torch
import torchvision.ops
from PIL import Image

from .detection import DetectionProcessor
from .models import (
    BoundingBox,
    IconElement,
    ParseResult,
    ParserMetadata,
    TextElement,
    UIElement,
)
from .ocr import OCRProcessor
from .visualization import BoxAnnotator

logger = logging.getLogger(__name__)

_ICON_COLLISION_MIN_CONFIDENCE = 0.3
_OCR_ICON_OVERLAP_THRESHOLD = 0.6


def _intersection_over_area(first: UIElement, second: UIElement) -> float:
    """Return intersection divided by the smaller element area."""
    left = max(first.bbox.x1, second.bbox.x1)
    top = max(first.bbox.y1, second.bbox.y1)
    right = min(first.bbox.x2, second.bbox.x2)
    bottom = min(first.bbox.y2, second.bbox.y2)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    first_area = max(0.0, first.bbox.x2 - first.bbox.x1) * max(
        0.0, first.bbox.y2 - first.bbox.y1
    )
    second_area = max(0.0, second.bbox.x2 - second.bbox.x1) * max(
        0.0, second.bbox.y2 - second.bbox.y1
    )
    smaller_area = min(first_area, second_area)
    return intersection / smaller_area if smaller_area > 0 else 0.0


def _has_neighboring_text(text: TextElement, candidates: List[UIElement]) -> bool:
    """Whether an OCR fragment has a close, line-aligned neighbor that can form text."""
    text_height = max(0.0, text.bbox.y2 - text.bbox.y1)
    for candidate in candidates:
        if candidate is text or not isinstance(candidate, TextElement):
            continue
        vertical_overlap = max(
            0.0,
            min(text.bbox.y2, candidate.bbox.y2)
            - max(text.bbox.y1, candidate.bbox.y1),
        )
        candidate_height = max(0.0, candidate.bbox.y2 - candidate.bbox.y1)
        min_height = min(text_height, candidate_height)
        if min_height <= 0 or vertical_overlap / min_height < 0.5:
            continue
        horizontal_gap = max(
            0.0,
            max(text.bbox.x1, candidate.bbox.x1)
            - min(text.bbox.x2, candidate.bbox.x2),
        )
        if horizontal_gap <= max(text_height, candidate_height) * 0.75:
            return True
    return False


def _is_ambiguous_short_ocr(text: TextElement, candidates: List[UIElement]) -> bool:
    """Identify isolated, compact OCR glyphs without using a character blacklist."""
    content = text.content.strip()
    if len(content) != 1 or _has_neighboring_text(text, candidates):
        return False
    width = max(0.0, text.bbox.x2 - text.bbox.x1)
    height = max(0.0, text.bbox.y2 - text.bbox.y1)
    if width <= 0 or height <= 0:
        return False
    aspect_ratio = width / height
    return 0.5 <= aspect_ratio <= 2.0


def _arbitrate_ocr_icon_collisions(
    icons: List[UIElement],
    texts: List[UIElement],
) -> tuple[List[UIElement], set[int], Dict[str, int]]:
    """Suppress clear duplicate icons and protect ambiguous pairs for Florence."""
    diagnostics = {
        "raw_icon_count": len(icons),
        "raw_text_count": len(texts),
        "ocr_icon_collision_count": 0,
        "icons_suppressed_by_ocr": 0,
        "ambiguous_collisions_preserved": 0,
    }
    protected_ids: set[int] = set()
    retained_icons: List[UIElement] = []
    collision_sequence = 0

    for icon in icons:
        collisions = [
            text
            for text in texts
            if _intersection_over_area(icon, text) >= _OCR_ICON_OVERLAP_THRESHOLD
        ]
        diagnostics["ocr_icon_collision_count"] += len(collisions)
        if not collisions:
            retained_icons.append(icon)
            continue

        ambiguous = (
            icon.interactivity
            and icon.confidence >= _ICON_COLLISION_MIN_CONFIDENCE
            and all(_is_ambiguous_short_ocr(text, texts) for text in collisions)
        )
        if not ambiguous:
            diagnostics["icons_suppressed_by_ocr"] += 1
            continue

        collision_sequence += 1
        collision_id = f"ocr-icon-{collision_sequence}"
        icon.metadata.update(
            {
                "semantic_collision_id": collision_id,
                "semantic_collision_role": "icon",
                "semantic_uncertainty": "ocr_icon_collision",
            }
        )
        protected_ids.add(id(icon))
        for text in collisions:
            text.metadata.update(
                {
                    "semantic_collision_id": collision_id,
                    "semantic_collision_role": "text",
                    "semantic_uncertainty": "ocr_icon_collision",
                }
            )
            protected_ids.add(id(text))
        diagnostics["ambiguous_collisions_preserved"] += len(collisions)
        retained_icons.append(icon)

    return retained_icons, protected_ids, diagnostics





class OmniParser:
    """Enhanced UI parser using computer vision and OCR for detecting interactive elements."""

    def __init__(
        self,
        model_path: Optional[Union[str, Path]] = None,
        force_device: Optional[str] = None,
        ocr_languages: Optional[List[str]] = None,
    ):
        """Initialize the OmniParser.

        Args:
            model_path: Optional path to the YOLO model
            force_device: Force specific device (cpu/cuda/mps)
            ocr_languages: OCR language hints. RapidOCR uses bundled PP-OCR Chinese+English models.
        """
        self.detector = DetectionProcessor(
            model_path=Path(model_path) if model_path else None,
            force_device=force_device,
        )
        self.ocr = OCRProcessor(languages=ocr_languages)
        self.visualizer = BoxAnnotator()
        self._last_stage_timings: Dict[str, float] = {}
        self._last_collision_diagnostics: Dict[str, int] = {}

    def process_image(
        self,
        image: Image.Image,
        box_threshold: float = 0.3,
        iou_threshold: float = 0.1,
        use_ocr: bool = True,
    ) -> Tuple[Image.Image, List[UIElement]]:
        """Process an image to detect UI elements and optionally text.

        Args:
            image: Input PIL Image
            box_threshold: Confidence threshold for detection
            iou_threshold: IOU threshold for NMS
            use_ocr: Whether to enable OCR processing

        Returns:
            Tuple of (annotated image, list of detections)
        """
        try:
            process_start = time.perf_counter()
            stage_timings: Dict[str, float] = {}
            logger.info("Starting UI element detection...")

            # Detect icons
            stage_start = time.perf_counter()
            icon_detections = self.detector.detect_icons(
                image=image, box_threshold=box_threshold, iou_threshold=iou_threshold
            )
            stage_timings["yolo_latency"] = time.perf_counter() - stage_start
            logger.info(f"Found {len(icon_detections)} interactive elements")
            self._last_collision_diagnostics = {
                "raw_icon_count": len(icon_detections),
                "raw_text_count": 0,
                "ocr_icon_collision_count": 0,
                "icons_suppressed_by_ocr": 0,
                "ambiguous_collisions_preserved": 0,
            }

            # Convert icon detections to typed objects
            elements: List[UIElement] = cast(
                List[UIElement],
                [
                    IconElement(
                        id=i + 1,
                        bbox=BoundingBox(
                            x1=det["bbox"][0],
                            y1=det["bbox"][1],
                            x2=det["bbox"][2],
                            y2=det["bbox"][3],
                        ),
                        confidence=det["confidence"],
                        scale=det.get("scale"),
                    )
                    for i, det in enumerate(icon_detections)
                ],
            )

            # Run OCR if enabled
            if use_ocr:
                logger.info("Running OCR detection...")
                stage_start = time.perf_counter()
                text_detections = self.ocr.detect_text(image=image, confidence_threshold=0.5)
                stage_timings["ocr_latency"] = time.perf_counter() - stage_start
                if text_detections is None:
                    text_detections = []
                logger.info(f"Found {len(text_detections)} text regions")

                # Convert text detections to typed objects
                stage_start = time.perf_counter()
                text_elements = cast(
                    List[UIElement],
                    [
                        TextElement(
                            id=len(elements) + i + 1,
                            bbox=BoundingBox(
                                x1=det["bbox"][0],
                                y1=det["bbox"][1],
                                x2=det["bbox"][2],
                                y2=det["bbox"][3],
                            ),
                            content=det["content"],
                            confidence=det["confidence"],
                        )
                        for i, det in enumerate(text_detections)
                    ],
                )

                if elements and text_elements:
                    elements, protected_ids, collision_diagnostics = (
                        _arbitrate_ocr_icon_collisions(elements, text_elements)
                    )
                    self._last_collision_diagnostics = collision_diagnostics

                    # Merge detections using NMS
                    all_elements = elements + text_elements
                    boxes = torch.tensor([elem.bbox.coordinates for elem in all_elements])
                    scores = torch.tensor([elem.confidence for elem in all_elements])
                    keep_indices = torchvision.ops.nms(boxes, scores, iou_threshold)
                    kept_positions = {int(i) for i in keep_indices}
                    kept_positions.update(
                        i for i, elem in enumerate(all_elements) if id(elem) in protected_ids
                    )
                    elements = [
                        elem for i, elem in enumerate(all_elements) if i in kept_positions
                    ]
                else:
                    # Just add text elements to the list if IOU doesn't need to be applied
                    elements.extend(text_elements)
                    self._last_collision_diagnostics["raw_text_count"] = len(text_elements)
                stage_timings["merge_latency"] = time.perf_counter() - stage_start
            else:
                stage_timings["ocr_latency"] = 0.0
                stage_timings["merge_latency"] = 0.0

            # Calculate drawing parameters based on image size
            box_overlay_ratio = max(image.size) / 3200
            draw_config = {
                "font_size": int(12 * box_overlay_ratio),
                "box_thickness": max(int(2 * box_overlay_ratio), 1),
                "text_padding": max(int(3 * box_overlay_ratio), 1),
            }

            # Convert elements back to dict format for visualization
            detection_dicts = [
                {
                    "type": elem.type,
                    "bbox": elem.bbox.coordinates,
                    "confidence": elem.confidence,
                    "content": elem.content if isinstance(elem, TextElement) else None,
                }
                for elem in elements
            ]

            # Create visualization
            logger.info("Creating visualization...")
            stage_start = time.perf_counter()
            annotated_image = self.visualizer.draw_boxes(
                image=image.copy(), detections=detection_dicts, draw_config=draw_config
            )
            stage_timings["visualization_latency"] = time.perf_counter() - stage_start
            stage_timings["omniparser_latency"] = time.perf_counter() - process_start
            self._last_stage_timings = stage_timings
            logger.info("Visualization complete")

            return annotated_image, elements

        except Exception as e:
            logger.error(f"Error in process_image: {str(e)}")
            import traceback

            logger.error(traceback.format_exc())
            raise

    def parse(
        self,
        screenshot_data: Union[bytes, str],
        box_threshold: float = 0.3,
        iou_threshold: float = 0.1,
        use_ocr: bool = True,
    ) -> ParseResult:
        """Parse a UI screenshot to detect interactive elements and text.

        Args:
            screenshot_data: Raw bytes or base64 string of the screenshot
            box_threshold: Confidence threshold for detection
            iou_threshold: IOU threshold for NMS
            use_ocr: Whether to enable OCR processing

        Returns:
            ParseResult object containing elements, annotated image, and metadata
        """
        try:
            start_time = time.perf_counter()

            # Convert input to PIL Image
            stage_start = time.perf_counter()
            if isinstance(screenshot_data, str):
                screenshot_data = base64.b64decode(screenshot_data)
            image = Image.open(io.BytesIO(screenshot_data)).convert("RGB")
            image_decode_latency = time.perf_counter() - stage_start

            # Process image
            annotated_image, elements = self.process_image(
                image=image,
                box_threshold=box_threshold,
                iou_threshold=iou_threshold,
                use_ocr=use_ocr,
            )

            # Convert annotated image to base64
            stage_start = time.perf_counter()
            buffered = io.BytesIO()
            annotated_image.save(buffered, format="PNG")
            annotated_image_base64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
            annotation_encode_latency = time.perf_counter() - stage_start

            # Generate screen info text
            screen_info = []
            parsed_content_list = []

            # Set element IDs and generate human-readable descriptions
            for i, elem in enumerate(elements):
                # Set the ID (1-indexed)
                elem.id = i + 1

                if isinstance(elem, IconElement):
                    screen_info.append(
                        f"Box #{i+1}: Icon (confidence={elem.confidence:.3f}, bbox={elem.bbox.coordinates})"
                    )
                    parsed_content_list.append(
                        {
                            "id": i + 1,
                            "type": "icon",
                            "bbox": elem.bbox.coordinates,
                            "confidence": elem.confidence,
                            "content": None,
                        }
                    )
                elif isinstance(elem, TextElement):
                    screen_info.append(
                        f"Box #{i+1}: Text '{elem.content}' (confidence={elem.confidence:.3f}, bbox={elem.bbox.coordinates})"
                    )
                    parsed_content_list.append(
                        {
                            "id": i + 1,
                            "type": "text",
                            "bbox": elem.bbox.coordinates,
                            "confidence": elem.confidence,
                            "content": elem.content,
                        }
                    )

            # Calculate metadata
            latency = time.perf_counter() - start_time
            width, height = image.size
            stage_timings = {
                "image_decode_latency": image_decode_latency,
                **self._last_stage_timings,
                "annotation_encode_latency": annotation_encode_latency,
                "total_latency": latency,
            }

            # Create ParseResult object with enhanced properties
            result = ParseResult(
                elements=elements,
                annotated_image_base64=annotated_image_base64,
                screen_info=screen_info,
                parsed_content_list=parsed_content_list,
                metadata=ParserMetadata(
                    image_size=(width, height),
                    num_icons=len([e for e in elements if isinstance(e, IconElement)]),
                    num_text=len([e for e in elements if isinstance(e, TextElement)]),
                    device=self.detector.device,
                    ocr_enabled=use_ocr,
                    latency=latency,
                    stage_timings=stage_timings,
                    **self._last_collision_diagnostics,
                ),
            )

            # Return the ParseResult object directly
            return result

        except Exception as e:
            logger.error(f"Error in parse: {str(e)}")
            import traceback

            logger.error(traceback.format_exc())
            raise


def main():
    """Command line interface for UI element detection."""
    parser = argparse.ArgumentParser(description="Detect UI elements and text in images")
    parser.add_argument("image_path", help="Path to the input image")
    parser.add_argument("--model-path", help="Path to YOLO model")
    parser.add_argument(
        "--box-threshold", type=float, default=0.3, help="Box confidence threshold (default: 0.3)"
    )
    parser.add_argument(
        "--iou-threshold", type=float, default=0.1, help="IOU threshold (default: 0.1)"
    )
    parser.add_argument(
        "--ocr", action="store_true", default=True, help="Enable OCR processing (default: True)"
    )
    parser.add_argument("--output", help="Output path for annotated image")
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO)

    try:
        # Initialize parser
        parser = OmniParser(model_path=args.model_path)

        # Load and process image
        logger.info(f"Loading image from: {args.image_path}")
        image = Image.open(args.image_path).convert("RGB")
        logger.info(f"Image loaded successfully, size: {image.size}")

        # Process image
        annotated_image, elements = parser.process_image(
            image=image,
            box_threshold=args.box_threshold,
            iou_threshold=args.iou_threshold,
            use_ocr=args.ocr,
        )

        # Save output image
        output_path = args.output or str(
            Path(args.image_path).parent
            / f"{Path(args.image_path).stem}_analyzed{Path(args.image_path).suffix}"
        )
        logger.info(f"Saving annotated image to: {output_path}")

        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        annotated_image.save(output_path)
        logger.info(f"Image saved successfully to {output_path}")

        # Print detections
        logger.info("\nDetections:")
        for i, elem in enumerate(elements):
            if isinstance(elem, IconElement):
                logger.info(
                    f"Interactive element {i}: confidence={elem.confidence:.3f}, bbox={elem.bbox.coordinates}"
                )
            elif isinstance(elem, TextElement):
                logger.info(f"Text {i}: '{elem.content}', bbox={elem.bbox.coordinates}")

    except Exception as e:
        logger.error(f"Error processing image: {str(e)}")
        import traceback

        logger.error(traceback.format_exc())
        return 1

    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
