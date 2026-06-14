"""OmniParser v2 Florence-2 captions for vision-detected UI icons."""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image

logger = logging.getLogger(__name__)

BUNDLED_FLORENCE2_MODEL_DIR = (
    Path(__file__).resolve().parents[1]
    / "models"
    / "omniparser_v2"
    / "icon_caption"
)
GENERATION_PROMPT = "<CAPTION>"
GENERATION_CONFIDENCE_PROVENANCE = "unscored_greedy_generation"

_GENERIC_CAPTIONS = {
    "button",
    "icon",
    "image",
    "logo",
    "shape",
    "symbol",
    "ui icon",
    "unknown",
    "unknown icon",
}
_SVG_PATH_CAPTION = re.compile(
    r"^[Mm]\s*-?\d+(?:[\d\s,.\-]+)[LlHhVvCcSsQqTtAaZz]"
)


@dataclass(frozen=True)
class IconCaption:
    raw_caption: str
    normalized_label: Optional[str]
    confidence: Optional[float] = None
    confidence_provenance: str = GENERATION_CONFIDENCE_PROVENANCE

    @property
    def label(self) -> Optional[str]:
        return self.normalized_label


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(
    name: str,
    default: int,
    minimum: int = 1,
    maximum: Optional[int] = None,
) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = max(minimum, int(raw))
        return min(maximum, value) if maximum is not None else value
    except ValueError:
        return default


def normalize_caption(caption: str) -> Optional[str]:
    """Conservatively clean a generated caption without inventing semantics."""
    cleaned = re.sub(r"\s+", " ", caption or "").strip(" \t\r\n.,;:")
    cleaned = re.sub(r"^(?:a|an|the)\s+", "", cleaned, flags=re.IGNORECASE)
    if not cleaned:
        return None
    if cleaned.casefold() in _GENERIC_CAPTIONS:
        return None
    lowered = cleaned.casefold()
    if (
        _SVG_PATH_CAPTION.match(cleaned)
        or "<svg" in lowered
        or "path d=" in lowered
        or lowered.startswith("data:image/")
    ):
        return None
    return cleaned[:160]


def default_model_name_or_path() -> str:
    configured = os.environ.get("CUA_FLORENCE2_MODEL")
    if configured:
        return configured
    return str(BUNDLED_FLORENCE2_MODEL_DIR)


def default_processor_name_or_path(model_name_or_path: str) -> str:
    configured = os.environ.get("CUA_FLORENCE2_PROCESSOR")
    if configured:
        return configured
    return model_name_or_path


