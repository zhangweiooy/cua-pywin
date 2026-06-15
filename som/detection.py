# ruff: noqa: E402
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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
import torchvision
from PIL import Image

logger = logging.getLogger(__name__)


class DetectionProcessor:
    """Class for handling YOLO-based icon detection."""

    def __init__(
        self,
        model_path: Optional[Path] = None,
        force_device: Optional[str] = None,
    ):
        """Initialize the detection processor.

        Args:
            model_path: Path to YOLOv8 model
            force_device: Force specific device (cuda, cpu, mps)
        """
        self.model_path = model_path
        self.model = None  # type: Any  # Will be set to YOLO model in load_model

        # Set device
        self.device = "cpu"
        if torch.cuda.is_available() and force_device != "cpu":
            self.device = "cuda"
        elif (
            hasattr(torch, "backends")
            and hasattr(torch.backends, "mps")
            and torch.backends.mps.is_available()
            and force_device != "cpu"
        ):
            self.device = "mps"

        if force_device:
            self.device = force_device

        logger.info(f"Using device: {self.device}")

    def load_model(self) -> None:
        """Load the bundled YOLO model without network fallback."""
        try:
            if self.model_path is None:
                self.model_path = Path(__file__).parent / "weights" / "icon_detect" / "model.pt"

            if not self.model_path.exists():
                raise FileNotFoundError(
                    "Bundled OmniParser YOLO model is missing: "
                    f"{self.model_path}. Reinstall cua-pywin or use a complete "
                    "developer build; runtime model download is disabled."
                )

            # If model is already loaded, skip reloading
            if self.model is not None:
                logger.info("Model already loaded, skipping reload")
                return

            logger.info(f"Loading YOLOv8 model from {self.model_path}")
            from ultralytics import YOLO

            self.model = YOLO(str(self.model_path))  # Convert Path to string for compatibility

            # Verify model loaded successfully
            if self.model is None:
                raise ValueError("Model failed to initialize but didn't raise an exception")

            if self.device in ["cuda", "mps"]:
                self.model.to(self.device)

            logger.info(f"Model loaded successfully with device: {self.device}")
        except Exception as e:
            logger.error(f"Failed to load model: {str(e)}")
            # Re-raise with more informative message but preserve the model as None
            self.model = None
            raise RuntimeError(f"Failed to initialize detection model: {str(e)}") from e

    def detect_icons(
        self,
        image: Image.Image,
        box_threshold: float = 0.05,
        iou_threshold: float = 0.1,
        multi_scale: bool = True,
    ) -> List[Dict[str, Any]]:
        """Detect icons in an image using YOLO.

        Args:
            image: PIL Image to process
            box_threshold: Confidence threshold for detection
            iou_threshold: IOU threshold for NMS
            multi_scale: Whether to use multi-scale detection

        Returns:
            List of icon detection dictionaries
        """
        # Load model if not already loaded
        if self.model is None:
            self.load_model()

        # Double-check the model was successfully loaded
        if self.model is None:
            logger.error("Model failed to load and is still None")
            return []  # Return empty list instead of crashing

        img_width, img_height = image.size
        all_detections = []

        # Define detection scales
        scales = (
            [{"size": 1280, "conf": box_threshold}]  # Single scale for CPU
            if self.device == "cpu"
            else [
                {"size": 640, "conf": box_threshold},  # Base scale
                {"size": 1280, "conf": box_threshold},  # Medium scale
                {"size": 1920, "conf": box_threshold},  # Large scale
            ]
        )

        if not multi_scale:
            scales = [scales[0]]

        # Run detection at each scale
        for scale in scales:
            try:
                if self.model is None:
                    logger.error("Model is None, skipping detection")
                    continue

                results = self.model.predict(
                    source=image,
                    conf=scale["conf"],
                    iou=iou_threshold,
                    max_det=1000,
                    verbose=False,
                    augment=self.device != "cpu",
                    agnostic_nms=True,
                    imgsz=scale["size"],
                    device=self.device,
                )

                # Process results
                for r in results:
                    boxes = r.boxes
                    if not hasattr(boxes, "conf") or not hasattr(boxes, "xyxy"):
                        logger.warning("Boxes object missing expected attributes")
                        continue

                    confidences = boxes.conf
                    coords = boxes.xyxy

                    # Handle different types of tensors (PyTorch, NumPy, etc.)
                    if hasattr(confidences, "cpu"):
                        confidences = confidences.cpu()
                    if hasattr(coords, "cpu"):
                        coords = coords.cpu()

                    for conf, bbox in zip(confidences, coords):
                        # Normalize coordinates
                        x1, y1, x2, y2 = bbox.tolist()
                        norm_bbox = [
                            x1 / img_width,
                            y1 / img_height,
                            x2 / img_width,
                            y2 / img_height,
                        ]

                        all_detections.append(
                            {
                                "type": "icon",
                                "confidence": conf.item(),
                                "bbox": norm_bbox,
                                "scale": scale["size"],
                                "interactivity": True,
                            }
                        )

            except Exception as e:
                logger.warning(f"Detection failed at scale {scale['size']}: {str(e)}")
                continue

        # Merge detections using NMS
        if len(all_detections) > 0:
            boxes = torch.tensor([d["bbox"] for d in all_detections])
            scores = torch.tensor([d["confidence"] for d in all_detections])

            keep_indices = torchvision.ops.nms(boxes, scores, iou_threshold)

            merged_detections = [all_detections[i] for i in keep_indices]
        else:
            merged_detections = []

        return merged_detections
