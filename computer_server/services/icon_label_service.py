"""TinyCLIP-based semantic labels for vision-detected UI icons."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image

logger = logging.getLogger(__name__)

DEFAULT_TINYCLIP_MODEL = "wkcn/TinyCLIP-ViT-8M-16-Text-3M-YFCC15M"
BUNDLED_TINYCLIP_MODEL_DIR = (
    Path(__file__).resolve().parents[1]
    / "models"
    / "tinyclip"
    / "wkcn-TinyCLIP-ViT-8M-16-Text-3M-YFCC15M"
)


@dataclass(frozen=True)
class IconLabelCandidate:
    label: str
    prompt: str


@dataclass(frozen=True)
class IconLabel:
    label: str
    score: float
    margin: float


DEFAULT_ICON_LABEL_CANDIDATES: tuple[IconLabelCandidate, ...] = (
    IconLabelCandidate("send button", "a send button icon in a software interface"),
    IconLabelCandidate("settings icon", "a settings gear icon in a software interface"),
    IconLabelCandidate("close button", "a close x button icon in a software interface"),
    IconLabelCandidate("search button", "a search magnifying glass icon in a software interface"),
    IconLabelCandidate("menu button", "a hamburger menu button icon in a software interface"),
    IconLabelCandidate("more options button", "a three dots more options icon in a software interface"),
    IconLabelCandidate("back button", "a back arrow icon in a software interface"),
    IconLabelCandidate("forward button", "a forward arrow icon in a software interface"),
    IconLabelCandidate("refresh button", "a refresh reload icon in a software interface"),
    IconLabelCandidate("add button", "a plus add button icon in a software interface"),
    IconLabelCandidate("remove button", "a minus remove button icon in a software interface"),
    IconLabelCandidate("delete button", "a trash delete button icon in a software interface"),
    IconLabelCandidate("edit button", "an edit pencil button icon in a software interface"),
    IconLabelCandidate("save button", "a save disk button icon in a software interface"),
    IconLabelCandidate("download button", "a download arrow icon in a software interface"),
    IconLabelCandidate("upload button", "an upload arrow icon in a software interface"),
    IconLabelCandidate("share button", "a share button icon in a software interface"),
    IconLabelCandidate("copy button", "a copy duplicate button icon in a software interface"),
    IconLabelCandidate("paste button", "a clipboard paste button icon in a software interface"),
    IconLabelCandidate("attach file button", "a paperclip attachment button icon in a software interface"),
    IconLabelCandidate("emoji button", "a smiley face emoji button icon in a software interface"),
    IconLabelCandidate("microphone button", "a microphone voice input button icon in a software interface"),
    IconLabelCandidate("camera button", "a camera button icon in a software interface"),
    IconLabelCandidate("play button", "a media play triangle button icon in a software interface"),
    IconLabelCandidate("pause button", "a media pause button icon in a software interface"),
    IconLabelCandidate("stop button", "a media stop square button icon in a software interface"),
    IconLabelCandidate("next button", "a next track button icon in a software interface"),
    IconLabelCandidate("previous button", "a previous track button icon in a software interface"),
    IconLabelCandidate("volume button", "a speaker volume button icon in a software interface"),
    IconLabelCandidate("mute button", "a muted speaker button icon in a software interface"),
    IconLabelCandidate("maximize button", "a maximize window button icon in a software interface"),
    IconLabelCandidate("minimize button", "a minimize window button icon in a software interface"),
    IconLabelCandidate("restore window button", "a restore window button icon in a software interface"),
    IconLabelCandidate("home button", "a home house button icon in a software interface"),
    IconLabelCandidate("profile button", "a user profile avatar button icon in a software interface"),
    IconLabelCandidate("notification button", "a bell notification button icon in a software interface"),
    IconLabelCandidate("calendar button", "a calendar button icon in a software interface"),
    IconLabelCandidate("clock button", "a clock time button icon in a software interface"),
    IconLabelCandidate("folder button", "a folder button icon in a software interface"),
    IconLabelCandidate("file button", "a document file button icon in a software interface"),
    IconLabelCandidate("lock icon", "a lock security icon in a software interface"),
    IconLabelCandidate("unlock icon", "an unlock security icon in a software interface"),
    IconLabelCandidate("warning icon", "a warning triangle icon in a software interface"),
    IconLabelCandidate("info icon", "an information circle icon in a software interface"),
    IconLabelCandidate("help icon", "a question mark help icon in a software interface"),
    IconLabelCandidate("filter button", "a filter funnel button icon in a software interface"),
    IconLabelCandidate("sort button", "a sort arrows button icon in a software interface"),
    IconLabelCandidate("favorite button", "a star favorite button icon in a software interface"),
    IconLabelCandidate("bookmark button", "a bookmark ribbon button icon in a software interface"),
    IconLabelCandidate("expand button", "an expand arrows button icon in a software interface"),
    IconLabelCandidate("collapse button", "a collapse arrows button icon in a software interface"),
    IconLabelCandidate("checkbox", "a checkbox square control in a software interface"),
    IconLabelCandidate("radio button", "a radio button circle control in a software interface"),
)


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _candidate_from_label(label: str) -> IconLabelCandidate:
    clean = label.strip()
    return IconLabelCandidate(clean, f"a {clean} icon in a software interface")


def _load_candidates_from_env() -> Optional[tuple[IconLabelCandidate, ...]]:
    raw = os.environ.get("CUA_TINYCLIP_LABELS")
    if not raw:
        return None
    labels = [label.strip() for label in raw.split(",") if label.strip()]
    if not labels:
        return None
    return tuple(_candidate_from_label(label) for label in labels)


def default_model_name_or_path() -> str:
    """Return bundled model path when present, otherwise the HF model id."""
    configured = os.environ.get("CUA_TINYCLIP_MODEL")
    if configured:
        return configured
    if BUNDLED_TINYCLIP_MODEL_DIR.exists():
        return str(BUNDLED_TINYCLIP_MODEL_DIR)
    return DEFAULT_TINYCLIP_MODEL


class IconLabelService:
    """Batch TinyCLIP icon labeling with safe optional degradation."""

    def __init__(
        self,
        model_name_or_path: Optional[str] = None,
        candidates: Optional[Sequence[IconLabelCandidate]] = None,
        min_score: Optional[float] = None,
        min_margin: Optional[float] = None,
        allow_download: Optional[bool] = None,
        enabled: Optional[bool] = None,
    ) -> None:
        self.model_name_or_path = model_name_or_path or default_model_name_or_path()
        self.candidates = tuple(candidates or _load_candidates_from_env() or DEFAULT_ICON_LABEL_CANDIDATES)
        self.min_score = min_score if min_score is not None else _env_float("CUA_TINYCLIP_MIN_SCORE", 0.12)
        self.min_margin = min_margin if min_margin is not None else _env_float("CUA_TINYCLIP_MIN_MARGIN", 0.02)
        self.allow_download = allow_download if allow_download is not None else _env_flag("CUA_TINYCLIP_ALLOW_DOWNLOAD", False)
        self.enabled = enabled if enabled is not None else _env_flag("CUA_ENABLE_TINYCLIP", True)

        self._lock = threading.RLock()
        self._state = "idle"
        self._failure: Optional[str] = None
        self._processor: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device: Any = None
        self._text_features: Any = None

    @property
    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "failure": self._failure,
            "model": self.model_name_or_path,
            "candidate_count": len(self.candidates),
        }

    def ensure_ready(self) -> bool:
        """Load TinyCLIP and precompute text features once."""
        if not self.enabled or not self.candidates:
            self._state = "disabled"
            return False
        if self._state == "ready":
            return True
        if self._state == "failed":
            return False

        with self._lock:
            if self._state == "ready":
                return True
            if self._state == "failed":
                return False
            try:
                self._state = "loading"
                import torch
                from transformers import CLIPModel, CLIPProcessor

                force_device = os.environ.get("CUA_TINYCLIP_DEVICE")
                if force_device:
                    device = torch.device(force_device)
                else:
                    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

                local_files_only = not self.allow_download
                processor = CLIPProcessor.from_pretrained(
                    self.model_name_or_path,
                    local_files_only=local_files_only,
                )
                model = CLIPModel.from_pretrained(
                    self.model_name_or_path,
                    local_files_only=local_files_only,
                )
                model.eval()
                model.to(device)

                prompts = [candidate.prompt for candidate in self.candidates]
                text_inputs = processor(text=prompts, return_tensors="pt", padding=True)
                text_inputs = self._move_to_device(text_inputs, device)
                with torch.inference_mode():
                    text_features = self._as_tensor_features(model.get_text_features(**text_inputs))
                    text_features = text_features / text_features.norm(dim=-1, keepdim=True)

                self._torch = torch
                self._device = device
                self._processor = processor
                self._model = model
                self._text_features = text_features
                self._state = "ready"
                self._failure = None
                logger.info(
                    "TinyCLIP icon labeler ready: model=%s, candidates=%d, device=%s",
                    self.model_name_or_path,
                    len(self.candidates),
                    device,
                )
                return True
            except Exception as exc:
                self._state = "failed"
                self._failure = str(exc)
                logger.warning("TinyCLIP icon labeler disabled: %s", exc)
                return False

    def label_icons(self, images: Sequence[Image.Image]) -> list[Optional[IconLabel]]:
        """Return one optional label per image using a single batched inference."""
        if not images:
            return []
        if not self.ensure_ready():
            return [None] * len(images)

        with self._lock:
            return self._label_icons_ready(images)

    def _label_icons_ready(self, images: Sequence[Image.Image]) -> list[Optional[IconLabel]]:
        torch = self._torch
        processor = self._processor
        model = self._model
        device = self._device
        if torch is None or processor is None or model is None or device is None:
            return [None] * len(images)

        rgb_images = [image.convert("RGB") for image in images]
        image_inputs = processor(images=rgb_images, return_tensors="pt")
        image_inputs = self._move_to_device(image_inputs, device)

        with torch.inference_mode():
            image_features = self._as_tensor_features(model.get_image_features(**image_inputs))
            image_features = image_features / image_features.norm(dim=-1, keepdim=True)
            logits = image_features @ self._text_features.T
            if hasattr(model, "logit_scale"):
                logits = logits * model.logit_scale.exp()
            probs = logits.softmax(dim=-1)
            top_k = min(2, len(self.candidates))
            top_scores, top_indices = probs.topk(top_k, dim=-1)

        labels: list[Optional[IconLabel]] = []
        for row in range(len(images)):
            score = float(top_scores[row, 0].detach().cpu())
            idx = int(top_indices[row, 0].detach().cpu())
            second = float(top_scores[row, 1].detach().cpu()) if top_k > 1 else 0.0
            margin = score - second
            if score < self.min_score or margin < self.min_margin:
                labels.append(None)
                continue
            labels.append(IconLabel(self.candidates[idx].label, score, margin))
        return labels

    @staticmethod
    def _move_to_device(inputs: Any, device: Any) -> Any:
        if hasattr(inputs, "to"):
            return inputs.to(device)
        if isinstance(inputs, dict):
            return {
                key: value.to(device) if hasattr(value, "to") else value
                for key, value in inputs.items()
            }
        return inputs

    @staticmethod
    def _as_tensor_features(output: Any) -> Any:
        if hasattr(output, "norm"):
            return output
        for attr in ("text_embeds", "image_embeds", "pooler_output", "last_hidden_state"):
            value = getattr(output, attr, None)
            if value is None:
                continue
            if attr == "last_hidden_state":
                return value[:, 0]
            return value
        if isinstance(output, (tuple, list)) and output:
            return output[0]
        raise TypeError(f"Unsupported CLIP feature output type: {type(output).__name__}")


_icon_label_service: IconLabelService | None = None
_icon_label_service_lock = threading.Lock()


def get_icon_label_service() -> IconLabelService:
    global _icon_label_service
    if _icon_label_service is None:
        with _icon_label_service_lock:
            if _icon_label_service is None:
                _icon_label_service = IconLabelService()
    return _icon_label_service


def label_icons(images: Sequence[Image.Image]) -> list[Optional[IconLabel]]:
    return get_icon_label_service().label_icons(images)


def warm_up() -> bool:
    return get_icon_label_service().ensure_ready()