class IconCaptionService:
    """Batch Florence-2 icon captioning with local-only operation by default."""

    def __init__(
        self,
        model_name_or_path: Optional[str] = None,
        processor_name_or_path: Optional[str] = None,
        enabled: Optional[bool] = None,
        batch_size: Optional[int] = None,
        max_new_tokens: Optional[int] = None,
    ) -> None:
        self.model_name_or_path = model_name_or_path or default_model_name_or_path()
        self.processor_name_or_path = (
            processor_name_or_path
            or default_processor_name_or_path(self.model_name_or_path)
        )
        self.enabled = (
            enabled
            if enabled is not None
            else _env_flag("CUA_ENABLE_FLORENCE2", True)
        )
        self.batch_size = (
            max(1, min(256, int(batch_size)))
            if batch_size is not None
            else _env_int("CUA_FLORENCE2_BATCH_SIZE", 64, maximum=256)
        )
        self.max_new_tokens = (
            max(1, min(128, int(max_new_tokens)))
            if max_new_tokens is not None
            else _env_int("CUA_FLORENCE2_MAX_NEW_TOKENS", 20, maximum=128)
        )

        self._lock = threading.RLock()
        self._state = "idle"
        self._failure: Optional[str] = None
        self._processor: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device: Any = None
        self._dtype: Any = None

    @property
    def status(self) -> dict[str, Any]:
        return {
            "state": self._state,
            "failure": self._failure,
            "model": self.model_name_or_path,
            "processor": self.processor_name_or_path,
            "batch_size": self.batch_size,
        }

    def ensure_ready(self) -> bool:
        if not self.enabled:
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
                model_path = self._require_local_directory(
                    self.model_name_or_path,
                    "Florence-2 model",
                )
                processor_path = self._require_local_directory(
                    self.processor_name_or_path,
                    "Florence-2 processor",
                )
                import torch
                from transformers import AutoModelForCausalLM, AutoProcessor

                forced_device = os.environ.get("CUA_FLORENCE2_DEVICE")
                if forced_device:
                    device = torch.device(forced_device)
                else:
                    device = torch.device(
                        "cuda" if torch.cuda.is_available() else "cpu"
                    )
                dtype = torch.float16 if device.type == "cuda" else torch.float32

                processor = AutoProcessor.from_pretrained(
                    str(processor_path),
                    local_files_only=True,
                    trust_remote_code=True,
                )
                model_kwargs = {
                    "local_files_only": True,
                    "trust_remote_code": True,
                    "torch_dtype": dtype,
                }
                model = AutoModelForCausalLM.from_pretrained(
                    str(model_path),
                    **model_kwargs,
                )
                model.eval()
                model.to(device)

                self._torch = torch
                self._device = device
                self._dtype = dtype
                self._processor = processor
                self._model = model
                self._state = "ready"
                self._failure = None
                logger.info(
                    "OmniParser v2 Florence-2 captioner ready: model=%s, device=%s",
                    self.model_name_or_path,
                    device,
                )
                return True
            except Exception as exc:
                self._state = "failed"
                self._failure = str(exc)
                logger.warning("Florence-2 icon captioner disabled: %s", exc)
                return False

    @staticmethod
    def _require_local_directory(value: str, label: str) -> Path:
        path = Path(value).expanduser()
        if not path.is_dir():
            raise FileNotFoundError(
                f"{label} must be a local directory: {path}. "
                "Runtime model download is disabled; install a complete "
                "cua-pywin release or update the developer model bundle."
            )
        return path.resolve()

    def caption_icons(
        self,
        images: Sequence[Image.Image],
    ) -> list[Optional[IconCaption]]:
        """Return one generated caption per crop using bounded batch inference."""
        if not images:
            return []
        if not self.ensure_ready():
            return [None] * len(images)

        with self._lock:
            try:
                return self._caption_icons_ready(images)
            except Exception:
                if (
                    self._torch is not None
                    and self._device is not None
                    and self._device.type == "cuda"
                ):
                    self._torch.cuda.empty_cache()
                raise

    def _caption_icons_ready(
        self,
        images: Sequence[Image.Image],
    ) -> list[Optional[IconCaption]]:
        torch = self._torch
        processor = self._processor
        model = self._model
        device = self._device
        dtype = self._dtype
        if (
            torch is None
            or processor is None
            or model is None
            or device is None
        ):
            return [None] * len(images)

        captions: list[Optional[IconCaption]] = []
        rgb_images = [image.convert("RGB") for image in images]
        for start in range(0, len(rgb_images), self.batch_size):
            batch = rgb_images[start : start + self.batch_size]
            inputs = processor(
                images=batch,
                text=[GENERATION_PROMPT] * len(batch),
                return_tensors="pt",
                do_resize=False,
            )
            inputs = self._move_inputs(inputs, device, dtype)
            with torch.inference_mode():
                generated_ids = model.generate(
                    input_ids=inputs["input_ids"],
                    pixel_values=inputs["pixel_values"],
                    max_new_tokens=self.max_new_tokens,
                    num_beams=1,
                    do_sample=False,
                    early_stopping=False,
                )
            generated_texts = processor.batch_decode(
                generated_ids,
                skip_special_tokens=True,
            )
            if len(generated_texts) != len(batch):
                raise RuntimeError(
                    "Florence-2 returned "
                    f"{len(generated_texts)} captions for {len(batch)} icon crops"
                )
            for raw in generated_texts:
                raw_caption = re.sub(r"\s+", " ", str(raw)).strip()
                captions.append(
                    IconCaption(
                        raw_caption=raw_caption,
                        normalized_label=normalize_caption(raw_caption),
                    )
                )
        return captions

    @staticmethod
    def _move_inputs(inputs: Any, device: Any, dtype: Any) -> Any:
        moved: dict[str, Any] = {}
        for key, value in dict(inputs).items():
            if not hasattr(value, "to"):
                moved[key] = value
            elif getattr(value, "is_floating_point", lambda: False)():
                moved[key] = value.to(device=device, dtype=dtype)
            else:
                moved[key] = value.to(device=device)
        return moved


_icon_caption_service: IconCaptionService | None = None
_icon_caption_service_lock = threading.Lock()


def get_icon_caption_service() -> IconCaptionService:
    global _icon_caption_service
    if _icon_caption_service is None:
        with _icon_caption_service_lock:
            if _icon_caption_service is None:
                _icon_caption_service = IconCaptionService()
    return _icon_caption_service


def caption_icons(
    images: Sequence[Image.Image],
) -> list[Optional[IconCaption]]:
    return get_icon_caption_service().caption_icons(images)


def warm_up() -> bool:
    return get_icon_caption_service().ensure_ready()
