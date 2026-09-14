"""Optional icon semantics behind the stable :class:`IconCaption` boundary.

Only one backend is loaded by a service instance.  The production default is
the ONNX Community split-graph INT8 form of the existing OmniParser
Florence-2 model; the original PyTorch baseline is selected explicitly with
``CUA_ICON_SEMANTIC_BACKEND``. Missing local resources degrade to no semantic label.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Sequence

from PIL import Image

logger = logging.getLogger(__name__)

BUNDLED_FLORENCE2_MODEL_DIR = (
    Path(__file__).resolve().parents[1] / "models" / "omniparser_v2" / "icon_caption"
)
BUNDLED_FLORENCE2_COMMUNITY_ONNX_DIR = (
    Path(__file__).resolve().parents[1]
    / "models"
    / "omniparser_v2"
    / "icon_caption_onnx_community"
)
GENERATION_PROMPT = "<CAPTION>"
GENERATION_CONFIDENCE_PROVENANCE = "unscored_greedy_generation"
FLORENCE2_COMMUNITY_CONFIDENCE_PROVENANCE = "florence2_onnx_community_greedy_generation"
SUPPORTED_BACKENDS = {"florence2", "florence2_int8_community"}

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
_SVG_PATH_CAPTION = re.compile(r"^[Mm]\s*-?\d+(?:[\d\s,.\-]+)[LlHhVvCcSsQqTtAaZz]")


@dataclass(frozen=True)
class IconCaption:
    raw_caption: str
    normalized_label: Optional[str]
    confidence: Optional[float] = None
    confidence_provenance: str = GENERATION_CONFIDENCE_PROVENANCE
    candidates: tuple[tuple[str, float], ...] = ()
    backend: str = "florence2"

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


def default_backend() -> str:
    return (
        os.environ.get("CUA_ICON_SEMANTIC_BACKEND", "florence2_int8_community")
        .strip()
        .lower()
    )


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
    """Batch icon semantics with one selected, local-only backend."""

    def __init__(
        self,
        model_name_or_path: Optional[str] = None,
        processor_name_or_path: Optional[str] = None,
        enabled: Optional[bool] = None,
        batch_size: Optional[int] = None,
        max_new_tokens: Optional[int] = None,
        backend: Optional[str] = None,
    ) -> None:
        self.backend = (backend or default_backend()).strip().lower()
        self.model_name_or_path = model_name_or_path or self._default_model_path()
        self.processor_name_or_path = (
            processor_name_or_path or self._default_processor_path()
        )
        if enabled is None:
            enabled = (
                _env_flag("CUA_ENABLE_FLORENCE2", True)
                if self.backend == "florence2"
                else _env_flag(
                    "CUA_ENABLE_ICON_SEMANTICS",
                    _env_flag("CUA_ENABLE_FLORENCE2", True),
                )
            )
        self.enabled = enabled
        self.batch_size = (
            max(1, min(256, int(batch_size)))
            if batch_size is not None
            else _env_int(
                "CUA_ICON_SEMANTIC_BATCH_SIZE",
                _env_int("CUA_FLORENCE2_BATCH_SIZE", 64, maximum=256),
                maximum=256,
            )
        )
        self.max_new_tokens = (
            max(1, min(128, int(max_new_tokens)))
            if max_new_tokens is not None
            else _env_int("CUA_FLORENCE2_MAX_NEW_TOKENS", 20, maximum=128)
        )
        self._condition = threading.Condition(threading.RLock())
        self._state = "idle" if enabled else "disabled"
        self._failure: Optional[str] = None
        self._inference_failure: Optional[str] = None
        self._background_thread: Optional[threading.Thread] = None
        self._processor: Any = None
        self._model: Any = None
        self._torch: Any = None
        self._device: Any = None
        self._dtype: Any = None
        self._community_runner: Any = None
        self._inference_lock = threading.Lock()

    def _default_model_path(self) -> str:
        if self.backend == "florence2_int8_community":
            return os.environ.get(
                "CUA_FLORENCE2_COMMUNITY_ONNX_DIR",
                str(BUNDLED_FLORENCE2_COMMUNITY_ONNX_DIR),
            )
        return default_model_name_or_path()

    def _default_processor_path(self) -> str:
        if self.backend == "florence2_int8_community":
            return self.model_name_or_path
        return default_processor_name_or_path(self.model_name_or_path)

    @property
    def status(self) -> dict[str, Any]:
        with self._condition:
            state = self._state
            failure = self._failure
            inference_failure = self._inference_failure
        status = {
            "state": state,
            "failure": failure,
            "inference_failure": inference_failure,
            "backend": self.backend,
            "model": self.model_name_or_path,
            "processor": self.processor_name_or_path,
            "batch_size": self.batch_size,
        }
        if self._community_runner is not None:
            status["execution_providers"] = self._community_runner.providers
        return status

    def ensure_ready(self, *, wait: bool = True) -> bool:
        """Load the selected backend, optionally without waiting for a loader."""
        if not self.enabled:
            with self._condition:
                self._state = "disabled"
            return False

        while True:
            with self._condition:
                if self._state == "ready":
                    return True
                if self._state == "failed":
                    return False
                if self._state == "loading":
                    if not wait:
                        return False
                    self._condition.wait()
                    continue
                self._state = "loading"
                break

        started = time.perf_counter()
        try:
            self._load_backend()
        except Exception as exc:
            with self._condition:
                self._state = "failed"
                self._failure = str(exc)
                self._condition.notify_all()
            logger.warning(
                "Icon semantic backend disabled: backend=%s duration_ms=%.2f error=%s",
                self.backend,
                (time.perf_counter() - started) * 1000,
                exc,
                exc_info=True,
            )
            return False

        with self._condition:
            self._state = "ready"
            self._failure = None
            self._condition.notify_all()
        logger.info(
            "Icon semantic backend ready: backend=%s duration_ms=%.2f model=%s",
            self.backend,
            (time.perf_counter() - started) * 1000,
            self.model_name_or_path,
        )
        return True

    def start_background_warm_up(self) -> bool:
        """Schedule loading without making CUA detection wait for semantics."""
        if not self.enabled:
            return False
        with self._condition:
            if self._state in {"loading", "ready", "failed", "disabled"}:
                return False
            if (
                self._background_thread is not None
                and self._background_thread.is_alive()
            ):
                return False
            thread = threading.Thread(
                target=self.ensure_ready,
                kwargs={"wait": True},
                name=f"icon-semantic-{self.backend}",
                daemon=True,
            )
            self._background_thread = thread
            thread.start()
            return True

    def _load_backend(self) -> None:
        if self.backend not in SUPPORTED_BACKENDS:
            raise ValueError(
                f"Unsupported icon semantic backend: {self.backend!r}; "
                f"choose one of {sorted(SUPPORTED_BACKENDS)}"
            )
        if self.backend == "florence2":
            self._load_florence2()
        elif self.backend == "florence2_int8_community":
            self._load_florence2_community()

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

    def _load_florence2(self) -> None:
        model_path = self._require_local_directory(
            self.model_name_or_path, "Florence-2 model"
        )
        processor_path = self._require_local_directory(
            self.processor_name_or_path, "Florence-2 processor"
        )
        import torch
        from transformers import AutoModelForCausalLM, AutoProcessor

        forced_device = os.environ.get("CUA_FLORENCE2_DEVICE")
        device = torch.device(
            forced_device
            if forced_device
            else ("cuda" if torch.cuda.is_available() else "cpu")
        )
        dtype = torch.float16 if device.type == "cuda" else torch.float32
        processor = AutoProcessor.from_pretrained(
            str(processor_path),
            local_files_only=True,
            trust_remote_code=True,
        )
        model = AutoModelForCausalLM.from_pretrained(
            str(model_path),
            local_files_only=True,
            trust_remote_code=True,
            torch_dtype=dtype,
        )
        model.eval()
        model.to(device)
        self._torch = torch
        self._device = device
        self._dtype = dtype
        self._processor = processor
        self._model = model

    def _load_florence2_community(self) -> None:
        from computer_server.services.community_florence2_service import (
            CommunityFlorence2Runner,
        )

        runner = CommunityFlorence2Runner(
            self.model_name_or_path,
            self.model_name_or_path,
            batch_size=self.batch_size,
            max_new_tokens=self.max_new_tokens,
        )
        runner.load()
        self._community_runner = runner

    def _ready_without_waiting(self) -> bool:
        with self._condition:
            if self._state == "ready":
                return True
            if self._state != "idle":
                return False
        self.start_background_warm_up()
        return False

    def caption_icons(
        self,
        images: Sequence[Image.Image],
    ) -> list[Optional[IconCaption]]:
        """Return one semantic result per crop without blocking CUA on loading."""
        if not images or not self._ready_without_waiting():
            return [] if not images else [None] * len(images)

        if not self._inference_lock.acquire(blocking=False):
            raise RuntimeError("Icon semantic inference is already in progress")
        try:
            try:
                captions = self._caption_icons_ready(images)
                with self._condition:
                    self._inference_failure = None
                return captions
            except Exception as exc:
                with self._condition:
                    self._inference_failure = str(exc)
                logger.warning(
                    "Icon semantic inference failed: backend=%s error=%s",
                    self.backend,
                    exc,
                    exc_info=True,
                )
                if (
                    self._torch is not None
                    and self._device is not None
                    and getattr(self._device, "type", None) == "cuda"
                ):
                    self._torch.cuda.empty_cache()
                raise
        finally:
            self._inference_lock.release()

    def _caption_icons_ready(
        self,
        images: Sequence[Image.Image],
    ) -> list[Optional[IconCaption]]:
        if self.backend == "florence2_int8_community":
            return self._caption_florence2_community(images)
        return self._caption_florence2(images)

    def _caption_florence2_community(
        self,
        images: Sequence[Image.Image],
    ) -> list[Optional[IconCaption]]:
        if self._community_runner is None:
            return [None] * len(images)
        raw_captions = self._community_runner.caption_images(images)
        if len(raw_captions) != len(images):
            raise RuntimeError(
                "Community Florence-2 returned "
                f"{len(raw_captions)} captions for {len(images)} icon crops"
            )
        return [
            IconCaption(
                raw_caption=raw,
                normalized_label=normalize_caption(raw),
                confidence_provenance=FLORENCE2_COMMUNITY_CONFIDENCE_PROVENANCE,
                backend="florence2_int8_community",
            )
            for raw in raw_captions
        ]

    def _caption_florence2(
        self,
        images: Sequence[Image.Image],
    ) -> list[Optional[IconCaption]]:
        torch = self._torch
        processor = self._processor
        model = self._model
        device = self._device
        dtype = self._dtype
        if torch is None or processor is None or model is None or device is None:
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
            captions.extend(
                IconCaption(
                    raw_caption=re.sub(r"\s+", " ", str(raw)).strip(),
                    normalized_label=normalize_caption(str(raw)),
                )
                for raw in generated_texts
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
